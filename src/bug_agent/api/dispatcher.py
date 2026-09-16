"""有界并发的进程内任务调度器。"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Callable, Protocol

from ..application.rca_reconciliation import reconcile
from ..domain.contracts import BugAnalysisResult, BugAnalysisTask, InvestigationState
from ..domain.models import ToolEvent
from ..infrastructure.persistence.rca_store import RCAStore
from ..presentation.report_parser import present_conversation_answer
from .models import ConversationPersistence, TaskRecord
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
        self._active: dict[int, asyncio.Task[str]] = {}
        self._stopping = False
        self._cancelled_by_user: set[int] = set()

    async def start(self) -> None:
        self._stopping = False
        self.store.requeue_running_conversation_messages()
        for message_id in self.store.queued_conversation_messages():
            self._queue.put_nowait(message_id)
        self._runners = [asyncio.create_task(self._run_loop()) for _ in range(self.concurrency)]

    async def stop(self) -> None:
        self._stopping = True
        for execution in self._active.values():
            execution.cancel()
        for runner in self._runners:
            runner.cancel()
        await asyncio.gather(*self._runners, return_exceptions=True)
        self._runners = []
        self._active = {}
        self._cancelled_by_user = set()

    async def submit(self, message_id: int) -> None:
        await self._queue.put(message_id)

    async def cancel(self, conversation_id: str, message_id: int) -> bool:
        cancelled = self.store.cancel_conversation_message(conversation_id, message_id)
        execution = self._active.get(message_id)
        if execution is not None:
            self._cancelled_by_user.add(message_id)
            execution.cancel()
        return cancelled

    async def _run_loop(self) -> None:
        while True:
            message_id = await self._queue.get()
            try:
                claimed = self.store.claim_conversation_message(message_id)
                if claimed is None:
                    continue
                conversation, message, history = claimed
                worker = self.worker_factory()
                state_saved = False

                def on_tool_event(event: ToolEvent) -> None:
                    self.store.append_tool_event(
                        conversation.conversation_id, message.message_id, event,
                    )

                def on_progress(progress: dict) -> None:
                    self.store.append_progress_event(
                        conversation.conversation_id, message.message_id, progress,
                    )

                def on_investigation_state(state: InvestigationState) -> None:
                    nonlocal state_saved
                    self.store.save_conversation_investigation_state(
                        conversation.conversation_id, state,
                    )
                    state_saved = True

                try:
                    method = worker.answer_conversation_turn
                    parameters = inspect.signature(method).parameters
                    kwargs = {}
                    if "on_tool_event" in parameters:
                        kwargs["on_tool_event"] = on_tool_event
                    if "on_progress" in parameters:
                        kwargs["on_progress"] = on_progress
                    if "initial_investigation_state" in parameters:
                        kwargs["initial_investigation_state"] = conversation.investigation_state
                    if "on_investigation_state" in parameters:
                        kwargs["on_investigation_state"] = on_investigation_state
                    execution = asyncio.create_task(
                        method(conversation.task, history, message.content, **kwargs),
                        name=f"bug-agent-conversation-{message_id}",
                    )
                    self._active[message_id] = execution
                    answer = await execution
                except asyncio.CancelledError:
                    if message_id not in self._cancelled_by_user:
                        self.store.requeue_running_conversation_messages()
                    if self._stopping:
                        raise
                except Exception as exc:
                    logger.exception("会话回合失败 (message_id=%s)", message_id)
                    self.store.fail_conversation_message(message_id, type(exc).__name__)
                else:
                    tool_events = self.store.conversation_tool_events(
                        conversation.conversation_id,
                    )
                    presentation = present_conversation_answer(
                        answer,
                        task_id=conversation.task.task_id,
                        tool_events=tool_events,
                    )
                    persistence = ConversationPersistence(
                        investigation_state_saved=state_saved,
                    )
                    if presentation.report is not None:
                        report = presentation.report
                        result = BugAnalysisResult(
                            task_id=conversation.task.task_id,
                            status=(
                                "insufficient_evidence"
                                if report.conclusion_status == "insufficient_evidence"
                                else "completed"
                            ),
                            report=report,
                            steps=max((event.step for event in tool_events), default=0),
                            structured_output=True,
                            report_validation=presentation.report_validation,
                        )
                        reconciled = reconcile(conversation.task, result)
                        if reconciled is not None:
                            rca_store = RCAStore(conversation.task)
                            persistence = persistence.model_copy(update={
                                "rca_saved": True,
                                "rca_markdown_path": str(rca_store.markdown_path),
                                "rca_state_path": str(rca_store.state_path),
                                "rca_events_path": str(rca_store.events_path),
                            })
                    self.store.complete_conversation_message(
                        message_id,
                        presentation.content,
                        content_format=presentation.content_format,
                        report=presentation.report,
                        report_validation=presentation.report_validation,
                        persistence=persistence,
                    )
                finally:
                    self._active.pop(message_id, None)
                    self._cancelled_by_user.discard(message_id)
            finally:
                self._queue.task_done()
