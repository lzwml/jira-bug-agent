"""HTTP API 自身的部署配置。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class ApiConfig:
    database_path: Path
    concurrency: int = 2
    allowed_local_roots: tuple[Path, ...] = ()
    api_key: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_environment(cls) -> "ApiConfig":
        database_path = Path(
            os.getenv("BUG_AGENT_API_DB_PATH", ".bug-agent/api/tasks.sqlite3")
        ).expanduser().resolve()
        concurrency = int(os.getenv("BUG_AGENT_API_CONCURRENCY", "2"))
        if not 1 <= concurrency <= 32:
            raise ValueError("BUG_AGENT_API_CONCURRENCY 必须在 1..32 之间")
        port = int(os.getenv("BUG_AGENT_API_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("BUG_AGENT_API_PORT 必须在 1..65535 之间")
        raw_roots = os.getenv("BUG_AGENT_API_ALLOWED_LOCAL_ROOTS", "")
        roots = tuple(
            Path(item.strip()).expanduser().resolve()
            for item in raw_roots.split(os.pathsep)
            if item.strip()
        )
        return cls(
            database_path=database_path,
            concurrency=concurrency,
            allowed_local_roots=roots,
            api_key=os.getenv("BUG_AGENT_API_KEY") or None,
            host=os.getenv("BUG_AGENT_API_HOST", "127.0.0.1"),
            port=port,
        )
