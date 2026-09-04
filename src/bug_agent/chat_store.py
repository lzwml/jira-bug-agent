"""CLI 交互式会话的 SQLite 持久化存储。

每个 Case 的 .bug-agent/chat.db 中保存会话轮次，支持退出后 resume。
与 runstore.py 不同：runstore 记录的是每次 Agent 运行的完整 trace，
chat_store 记录的是交互式对话的用户消息、最终回答和工具调用轨迹。

链路可查性：每轮对话的 tool_events 完整落盘，包括每次工具调用的名称、
参数、结果和成功/失败状态。复盘时可据此判断是 MCP Server 返回了错误数据、
Skill 指引了错误的调用顺序，还是模型本身推理有误。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import BugAnalysisTask


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_task(task: BugAnalysisTask) -> str:
    return json.dumps(
        task.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )


def resolve_chat_db_path(task: BugAnalysisTask) -> Path | None:
    """返回该 task 对应的 chat.db 路径，与 runstore 的 Case 目录一致。

    - jira 模式：<export_root>/<ISSUE_KEY>/.bug-agent/chat.db
    - local 模式：<case_path>/.bug-agent/chat.db
    """
    if task.source == "local":
        if not task.case_path:
            return None
        return Path(task.case_path).expanduser().resolve() / ".bug-agent" / "chat.db"
    from .config import default_export_root

    issue_key = (task.issue_key or "").upper()
    if not issue_key:
        return None
    return default_export_root() / issue_key / ".bug-agent" / "chat.db"


def resolve_chat_session_id(task: BugAnalysisTask) -> str:
    """从 task 派生稳定的会话 ID，同一 Case 重复执行 chat 时命中同一会话。"""
    if task.source == "jira":
        return f"chat-{task.issue_key.upper()}"
    if task.case_path:
        return f"chat-local-{Path(task.case_path).expanduser().resolve()}"
    return f"chat-{task.task_id}"


class ChatStore:
    """每次操作使用独立连接，多个执行器安全共享。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY,
                    task_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'closed')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_turns (
                    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_index INTEGER NOT NULL,
                    user_message TEXT NOT NULL,
                    assistant_answer TEXT NOT NULL,
                    steps INTEGER NOT NULL DEFAULT 0,
                    agent_status TEXT NOT NULL DEFAULT 'completed',
                    tool_events_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(session_id)
                )
            """)
            # 兼容旧表（无 tool_events_json 列）的迁移
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_turns)")}
            if "tool_events_json" not in columns:
                conn.execute("ALTER TABLE chat_turns ADD COLUMN tool_events_json TEXT NOT NULL DEFAULT '[]'")

    def get_session(self, session_id: str) -> dict | None:
        """获取会话元数据及所有轮次，不存在时返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_sessions WHERE session_id = ?", (session_id,),
            ).fetchone()
            if row is None:
                return None
            turns = conn.execute(
                "SELECT * FROM chat_turns WHERE session_id = ? ORDER BY turn_index",
                (session_id,),
            ).fetchall()
            return {
                "session_id": row["session_id"],
                "status": row["status"],
                "task_json": row["task_json"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "turns": [dict(t) for t in turns],
            }

    def create_session(self, session_id: str, task: BugAnalysisTask) -> bool:
        """创建新会话；已存在且 active 时返回 False，closed 则重新激活。"""
        timestamp = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM chat_sessions WHERE session_id = ?", (session_id,),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "closed":
                    conn.execute(
                        "UPDATE chat_sessions SET status = 'active', updated_at = ? WHERE session_id = ?",
                        (timestamp, session_id),
                    )
                    return True
                return False
            conn.execute(
                """INSERT INTO chat_sessions (session_id, task_json, status, created_at, updated_at)
                   VALUES (?, ?, 'active', ?, ?)""",
                (session_id, _canonical_task(task), timestamp, timestamp),
            )
            return True

    def add_turn(
        self, session_id: str, turn_index: int, user_message: str,
        assistant_answer: str, steps: int, agent_status: str,
        tool_events: list[dict[str, Any]] | None = None,
    ) -> None:
        """追加一轮对话记录，含完整的工具调用轨迹。"""
        timestamp = _now()
        tool_events_json = json.dumps(tool_events or [], ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO chat_turns
                   (session_id, turn_index, user_message, assistant_answer,
                    steps, agent_status, tool_events_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, turn_index, user_message, assistant_answer,
                 steps, agent_status, tool_events_json, timestamp),
            )
            conn.execute(
                "UPDATE chat_sessions SET updated_at = ? WHERE session_id = ?",
                (timestamp, session_id),
            )

    def close_session(self, session_id: str) -> None:
        """标记会话为已关闭。"""
        with self._connect() as conn:
            conn.execute(
                "UPDATE chat_sessions SET status = 'closed', updated_at = ? WHERE session_id = ?",
                (_now(), session_id),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn