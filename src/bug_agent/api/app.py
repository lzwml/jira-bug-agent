"""FastAPI 应用工厂。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import secrets

from fastapi import FastAPI, Header, HTTPException, Request, status

from ..config import AgentConfig
from ..contracts import BugAnalysisTask
from ..worker import BugAnalysisWorker
from .config import ApiConfig
from .dispatcher import TaskDispatcher, WorkerFactory
from .models import TaskRecord, TaskSubmission
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await dispatcher.start()
        app.state.dispatcher = dispatcher
        try:
            yield
        finally:
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/tasks", response_model=TaskSubmission, status_code=status.HTTP_202_ACCEPTED)
    async def submit_task(
        task: BugAnalysisTask,
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> TaskSubmission:
        authorize(x_api_key)
        if task.source == "local":
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
            task = task.model_copy(update={"case_path": str(case_path)})
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

    return app
