"""OpenGrok MCP 的环境配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OpenGrokConfig:
    base_url: str
    username: str = ""
    password: str = ""
    verify_ssl: bool = True
    timeout_seconds: float = 30.0
    default_project: str = ""
    default_max_results: int = 25

    @classmethod
    def from_environment(cls) -> "OpenGrokConfig":
        # 加载 .env 文件（与 jira-bug-mcp 共享 loader，但独立运行时仍可用）
        try:
            from jira_bug_mcp.config import load_local_env
            load_local_env()
        except ImportError:
            pass
        base_url = os.getenv("OPENGROK_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ValueError("缺少 OPENGROK_BASE_URL，例如 https://opengrok.example.com/source")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OPENGROK_BASE_URL 必须是有效的 http(s) 地址")

        return cls(
            base_url=base_url,
            username=os.getenv("OPENGROK_USERNAME", "").strip(),
            password=os.getenv("OPENGROK_PASSWORD", "").strip(),
            verify_ssl=_env_bool("OPENGROK_VERIFY_SSL", True),
            timeout_seconds=float(os.getenv("OPENGROK_TIMEOUT", "30")),
            default_project=os.getenv("OPENGROK_DEFAULT_PROJECT", "").strip(),
            default_max_results=int(os.getenv("OPENGROK_DEFAULT_MAX_RESULTS", "25")),
        )