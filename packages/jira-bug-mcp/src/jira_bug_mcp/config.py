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


_LOCAL_ENV_PREFIXES = ("JIRA_", "BUG_AGENT_", "LOG_ANALYZER_")


def load_local_env(path: Path | None = None) -> Path | None:
    """加载项目本地 `.env`，但不覆盖进程已有环境变量。

    这不是 Shell 解释器：不执行命令、不展开变量，且只接受 Agent
    明确使用的三类配置前缀。生产环境仍应优先使用密钥管理系统。
    """

    env_path = (path or Path.cwd() / ".env").resolve()
    if not env_path.is_file():
        return None
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{env_path} 第 {line_number} 行不是 KEY=VALUE")
        name, value = (part.strip() for part in line.split("=", 1))
        if not name.startswith(_LOCAL_ENV_PREFIXES):
            raise ValueError(f"{env_path} 第 {line_number} 包含不允许的配置名: {name}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        # 空值表示“未配置”，例如初始生成的 JIRA_TOKEN=。
        if value:
            os.environ.setdefault(name, value)
    return env_path


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
    # 内网 Jira 默认不继承 HTTP_PROXY/HTTPS_PROXY，避免已被设为
    # DIRECT 的流量又被 httpx 送回全局代理。
    trust_env: bool = False
    timeout_seconds: float = 30.0
    max_attachment_bytes: int = 50 * 1024 * 1024
    max_export_bytes: int = 200 * 1024 * 1024
    # 企业 Jira 的复现步骤、车型、软件版本常是自定义字段。
    # 必须通过白名单显式选择，不默认抓取所有 customfield_* 数据。
    extra_fields: tuple[str, ...] = ()

    @property
    def api_version(self) -> str:
        return "3" if self.deployment == "cloud" else "2"

    @classmethod
    def from_environment(cls) -> "JiraConfig":
        load_local_env()
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
            trust_env=_env_bool("JIRA_TRUST_ENV", False),
            timeout_seconds=float(os.getenv("JIRA_TIMEOUT_SECONDS", "30")),
            max_attachment_bytes=int(os.getenv("JIRA_MAX_ATTACHMENT_BYTES", str(50 * 1024 * 1024))),
            max_export_bytes=int(os.getenv("JIRA_MAX_EXPORT_BYTES", str(200 * 1024 * 1024))),
            extra_fields=tuple(
                field.strip()
                for field in os.getenv("JIRA_EXTRA_FIELDS", "").split(",")
                if field.strip()
            ),
        )
