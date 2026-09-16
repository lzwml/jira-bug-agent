"""Agent Core 对外依赖的最小端口契约。"""

from __future__ import annotations

from typing import Any, Protocol


class AgentRuntimeConfig(Protocol):
    max_steps: int
    max_run_seconds: float
    max_tool_calls: int
    max_tool_result_chars: int
    llm_max_retries: int
    llm_retry_base_seconds: float


class ModelProvider(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]: ...


class ToolRouter(Protocol):
    def openai_tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


class ProviderFailure(RuntimeError):
    """Provider 适配器向核心循环暴露的可重试失败契约。"""

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable
