"""ChatStore JSON 文件持久化会话存储测试。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

from bug_agent.chat_store import ChatStore, _session_path
from bug_agent.contracts import BugAnalysisTask


def _temp_dir() -> Path:
    return Path(tempfile.gettempdir()) / "bug-agent-chat-test" / uuid4().hex


def test_chat_store_creates_session_json_file():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))
        assert store.create_session("test-session", task) is True

        path = _session_path(sessions_dir, "test-session")
        assert path.is_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 3
        assert data["session_id"] == "test-session"
        assert data["status"] == "active"
        assert data["turns"] == []
        assert data["tool_catalogs"] == []
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_add_turn_with_tool_events():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))
        store.create_session("test-session", task)

        tool_events = [
            {"step": 1, "tool_name": "open_case", "arguments": {"case_path": "/tmp"}, "success": True},
            {"step": 2, "tool_name": "search_evidence", "arguments": {"query": "error"}, "success": True},
        ]
        store.add_turn(
            "test-session", 1, "分析 Bug", "根因是 OOM", 2, "completed",
            tool_events=tool_events,
            human_checkpoint={"checkpoint_id": "cp-1"},
            human_intervention={"response": "第二次复现"},
            token_usage={"total_tokens": 123},
        )

        session = store.get_session("test-session")
        assert session is not None
        assert len(session["turns"]) == 1
        turn = session["turns"][0]
        assert turn["user_message"] == "分析 Bug"
        assert turn["assistant_answer"] == "根因是 OOM"
        assert turn["steps"] == 2
        events = turn["tool_events"]
        assert len(events) == 2
        assert events[0]["tool_name"] == "open_case"
        assert events[0]["success"] is True
        assert events[1]["tool_name"] == "search_evidence"
        assert turn["human_checkpoint"]["checkpoint_id"] == "cp-1"
        assert turn["human_intervention"]["response"] == "第二次复现"
        assert turn["token_usage"]["total_tokens"] == 123
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_add_turn_without_tool_events_defaults_to_empty():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))
        store.create_session("test-session", task)

        store.add_turn("test-session", 1, "问题", "回答", 1, "completed")

        session = store.get_session("test-session")
        turn = session["turns"][0]
        assert turn["tool_events"] == []
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_versions_runtime_tool_catalog_and_binds_turn():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))
        store.create_session("test-session", task)
        tools = [{
            "name": "search_evidence",
            "description": "搜索证据",
            "parameters": {"type": "object", "required": ["query"]},
        }]

        catalog_id = store.register_tool_catalog("test-session", tools)
        assert store.register_tool_catalog("test-session", tools) == catalog_id
        store.add_turn("test-session", 1, "查找", "完成", 1, "completed")

        session = store.get_session("test-session")
        assert len(session["tool_catalogs"]) == 1
        assert session["tool_catalogs"][0]["tools"] == tools
        assert session["turns"][0]["tool_catalog_id"] == catalog_id
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_session_init_and_close():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))

        assert store.create_session("test-session", task) is True
        session = store.get_session("test-session")
        assert session["status"] == "active"

        store.close_session("test-session")
        session = store.get_session("test-session")
        assert session["status"] == "closed"

        # 关闭的会话重新创建应重新激活
        assert store.create_session("test-session", task) is True
        session = store.get_session("test-session")
        assert session["status"] == "active"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_duplicate_active_session_returns_false():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))

        assert store.create_session("test-session", task) is True
        assert store.create_session("test-session", task) is False
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_get_nonexistent_session_returns_none():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        assert store.get_session("nonexistent") is None
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_multiple_turns():
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))
        store.create_session("multi-turn", task)

        store.add_turn("multi-turn", 1, "第一问", "第一答", 3, "completed",
                       tool_events=[{"tool_name": "open_case"}])
        store.add_turn("multi-turn", 2, "第二问", "第二答", 2, "completed",
                       tool_events=[{"tool_name": "search_evidence"}])
        store.add_turn("multi-turn", 3, "第三问", "第三答", 1, "max_steps",
                       tool_events=[])

        session = store.get_session("multi-turn")
        assert len(session["turns"]) == 3
        assert session["turns"][0]["turn_index"] == 1
        assert session["turns"][1]["turn_index"] == 2
        assert session["turns"][2]["turn_index"] == 3
        assert session["turns"][2]["agent_status"] == "max_steps"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)


def test_chat_store_json_is_human_readable():
    """JSON 文件应对人类可读，包含缩进和换行。"""
    sessions_dir = _temp_dir()
    try:
        store = ChatStore(sessions_dir)
        task = BugAnalysisTask(source="local", case_path=str(sessions_dir))
        store.create_session("readable", task)
        store.add_turn("readable", 1, "分析", "结果", 1, "completed")

        path = _session_path(sessions_dir, "readable")
        raw = path.read_text(encoding="utf-8")
        # 应该有多行（indent=2）
        assert "\n" in raw
        assert "  " in raw
        # 重新解析确认是合法 JSON
        data = json.loads(raw)
        assert data["session_id"] == "readable"
    finally:
        import shutil
        shutil.rmtree(sessions_dir, ignore_errors=True)
