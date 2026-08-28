"""Agent Runtime 配置，只从环境或 CLI 读取，不绑定某个模型供应商。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from jira_bug_mcp.config import load_local_env


@dataclass(frozen=True)
class AgentConfig:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    max_steps: int = 12
    llm_timeout_seconds: float = 120.0
    max_tool_result_chars: int = 40_000

    @classmethod
    def from_environment(cls) -> "AgentConfig":
        load_local_env()
        base_url = os.getenv("BUG_AGENT_LLM_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv("BUG_AGENT_LLM_API_KEY", "").strip()
        model = os.getenv("BUG_AGENT_LLM_MODEL", "").strip()
        missing = [
            name for name, value in {
                "BUG_AGENT_LLM_BASE_URL": base_url,
                "BUG_AGENT_LLM_API_KEY": api_key,
                "BUG_AGENT_LLM_MODEL": model,
            }.items() if not value
        ]
        if missing:
            raise ValueError(f"缺少 Agent 配置: {', '.join(missing)}")
        max_steps = int(os.getenv("BUG_AGENT_MAX_STEPS", "12"))
        if not 1 <= max_steps <= 100:
            raise ValueError("BUG_AGENT_MAX_STEPS 必须在 1..100 之间")
        max_tool_chars = int(os.getenv("BUG_AGENT_MAX_TOOL_RESULT_CHARS", "40000"))
        if max_tool_chars < 1000:
            raise ValueError("BUG_AGENT_MAX_TOOL_RESULT_CHARS 不能小于 1000")
        return cls(
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            max_steps=max_steps,
            llm_timeout_seconds=float(os.getenv("BUG_AGENT_LLM_TIMEOUT_SECONDS", "120")),
            max_tool_result_chars=max_tool_chars,
        )


def default_export_root() -> Path:
    """Jira 和 Log MCP 共享的 Case 根目录。"""

    return Path(os.getenv("JIRA_EXPORT_ROOT", "exports")).expanduser().resolve()
