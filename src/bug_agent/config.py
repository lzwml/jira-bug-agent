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
    llm_max_retries: int = 2
    llm_retry_base_seconds: float = 1.0
    max_tool_result_chars: int = 40_000
    # Jira 原文读取硬上限；较小上下文直接注入，较大上下文才分块编译。
    jira_initial_context_max_chars: int = 1_000_000
    jira_direct_context_max_chars: int = 60_000
    jira_context_chunk_chars: int = 24_000
    jira_context_summary_max_chars: int = 30_000
    jira_compiler_max_chunks: int = 12
    jira_compiler_max_attempts: int = 16
    jira_compiler_timeout_seconds: float = 180.0
    # OpenGrok 代码搜索（可选，默认关闭）
    enable_code_search: bool = False
    opengrok_base_url: str = ""
    opengrok_username: str = ""
    opengrok_password: str = ""
    opengrok_verify_ssl: bool = True

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
        max_retries = int(os.getenv("BUG_AGENT_LLM_MAX_RETRIES", "2"))
        if not 0 <= max_retries <= 10:
            raise ValueError("BUG_AGENT_LLM_MAX_RETRIES 必须在 0..10 之间")
        retry_base_seconds = float(os.getenv("BUG_AGENT_LLM_RETRY_BASE_SECONDS", "1"))
        if not 0 <= retry_base_seconds <= 60:
            raise ValueError("BUG_AGENT_LLM_RETRY_BASE_SECONDS 必须在 0..60 之间")
        jira_context_max_chars = int(os.getenv("BUG_AGENT_JIRA_CONTEXT_MAX_CHARS", "1000000"))
        if not 4000 <= jira_context_max_chars <= 10_000_000:
            raise ValueError("BUG_AGENT_JIRA_CONTEXT_MAX_CHARS 必须在 4000..10000000 之间")
        jira_direct_chars = int(os.getenv("BUG_AGENT_JIRA_DIRECT_CONTEXT_MAX_CHARS", "60000"))
        if not 4000 <= jira_direct_chars <= jira_context_max_chars:
            raise ValueError(
                "BUG_AGENT_JIRA_DIRECT_CONTEXT_MAX_CHARS 必须在 4000..BUG_AGENT_JIRA_CONTEXT_MAX_CHARS 之间"
            )
        jira_chunk_chars = int(os.getenv("BUG_AGENT_JIRA_CONTEXT_CHUNK_CHARS", "24000"))
        if not 4000 <= jira_chunk_chars <= 100_000:
            raise ValueError("BUG_AGENT_JIRA_CONTEXT_CHUNK_CHARS 必须在 4000..100000 之间")
        jira_summary_chars = int(os.getenv("BUG_AGENT_JIRA_CONTEXT_SUMMARY_MAX_CHARS", "30000"))
        if not 4000 <= jira_summary_chars <= 200_000:
            raise ValueError("BUG_AGENT_JIRA_CONTEXT_SUMMARY_MAX_CHARS 必须在 4000..200000 之间")
        compiler_max_chunks = int(os.getenv("BUG_AGENT_JIRA_COMPILER_MAX_CHUNKS", "12"))
        if not 1 <= compiler_max_chunks <= 100:
            raise ValueError("BUG_AGENT_JIRA_COMPILER_MAX_CHUNKS 必须在 1..100 之间")
        compiler_max_attempts = int(os.getenv("BUG_AGENT_JIRA_COMPILER_MAX_ATTEMPTS", "16"))
        if not 1 <= compiler_max_attempts <= 200:
            raise ValueError("BUG_AGENT_JIRA_COMPILER_MAX_ATTEMPTS 必须在 1..200 之间")
        compiler_timeout = float(os.getenv("BUG_AGENT_JIRA_COMPILER_TIMEOUT_SECONDS", "180"))
        if not 1 <= compiler_timeout <= 3600:
            raise ValueError("BUG_AGENT_JIRA_COMPILER_TIMEOUT_SECONDS 必须在 1..3600 之间")
        # OpenGrok 代码搜索（可选）
        enable_code_search = os.getenv("OPENGROK_ENABLE_CODE_SEARCH", "").strip().lower() == "true"
        opengrok_base_url = os.getenv("OPENGROK_BASE_URL", "").strip().rstrip("/")
        opengrok_username = os.getenv("OPENGROK_USERNAME", "").strip()
        opengrok_password = os.getenv("OPENGROK_PASSWORD", "").strip()
        opengrok_verify_ssl = os.getenv("OPENGROK_VERIFY_SSL", "true").strip().lower() != "false"
        if enable_code_search:
            if not opengrok_base_url:
                raise ValueError("OPENGROK_BASE_URL 未设置（启用代码搜索时必须配置）")
        return cls(
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            max_steps=max_steps,
            llm_timeout_seconds=float(os.getenv("BUG_AGENT_LLM_TIMEOUT_SECONDS", "120")),
            llm_max_retries=max_retries,
            llm_retry_base_seconds=retry_base_seconds,
            max_tool_result_chars=max_tool_chars,
            jira_initial_context_max_chars=jira_context_max_chars,
            jira_direct_context_max_chars=jira_direct_chars,
            jira_context_chunk_chars=jira_chunk_chars,
            jira_context_summary_max_chars=jira_summary_chars,
            jira_compiler_max_chunks=compiler_max_chunks,
            jira_compiler_max_attempts=compiler_max_attempts,
            jira_compiler_timeout_seconds=compiler_timeout,
            enable_code_search=enable_code_search,
            opengrok_base_url=opengrok_base_url,
            opengrok_username=opengrok_username,
            opengrok_password=opengrok_password,
            opengrok_verify_ssl=opengrok_verify_ssl,
        )


def default_export_root() -> Path:
    """Jira 和 Log MCP 共享的 Case 根目录。"""

    return Path(os.getenv("JIRA_EXPORT_ROOT", "exports")).expanduser().resolve()
