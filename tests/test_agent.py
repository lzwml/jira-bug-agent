from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bug_agent.agent import BugAnalysisAgent
from bug_agent.config import AgentConfig
from bug_agent.provider import ProviderError


CONFIG = AgentConfig(
    llm_base_url="https://llm.test/v1",
    llm_api_key="test-key",
    llm_model="test-model",
    max_steps=3,
)


class FakeProvider:
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.messages_seen: list[list[dict]] = []

    async def complete(self, messages, tools):
        self.messages_seen.append(list(messages))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeRouter:
    def __init__(self):
        self.calls = []

    def openai_tools(self):
        return [{
            "type": "function",
            "function": {"name": "open_case", "description": "open", "parameters": {"type": "object"}},
        }]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"success": True, "data": {"case_id": "case_1"}})


@pytest.mark.anyio
async def test_agent_executes_tool_then_returns_final_answer():
    provider = FakeProvider([
        {
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "open_case", "arguments": '{"case_path":"C:/case"}'},
            }],
        },
        {"content": "已确认：日志中存在启动失败证据。"},
    ])
    router = FakeRouter()
    result = await BugAnalysisAgent(CONFIG, provider).run("分析", "system", router)

    assert result.status == "completed"
    assert result.steps == 2
    assert router.calls == [("open_case", {"case_path": "C:/case"})]
    assert result.tool_events[0].success is True
    assert provider.messages_seen[1][-1]["role"] == "tool"


@pytest.mark.anyio
async def test_invalid_tool_json_becomes_observation_instead_of_crash():
    provider = FakeProvider([
        {
            "tool_calls": [{
                "id": "bad-call",
                "type": "function",
                "function": {"name": "open_case", "arguments": "not-json"},
            }],
        },
        {"content": "参数无效，无法继续。"},
    ])
    router = FakeRouter()
    result = await BugAnalysisAgent(CONFIG, provider).run("分析", "system", router)

    assert result.status == "completed"
    assert result.tool_events[0].success is False
    assert "INVALID_TOOL_ARGUMENTS" in result.tool_events[0].result
    assert router.calls == []


@pytest.mark.anyio
async def test_agent_stops_at_step_budget():
    tool_message = {
        "tool_calls": [{
            "id": "repeat",
            "type": "function",
            "function": {"name": "open_case", "arguments": "{}"},
        }],
    }
    provider = FakeProvider([tool_message, tool_message, tool_message])
    result = await BugAnalysisAgent(CONFIG, provider).run("分析", "system", FakeRouter())
    assert result.status == "max_steps"
    assert result.steps == 3


@pytest.mark.anyio
async def test_agent_retries_transient_provider_failure_without_using_step_budget():
    provider = FakeProvider([
        ProviderError("模型服务暂时不可用: HTTP 429", True),
        {"content": "重试后完成"},
    ])
    config = replace(CONFIG, llm_retry_base_seconds=0)

    result = await BugAnalysisAgent(config, provider).run("分析", "system", FakeRouter())

    assert result.status == "completed"
    assert result.steps == 1
    assert len(provider.messages_seen) == 2


@pytest.mark.anyio
async def test_agent_does_not_retry_non_retryable_provider_failure():
    provider = FakeProvider([ProviderError("认证失败", False)])
    config = replace(CONFIG, llm_retry_base_seconds=0)

    result = await BugAnalysisAgent(config, provider).run("分析", "system", FakeRouter())

    assert result.status == "failed"
    assert result.steps == 0
    assert len(provider.messages_seen) == 1


@pytest.mark.anyio
async def test_goal_mode_runs_beyond_max_steps():
    """Goal 模式下忽略 max_steps 限制，模型可以无限调用工具直到给出答案。"""
    tool_message = {
        "tool_calls": [{
            "id": "repeat",
            "type": "function",
            "function": {"name": "open_case", "arguments": "{}"},
        }],
    }
    # 5 次工具调用后给出最终答案
    provider = FakeProvider([
        tool_message,
        tool_message,
        tool_message,
        tool_message,
        {"content": "调查完成，已确认根因。"},
    ])
    config = replace(CONFIG, max_steps=3)  # 只有 3 步预算
    result = await BugAnalysisAgent(config, provider).run(
        "分析", "system", FakeRouter(), goal_mode=True,
    )

    assert result.status == "completed"
    assert result.steps == 5  # 远超 max_steps=3
    assert "调查完成" in result.final_answer


@pytest.mark.anyio
async def test_non_goal_mode_still_stops_at_max_steps():
    """非 goal 模式下 max_steps 限制仍然生效。"""
    tool_message = {
        "tool_calls": [{
            "id": "repeat",
            "type": "function",
            "function": {"name": "open_case", "arguments": "{}"},
        }],
    }
    provider = FakeProvider([tool_message, tool_message, tool_message, tool_message])
    config = replace(CONFIG, max_steps=3)
    result = await BugAnalysisAgent(config, provider).run("分析", "system", FakeRouter())

    assert result.status == "max_steps"
    assert result.steps == 3
