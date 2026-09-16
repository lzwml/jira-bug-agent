from __future__ import annotations

import httpx
import pytest

from bug_agent.infrastructure.config import AgentConfig
from bug_agent.infrastructure.provider import OpenAICompatibleProvider, ProviderError


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
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "done"}}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 80},
                "completion_tokens_details": {"reasoning_tokens": 12},
            },
        })

    provider = OpenAICompatibleProvider(config(), httpx.MockTransport(handler))
    try:
        message = await provider.complete([{"role": "user", "content": "x"}], [])
    finally:
        await provider.close()
    assert message["content"] == "done"
    assert message.token_usage.model_dump() == {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "cached_prompt_tokens": 80,
        "reasoning_tokens": 12,
    }
    assert provider.token_usage is not None
    assert provider.token_usage.total_tokens == 150


@pytest.mark.anyio
async def test_provider_normalizes_input_output_usage_and_computes_missing_total():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": "done"}}],
        "usage": {"input_tokens": 7, "output_tokens": 5},
    }))
    provider = OpenAICompatibleProvider(config(), transport)
    try:
        message = await provider.complete([], [])
    finally:
        await provider.close()

    assert message.token_usage.prompt_tokens == 7
    assert message.token_usage.completion_tokens == 5
    assert message.token_usage.total_tokens == 12


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
