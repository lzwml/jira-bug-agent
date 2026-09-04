"""ChatStore 持久化会话存储测试。"""

from __future__ import annotations

import gc
import json
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

from bug_agent.chat_store import ChatStore
from bug_agent.contracts import BugAnalysisTask


def _temp_db() -> Path:
    """创建本地唯一临时 SQLite 文件路径。"""
    tmp = Path(tempfile.gettempdir()) / "bug-agent-chat-test"
    tmp.mkdir(exist_ok=True)
    return tmp / f"chat-{uuid4().hex}.db"


def _close_and_cleanup(db_path: Path) -> None:
    """强制关闭可能残留的 SQLite 连接，然后删除文件。"""
    gc.collect()
    try:
        db_path.unlink()
    except OSError:
        pass


def test_chat_store_creates_tables_with_tool_events_column():
    db_path = _temp_db()
    try:
        store = ChatStore(db_path)
        store.initialize()

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_turns)")}
        assert "tool_events_json" in columns
        conn.close()
    finally:
        _close_and_cleanup(db_path)


def test_chat_store_migrates_old_table_without_tool_events_column():
    db_path = _temp_db()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE chat_sessions (
                session_id TEXT PRIMARY KEY,
                task_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE chat_turns (
                turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                user_message TEXT NOT NULL,
                assistant_answer TEXT NOT NULL,
                steps INTEGER NOT NULL DEFAULT 0,
                agent_status TEXT NOT NULL DEFAULT 'completed',
                created_at TEXT NOT NULL
            )
        """)
        conn.close()

        store = ChatStore(db_path)
        store.initialize()

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(chat_turns)")}
        assert "tool_events_json" in columns
        conn.close()
    finally:
        _close_and_cleanup(db_path)


def test_chat_store_add_turn_with_tool_events():
    db_path = _temp_db()
    try:
        store = ChatStore(db_path)
        store.initialize()
        task = BugAnalysisTask(source="local", case_path=str(db_path.parent))
        store.create_session("test-session", task)

        tool_events = [
            {"step": 1, "tool_name": "open_case", "arguments": {"case_path": "/tmp"}, "success": True},
            {"step": 2, "tool_name": "search_evidence", "arguments": {"query": "error"}, "success": True},
        ]
        store.add_turn(
            "test-session", 1, "分析 Bug", "根因是 OOM", 2, "completed",
            tool_events=tool_events,
        )

        session = store.get_session("test-session")
        assert session is not None
        assert len(session["turns"]) == 1
        turn = session["turns"][0]
        assert turn["user_message"] == "分析 Bug"
        assert turn["assistant_answer"] == "根因是 OOM"
        assert turn["steps"] == 2
        events = json.loads(turn["tool_events_json"])
        assert len(events) == 2
        assert events[0]["tool_name"] == "open_case"
        assert events[0]["success"] is True
        assert events[1]["tool_name"] == "search_evidence"
    finally:
        _close_and_cleanup(db_path)


def test_chat_store_add_turn_without_tool_events_defaults_to_empty():
    db_path = _temp_db()
    try:
        store = ChatStore(db_path)
        store.initialize()
        task = BugAnalysisTask(source="local", case_path=str(db_path.parent))
        store.create_session("test-session", task)

        store.add_turn("test-session", 1, "问题", "回答", 1, "completed")

        session = store.get_session("test-session")
        turn = session["turns"][0]
        assert turn["tool_events_json"] == "[]"
    finally:
        _close_and_cleanup(db_path)


def test_chat_store_session_init_and_close():
    db_path = _temp_db()
    try:
        store = ChatStore(db_path)
        store.initialize()
        task = BugAnalysisTask(source="local", case_path=str(db_path.parent))

        assert store.create_session("test-session", task) is True
        session = store.get_session("test-session")
        assert session["status"] == "active"

        store.close_session("test-session")
        session = store.get_session("test-session")
        assert session["status"] == "closed"

        assert store.create_session("test-session", task) is True
        session = store.get_session("test-session")
        assert session["status"] == "active"
    finally:
        _close_and_cleanup(db_path)


def test_chat_store_duplicate_active_session_returns_false():
    db_path = _temp_db()
    try:
        store = ChatStore(db_path)
        store.initialize()
        task = BugAnalysisTask(source="local", case_path=str(db_path.parent))

        assert store.create_session("test-session", task) is True
        assert store.create_session("test-session", task) is False
    finally:
        _close_and_cleanup(db_path)