"""ConversationSession 连续问答测试。"""

from __future__ import annotations

import json

import pytest

from bug_agent.agent import BugAnalysisAgent
from bug_agent.config import AgentConfig
from bug_agent.conversation import ConversationSession
from bug_agent.prompts import (
    CHAT_REPORT_SYNTHESIS_PROMPT,
    CONVERSATION_FOLLOWUP_SYSTEM_PROMPT,
)


CONFIG = AgentConfig(
    llm_base_url="https://llm.test/v1",
    llm_api_key="test-key",
    llm_model="test-model",
    max_steps=3,
)


class FakeProvider:
    """模拟 Provider，按顺序返回预设响应。"""

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
    """模拟 ToolRouter，不实际调用工具。"""

    def __init__(self):
        self.calls = []

    def openai_tools(self):
        return [{
            "type": "function",
            "function": {
                "name": "search_evidence",
                "description": "搜索日志证据",
                "parameters": {"type": "object"},
            },
        }]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"success": True, "data": {"results": ["匹配行1", "匹配行2"]}})


def make_session(agent_responses, max_steps_per_turn=None):
    """创建测试用的 ConversationSession。"""
    provider = FakeProvider(agent_responses)
    agent = BugAnalysisAgent(CONFIG, provider)
    router = FakeRouter()
    session = ConversationSession(
        agent=agent,
        system_prompt="你是 Bug 分析助手。",
        router=router,
        max_steps_per_turn=max_steps_per_turn,
    )
    return session, provider, router


@pytest.mark.anyio
async def test_first_turn_executes_agent_and_returns_answer():
    """第一轮 send() 应执行 Agent 并返回回答。"""
    session, provider, router = make_session([
        {"content": "分析完成：根因是 SurfaceFlinger 超时。"},
    ])

    turn = await session.send("分析 APP-42")

    assert turn.result.status == "completed"
    assert "SurfaceFlinger" in turn.result.final_answer
    assert turn.user_message == "分析 APP-42"
    # 工具未被调用（因为第一轮直接返回了最终答案）
    assert len(provider.messages_seen) == 1
    # 消息历史被更新
    assert len(session._messages) > 2  # system + user + assistant


@pytest.mark.anyio
async def test_second_turn_injects_followup_prompt():
    """第二轮 send() 应在用户消息前追加追问系统提示。"""
    session, provider, router = make_session([
        {"content": "第一轮回答。"},
        {"content": "第二轮追问回答。"},
    ])

    await session.send("分析 APP-42")
    # 第一轮后 provider 已消费一条响应
    await session.send("能详细看看吗？")

    # 验证第二轮的消息中包含了追问提示
    second_messages = provider.messages_seen[1]
    system_contents = [m["content"] for m in second_messages if m["role"] == "system"]
    assert CONVERSATION_FOLLOWUP_SYSTEM_PROMPT in system_contents
    assert "必须调用合适工具" in CONVERSATION_FOLLOWUP_SYSTEM_PROMPT
    assert "不要未经验证就回答“你说得对”" in CONVERSATION_FOLLOWUP_SYSTEM_PROMPT
    assert "PID/TID" in CONVERSATION_FOLLOWUP_SYSTEM_PROMPT


def test_chat_synthesis_drops_retracted_guesses_and_preserves_identity_conflicts():
    assert "都不是天然真值" in CHAT_REPORT_SYNTHESIS_PROMPT
    assert "旧因果结论视为已失效" in CHAT_REPORT_SYNTHESIS_PROMPT
    assert "PID/TID" in CHAT_REPORT_SYNTHESIS_PROMPT
    assert "降低结论等级" in CHAT_REPORT_SYNTHESIS_PROMPT


def test_report_prompt_forbids_confirmed_wording_for_hypotheses():
    from bug_agent.prompts import REPORT_FORMAT_PROMPT

    assert "根因尚未确认" in REPORT_FORMAT_PROMPT
    assert "由…引发" in REPORT_FORMAT_PROMPT
    assert "把 hypotheses 写成已确认因果" in REPORT_FORMAT_PROMPT


@pytest.mark.anyio
async def test_tool_calls_are_recorded_in_turn():
    """每轮的工具调用应记录在 turn.result.tool_events 中。"""
    session, provider, router = make_session([
        {
            "tool_calls": [{
                "id": "tc-1",
                "type": "function",
                "function": {
                    "name": "search_evidence",
                    "arguments": '{"query": "SurfaceFlinger"}',
                },
            }],
        },
        {"content": "已找到证据。"},
    ])

    turn = await session.send("找 SurfaceFlinger 日志")

    assert turn.result.status == "completed"
    assert len(turn.result.tool_events) == 1
    assert turn.result.tool_events[0].tool_name == "search_evidence"
    assert turn.result.tool_events[0].success is True


@pytest.mark.anyio
async def test_session_stops_only_after_finalize():
    """会话只在 finalize() 后变为 inactive，send() 本身不限制轮次。"""
    session, provider, router = make_session(
        [{"content": "回答1"}, {"content": "回答2"}, {"content": "回答3"}],
    )

    assert session.is_active
    await session.send("第一问")
    assert session.is_active
    await session.send("第二问")
    assert session.is_active
    await session.send("第三问")
    assert session.is_active  # 没有 max_turns，不会自动关闭

    await session.finalize()
    assert not session.is_active

    with pytest.raises(RuntimeError, match="会话已结束"):
        await session.send("第四问")


