"""基于 SQLite 的持久化任务状态存储。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from ..contracts import BugAnalysisResult, BugAnalysisTask
from .models import TaskRecord


class TaskConflictError(ValueError):
    """同一个 task_id 被用于不同的任务内容。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_task(task: BugAnalysisTask) -> str:
    return json.dumps(
        task.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )


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

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

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
