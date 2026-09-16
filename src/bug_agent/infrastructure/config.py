"""Agent Runtime 配置，只从环境或 CLI 读取，不绑定某个模型供应商。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from jira_bug_mcp.config import load_local_env


def _find_project_root() -> Path:
    """从当前文件向上查找 pyproject.toml，确定项目根目录。"""
    current = Path(__file__).resolve().parent
    for ancestor in [current, *current.parents]:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    return Path.cwd()


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
    # Agent 级硬预算。即使 goal_mode=True，也不能绕过工具调用数和总运行时间。
    max_tool_calls: int = 64
    max_run_seconds: float = 900.0
    # 生产默认对 RCA 引用做工具轨迹反向校验；仅兼容旧测试/迁移时关闭。
    strict_evidence_validation: bool = True
    # 调查控制面：false 保持旧行为；shadow 记录状态和违规但不阻断；
    # enforce 预留给通过专项 Eval 后的路线。
    adaptive_investigation_mode: str = "shadow"
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
    # 本地源码映射（用于 code_local_mcp）
    locode_map: str = ""   # "android_b=g:/work/N60/b_android,yocto=g:/work/N60/yocto"
    locode_root: str = ""  # "g:/work/N60" → 自动发现子目录
    # 视频证据 MCP（可选，默认关闭）
    enable_video_analysis: bool = False

    @classmethod
    def from_environment(cls) -> "AgentConfig":
        # 优先从项目根目录加载 .env（pyproject.toml 所在目录），
        # 回退到 CWD，避免从上级目录运行时找不到配置文件。
        _env_path = _find_project_root() / ".env"
        load_local_env(_env_path if _env_path.is_file() else None)
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
        max_tool_calls = int(os.getenv("BUG_AGENT_MAX_TOOL_CALLS", "64"))
        if not 1 <= max_tool_calls <= 1000:
            raise ValueError("BUG_AGENT_MAX_TOOL_CALLS 必须在 1..1000 之间")
        max_run_seconds = float(os.getenv("BUG_AGENT_MAX_RUN_SECONDS", "900"))
        if not 1 <= max_run_seconds <= 86_400:
            raise ValueError("BUG_AGENT_MAX_RUN_SECONDS 必须在 1..86400 之间")
        max_retries = int(os.getenv("BUG_AGENT_LLM_MAX_RETRIES", "2"))
        if not 0 <= max_retries <= 10:
            raise ValueError("BUG_AGENT_LLM_MAX_RETRIES 必须在 0..10 之间")
        adaptive_mode = os.getenv("BUG_AGENT_ADAPTIVE_INVESTIGATION", "shadow").strip().lower()
        if adaptive_mode not in {"false", "shadow", "enforce"}:
            raise ValueError("BUG_AGENT_ADAPTIVE_INVESTIGATION 必须是 false、shadow 或 enforce")
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
        locode_map = os.getenv("LOCODE_MAP", "").strip()
        locode_root = os.getenv("LOCODE_ROOT", "").strip()
        if enable_code_search:
            if not opengrok_base_url:
                raise ValueError("OPENGROK_BASE_URL 未设置（启用代码搜索时必须配置）")
            if not locode_map and not locode_root:
                raise ValueError("LOCODE_MAP 或 LOCODE_ROOT 未设置（启用代码搜索时必须配置本地源码路径）")
        enable_video_analysis = os.getenv("VIDEO_ANALYZER_ENABLE", "").strip().lower() == "true"
        return cls(
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            max_steps=max_steps,
            llm_timeout_seconds=float(os.getenv("BUG_AGENT_LLM_TIMEOUT_SECONDS", "120")),
            llm_max_retries=max_retries,
            llm_retry_base_seconds=retry_base_seconds,
            max_tool_result_chars=max_tool_chars,
            max_tool_calls=max_tool_calls,
            max_run_seconds=max_run_seconds,
            strict_evidence_validation=(
                os.getenv("BUG_AGENT_STRICT_EVIDENCE_VALIDATION", "true").strip().lower()
                != "false"
            ),
            adaptive_investigation_mode=adaptive_mode,
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
            locode_map=locode_map,
            locode_root=locode_root,
            enable_video_analysis=enable_video_analysis,
        )


def default_export_root() -> Path:
    """Jira 和 Log MCP 共享的 Case 根目录。"""

    return Path(os.getenv("JIRA_EXPORT_ROOT", "exports")).expanduser().resolve()
