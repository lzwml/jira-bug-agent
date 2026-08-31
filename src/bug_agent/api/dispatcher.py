"""有界并发的进程内任务调度器。"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Protocol

from ..contracts import BugAnalysisResult, BugAnalysisTask
from .models import TaskRecord
from .task_store import SqliteTaskStore

logger = logging.getLogger(__name__)


class Worker(Protocol):
    async def execute(self, task: BugAnalysisTask) -> BugAnalysisResult: ...


WorkerFactory = Callable[[], Worker]


class TaskDispatcher:
    def __init__(
        self, store: SqliteTaskStore, worker_factory: WorkerFactory, *, concurrency: int,
    ):
        self.store = store
        self.worker_factory = worker_factory
        self.concurrency = concurrency
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._runners: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.store.initialize()
        self.store.requeue_interrupted()
        for task_id in self.store.queued_task_ids():
            self._queue.put_nowait(task_id)
        self._runners = [
            asyncio.create_task(self._run_loop(), name=f"bug-agent-runner-{index}")
            for index in range(self.concurrency)
        ]

    async def stop(self) -> None:
        for runner in self._runners:
            runner.cancel()
        if self._runners:
            await asyncio.gather(*self._runners, return_exceptions=True)
        self._runners = []

    async def submit(self, task: BugAnalysisTask) -> tuple[TaskRecord, bool]:
        record, created = self.store.submit(task)
        if created:
            await self._queue.put(task.task_id)
        return record, created

    async def _run_loop(self) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                if not self.store.mark_running(task_id):
                    continue
                record = self.store.get(task_id)
                if record is None:
                    continue
                try:
                    result = await self.worker_factory().execute(record.task)
                except asyncio.CancelledError:
                    self.store.requeue(task_id)
                    raise
                except Exception as exc:  # Worker 边界之外仍只暴露安全的异常类型。
                    logger.exception("后台任务发生未处理异常 (task_id=%s)", task_id)
                    self.store.mark_failed(task_id, type(exc).__name__)
                else:
                    self.store.mark_completed(task_id, result)
            finally:
                self._queue.task_done()
