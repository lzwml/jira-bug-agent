"""用于外部 Workflow 的异步 HTTP API。"""

from .app import create_app

__all__ = ["create_app"]
