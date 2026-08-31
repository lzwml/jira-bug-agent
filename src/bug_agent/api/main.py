"""bug-agent-api 命令入口。"""

from __future__ import annotations

import uvicorn

from .config import ApiConfig


def main() -> None:
    settings = ApiConfig.from_environment()
    uvicorn.run(
        "bug_agent.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
