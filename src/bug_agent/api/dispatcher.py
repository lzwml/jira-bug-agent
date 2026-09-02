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

    async def answer_conversation_turn(self, task: BugAnalysisTask, history: list[dict[str, str]], user_message: str, *, max_steps_per_turn: int | None = None) -> str: ...


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

    async def continue_from(
        self, previous_task_id: str, task: BugAnalysisTask,
    ) -> tuple[TaskRecord, bool]:
        record, created = self.store.create_continuation(previous_task_id, task)
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


class ConversationDispatcher:
    """按消息顺序执行持久会话回合；重启后从 SQLite 重新入队。"""

    def __init__(self, store: SqliteTaskStore, worker_factory: WorkerFactory, *, concurrency: int):
        self.store, self.worker_factory, self.concurrency = store, worker_factory, concurrency
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._runners: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.store.requeue_running_conversation_messages()
        for message_id in self.store.queued_conversation_messages():
            self._queue.put_nowait(message_id)
        self._runners = [asyncio.create_task(self._run_loop()) for _ in range(self.concurrency)]

    async def stop(self) -> None:
        for runner in self._runners:
            runner.cancel()
        await asyncio.gather(*self._runners, return_exceptions=True)
        self._runners = []

    async def submit(self, message_id: int) -> None:
        await self._queue.put(message_id)

    async def _run_loop(self) -> None:
        while True:
            message_id = await self._queue.get()
            try:
                claimed = self.store.claim_conversation_message(message_id)
                if claimed is None:
                    continue
                conversation, message, history = claimed
                try:
                    answer = await self.worker_factory().answer_conversation_turn(
                        conversation.task, history, message.content,
                    )
                except asyncio.CancelledError:
                    self.store.requeue_running_conversation_messages()
                    raise
                except Exception as exc:
                    logger.exception("会话回合失败 (message_id=%s)", message_id)
                    self.store.fail_conversation_message(message_id, type(exc).__name__)
                else:
                    self.store.complete_conversation_message(message_id, answer)
            finally:
                self._queue.task_done()
