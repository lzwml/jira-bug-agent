"""Jira MCP 的环境配置。

认证信息只从环境变量读取，不进入 Tool 参数，也不会出现在 ToolResult 中。
这既减少模型接触密钥的机会，也让同一套 MCP 能迁移到不同 Agent Runtime。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class JiraConfig:
    base_url: str
    deployment: Literal["cloud", "datacenter"] = "cloud"
    auth_mode: Literal["basic", "bearer", "none"] = "basic"
    user: str | None = None
    token: str | None = None
    export_root: Path = Path("exports")
    verify_ssl: bool = True
    timeout_seconds: float = 30.0
    max_attachment_bytes: int = 50 * 1024 * 1024
    max_export_bytes: int = 200 * 1024 * 1024

    @property
    def api_version(self) -> str:
        return "3" if self.deployment == "cloud" else "2"

    @classmethod
    def from_environment(cls) -> "JiraConfig":
        base_url = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ValueError("缺少 JIRA_BASE_URL，例如 https://example.atlassian.net")
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("JIRA_BASE_URL 必须是有效的 http(s) 根地址")
        deployment = os.getenv("JIRA_DEPLOYMENT", "cloud").strip().lower()
        auth_mode = os.getenv("JIRA_AUTH_MODE", "basic").strip().lower()
        if deployment not in {"cloud", "datacenter"}:
            raise ValueError("JIRA_DEPLOYMENT 只能是 cloud 或 datacenter")
        if auth_mode not in {"basic", "bearer", "none"}:
            raise ValueError("JIRA_AUTH_MODE 只能是 basic、bearer 或 none")
        return cls(
            base_url=base_url,
            deployment=deployment,  # type: ignore[arg-type]
            auth_mode=auth_mode,  # type: ignore[arg-type]
            user=os.getenv("JIRA_USER"),
            token=os.getenv("JIRA_TOKEN"),
            export_root=Path(os.getenv("JIRA_EXPORT_ROOT", "exports")).expanduser(),
            verify_ssl=_env_bool("JIRA_VERIFY_SSL", True),
            timeout_seconds=float(os.getenv("JIRA_TIMEOUT_SECONDS", "30")),
            max_attachment_bytes=int(os.getenv("JIRA_MAX_ATTACHMENT_BYTES", str(50 * 1024 * 1024))),
            max_export_bytes=int(os.getenv("JIRA_MAX_EXPORT_BYTES", str(200 * 1024 * 1024))),
        )
