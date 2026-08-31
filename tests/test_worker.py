from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from bug_agent.config import AgentConfig
from bug_agent.contracts import BugAnalysisTask
from bug_agent.provider import ProviderError
from bug_agent.skills import SkillRegistry
from bug_agent.worker import BugAnalysisWorker


CONFIG = AgentConfig(
    llm_base_url="https://llm.test/v1",
    llm_api_key="key",
    llm_model="model",
    max_steps=12,
)


def write_jira_case(path, issue_key="APP-42", comments=None, *, complete=True):
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
                "complete": complete,
                "truncated": not complete,
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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, dict):
            return response
        return {"content": response}

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
async def test_worker_allows_agent_to_activate_specialized_skill(tmp_path):
    harness = Harness([{
        "content": "",
        "tool_calls": [{
            "id": "activate-1",
            "function": {
                "name": "activate_skill",
                "arguments": json.dumps({
                    "name": "android-black-screen",
                    "reason": "Issue 报告黑屏，首轮证据指向 SurfaceFlinger",
                }, ensure_ascii=False),
            },
        }],
    }, report_json()])
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)

    result = await worker.execute(BugAnalysisTask(
        task_id="auto-skill-1", source="local", case_path=str(tmp_path),
        include_trace=True,
    ))

    assert result.status == "completed"
    assert result.applied_skills == ["android-log-triage", "android-black-screen"]
    assert [item.source for item in result.skill_activations] == ["default", "agent"]
    assert result.skill_activations[-1].reason.startswith("Issue 报告黑屏")
    assert result.trace[0].tool_name == "activate_skill"
    assert result.trace[0].success is True
    assert "SurfaceFlinger" in harness.provider.messages[1][-1]["content"]


