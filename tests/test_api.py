from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bug_agent.api.app import create_app
from bug_agent.api.config import ApiConfig
from bug_agent.api.dispatcher import TaskDispatcher
from bug_agent.api.task_store import SqliteTaskStore, TaskConflictError
from bug_agent.contracts import (
    BugAnalysisResult,
    BugAnalysisTask,
    EvidenceReference,
    IncidentProfile,
    InvestigationState,
    RCAReport,
)
from bug_agent.models import ToolEvent


def completed_result(task_id: str) -> BugAnalysisResult:
    return BugAnalysisResult(
        task_id=task_id,
        status="completed",
        report=RCAReport(conclusion_status="confirmed", summary="已定位"),
        steps=2,
        structured_output=True,
    )


class ImmediateWorker:
    async def execute(self, task: BugAnalysisTask) -> BugAnalysisResult:
        return completed_result(task.task_id)

    async def answer_conversation_turn(self, task, history, user_message, *, max_steps_per_turn=None):
        return f"已收到：{user_message}"


def test_sqlite_store_is_idempotent_and_detects_conflict(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = BugAnalysisTask(task_id="stable-1", source="jira", issue_key="APP-42")

    first, created = store.submit(task)
    second, created_again = store.submit(task)

    assert created is True
    assert created_again is False
    assert first.task_id == second.task_id
    with pytest.raises(TaskConflictError):
        store.submit(task.model_copy(update={"objective": "另一个目标"}))


def test_sqlite_store_requeues_interrupted_tasks(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = BugAnalysisTask(task_id="recover-1", source="jira", issue_key="APP-42")
    store.submit(task)
    assert store.mark_running(task.task_id) is True

    assert store.requeue_interrupted() == 1
    assert store.get(task.task_id).status == "queued"


@pytest.mark.anyio
async def test_dispatcher_enforces_concurrency_limit(tmp_path):
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    release = asyncio.Event()
    two_started = asyncio.Event()
    state = {"active": 0, "max_active": 0, "started": 0}

    class ControlledWorker:
        async def execute(self, task: BugAnalysisTask) -> BugAnalysisResult:
            state["active"] += 1
            state["started"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            if state["started"] == 2:
                two_started.set()
            await release.wait()
            state["active"] -= 1
            return completed_result(task.task_id)

    dispatcher = TaskDispatcher(store, ControlledWorker, concurrency=2)
    await dispatcher.start()
    try:
        for index in range(3):
            await dispatcher.submit(BugAnalysisTask(
                task_id=f"parallel-{index}", source="jira", issue_key=f"APP-{index + 1}",
            ))
        await asyncio.wait_for(two_started.wait(), timeout=2)
        assert state["max_active"] == 2
        assert state["started"] == 2
        release.set()
        await asyncio.wait_for(dispatcher._queue.join(), timeout=2)
        assert state["started"] == 3
    finally:
        await dispatcher.stop()


@pytest.mark.anyio
async def test_http_api_submits_queries_and_reuses_same_task(tmp_path):
    case_root = tmp_path / "cases"
    case_path = case_root / "APP-42"
    case_path.mkdir(parents=True)
    settings = ApiConfig(
        database_path=tmp_path / "tasks.sqlite3",
        concurrency=1,
        allowed_local_roots=(case_root.resolve(),),
        api_key="secret",
    )
    app = create_app(settings, ImmediateWorker)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            unauthorized = await client.get("/tasks/missing")
            assert unauthorized.status_code == 401
            headers = {"X-API-Key": "secret"}
            payload = {
                "task_id": "api-1",
                "source": "local",
                "case_path": str(case_path),
            }
            submitted = await client.post("/tasks", json=payload, headers=headers)
            assert submitted.status_code == 202
            assert submitted.json() == {
                "task_id": "api-1", "status": "queued", "created": True,
            }

            for _ in range(50):
                queried = await client.get("/tasks/api-1", headers=headers)
                if queried.json()["status"] == "completed":
                    break
                await asyncio.sleep(0)
            assert queried.status_code == 200
            assert queried.json()["result"]["report"]["summary"] == "已定位"

            duplicate = await client.post("/tasks", json=payload, headers=headers)
            assert duplicate.status_code == 202
            assert duplicate.json()["created"] is False
            assert duplicate.json()["status"] == "completed"

            conflicting = await client.post(
                "/tasks", json={**payload, "objective": "不同目标"}, headers=headers,
            )
            assert conflicting.status_code == 409


@pytest.mark.anyio
async def test_http_api_continuation_inherits_case_and_links_previous_task(tmp_path):
    case_root = tmp_path / "cases"
    case_path = case_root / "APP-42"
    case_path.mkdir(parents=True)
    app = create_app(
        ApiConfig(
            database_path=tmp_path / "tasks.sqlite3",
            concurrency=1,
            allowed_local_roots=(case_root.resolve(),),
        ),
        ImmediateWorker,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            parent = await client.post("/tasks", json={
                "task_id": "first-pass", "source": "local", "case_path": str(case_path),
                "objective": "首次分析", "metadata": {"issue": "APP-42"},
            })
            assert parent.status_code == 202
            for _ in range(50):
                parent_record = await client.get("/tasks/first-pass")
                if parent_record.json()["status"] == "completed":
                    break
                await asyncio.sleep(0)

            continuation = await client.post("/tasks/first-pass/continuations", json={
                "task_id": "second-pass",
                "objective": "新增日志后验证根因",
                "metadata": {"source": "new-log"},
            })
            assert continuation.status_code == 202
            assert continuation.json() == {
                "task_id": "second-pass", "status": "queued", "created": True,
            }
            record = await client.get("/tasks/second-pass")
            payload = record.json()["task"]
            assert payload["continuation_of"] == "first-pass"
            assert payload["case_path"] == str(case_path.resolve())
            assert payload["objective"] == "新增日志后验证根因"
            assert payload["metadata"] == {"issue": "APP-42", "source": "new-log"}

            duplicate = await client.post("/tasks/first-pass/continuations", json={
                "task_id": "second-pass",
                "objective": "新增日志后验证根因",
                "metadata": {"source": "new-log"},
            })
            assert duplicate.status_code == 202
            assert duplicate.json()["created"] is False


@pytest.mark.anyio
async def test_http_api_persists_case_conversation_messages(tmp_path):
    case_root = tmp_path / "cases"
    case_path = case_root / "APP-42"
    case_path.mkdir(parents=True)
    app = create_app(
        ApiConfig(
            database_path=tmp_path / "tasks.sqlite3",
            allowed_local_roots=(case_root.resolve(),),
        ),
        ImmediateWorker,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            created = await client.post("/conversations", json={
                "conversation_id": "case-chat-1",
                "task": {"task_id": "case-chat-task", "source": "local", "case_path": str(case_path)},
            })
            assert created.status_code == 201
            assert created.json()["status"] == "active"
            assert created.json()["messages"] == []

            message = await client.post("/conversations/case-chat-1/messages", json={
                "content": "复现只发生在冷启动，优先看 SurfaceFlinger。",
            })
            assert message.status_code == 202
            assert message.json()["role"] == "user"

            record = await client.get("/conversations/case-chat-1")
            for _ in range(50):
                record = await client.get("/conversations/case-chat-1")
                if len(record.json()["messages"]) == 2:
                    break
                await asyncio.sleep(0)
            assert record.status_code == 200
            assert record.json()["messages"][0]["content"] == "复现只发生在冷启动，优先看 SurfaceFlinger。"
            assert record.json()["messages"][1]["role"] == "assistant"


@pytest.mark.anyio
async def test_conversation_create_resumes_latest_active_jira_case_by_default(tmp_path):
    app = create_app(
        ApiConfig(database_path=tmp_path / "tasks.sqlite3"),
        ImmediateWorker,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            first = await client.post("/conversations", json={
                "task": {
                    "task_id": "jira-first", "source": "jira",
                    "issue_key": "BAIC-47248", "objective": "首次分析",
                },
            })
            first_id = first.json()["conversation_id"]
            await client.post(
                f"/conversations/{first_id}/messages",
                json={"content": "保留这条人工纠偏"},
            )
            for _ in range(50):
                current = await client.get(f"/conversations/{first_id}")
                if len(current.json()["messages"]) == 2:
                    break
                await asyncio.sleep(0)

            resumed = await client.post("/conversations", json={
                "task": {
                    "task_id": "jira-second", "source": "jira",
                    "issue_key": "BAIC-47248", "objective": "不同的后续目标",
                },
            })

            assert resumed.status_code == 201
            assert resumed.json()["conversation_id"] == first_id
            assert any(
                item["content"] == "保留这条人工纠偏"
                for item in resumed.json()["messages"]
            )

            forced = await client.post("/conversations", json={
                "conversation_id": "explicit-fresh-analysis",
                "task": {
                    "task_id": "jira-third", "source": "jira",
                    "issue_key": "BAIC-47248",
                },
            })
            assert forced.json()["conversation_id"] == "explicit-fresh-analysis"
            assert forced.json()["messages"] == []


@pytest.mark.anyio
async def test_http_api_cannot_continue_running_task(tmp_path):
    case_root = tmp_path / "cases"
    case_path = case_root / "APP-42"
    case_path.mkdir(parents=True)
    release = asyncio.Event()

    class WaitingWorker:
        async def execute(self, task: BugAnalysisTask) -> BugAnalysisResult:
            await release.wait()
            return completed_result(task.task_id)

    app = create_app(
        ApiConfig(
            database_path=tmp_path / "tasks.sqlite3", allowed_local_roots=(case_root.resolve(),),
        ),
        WaitingWorker,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            await client.post("/tasks", json={
                "task_id": "running-pass", "source": "local", "case_path": str(case_path),
            })
            for _ in range(50):
                record = await client.get("/tasks/running-pass")
                if record.json()["status"] == "running":
                    break
                await asyncio.sleep(0)
            response = await client.post("/tasks/running-pass/continuations", json={
                "objective": "继续分析",
            })
            assert response.status_code == 409
            release.set()


@pytest.mark.anyio
async def test_http_api_rejects_local_path_outside_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    app = create_app(
        ApiConfig(
            database_path=tmp_path / "tasks.sqlite3",
            allowed_local_roots=(allowed.resolve(),),
        ),
        ImmediateWorker,
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post("/tasks", json={
                "task_id": "outside-1",
                "source": "local",
                "case_path": str(outside),
            })
    assert response.status_code == 403


@pytest.mark.anyio
async def test_http_api_health_does_not_require_api_key(tmp_path):
    app = create_app(
        ApiConfig(database_path=Path(tmp_path) / "tasks.sqlite3", api_key="secret"),
        ImmediateWorker,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "api_version": "0.2.0",
        "capabilities": [
            "resolved_client_locations",
            "assistant_evidence_locations",
        ],
    }


@pytest.mark.anyio
async def test_conversation_dispatcher_consumes_queued_messages(tmp_path):
    """验证 ConversationDispatcher 从 SQLite 消费消息并生成 assistant 回复。"""
    from bug_agent.api.dispatcher import ConversationDispatcher

    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = BugAnalysisTask(task_id="conv-task", source="jira", issue_key="APP-42")
    store.create_conversation("conv-1", task)
    store.add_conversation_message("conv-1", role="user", content="冷启动黑屏？")

    dispatcher = ConversationDispatcher(store, ImmediateWorker, concurrency=1)
    await dispatcher.start()
    try:
        # 等待消息被消费
        for _ in range(50):
            record = store.get_conversation("conv-1")
            if any(msg.role == "assistant" for msg in record.messages):
                break
            await asyncio.sleep(0)
        record = store.get_conversation("conv-1")
        assert len(record.messages) == 2
        assert record.messages[0].role == "user"
        assert record.messages[0].status == "completed"
        assert record.messages[1].role == "assistant"
        assert record.messages[1].content == "已收到：冷启动黑屏？"
    finally:
        await dispatcher.stop()


@pytest.mark.anyio
async def test_conversation_api_persists_human_rca_and_machine_report(tmp_path):
    """VS Code receives a readable presentation and a typed report in one response."""
    from bug_agent.api.dispatcher import ConversationDispatcher

    class ReportWorker(ImmediateWorker):
        async def answer_conversation_turn(
            self, task, history, user_message, *, max_steps_per_turn=None,
        ):
            return json.dumps({
                "conclusion_status": "hypothesis_only",
                "summary": "当前仅怀疑 DVR 存在相机资源竞争，根因尚未确认。",
                "observed_symptom": "DVR 画面卡死",
                "root_cause": None,
                "missing_evidence": ["CameraService 资源归属"],
            }, ensure_ascii=False)

    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    case_path = tmp_path / "BAIC-47248"
    case_path.mkdir()
    task = BugAnalysisTask(
        task_id="rca-chat-task", source="local", case_path=str(case_path),
    )
    store.create_conversation("rca-chat", task)
    store.add_conversation_message("rca-chat", role="user", content="生成结论")
    dispatcher = ConversationDispatcher(store, ReportWorker, concurrency=1)
    await dispatcher.start()
    try:
        for _ in range(50):
            record = store.get_conversation("rca-chat")
            if any(message.role == "assistant" for message in record.messages):
                break
            await asyncio.sleep(0)
        assistant = next(message for message in record.messages if message.role == "assistant")
        assert assistant.content_format == "markdown"
        assert assistant.content.startswith("# 阶段性 RCA：rca-chat-task")
        assert assistant.report is not None
        assert assistant.report.conclusion_status == "hypothesis_only"
        assert assistant.persistence is not None
        assert assistant.persistence.rca_saved is True
        assert Path(assistant.persistence.rca_markdown_path).is_file()
        assert Path(assistant.persistence.rca_state_path).is_file()
        assert Path(assistant.persistence.rca_events_path).is_file()
        events = store.list_conversation_events("rca-chat")
        response = next(event for event in events if event.kind == "assistant_message")
        assert response.content_format == "markdown"
        assert response.report is not None
        assert response.report.root_cause is None
        assert response.persistence is not None
        assert response.persistence.rca_saved is True
    finally:
        await dispatcher.stop()


@pytest.mark.anyio
async def test_conversation_restores_host_persisted_investigation_state(tmp_path):
    """A later VS Code turn receives the prior turn's structured investigation state."""
    from bug_agent.api.dispatcher import ConversationDispatcher

    seen_states = []

    class StatefulWorker(ImmediateWorker):
        async def answer_conversation_turn(
            self, task, history, user_message, *, max_steps_per_turn=None,
            initial_investigation_state=None, on_investigation_state=None,
        ):
            seen_states.append(initial_investigation_state)
            state = initial_investigation_state or InvestigationState()
            if state.incident_profile is None:
                state.incident_profile = IncidentProfile(
                    symptom_family="anr_freeze",
                    user_visible_symptom="DVR 卡死",
                )
            on_investigation_state(state)
            return f"已处理：{user_message}"

    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = BugAnalysisTask(task_id="resume-state", source="jira", issue_key="BAIC-47248")
    store.create_conversation("resume-state-conv", task)
    dispatcher = ConversationDispatcher(store, StatefulWorker, concurrency=1)
    await dispatcher.start()
    try:
        first = store.add_conversation_message(
            "resume-state-conv", role="user", content="先调查生命周期",
        )
        await dispatcher.submit(first.message_id)
        await asyncio.wait_for(dispatcher._queue.join(), timeout=2)
        saved = store.get_conversation("resume-state-conv")
        assert saved.investigation_state.incident_profile.user_visible_symptom == "DVR 卡死"

        second = store.add_conversation_message(
            "resume-state-conv", role="user", content="继续检查相机资源",
        )
        await dispatcher.submit(second.message_id)
        await asyncio.wait_for(dispatcher._queue.join(), timeout=2)
        assert seen_states[0] is None
        assert seen_states[1].incident_profile.symptom_family == "anr_freeze"
        assistant = store.get_conversation("resume-state-conv").messages[-1]
        assert assistant.persistence.investigation_state_saved is True
    finally:
        await dispatcher.stop()


def test_complete_message_idempotent_when_already_completed(tmp_path):
    """complete_conversation_message 对已完成的 message 不应重复插入 assistant 消息。"""
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = BugAnalysisTask(task_id="idem-task", source="jira", issue_key="APP-42")
    store.create_conversation("idem-conv", task)
    store.add_conversation_message("idem-conv", role="user", content="问题")

    # 手动将消息状态设为 running 并完成一次
    with store._connect() as conn:
        conn.execute("UPDATE conversation_messages SET status = 'running' WHERE message_id = 1")
    store.complete_conversation_message(1, "第一次完成")
    store.complete_conversation_message(1, "第二次完成（应被忽略）")

    record = store.get_conversation("idem-conv")
    # 只有一条 assistant 消息，第二次完成被跳过
    assistant_msgs = [m for m in record.messages if m.role == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0].content == "第一次完成"


def test_assistant_report_exposes_openable_evidence_locations(tmp_path):
    case_path = tmp_path / "APP-42"
    log_path = case_path / "logs" / "main.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("fatal\n", encoding="utf-8")
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = BugAnalysisTask(
        task_id="evidence-card", source="local", case_path=str(case_path),
    )
    store.create_conversation("evidence-card-conv", task)
    message = store.add_conversation_message(
        "evidence-card-conv", role="user", content="给出证据",
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE conversation_messages SET status = 'running' WHERE message_id = ?",
            (message.message_id,),
        )
    report = RCAReport(
        conclusion_status="hypothesis_only",
        summary="根因尚未确认",
        evidence=[EvidenceReference(
            evidence_id="ev-1", relative_path="logs/main.log",
            line_start=1, line_end=1, excerpt="fatal",
        )],
    )

    store.complete_conversation_message(message.message_id, "报告", report=report)

    assistant = store.get_conversation("evidence-card-conv").messages[-1]
    assert assistant.locations[0].identifier == "ev-1"
    assert assistant.locations[0].resolved_path == str(log_path.resolve())
    assert assistant.locations[0].availability == "ready"
    event = next(
        item for item in store.list_conversation_events("evidence-card-conv")
        if item.kind == "assistant_message"
    )
    assert event.locations[0].resolved_path == str(log_path.resolve())


def test_fail_message_idempotent_when_already_completed(tmp_path):
    """fail_conversation_message 对已完成的消息不应覆盖状态。"""
    store = SqliteTaskStore(tmp_path / "tasks.sqlite3")
    store.initialize()
    task = BugAnalysisTask(task_id="fail-task", source="jira", issue_key="APP-42")
    store.create_conversation("fail-conv", task)
    store.add_conversation_message("fail-conv", role="user", content="问题")

    with store._connect() as conn:
        conn.execute("UPDATE conversation_messages SET status = 'running' WHERE message_id = 1")
    store.complete_conversation_message(1, "已完成")
    store.fail_conversation_message(1, "不应覆盖")

    record = store.get_conversation("fail-conv")
    user_msg = record.messages[0]
    assert user_msg.status == "completed"
    assert user_msg.error is None


@pytest.mark.anyio
async def test_conversation_session_on_close_called_once():
    """验证 ConversationSession 的 on_close 回调在 finalize 中只被调用一次。"""
    from bug_agent.agent import BugAnalysisAgent
    from bug_agent.config import AgentConfig
    from bug_agent.conversation import ConversationSession

    close_count = 0

    class DummyRouter:
        async def call(self, name, arguments):
            return "{}"

        def openai_tools(self):
            return []

    class DummyProvider:
        async def complete(self, messages, tools):
            return {"role": "assistant", "content": "done"}

        async def close(self):
            pass

    async def on_close():
        nonlocal close_count
        close_count += 1

    config = AgentConfig(
        llm_base_url="http://localhost", llm_api_key="test", llm_model="test",
        max_steps=1,
    )
    agent = BugAnalysisAgent(config, DummyProvider())
    session = ConversationSession(
        agent=agent,
        system_prompt="test",
        router=DummyRouter(),
        max_steps_per_turn=1,
        on_close=on_close,
    )

    await session.send("hello")
    await session.finalize()
    await session.finalize()  # 第二次 finalize 不应再调用 on_close

    assert close_count == 1


def test_conversation_session_load_history():
    """验证 load_history 可将历史消息注入会话。"""
    from bug_agent.agent import BugAnalysisAgent
    from bug_agent.config import AgentConfig
    from bug_agent.conversation import ConversationSession

    class DummyRouter:
        async def call(self, name, arguments):
            return "{}"

        def openai_tools(self):
            return []

    class DummyProvider:
        async def complete(self, messages, tools):
            return {"role": "assistant", "content": "done"}

        async def close(self):
            pass

    config = AgentConfig(
        llm_base_url="http://localhost", llm_api_key="test", llm_model="test",
        max_steps=1,
    )
    agent = BugAnalysisAgent(config, DummyProvider())
    session = ConversationSession(
        agent=agent,
        system_prompt="test",
        router=DummyRouter(),
    )

    session.load_history([
        {"role": "user", "content": "初始指令"},
        {"role": "assistant", "content": "上一轮回复"},
    ])
    msgs = session.messages
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == "初始指令"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "上一轮回复"


def test_conversation_session_load_history_rejects_invalid_role():
    """load_history 对无效角色应抛出 ValueError。"""
    from bug_agent.agent import BugAnalysisAgent
    from bug_agent.config import AgentConfig
    from bug_agent.conversation import ConversationSession

    class DummyRouter:
        async def call(self, name, arguments):
            return "{}"

        def openai_tools(self):
            return []

    class DummyProvider:
        async def complete(self, messages, tools):
            return {"role": "assistant", "content": "done"}

        async def close(self):
            pass

    config = AgentConfig(
        llm_base_url="http://localhost", llm_api_key="test", llm_model="test",
        max_steps=1,
    )
    agent = BugAnalysisAgent(config, DummyProvider())
    session = ConversationSession(
        agent=agent, system_prompt="test", router=DummyRouter(),
    )

    import pytest as pytest_mod
    with pytest_mod.raises(ValueError, match="无效的消息角色"):
        session.load_history([{"role": "invalid_role", "content": "test"}])


@pytest.mark.anyio
async def test_conversation_events_expose_tool_locations_and_resume_cursor(tmp_path):
    case_root = tmp_path / "cases"
    case_path = case_root / "APP-42"
    (case_path / "logs").mkdir(parents=True)
    (case_path / "logs" / "main.log").write_text("FATAL\n", encoding="utf-8")

    class EventWorker(ImmediateWorker):
        async def answer_conversation_turn(
            self, task, history, user_message, *, max_steps_per_turn=None,
            on_tool_event=None, on_progress=None,
        ):
            if on_progress:
                on_progress({
                    "kind": "tool_started", "step": 1,
                    "tool_name": "search_evidence", "arguments": {"query": "FATAL"},
                })
            if on_tool_event:
                on_tool_event(ToolEvent(
                    step=1,
                    tool_call_id="call-1",
                    tool_name="search_evidence",
                    arguments={"query": "FATAL"},
                    result='{"success":true,"data":{"evidence":[{"evidence_id":"ev-1","relative_path":"logs/main.log","line_start":42,"line_end":44,"excerpt":"FATAL"}]}}',
                    success=True,
                ))
            return "请核对 ev-1"

    app = create_app(
        ApiConfig(
            database_path=tmp_path / "tasks.sqlite3",
            allowed_local_roots=(case_root.resolve(),),
        ),
        EventWorker,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            await client.post("/conversations", json={
                "conversation_id": "event-chat",
                "task": {
                    "task_id": "event-task", "source": "local",
                    "case_path": str(case_path),
                },
            })
            sent = await client.post(
                "/conversations/event-chat/messages", json={"content": "开始分析"},
            )
            assert sent.status_code == 202
            for _ in range(100):
                page = await client.get("/conversations/event-chat/events")
                kinds = [event["kind"] for event in page.json()["events"]]
                if "assistant_message" in kinds:
                    break
                await asyncio.sleep(0)
            assert kinds == [
                "message_queued", "message_running", "tool_started",
                "tool_completed", "assistant_message",
            ]
            location = page.json()["events"][3]["locations"][0]
            assert location["relative_path"] == "logs/main.log"
            assert location["line_start"] == 42
            assert location["case_root"] == str(case_path.resolve())
            assert location["resolved_path"] == str((case_path / "logs" / "main.log").resolve())
            assert location["availability"] == "ready"
            cursor = page.json()["next_cursor"]
            resumed = await client.get(
                "/conversations/event-chat/events", params={"after": cursor},
            )
            assert resumed.json() == {
                "schema_version": 1, "events": [], "next_cursor": cursor,
            }


@pytest.mark.anyio
async def test_conversation_turn_can_be_cancelled(tmp_path):
    case_root = tmp_path / "cases"
    case_path = case_root / "APP-42"
    case_path.mkdir(parents=True)
    started = asyncio.Event()

    class WaitingConversationWorker(ImmediateWorker):
        async def answer_conversation_turn(
            self, task, history, user_message, *, max_steps_per_turn=None,
        ):
            started.set()
            await asyncio.Event().wait()

    app = create_app(
        ApiConfig(
            database_path=tmp_path / "tasks.sqlite3",
            allowed_local_roots=(case_root.resolve(),),
        ),
        WaitingConversationWorker,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            await client.post("/conversations", json={
                "conversation_id": "cancel-chat",
                "task": {
                    "task_id": "cancel-task", "source": "local",
                    "case_path": str(case_path),
                },
            })
            sent = await client.post(
                "/conversations/cancel-chat/messages", json={"content": "开始"},
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            cancelled = await client.post(
                "/conversations/cancel-chat/messages/" +
                str(sent.json()["message_id"]) + "/cancel",
            )
            assert cancelled.status_code == 202
            record = await client.get("/conversations/cancel-chat")
            assert record.json()["messages"][0]["status"] == "cancelled"
