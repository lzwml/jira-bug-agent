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
    max_concurrency: int = 8
    retry_attempts: int = 3
    cache_ttl_seconds: float = 60.0

    @classmethod
    def from_environment(cls) -> "OpenGrokConfig":
        base_url = os.getenv("OPENGROK_BASE_URL", "").strip().rstrip("/")
        if not base_url:
            raise ValueError("缺少 OPENGROK_BASE_URL，例如 https://opengrok.example.com/source")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("OPENGROK_BASE_URL 必须是有效的 http(s) 地址")

        try:
            timeout_seconds = float(os.getenv("OPENGROK_TIMEOUT", "30"))
            default_max_results = int(os.getenv("OPENGROK_DEFAULT_MAX_RESULTS", "25"))
            max_concurrency = int(os.getenv("OPENGROK_MAX_CONCURRENCY", "8"))
            retry_attempts = int(os.getenv("OPENGROK_RETRY_ATTEMPTS", "3"))
            cache_ttl_seconds = float(os.getenv("OPENGROK_CACHE_TTL", "60"))
        except ValueError as exc:
            raise ValueError("OpenGrok 数值配置必须是合法数字") from exc

        if timeout_seconds <= 0 or default_max_results < 1 or max_concurrency < 1:
            raise ValueError("OPENGROK_TIMEOUT、默认结果数和并发数必须大于 0")
        if not 0 <= retry_attempts <= 5 or cache_ttl_seconds < 0:
            raise ValueError("OPENGROK_RETRY_ATTEMPTS 必须为 0–5，缓存 TTL 不能小于 0")

        return cls(
            base_url=base_url,
            username=os.getenv("OPENGROK_USERNAME", "").strip(),
            password=os.getenv("OPENGROK_PASSWORD", "").strip(),
            verify_ssl=_env_bool("OPENGROK_VERIFY_SSL", True),
            timeout_seconds=timeout_seconds,
            default_project=os.getenv("OPENGROK_DEFAULT_PROJECT", "").strip(),
            default_max_results=default_max_results,
            max_concurrency=max_concurrency,
            retry_attempts=retry_attempts,
            cache_ttl_seconds=cache_ttl_seconds,
        )
