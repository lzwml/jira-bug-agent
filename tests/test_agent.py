from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bug_agent.agent_core import BugAnalysisAgent
from bug_agent.domain.models import CompletionTokenUsage
from bug_agent.infrastructure.config import AgentConfig
from bug_agent.infrastructure.provider import ProviderError, ProviderMessage


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
async def test_agent_accumulates_provider_token_usage_across_steps():
    provider = FakeProvider([
        ProviderMessage({
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "open_case", "arguments": "{}"},
            }],
        }, CompletionTokenUsage(
            prompt_tokens=100, completion_tokens=10, total_tokens=110,
            cached_prompt_tokens=40, reasoning_tokens=4,
        )),
        ProviderMessage({"content": "完成"}, CompletionTokenUsage(
            prompt_tokens=180, completion_tokens=20, total_tokens=200,
            cached_prompt_tokens=80, reasoning_tokens=6,
        )),
    ])

    result = await BugAnalysisAgent(CONFIG, provider).run(
        "分析", "system", FakeRouter(),
    )

    assert result.token_usage is not None
    assert result.token_usage.model_dump() == {
        "prompt_tokens": 280,
        "completion_tokens": 30,
        "total_tokens": 310,
        "cached_prompt_tokens": 120,
        "reasoning_tokens": 10,
        "model_calls": 2,
        "reported_calls": 2,
        "complete": True,
    }


@pytest.mark.anyio
async def test_agent_marks_partially_reported_usage_incomplete():
    provider = FakeProvider([
        ProviderMessage({
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "open_case", "arguments": "{}"},
            }],
        }, CompletionTokenUsage(
            prompt_tokens=10, completion_tokens=2, total_tokens=12,
        )),
        {"content": "完成"},
    ])

    result = await BugAnalysisAgent(CONFIG, provider).run(
        "分析", "system", FakeRouter(),
    )

    assert result.token_usage is not None
    assert result.token_usage.model_calls == 2
    assert result.token_usage.reported_calls == 1
    assert result.token_usage.complete is False


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


@pytest.mark.anyio
async def test_goal_mode_still_respects_hard_tool_call_budget():
    tool_message = {
        "tool_calls": [{
            "id": "repeat",
            "type": "function",
            "function": {"name": "open_case", "arguments": "{}"},
        }],
    }
    provider = FakeProvider([tool_message, tool_message, tool_message])
    config = replace(CONFIG, max_tool_calls=2)

    result = await BugAnalysisAgent(config, provider).run(
        "分析", "system", FakeRouter(), goal_mode=True,
    )

    assert result.status == "max_steps"
    assert result.error == "MAX_TOOL_CALLS_EXCEEDED"
    assert len(result.tool_events) == 2


@pytest.mark.anyio
async def test_run_with_messages_continues_from_existing_history():
    """run_with_messages 应从已有消息历史继续执行，并返回更新后的消息列表。"""
    provider = FakeProvider([
        {
            "content": "",
            "tool_calls": [{
                "id": "call-2",
                "type": "function",
                "function": {"name": "open_case", "arguments": '{"case_path":"C:/case2"}'},
            }],
        },
        {"content": "第二轮分析完成。"},
    ])
    router = FakeRouter()
    original_count = 4
    messages = [
        {"role": "system", "content": "你是 Bug 分析助手。"},
        {"role": "user", "content": "分析 APP-42"},
        {"role": "assistant", "content": "第一轮分析：已确认日志中存在 SurfaceFlinger 错误。"},
        {"role": "user", "content": "能详细看看 SurfaceFlinger 吗？"},
    ]

    result, updated_messages = await BugAnalysisAgent(CONFIG, provider).run_with_messages(
        messages, router,
    )

    assert result.status == "completed"
    assert result.steps == 2
    assert "第二轮分析完成" in result.final_answer
    # 消息列表在原有基础上追加了 tool + assistant 消息
    assert len(updated_messages) > original_count
    assert any("SurfaceFlinger" in str(m.get("content", "")) for m in updated_messages if m["role"] == "user")


@pytest.mark.anyio
async def test_run_with_messages_respects_max_steps_override():
    """run_with_messages 的 max_steps_override 应覆盖配置中的步数预算。"""
    tool_message = {
        "tool_calls": [{
            "id": "repeat",
            "type": "function",
            "function": {"name": "open_case", "arguments": "{}"},
        }],
    }
    provider = FakeProvider([tool_message, tool_message, tool_message, tool_message])
    messages = [
        {"role": "system", "content": "你是助手。"},
        {"role": "user", "content": "分析"},
    ]

    result, _ = await BugAnalysisAgent(CONFIG, provider).run_with_messages(
        messages, FakeRouter(), max_steps_override=2,
    )

    assert result.status == "max_steps"
    assert result.steps == 2  # 只用了 2 步，而不是 config 的 3 步


@pytest.mark.anyio
async def test_run_with_messages_accumulates_tool_events():
    """run_with_messages 应正确累积工具调用事件。"""
    provider = FakeProvider([
        {
            "tool_calls": [{
                "id": "ev-1",
                "type": "function",
                "function": {"name": "open_case", "arguments": '{"case_path":"C:/a"}'},
            }],
        },
        {"content": "完成。"},
    ])
    messages = [
        {"role": "system", "content": "助手"},
        {"role": "user", "content": "分析"},
    ]

    result, _ = await BugAnalysisAgent(CONFIG, provider).run_with_messages(messages, FakeRouter())

    assert result.status == "completed"
    assert len(result.tool_events) == 1
    assert result.tool_events[0].tool_name == "open_case"
    assert result.tool_events[0].success is True


@pytest.mark.anyio
async def test_run_with_messages_with_starting_step():
    """run_with_messages 的 starting_step 参数应正确偏移步号。"""
    provider = FakeProvider([
        {
            "tool_calls": [{
                "id": "ev-x",
                "type": "function",
                "function": {"name": "open_case", "arguments": "{}"},
            }],
        },
        {"content": "完成。"},
    ])
    messages = [
        {"role": "system", "content": "助手"},
        {"role": "user", "content": "分析"},
    ]

    result, _ = await BugAnalysisAgent(CONFIG, provider).run_with_messages(
        messages, FakeRouter(), starting_step=5,
    )

    assert result.status == "completed"
    assert result.steps == 2  # 相对步数，从 1 开始计数
    assert result.tool_events[0].step == 6  # 绝对步号 = starting_step + 1
