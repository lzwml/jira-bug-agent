"""最小 OpenAI-compatible Chat Completions Provider。"""

from __future__ import annotations

from typing import Any

import httpx

from .config import AgentConfig


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class OpenAICompatibleProvider:
    """兼容 DeepSeek、OpenAI-compatible Gateway 与多数自建模型服务。"""

    def __init__(self, config: AgentConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.client = httpx.AsyncClient(
            timeout=config.llm_timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {config.llm_api_key}"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        try:
            response = await self.client.post(
                f"{self.config.llm_base_url}/chat/completions",
                json={
                    "model": self.config.llm_model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                },
            )
        except httpx.TransportError as exc:
            raise ProviderError(f"模型服务连接失败: {type(exc).__name__}", True) from exc
        if response.status_code in {401, 403}:
            raise ProviderError("模型服务认证或权限失败", False)
        if response.status_code == 429 or response.status_code >= 500:
            raise ProviderError(f"模型服务暂时不可用: HTTP {response.status_code}", True)
        if not response.is_success:
            raise ProviderError(f"模型服务拒绝请求: HTTP {response.status_code}", False)
        try:
            payload = response.json()
            return payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("模型服务返回了无效响应", True) from exc

