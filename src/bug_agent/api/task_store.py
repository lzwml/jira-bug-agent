"""基于 SQLite 的持久化任务状态存储。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from ..contracts import (
    BugAnalysisResult,
    BugAnalysisTask,
    InvestigationState,
    RCAReport,
    ReportValidation,
)
from ..models import ToolEvent
from .client_events import locations_from_tool_event
from .models import (
    ConversationEvent,
    ConversationMessage,
    ConversationPersistence,
    ConversationRecord,
    TaskRecord,
)


class TaskConflictError(ValueError):
    """同一个 task_id 被用于不同的任务内容。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_task(task: BugAnalysisTask) -> str:
    return json.dumps(
        task.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )


def _case_root(task: BugAnalysisTask) -> Path | None:
    if task.source == "local":
        return Path(task.case_path).expanduser().resolve() if task.case_path else None
    if not task.issue_key:
        return None
    from ..config import default_export_root
    return (default_export_root() / task.issue_key.upper()).resolve()


class SqliteTaskStore:
    """每次操作使用独立连接，便于事件循环中的多个执行器安全共享。"""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'completed', 'failed')
                    ),
                    result_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    task_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'closed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'completed',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS conversation_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    message_id INTEGER,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(conversation_id)
                )
            """)
            connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_conversation_events_cursor
                ON conversation_events(conversation_id, event_id)
            """)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(conversation_messages)")}
            if "status" not in columns:
                connection.execute("ALTER TABLE conversation_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
            if "error" not in columns:
                connection.execute("ALTER TABLE conversation_messages ADD COLUMN error TEXT")
            if "content_format" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_messages ADD COLUMN content_format TEXT NOT NULL DEFAULT 'plain_text'"
                )
            if "report_json" not in columns:
                connection.execute("ALTER TABLE conversation_messages ADD COLUMN report_json TEXT")
            if "report_validation_json" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_messages ADD COLUMN report_validation_json TEXT"
                )
            if "persistence_json" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_messages ADD COLUMN persistence_json TEXT"
                )
            conversation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(conversations)")
            }
            if "investigation_state_json" not in conversation_columns:
                connection.execute(
                    "ALTER TABLE conversations ADD COLUMN investigation_state_json TEXT"
                )

    def submit(self, task: BugAnalysisTask) -> tuple[TaskRecord, bool]:
        payload = _canonical_task(task)
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM analysis_tasks WHERE task_id = ?", (task.task_id,),
            ).fetchone()
            if row is not None:
                if row["task_json"] != payload:
                    raise TaskConflictError(
                        f"task_id={task.task_id} 已用于不同的任务内容"
                    )
                return self._record(row), False
            connection.execute(
                """INSERT INTO analysis_tasks
                   (task_id, task_json, status, created_at, updated_at)
                   VALUES (?, ?, 'queued', ?, ?)""",
                (task.task_id, payload, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM analysis_tasks WHERE task_id = ?", (task.task_id,),
            ).fetchone()
            return self._record(row), True

    def get(self, task_id: str) -> TaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_tasks WHERE task_id = ?", (task_id,),
            ).fetchone()
        return self._record(row) if row is not None else None

    def create_continuation(
        self, previous_task_id: str, task: BugAnalysisTask,
    ) -> tuple[TaskRecord, bool]:
        """原子校验上一轮并创建续分析任务。"""
        if task.continuation_of != previous_task_id:
            raise ValueError("续分析任务必须指向请求路径中的上一轮 task_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM analysis_tasks WHERE task_id = ?", (previous_task_id,),
            ).fetchone()
            if previous is None:
                raise KeyError(previous_task_id)
            if previous["status"] != "completed":
                raise RuntimeError("上一轮任务尚未完成，不能发起续分析")
        # submit 自己使用短事务；上一轮完成后不会再变化，故不需要持锁跨调用。
        return self.submit(task)

    def queued_task_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT task_id FROM analysis_tasks WHERE status = 'queued' ORDER BY created_at"
            ).fetchall()
        return [row["task_id"] for row in rows]

    def requeue_interrupted(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE analysis_tasks SET status = 'queued', updated_at = ?
                   WHERE status = 'running'""",
                (_now(),),
            )
            return cursor.rowcount

    def mark_running(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE analysis_tasks SET status = 'running', updated_at = ?
                   WHERE task_id = ? AND status = 'queued'""",
                (_now(), task_id),
            )
            return cursor.rowcount == 1

    def mark_completed(self, task_id: str, result: BugAnalysisResult) -> None:
        lifecycle_status = "failed" if result.status == "failed" else "completed"
        with self._connect() as connection:
            connection.execute(
                """UPDATE analysis_tasks
                   SET status = ?, result_json = ?, error = ?, updated_at = ?
                   WHERE task_id = ?""",
                (
                    lifecycle_status,
                    result.model_dump_json(),
                    result.error,
                    _now(),
                    task_id,
                ),
            )

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE analysis_tasks
                   SET status = 'failed', error = ?, updated_at = ?
                   WHERE task_id = ?""",
                (error, _now(), task_id),
            )

    def requeue(self, task_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE analysis_tasks SET status = 'queued', updated_at = ?
                   WHERE task_id = ? AND status = 'running'""",
                (_now(), task_id),
            )

    def create_conversation(
        self, conversation_id: str, task: BugAnalysisTask,
    ) -> ConversationRecord:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,),
            ).fetchone()
            if existing is not None:
                if existing["task_json"] != _canonical_task(task):
                    raise TaskConflictError(f"conversation_id={conversation_id} 已用于不同的 Case")
                return self._conversation_record(connection, existing)
            connection.execute(
                """INSERT INTO conversations
                   (conversation_id, task_json, status, created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?)""",
                (conversation_id, _canonical_task(task), timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,),
            ).fetchone()
            return self._conversation_record(connection, row)

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conversations WHERE conversation_id = ?", (conversation_id,),
            ).fetchone()
            return self._conversation_record(connection, row) if row is not None else None

    def find_active_conversation(self, task: BugAnalysisTask) -> ConversationRecord | None:
        """Return the most recently updated active conversation for the same Case."""

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM conversations WHERE status = 'active'
                   ORDER BY updated_at DESC"""
            ).fetchall()
            for row in rows:
                candidate = BugAnalysisTask.model_validate_json(row["task_json"])
                if self._same_case(candidate, task):
                    return self._conversation_record(connection, row)
        return None

    def save_conversation_investigation_state(
        self, conversation_id: str, state: InvestigationState,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE conversations SET investigation_state_json = ?, updated_at = ?
                   WHERE conversation_id = ?""",
                (state.model_dump_json(), _now(), conversation_id),
            )

    def list_conversations(self) -> list[ConversationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
            return [self._conversation_record(connection, row) for row in rows]

    @staticmethod
    def _same_case(left: BugAnalysisTask, right: BugAnalysisTask) -> bool:
        if left.source != right.source:
            return False
        if left.source == "jira":
            return bool(
                left.issue_key and right.issue_key
                and left.issue_key.casefold() == right.issue_key.casefold()
            )
        if not left.case_path or not right.case_path:
            return False
        return str(Path(left.case_path).resolve()).casefold() == str(
            Path(right.case_path).resolve()
        ).casefold()

    def add_conversation_message(
        self, conversation_id: str, *, role: str, content: str,
    ) -> ConversationMessage:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM conversations WHERE conversation_id = ?", (conversation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            if row["status"] != "active":
                raise RuntimeError("会话已经关闭")
            pending = connection.execute(
                """SELECT 1 FROM conversation_messages
                   WHERE conversation_id = ? AND role = 'user'
                   AND status IN ('queued', 'running') LIMIT 1""",
                (conversation_id,),
            ).fetchone()
            if pending is not None:
                raise RuntimeError("同一会话已有一轮正在排队或分析")
            cursor = connection.execute(
                """INSERT INTO conversation_messages (conversation_id, role, content, status, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (conversation_id, role, content, "queued" if role == "user" else "completed", timestamp),
            )
            if role == "user":
                self._append_event(
                    connection,
                    conversation_id=conversation_id,
                    message_id=cursor.lastrowid,
                    kind="message_queued",
                    payload={"content": content},
                    timestamp=timestamp,
                )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (timestamp, conversation_id),
            )
            return ConversationMessage(
                message_id=cursor.lastrowid, role=role, content=content,
                status="queued" if role == "user" else "completed", created_at=timestamp,
            )

    def queued_conversation_messages(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT message_id FROM conversation_messages WHERE role = 'user' AND status = 'queued' ORDER BY message_id",
            ).fetchall()
        return [row["message_id"] for row in rows]

    def requeue_running_conversation_messages(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE conversation_messages SET status = 'queued' WHERE role = 'user' AND status = 'running'",
            )
            return cursor.rowcount

    def claim_conversation_message(self, message_id: int) -> tuple[ConversationRecord, ConversationMessage, list[dict[str, str]]] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM conversation_messages WHERE message_id = ?", (message_id,)).fetchone()
            if row is None or row["role"] != "user" or row["status"] != "queued":
                return None
            connection.execute("UPDATE conversation_messages SET status = 'running' WHERE message_id = ?", (message_id,))
            self._append_event(
                connection,
                conversation_id=row["conversation_id"],
                message_id=message_id,
                kind="message_running",
                payload={},
            )
            conversation = connection.execute("SELECT * FROM conversations WHERE conversation_id = ?", (row["conversation_id"],)).fetchone()
            history = connection.execute(
                "SELECT role, content FROM conversation_messages WHERE conversation_id = ? AND message_id < ? AND status = 'completed' ORDER BY message_id",
                (row["conversation_id"], message_id),
            ).fetchall()
            record = self._conversation_record(connection, conversation)
            message = ConversationMessage(message_id=message_id, role="user", content=row["content"], status="running", created_at=row["created_at"])
            return record, message, [dict(item) for item in history]

    def complete_conversation_message(
        self,
        message_id: int,
        answer: str,
        *,
        content_format: str = "plain_text",
        report: RCAReport | None = None,
        report_validation: ReportValidation | None = None,
        persistence: ConversationPersistence | None = None,
    ) -> None:
        timestamp = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT conversation_id, status FROM conversation_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                return
            if row["status"] != "running":
                # 已完成的或已失败的消息无需重复处理
                return
            connection.execute(
                "UPDATE conversation_messages SET status = 'completed', error = NULL WHERE message_id = ?",
                (message_id,),
            )
            cursor = connection.execute(
                """INSERT INTO conversation_messages
                   (conversation_id, role, content, content_format, report_json,
                    report_validation_json, persistence_json, status, created_at)
                   VALUES (?, 'assistant', ?, ?, ?, ?, ?, 'completed', ?)""",
                (
                    row["conversation_id"], answer, content_format,
                    report.model_dump_json() if report else None,
                    report_validation.model_dump_json() if report_validation else None,
                    persistence.model_dump_json() if persistence else None,
                    timestamp,
                ),
            )
            presentation_payload = {
                "content": answer,
                "content_format": content_format,
                "report": report.model_dump(mode="json") if report else None,
                "report_validation": (
                    report_validation.model_dump(mode="json")
                    if report_validation else None
                ),
                "persistence": persistence.model_dump(mode="json") if persistence else None,
            }
            self._append_event(
                connection,
                conversation_id=row["conversation_id"],
                message_id=cursor.lastrowid,
                kind="assistant_message",
                payload=presentation_payload,
                timestamp=timestamp,
            )
            connection.execute("UPDATE conversations SET updated_at = ? WHERE conversation_id = ?", (timestamp, row["conversation_id"]))

    def fail_conversation_message(self, message_id: int, error: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT conversation_id, status FROM conversation_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            cursor = connection.execute(
                "UPDATE conversation_messages SET status = 'failed', error = ? WHERE message_id = ? AND status = 'running'",
                (error, message_id),
            )
            if row is not None and cursor.rowcount:
                self._append_event(
                    connection,
                    conversation_id=row["conversation_id"],
                    message_id=message_id,
                    kind="turn_failed",
                    payload={"content": error},
                )

    def cancel_conversation_message(self, conversation_id: str, message_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE conversation_messages SET status = 'cancelled', error = NULL
                   WHERE conversation_id = ? AND message_id = ?
                   AND role = 'user' AND status IN ('queued', 'running')""",
                (conversation_id, message_id),
            )
            if not cursor.rowcount:
                return False
            self._append_event(
                connection,
                conversation_id=conversation_id,
                message_id=message_id,
                kind="turn_cancelled",
                payload={},
            )
            return True

    def append_tool_event(self, conversation_id: str, message_id: int, event: ToolEvent) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task_json FROM conversations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            task = BugAnalysisTask.model_validate_json(row["task_json"]) if row else None
            self._append_event(
                connection,
                conversation_id=conversation_id,
                message_id=message_id,
                kind="tool_completed",
                payload={
                    "step": event.step,
                    "tool_name": event.tool_name,
                    "success": event.success,
                    "arguments": event.arguments,
                    "result": event.result,
                    "locations": [
                        item.model_dump(mode="json")
                        for item in locations_from_tool_event(
                            event, case_root=_case_root(task) if task else None,
                        )
                    ],
                },
            )

    def append_progress_event(
        self, conversation_id: str, message_id: int, progress: dict,
    ) -> None:
        kind = str(progress.get("kind") or "phase_changed")
        if kind not in {"phase_changed", "tool_started"}:
            kind = "phase_changed"
        payload = {key: value for key, value in progress.items() if key != "kind"}
        with self._connect() as connection:
            self._append_event(
                connection,
                conversation_id=conversation_id,
                message_id=message_id,
                kind=kind,
                payload=payload,
            )

    def list_conversation_events(
        self, conversation_id: str, *, after: int = 0, limit: int = 200,
    ) -> list[ConversationEvent]:
        with self._connect() as connection:
            conversation = connection.execute(
                "SELECT task_json FROM conversations WHERE conversation_id = ?", (conversation_id,),
            ).fetchone()
            if conversation is None:
                raise KeyError(conversation_id)
            rows = connection.execute(
                """SELECT * FROM conversation_events
                   WHERE conversation_id = ? AND event_id > ?
                   ORDER BY event_id LIMIT ?""",
                (conversation_id, after, limit),
            ).fetchall()
        task = BugAnalysisTask.model_validate_json(conversation["task_json"])
        return [self._conversation_event(row, case_root=_case_root(task)) for row in rows]

    def conversation_tool_events(self, conversation_id: str) -> list[ToolEvent]:
        """Rebuild the durable tool trace used to validate a presented RCA."""

        with self._connect() as connection:
            rows = connection.execute(
                """SELECT event_id, payload_json FROM conversation_events
                   WHERE conversation_id = ? AND kind = 'tool_completed'
                   ORDER BY event_id""",
                (conversation_id,),
            ).fetchall()
        events: list[ToolEvent] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            events.append(ToolEvent(
                step=int(payload.get("step") or 0),
                tool_call_id=f"conversation-event-{row['event_id']}",
                tool_name=str(payload.get("tool_name") or ""),
                arguments=payload.get("arguments") or {},
                result=str(payload.get("result") or ""),
                success=bool(payload.get("success")),
            ))
        return events

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        message_id: int | None,
        kind: str,
        payload: dict,
        timestamp: str | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO conversation_events
               (conversation_id, message_id, kind, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                conversation_id,
                message_id,
                kind,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                timestamp or _now(),
            ),
        )

    @staticmethod
    def _conversation_event(
        row: sqlite3.Row, *, case_root: Path | None = None,
    ) -> ConversationEvent:
        payload = json.loads(row["payload_json"])
        if row["kind"] == "tool_completed" and payload.get("result"):
            tool_event = ToolEvent(
                step=int(payload.get("step") or 0),
                tool_call_id=f"conversation-event-{row['event_id']}",
                tool_name=str(payload.get("tool_name") or ""),
                arguments=payload.get("arguments") or {},
                result=str(payload.get("result") or ""),
                success=bool(payload.get("success")),
            )
            payload["locations"] = [
                item.model_dump(mode="json")
                for item in locations_from_tool_event(tool_event, case_root=case_root)
            ]
        return ConversationEvent(
            event_id=row["event_id"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            kind=row["kind"],
            created_at=row["created_at"],
            **payload,
        )

    @staticmethod
    def _record(row: sqlite3.Row) -> TaskRecord:
        result = (
            BugAnalysisResult.model_validate_json(row["result_json"])
            if row["result_json"] else None
        )
        return TaskRecord(
            task_id=row["task_id"],
            status=row["status"],
            task=BugAnalysisTask.model_validate_json(row["task_json"]),
            result=result,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _conversation_record(
        connection: sqlite3.Connection, row: sqlite3.Row,
    ) -> ConversationRecord:
        messages = connection.execute(
            """SELECT message_id, role, content, content_format, report_json,
                      report_validation_json, persistence_json, status, error, created_at
               FROM conversation_messages
               WHERE conversation_id = ? ORDER BY message_id""",
            (row["conversation_id"],),
        ).fetchall()
        task = BugAnalysisTask.model_validate_json(row["task_json"])
        return ConversationRecord(
            conversation_id=row["conversation_id"],
            status=row["status"],
            task=task,
            case_root=str(_case_root(task)) if _case_root(task) else None,
            messages=[SqliteTaskStore._conversation_message(message) for message in messages],
            investigation_state=(
                InvestigationState.model_validate_json(row["investigation_state_json"])
                if row["investigation_state_json"] else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _conversation_message(row: sqlite3.Row) -> ConversationMessage:
        return ConversationMessage(
            message_id=row["message_id"],
            role=row["role"],
            content=row["content"],
            content_format=row["content_format"] or "plain_text",
            report=(
                RCAReport.model_validate_json(row["report_json"])
                if row["report_json"] else None
            ),
            report_validation=(
                ReportValidation.model_validate_json(row["report_validation_json"])
                if row["report_validation_json"] else None
            ),
            persistence=(
                ConversationPersistence.model_validate_json(row["persistence_json"])
                if row["persistence_json"] else None
            ),
            status=row["status"],
            error=row["error"],
            created_at=row["created_at"],
        )
