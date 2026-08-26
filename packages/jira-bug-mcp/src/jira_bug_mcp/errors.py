"""统一的 ToolResult 与 Jira HTTP 错误。"""

from __future__ import annotations

from .domain import ToolResult


class JiraApiError(RuntimeError):
    """客户端层的可预期失败；不会携带请求头或认证信息。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def success(data: dict | None = None) -> ToolResult:
    return ToolResult(success=True, data=data or {})


def failure(code: str, message: str, retryable: bool = False) -> ToolResult:
    return ToolResult(
        success=False,
        error_code=code,
        error_message=message,
        retryable=retryable,
    )

