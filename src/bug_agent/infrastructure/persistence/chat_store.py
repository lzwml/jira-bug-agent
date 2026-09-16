"""CLI 交互式会话的 JSON 文件持久化存储。

每个 Case 的 .bug-agent/chat-sessions/<session_id>.json 中保存完整会话，
包含所有轮次的用户消息、模型回答和工具调用轨迹。与 runstore 的
.bug-agent/runs/*.json 风格一致。

链路可查性：每轮对话的 tool_events 完整落盘，包括每次工具调用的名称、
参数、结果和成功/失败状态。复盘时直接打开 JSON 文件即可查看，
无需 SQL 客户端。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ...domain.contracts import BugAnalysisTask
from .optimization_feedback import write_optimization_candidate


OptimizationCandidateWriter = Callable[..., Path | None]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_chat_sessions_dir(task: BugAnalysisTask) -> Path | None:
    """返回该 task 对应的 chat-sessions 目录。

    - jira 模式：<export_root>/<ISSUE_KEY>/.bug-agent/chat-sessions/
    - local 模式：<case_path>/.bug-agent/chat-sessions/
    """
    if task.source == "local":
        if not task.case_path:
            return None
        return Path(task.case_path).expanduser().resolve() / ".bug-agent" / "chat-sessions"
    from ..config import default_export_root

    issue_key = (task.issue_key or "").upper()
    if not issue_key:
        return None
    return default_export_root() / issue_key / ".bug-agent" / "chat-sessions"


def resolve_chat_session_id(task: BugAnalysisTask) -> str:
    """从 task 派生稳定的会话 ID，同一 Case 重复执行 chat 时命中同一会话。"""
    if task.source == "jira":
        return f"chat-{task.issue_key.upper()}"
    if task.case_path:
        return f"chat-local-{Path(task.case_path).expanduser().resolve()}"
    return f"chat-{task.task_id}"


def _session_path(sessions_dir: Path, session_id: str) -> Path:
    return sessions_dir / f"{session_id}.json"


class ChatStore:
    """每次操作原子写入单个 JSON 文件，与 runs/*.json 风格一致。"""

    def __init__(
        self,
        sessions_dir: Path,
        *,
        optimization_candidate_writer: OptimizationCandidateWriter | None = (
            write_optimization_candidate
        ),
    ):
        self._dir = sessions_dir
        self._optimization_candidate_writer = optimization_candidate_writer

    @property
    def sessions_dir(self) -> Path:
        return self._dir

    def _atomic_write(self, path: Path, content: str) -> None:
        """原子写入：先写临时文件，再 rename。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with tmp.open("x", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def get_session(self, session_id: str) -> dict | None:
        """获取会话数据，不存在时返回 None。"""
        path = _session_path(self._dir, session_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def create_session(self, session_id: str, task: BugAnalysisTask) -> bool:
        """创建新会话；已存在且 active 时返回 False，closed 则重新激活。"""
        path = _session_path(self._dir, session_id)
        timestamp = _now()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("status") == "active":
                return False
            # closed → 重新激活
            data["status"] = "active"
            data["schema_version"] = 3
            data["updated_at"] = timestamp
            self._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
            return True

        data = {
            "schema_version": 3,
            "session_id": session_id,
            "task": task.model_dump(mode="json"),
            "status": "active",
            "created_at": timestamp,
            "updated_at": timestamp,
            "turns": [],
            "tool_catalogs": [],
            "active_tool_catalog_id": None,
        }
        self._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        return True

    def add_turn(
        self,
        session_id: str,
        turn_index: int,
        user_message: str,
        assistant_answer: str,
        steps: int,
        agent_status: str,
        tool_events: list[dict[str, Any]] | None = None,
        human_checkpoint: dict[str, Any] | None = None,
        human_intervention: dict[str, Any] | None = None,
        token_usage: dict[str, Any] | None = None,
    ) -> None:
        """追加一轮对话记录，含完整的工具调用轨迹。"""
        path = _session_path(self._dir, session_id)
        timestamp = _now()
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {
                "schema_version": 3,
                "session_id": session_id,
                "task": None,
                "status": "active",
                "created_at": timestamp,
                "updated_at": timestamp,
                "turns": [],
                "tool_catalogs": [],
                "active_tool_catalog_id": None,
            }

        data["schema_version"] = 3
        optimization_candidate = None
        if human_intervention is not None and self._optimization_candidate_writer is not None:
            optimization_candidate = self._optimization_candidate_writer(
                self._dir,
                session_id=session_id,
                turn_index=turn_index,
                task=data.get("task"),
                intervention_value=human_intervention,
            )
        data["turns"].append({
            "turn_index": turn_index,
            "user_message": user_message,
            "assistant_answer": assistant_answer,
            "steps": steps,
            "agent_status": agent_status,
            "tool_events": tool_events or [],
            "human_checkpoint": human_checkpoint,
            "human_intervention": human_intervention,
            "token_usage": token_usage,
            "optimization_candidate": str(optimization_candidate) if optimization_candidate else None,
            "tool_catalog_id": data.get("active_tool_catalog_id"),
            "created_at": timestamp,
        })
        data["updated_at"] = timestamp

        self._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))

    def register_tool_catalog(self, session_id: str, tools: list[dict[str, Any]]) -> str:
        """保存运行时真实 Tool Schema；同一内容只保存一次。"""
        path = _session_path(self._dir, session_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        canonical = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        catalog_id = "tools-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        catalogs = data.setdefault("tool_catalogs", [])
        if not any(item.get("catalog_id") == catalog_id for item in catalogs):
            catalogs.append({
                "catalog_id": catalog_id,
                "captured_at": _now(),
                "tools": tools,
            })
        data["schema_version"] = 3
        data["active_tool_catalog_id"] = catalog_id
        data["updated_at"] = _now()
        self._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
        return catalog_id

    def close_session(self, session_id: str) -> None:
        """标记会话为已关闭。"""
        path = _session_path(self._dir, session_id)
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "closed"
        data["updated_at"] = _now()
        self._atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