@pytest.mark.anyio
async def test_session_reports_turn_count():
    """会话应正确报告已完成轮次。"""
    session, provider, router = make_session(
        [{"content": "回答1"}, {"content": "回答2"}],
    )

    assert session.turn_count == 0

    await session.send("第一问")
    assert session.turn_count == 1

    await session.send("第二问")
    assert session.turn_count == 2


@pytest.mark.anyio
async def test_finalize_returns_conversation_result():
    """finalize() 应返回正确的 ConversationResult。"""
    session, provider, router = make_session([
        {"content": "回答1"},
        {"content": "回答2"},
    ])

    await session.send("第一问")
    await session.send("第二问")
    result = await session.finalize()

    assert len(result.turns) == 2
    assert result.total_steps == 2  # 每轮 1 步
    assert result.final_answer == "回答2"
    assert not session.is_active


@pytest.mark.anyio
async def test_finalize_is_idempotent():
    """多次调用 finalize() 不应报错。"""
    session, provider, router = make_session([
        {"content": "回答1"},
    ])

    await session.send("第一问")
    await session.finalize()
    # 第二次调用不应报错
    result = await session.finalize()
    assert len(result.turns) == 1


@pytest.mark.anyio
async def test_tool_events_persist_across_turns():
    """每轮的工具调用事件应独立记录，且后续轮次可看到之前轮次的工具结果。"""
    session, provider, router = make_session([
        # 第一轮：一次工具调用 + 回答
        {
            "tool_calls": [{
                "id": "tc-1",
                "type": "function",
                "function": {
                    "name": "search_evidence",
                    "arguments": '{"query": "error"}',
                },
            }],
        },
        {"content": "第一轮：找到 5 条错误日志。"},
        # 第二轮：直接回答（不调工具）
        {"content": "第二轮：基于之前的证据，根因是 OOM。"},
    ])

    turn1 = await session.send("搜索错误日志")
    assert len(turn1.result.tool_events) == 1
    assert turn1.result.tool_events[0].tool_name == "search_evidence"

    turn2 = await session.send("根因是什么？")
    assert len(turn2.result.tool_events) == 0  # 第二轮没有调工具

    # 第二轮的消息历史中应包含第一轮的工具结果
    second_messages = provider.messages_seen[1]
    tool_messages = [m for m in second_messages if m["role"] == "tool"]
    assert len(tool_messages) >= 1  # 至少第一轮的工具结果还在


@pytest.mark.anyio
async def test_max_steps_per_turn_is_respected():
    """每轮的 max_steps_per_turn 应生效，独立于 config.max_steps。"""
    session, provider, router = make_session(
        [
            # 第一轮：3 次工具调用后超限
            {
                "tool_calls": [{
                    "id": "r1",
                    "type": "function",
                    "function": {"name": "search_evidence", "arguments": "{}"},
                }],
            },
            {
                "tool_calls": [{
                    "id": "r2",
                    "type": "function",
                    "function": {"name": "search_evidence", "arguments": "{}"},
                }],
            },
            {"content": "第二轮：正常回答。"},
        ],
        max_steps_per_turn=2,  # 每轮最多 2 步
    )

    turn1 = await session.send("第一问")
    # 第一轮：发送了 2 次工具调用 → 达到 max_steps_per_turn=2
    assert turn1.result.status == "max_steps"
    assert turn1.result.steps == 2

    # 第二轮仍可继续
    assert session.is_active
    turn2 = await session.send("第二问")
    assert turn2.result.status == "completed"
    assert turn2.result.steps == 1


@pytest.mark.anyio
async def test_on_tool_event_callback_is_called():
    """on_tool_event 回调应在每次工具调用完成后被调用。"""
    events = []

    provider = FakeProvider([
        {
            "tool_calls": [{
                "id": "cb-1",
                "type": "function",
                "function": {
                    "name": "search_evidence",
                    "arguments": '{"query": "test"}',
                },
            }],
        },
        {"content": "完成。"},
    ])
    agent = BugAnalysisAgent(CONFIG, provider)
    router = FakeRouter()
    session = ConversationSession(
        agent=agent,
        system_prompt="助手",
        router=router,
        on_tool_event=events.append,
    )

    await session.send("分析")

    assert len(events) == 1
    assert events[0].tool_name == "search_evidence"


@pytest.mark.anyio
async def test_messages_property_returns_copy():
    """messages 属性应返回消息历史的副本，修改不影响内部状态。"""
    session, provider, router = make_session([
        {"content": "回答"},
    ])

    msgs = session.messages
    assert len(msgs) == 1  # 只有 system prompt

    await session.send("问")

    # 之前获取的副本不应被更新
    assert len(msgs) == 1
    # 新的副本应包含更多消息
    assert len(session.messages) > 1


def test_tool_catalog_exposes_the_exact_model_facing_contract():
    session, _, _ = make_session([{"content": "回答"}])

    assert session.tool_catalog == [{
        "name": "search_evidence",
        "description": "搜索日志证据",
        "parameters": {"type": "object"},
    }]
