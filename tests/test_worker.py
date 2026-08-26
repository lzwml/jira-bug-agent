from __future__ import annotations

import json

import pytest

from bug_agent.config import AgentConfig
from bug_agent.contracts import BugAnalysisTask
from bug_agent.worker import BugAnalysisWorker


CONFIG = AgentConfig(
    llm_base_url="https://llm.test/v1",
    llm_api_key="key",
    llm_model="model",
    max_steps=12,
)


def report_json(conclusion_status="confirmed"):
    return json.dumps({
        "conclusion_status": conclusion_status,
        "summary": "启动失败与 SurfaceFlinger Fatal 同时发生。",
        "confirmed_facts": ["Fatal 出现在启动阶段"],
        "hypotheses": [{
            "statement": "显示服务异常导致黑屏",
            "confidence": 0.8,
            "status": "supported",
            "supporting_evidence_ids": ["ev-1"],
            "falsification": "检查无 Fatal 的复现场景",
        }],
        "evidence": [{
            "evidence_id": "ev-1",
            "artifact_id": "artifact-1",
            "relative_path": "logcat.txt",
            "line_start": 42,
            "line_end": 42,
            "excerpt": "FATAL EXCEPTION",
        }],
        "missing_evidence": [],
        "next_actions": ["检查相关提交"],
    }, ensure_ascii=False)


class FakeProvider:
    def __init__(self, config, response):
        self.config = config
        self.response = response
        self.closed = False
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append(messages)
        return {"content": self.response}

    async def close(self):
        self.closed = True


class FakeRouter:
    def __init__(self):
        self.connections = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def connect_python_server(self, name, module, env_overrides=None):
        self.connections.append((name, module, env_overrides or {}))

    def openai_tools(self):
        return []

    async def call(self, name, arguments):
        raise AssertionError("本测试不应调用工具")


class Harness:
    def __init__(self, response):
        self.response = response
        self.provider = None
        self.router = None

    def provider_factory(self, config):
        self.provider = FakeProvider(config, self.response)
        return self.provider

    def router_factory(self):
        self.router = FakeRouter()
        return self.router


@pytest.mark.anyio
async def test_local_worker_returns_stable_structured_contract(tmp_path):
    harness = Harness(report_json())
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)
    task = BugAnalysisTask(
        task_id="task-1", source="local", case_path=str(tmp_path),
        max_steps=5, include_trace=True,
    )

    result = await worker.execute(task)

    assert result.task_id == "task-1"
    assert result.status == "completed"
    assert result.structured_output is True
    assert result.report.evidence[0].relative_path == "logcat.txt"
    assert harness.provider.config.max_steps == 5
    assert harness.provider.closed is True
    assert harness.router.connections[0][0:2] == ("log", "log_analyzer.server")


@pytest.mark.anyio
async def test_jira_worker_connects_jira_and_log_and_maps_insufficient_result(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_EXPORT_ROOT", str(tmp_path))
    harness = Harness(report_json("insufficient_evidence"))
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)

    result = await worker.execute(BugAnalysisTask(source="jira", issue_key="app-42"))

    assert result.status == "insufficient_evidence"
    assert [item[0] for item in harness.router.connections] == ["jira", "log"]
    assert "APP-42" in harness.provider.messages[0][1]["content"]


@pytest.mark.anyio
async def test_unstructured_model_output_is_explicitly_marked(tmp_path):
    harness = Harness("日志不足，暂时无法确认根因。")
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)

    result = await worker.execute(BugAnalysisTask(source="local", case_path=str(tmp_path)))

    assert result.status == "completed"
    assert result.structured_output is False
    assert result.report.summary == "日志不足，暂时无法确认根因。"
    assert result.trace == []


@pytest.mark.anyio
async def test_worker_closes_provider_when_local_path_is_invalid(tmp_path):
    harness = Harness(report_json())
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)
    missing = tmp_path / "missing"

    result = await worker.execute(BugAnalysisTask(source="local", case_path=str(missing)))

    assert result.status == "failed"
    assert "Case 目录不存在" in (result.error or "")
    assert harness.provider.closed is True
