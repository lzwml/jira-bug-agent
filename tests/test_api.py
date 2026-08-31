from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from bug_agent.api.app import create_app
from bug_agent.api.config import ApiConfig
from bug_agent.api.dispatcher import TaskDispatcher
from bug_agent.api.task_store import SqliteTaskStore, TaskConflictError
from bug_agent.contracts import BugAnalysisResult, BugAnalysisTask, RCAReport


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
    assert response.json() == {"status": "ok"}
