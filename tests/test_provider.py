from __future__ import annotations

import httpx
import pytest

from bug_agent.config import AgentConfig
from bug_agent.provider import OpenAICompatibleProvider, ProviderError


def config():
    return AgentConfig(
        llm_base_url="https://llm.test/v1",
        llm_api_key="secret",
        llm_model="model",
    )


@pytest.mark.anyio
async def test_provider_returns_assistant_message_and_sends_bearer_auth():
    async def handler(request: httpx.Request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    provider = OpenAICompatibleProvider(config(), httpx.MockTransport(handler))
    try:
        message = await provider.complete([{"role": "user", "content": "x"}], [])
    finally:
        await provider.close()
    assert message["content"] == "done"


@pytest.mark.anyio
async def test_provider_does_not_leak_auth_response_body():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"detail": "secret-token-in-body"})
    )
    provider = OpenAICompatibleProvider(config(), transport)
    try:
        with pytest.raises(ProviderError) as raised:
            await provider.complete([], [])
    finally:
        await provider.close()
    assert "secret-token-in-body" not in str(raised.value)
    assert raised.value.retryable is False

