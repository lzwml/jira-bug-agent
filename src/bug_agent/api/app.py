"""FastAPI 应用工厂。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ..config import AgentConfig
from ..contracts import BugAnalysisTask
from ..worker import BugAnalysisWorker
from .config import ApiConfig
from .dispatcher import ConversationDispatcher, TaskDispatcher, WorkerFactory
from .models import (
    ConversationCreate,
    ConversationEventPage,
    ConversationMessage,
    ConversationMessageCreate,
    ConversationRecord,
    ContinuationRequest,
    TaskRecord,
    TaskSubmission,
)
from .task_store import SqliteTaskStore, TaskConflictError


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def create_app(
    settings: ApiConfig | None = None,
    worker_factory: WorkerFactory | None = None,
) -> FastAPI:
    settings = settings or ApiConfig.from_environment()
    if worker_factory is None:
        agent_config = AgentConfig.from_environment()
        worker_factory = lambda: BugAnalysisWorker(agent_config)
    store = SqliteTaskStore(settings.database_path)
    dispatcher = TaskDispatcher(
        store, worker_factory, concurrency=settings.concurrency,
    )
    conversation_dispatcher = ConversationDispatcher(
        store, worker_factory, concurrency=settings.concurrency,
        # Store 拒绝同一会话并发消息；多个 Case 复用 API 的受控并发预算。
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await dispatcher.start()
        await conversation_dispatcher.start()
        app.state.dispatcher = dispatcher
        app.state.conversation_dispatcher = conversation_dispatcher
        try:
            yield
        finally:
            await conversation_dispatcher.stop()
            await dispatcher.stop()

    app = FastAPI(
        title="Jira Bug Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )

    def authorize(x_api_key: str | None) -> None:
        if settings.api_key is not None and not (
            x_api_key is not None and secrets.compare_digest(x_api_key, settings.api_key)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key 无效")

    def validate_local_task(task: BugAnalysisTask) -> BugAnalysisTask:
        if task.source != "local":
            return task
        case_path = Path(task.case_path or "").expanduser().resolve()
        if not settings.allowed_local_roots:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API 未配置本地 Case 允许目录",
            )
        if not _is_within(case_path, settings.allowed_local_roots):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="本地 Case 路径不在 API 允许目录内",
            )
        return task.model_copy(update={"case_path": str(case_path)})

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "api_version": "0.2.0",
            "capabilities": [
                "resolved_client_locations",
                "assistant_evidence_locations",
            ],
        }

    @app.post("/tasks", response_model=TaskSubmission, status_code=status.HTTP_202_ACCEPTED)
    async def submit_task(
        task: BugAnalysisTask,
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> TaskSubmission:
        authorize(x_api_key)
        task = validate_local_task(task)
        try:
            record, created = await request.app.state.dispatcher.submit(task)
        except TaskConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return TaskSubmission(task_id=record.task_id, status=record.status, created=created)

    @app.get("/tasks/{task_id}", response_model=TaskRecord)
    async def get_task(
        task_id: str,
        x_api_key: str | None = Header(default=None),
    ) -> TaskRecord:
        authorize(x_api_key)
        record = store.get(task_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
        return record

    @app.post("/conversations", response_model=ConversationRecord, status_code=status.HTTP_201_CREATED)
    async def create_conversation(
        request_body: ConversationCreate,
        x_api_key: str | None = Header(default=None),
    ) -> ConversationRecord:
        """建立 Case 级持久会话；消息和调查状态将在后续请求中连续累积。"""
        authorize(x_api_key)
        task = validate_local_task(request_body.task)
        if request_body.conversation_id is None:
            existing = store.find_active_conversation(task)
            if existing is not None:
                return existing
        conversation_id = request_body.conversation_id or str(uuid4())
        try:
            return store.create_conversation(conversation_id, task)
        except TaskConflictError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.get("/conversations/{conversation_id}", response_model=ConversationRecord)
    async def get_conversation(
        conversation_id: str,
        x_api_key: str | None = Header(default=None),
    ) -> ConversationRecord:
        authorize(x_api_key)
        record = store.get_conversation(conversation_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
        return record

    @app.get("/conversations", response_model=list[ConversationRecord])
    async def list_conversations(
        x_api_key: str | None = Header(default=None),
    ) -> list[ConversationRecord]:
        authorize(x_api_key)
        return store.list_conversations()

    @app.get(
        "/conversations/{conversation_id}/events",
        response_model=ConversationEventPage,
    )
    async def get_conversation_events(
        conversation_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=200, ge=1, le=500),
        x_api_key: str | None = Header(default=None),
    ) -> ConversationEventPage:
        authorize(x_api_key)
        finder = store.list_conversation_events
        try:
            events = finder(conversation_id, after=after, limit=limit)
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在",
            ) from exc
        return ConversationEventPage(
            events=events,
            next_cursor=events[-1].event_id if events else after,
        )

    @app.get("/conversations/{conversation_id}/events/stream")
    async def stream_conversation_events(
        conversation_id: str,
        request: Request,
        after: int = Query(default=0, ge=0),
        x_api_key: str | None = Header(default=None),
    ) -> StreamingResponse:
        authorize(x_api_key)
        if store.get_conversation(conversation_id) is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在",
            )

        async def stream():
            cursor = after
            while not await request.is_disconnected():
                events = store.list_conversation_events(
                    conversation_id, after=cursor, limit=200,
                )
                if not events:
                    yield ": keep-alive\n\n"
                for event in events:
                    cursor = event.event_id
                    payload = json.dumps(
                        event.model_dump(mode="json"), ensure_ascii=False,
                    )
                    yield f"id: {event.event_id}\nevent: {event.kind}\ndata: {payload}\n\n"
                await asyncio.sleep(0.4)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post(
        "/conversations/{conversation_id}/messages",
        response_model=ConversationMessage,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def add_conversation_message(
        conversation_id: str,
        request_body: ConversationMessageCreate,
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> ConversationMessage:
        """记录工程师新增的线索或追问。下一步将由会话执行器消费该消息。"""
        authorize(x_api_key)
        try:
            message = store.add_conversation_message(
                conversation_id, role="user", content=request_body.content,
            )
            await request.app.state.conversation_dispatcher.submit(message.message_id)
            return message
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    @app.post(
        "/conversations/{conversation_id}/messages/{message_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def cancel_conversation_message(
        conversation_id: str,
        message_id: int,
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, bool]:
        authorize(x_api_key)
        cancelled = await request.app.state.conversation_dispatcher.cancel(
            conversation_id, message_id,
        )
        if not cancelled:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该分析轮次不存在或已经结束",
            )
        return {"cancelled": True}

    @app.post("/tasks/{task_id}/continuations", response_model=TaskSubmission, status_code=status.HTTP_202_ACCEPTED)
    async def continue_task(
        task_id: str,
        request_body: ContinuationRequest,
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> TaskSubmission:
        """创建同一 Case 的下一轮调查，并把上一轮作为可审计父任务。"""
        authorize(x_api_key)
        previous = store.get(task_id)
        if previous is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
        inherited = previous.task
        changes = request_body.model_dump(exclude_none=True)
        changes.pop("metadata", None)
        changes["continuation_of"] = task_id
        changes["metadata"] = {**inherited.metadata, **request_body.metadata}
        # 续分析是新的运行，不能默认复用父任务的幂等键。
        changes["task_id"] = request_body.task_id or str(uuid4())
        task = BugAnalysisTask.model_validate({**inherited.model_dump(), **changes})
        try:
            record, created = await request.app.state.dispatcher.continue_from(task_id, task)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except (TaskConflictError, ValueError) as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        return TaskSubmission(task_id=record.task_id, status=record.status, created=created)

    return app