@pytest.mark.anyio
async def test_jira_worker_connects_jira_and_log_and_maps_insufficient_result(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_EXPORT_ROOT", str(tmp_path))
    case_path = tmp_path / "APP-42"
    write_jira_case(case_path, comments=[{
        "comment_id": "c-1", "author": "Tester", "body": "复现后黑屏",
        "created_at": "2026-08-28T10:00:00+08:00", "updated_at": None,
    }])
    harness = Harness(report_json("insufficient_evidence"))

    async def fake_export(task, router):
        return case_path

    worker = BugAnalysisWorker(
        CONFIG, harness.provider_factory, harness.router_factory,
        jira_exporter=fake_export,
    )

    result = await worker.execute(BugAnalysisTask(source="jira", issue_key="app-42"))

    assert result.status == "insufficient_evidence", result.error
    assert [item[0] for item in harness.router.connections] == ["jira", "log"]
    assert len(harness.provider.messages) == 1
    assert "复现后黑屏" in harness.provider.messages[0][1]["content"]
    assert "DIRECT_JIRA_CONTEXT" in harness.provider.messages[0][1]["content"]


@pytest.mark.anyio
async def test_large_jira_context_uses_bounded_compiler(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_EXPORT_ROOT", str(tmp_path))
    case_path = tmp_path / "APP-42"
    write_jira_case(case_path, comments=[{
        "comment_id": "c-1", "author": "Tester", "body": "X" * 5000,
        "created_at": "2026-08-28T10:00:00+08:00", "updated_at": None,
    }])
    harness = Harness([compiler_json(["c-1"]), report_json()])

    async def fake_export(task, router):
        return case_path

    worker = BugAnalysisWorker(
        replace(CONFIG, jira_direct_context_max_chars=4000),
        harness.provider_factory, harness.router_factory,
        jira_exporter=fake_export,
    )
    result = await worker.execute(BugAnalysisTask(
        task_id="compiled-mode", source="jira", issue_key="APP-42",
    ))

    assert result.status == "completed", result.error
    assert len(harness.provider.messages) == 2
    assert "COMPILED_JIRA_CONTEXT" in harness.provider.messages[1][1]["content"]
    record = json.loads((
        case_path / ".bug-agent" / "runs" / "compiled-mode.json"
    ).read_text(encoding="utf-8"))
    assert record["jira_context"]["context_mode"] == "compiled"
    assert record["jira_context"]["compiler_attempt_count"] == 1


@pytest.mark.anyio
async def test_local_jira_case_directly_injects_all_comments(tmp_path):
    write_jira_case(tmp_path, comments=[
        {"comment_id": "first", "body": "FIRST_MARKER"},
        {"comment_id": "middle", "body": "MIDDLE_MARKER"},
        {"comment_id": "last", "body": "LAST_MARKER"},
    ])
    harness = Harness(report_json())
    worker = BugAnalysisWorker(CONFIG, harness.provider_factory, harness.router_factory)

    result = await worker.execute(BugAnalysisTask(source="local", case_path=str(tmp_path)))

    assert result.status == "completed"
    initial_context = harness.provider.messages[0][1]["content"]
    assert all(marker in initial_context for marker in (
        "FIRST_MARKER", "MIDDLE_MARKER", "LAST_MARKER",
    ))


@pytest.mark.anyio
async def test_incomplete_jira_export_fails_before_log_or_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_EXPORT_ROOT", str(tmp_path))
    case_path = tmp_path / "APP-42"
    write_jira_case(case_path, comments=[], complete=False)
    harness = Harness(report_json())

    async def fake_export(task, router):
        return case_path

    worker = BugAnalysisWorker(
        CONFIG, harness.provider_factory, harness.router_factory,
        jira_exporter=fake_export,
    )
    result = await worker.execute(BugAnalysisTask(
        task_id="incomplete-export", source="jira", issue_key="APP-42",
    ))

    assert result.status == "failed"
    assert "JIRA_COMMENTS_INCOMPLETE" in (result.error or "")
    assert harness.provider is None
    assert [item[0] for item in harness.router.connections] == ["jira"]
    record = json.loads((
        case_path / ".bug-agent" / "runs" / "incomplete-export.json"
    ).read_text(encoding="utf-8"))
    assert record["failure"]["phase"] == "context_validation"


@pytest.mark.anyio
async def test_jira_export_path_outside_root_is_rejected_before_provider(tmp_path, monkeypatch):
    export_root = tmp_path / "exports"
    outside = tmp_path / "outside" / "APP-42"
    export_root.mkdir()
    write_jira_case(outside)
    monkeypatch.setenv("JIRA_EXPORT_ROOT", str(export_root))
    harness = Harness(report_json())

    async def fake_export(task, router):
        return outside

    worker = BugAnalysisWorker(
        CONFIG, harness.provider_factory, harness.router_factory,
        jira_exporter=fake_export,
    )
    result = await worker.execute(BugAnalysisTask(source="jira", issue_key="APP-42"))

    assert result.status == "failed"
    assert "超出配置的导出根目录" in (result.error or "")
    assert harness.provider is None
    assert [item[0] for item in harness.router.connections] == ["jira"]


@pytest.mark.anyio
async def test_compiler_provider_error_keeps_safe_message_and_phase(tmp_path):
    write_jira_case(tmp_path, comments=[{
        "comment_id": "c-1", "body": "X" * 5000,
    }])
    harness = Harness(ProviderError("模型服务认证或权限失败", retryable=False))
    worker = BugAnalysisWorker(
        replace(CONFIG, jira_direct_context_max_chars=4000),
        harness.provider_factory, harness.router_factory,
    )

    result = await worker.execute(BugAnalysisTask(
        task_id="compiler-provider-fail", source="local", case_path=str(tmp_path),
    ))

    assert result.status == "failed"
    assert result.error == "模型服务认证或权限失败"
    record = json.loads((
        tmp_path / ".bug-agent" / "runs" / "compiler-provider-fail.json"
    ).read_text(encoding="utf-8"))
    assert record["failure"] == {
        "phase": "context_compilation",
        "error_type": "ProviderError",
        "retryable": False,
    }
    assert record["jira_context"]["context_mode"] == "compiled"
    assert record["jira_context"]["compiler_attempt_count"] == 1


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
async def test_invalid_auto_skill_catalog_fails_before_provider_or_mcp_start(tmp_path):
    case_path = tmp_path / "case"
    case_path.mkdir()
    skills_root = tmp_path / "skills"
    triage = skills_root / "android-log-triage"
    triage.mkdir(parents=True)
    (triage / "SKILL.md").write_text(
        "---\nname: android-log-triage\ndescription: Triage logs\ncategory: base\n---\n"
        "Use open_case and inspect_case.",
        encoding="utf-8",
    )
    broken = skills_root / "broken-skill"
    broken.mkdir()
    (broken / "SKILL.md").write_text("missing frontmatter", encoding="utf-8")
    harness = Harness(report_json())
    worker = BugAnalysisWorker(
        CONFIG,
        harness.provider_factory,
        harness.router_factory,
        skill_registry=SkillRegistry(skills_root),
    )

    result = await worker.execute(BugAnalysisTask(source="local", case_path=str(case_path)))

    assert result.status == "failed"
    assert "YAML frontmatter" in (result.error or "")
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
    assert record["failure"]["phase"] == "local_validation"
