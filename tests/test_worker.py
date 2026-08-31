from __future__ import annotations

import hashlib
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


def write_jira_case(path, issue_key="APP-42", comments=None):
    path.mkdir(parents=True, exist_ok=True)
    comments = comments or []
    issue = {
        "key": issue_key,
        "summary": "黑屏",
        "description": "启动后黑屏",
        "environment": "bench",
        "comments": comments,
    }
    issue_text = json.dumps(issue, ensure_ascii=False, indent=2)
    issue_bytes = issue_text.encode("utf-8")
    (path / "issue.json").write_bytes(issue_bytes)
    (path / "issue.md").write_text("# Issue", encoding="utf-8")
    (path / "collection-manifest.json").write_text(json.dumps({
        "schema_version": 2,
        "source": "jira",
        "root_issue": issue_key,
        "root_issue_context": {
            "version": 1,
            "comments": {
                "total": len(comments),
                "collected": len(comments),
                "complete": True,
                "truncated": False,
            },
        },
        "issue_json_sha256": hashlib.sha256(issue_bytes).hexdigest(),
    }), encoding="utf-8")


def compiler_json(comment_ids=None):
    ids = ["issue-description", *(comment_ids or [])]
    return json.dumps({"sources": [{
        "source_id": source_id,
        "current_status": ["当前黑屏"],
        "conclusions": [],
        "attempted_actions": [],
        "next_steps": [],
        "clues": ["SurfaceFlinger"],
        "open_questions": [],
    } for source_id in ids]}, ensure_ascii=False)


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
        self.responses = list(response) if isinstance(response, list) else [response]
        self.closed = False
        self.messages = []

    async def complete(self, messages, tools):
        self.messages.append(messages)
        return {"content": self.responses.pop(0)}

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
    assert result.applied_skills == ["android-log-triage"]
    assert harness.provider.config.max_steps == 5
    assert harness.provider.closed is True
    assert harness.router.connections[0][0:2] == ("log", "log_analyzer.server")
    assert "Skill: android-log-triage" in harness.provider.messages[0][0]["content"]


@pytest.mark.anyio
async def test_jira_worker_connects_jira_and_log_and_maps_insufficient_result(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_EXPORT_ROOT", str(tmp_path))
    case_path = tmp_path / "APP-42"
    write_jira_case(case_path, comments=[{
        "comment_id": "c-1", "author": "Tester", "body": "复现后黑屏",
        "created_at": "2026-08-28T10:00:00+08:00", "updated_at": None,
    }])
    harness = Harness([compiler_json(["c-1"]), report_json("insufficient_evidence")])

    async def fake_export(task, router):
        return case_path

    worker = BugAnalysisWorker(
        CONFIG, harness.provider_factory, harness.router_factory,
        jira_exporter=fake_export,
    )

    result = await worker.execute(BugAnalysisTask(source="jira", issue_key="app-42"))

    assert result.status == "insufficient_evidence", result.error
    assert [item[0] for item in harness.router.connections] == ["jira", "log"]
    # 首次模型调用是 Comment Compiler，第二次才是主 Agent。
    assert "复现后黑屏" in harness.provider.messages[0][1]["content"]
    assert "COMPILED_JIRA_CONTEXT" in harness.provider.messages[1][1]["content"]


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
    # 本地输入路径在 Provider 创建前校验，失败时不会建立外部模型连接。
    assert harness.provider is None


@pytest.mark.anyio
async def test_unknown_skill_fails_before_provider_or_mcp_start(tmp_path):
    harness = Harness(report_json())
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)

    result = await worker.execute(BugAnalysisTask(
        source="local", case_path=str(tmp_path), skills=["not-found"],
    ))

    assert result.status == "failed"
    assert "Skill 不存在" in (result.error or "")
    assert harness.provider is None
    assert harness.router is None


@pytest.mark.anyio
async def test_run_record_is_written_with_full_trace(tmp_path):
    """运行记录应落盘到 <case>/.bug-agent/runs/<task_id>.json，且包含完整 trace。

    即使 task.include_trace=False（result.trace 为空），落盘的 trace 也必须完整，
    因为复盘依赖的是 run.tool_events，而不是对外契约里的 trace 字段。
    """
    harness = Harness(report_json())
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)

    result = await worker.execute(BugAnalysisTask(
        task_id="run-persist-1", source="local", case_path=str(tmp_path),
        include_trace=False,
    ))

    run_file = tmp_path / ".bug-agent" / "runs" / "run-persist-1.json"
    assert run_file.is_file()
    record = json.loads(run_file.read_text(encoding="utf-8"))
    assert record["schema_version"] == 1
    assert record["task"]["task_id"] == "run-persist-1"
    assert record["task"]["source"] == "local"
    assert record["result"]["status"] == "completed"
    # 对外契约 trace 为空，但落盘的 trace 来自 run.tool_events
    assert result.trace == []
    assert isinstance(record["trace"], list)
    assert record["agent_status"] == "completed"
    assert list(run_file.parent.glob("*.tmp")) == []


@pytest.mark.anyio
async def test_run_record_written_even_when_prepare_fails(tmp_path):
    """准备阶段失败（如 Case 目录不存在）也应落盘，trace 为空但不报错。"""
    harness = Harness(report_json())
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)
    missing = tmp_path / "missing"

    result = await worker.execute(BugAnalysisTask(
        task_id="run-persist-fail", source="local", case_path=str(missing),
    ))

    assert result.status == "failed"
    run_file = missing / ".bug-agent" / "runs" / "run-persist-fail.json"
    assert run_file.is_file()
    record = json.loads(run_file.read_text(encoding="utf-8"))
    assert record["result"]["status"] == "failed"
    assert record["trace"] == []
    assert record["agent_status"] is None
