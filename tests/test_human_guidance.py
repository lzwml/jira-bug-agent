from __future__ import annotations

import json

import pytest

from bug_agent.agent import BugAnalysisAgent
from bug_agent.config import AgentConfig
from bug_agent.conversation import ConversationSession
from bug_agent.human_guidance import HumanGuidanceToolRouter


CONFIG = AgentConfig(
    llm_base_url="https://llm.test/v1",
    llm_api_key="key",
    llm_model="model",
    max_steps=6,
)


class Router:
    def __init__(self):
        self.calls = []

    def openai_tools(self):
        return [{
            "type": "function",
            "function": {
                "name": "search_evidence", "description": "search",
                "parameters": {"type": "object"},
            },
        }]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"success": True, "data": {"matches": []}})


class Provider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages_seen = []

    async def complete(self, messages, tools):
        self.messages_seen.append(list(messages))
        return self.responses.pop(0)


def _calls(*names):
    return {"tool_calls": [{
        "id": f"call-{index}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    } for index, name in enumerate(names)]}


def _checkpoint_call():
    return {"tool_calls": [{
        "id": "ask-human",
        "type": "function",
        "function": {
            "name": "request_human_guidance",
            "arguments": json.dumps({
                "category": "incident_scope",
                "question": "故障发生在哪一次复现？",
                "blocking_reason": "两个 boot round 都包含相同进程异常",
                "requested_input": "复现的大致时间或 boot round",
                "options": ["第一次", "第二次"],
            }, ensure_ascii=False),
        },
    }]}


@pytest.mark.anyio
async def test_human_checkpoint_pauses_only_after_real_tool_investigation():
    provider = Provider([_calls("search_evidence", "search_evidence"), _checkpoint_call()])
    router = HumanGuidanceToolRouter(Router())

    result = await BugAnalysisAgent(CONFIG, provider).run("analyze", "system", router)

    assert result.status == "waiting_for_human"
    assert result.human_checkpoint is not None
    assert result.human_checkpoint.category == "incident_scope"
    assert result.human_checkpoint.tool_attempts_before == 2
    assert result.tool_events[-1].tool_name == "request_human_guidance"


@pytest.mark.anyio
async def test_premature_human_request_is_rejected_and_agent_must_continue():
    provider = Provider([_checkpoint_call(), {"content": "先继续自动调查。"}])
    router = HumanGuidanceToolRouter(Router())

    result = await BugAnalysisAgent(CONFIG, provider).run("analyze", "system", router)

    assert result.status == "completed"
    assert result.human_checkpoint is None
    assert result.tool_events[0].success is False
    assert "HUMAN_GUIDANCE_PREMATURE" in result.tool_events[0].result


@pytest.mark.anyio
async def test_skill_activation_does_not_count_as_evidence_investigation():
    delegate = Router()
    router = HumanGuidanceToolRouter(delegate)
    await router.call("activate_skill", {"name": "android-anr-ui-freeze"})
    response = await router.call("request_human_guidance", {
        "category": "other",
        "question": "请分析？",
        "blocking_reason": "尚未调查",
        "requested_input": "答案",
    })
    assert json.loads(response)["error_code"] == "HUMAN_GUIDANCE_PREMATURE"


@pytest.mark.anyio
async def test_human_request_cannot_share_a_turn_with_investigation_tools():
    provider = Provider([
        _calls("search_evidence", "search_evidence"),
        {"tool_calls": [*_calls("search_evidence")["tool_calls"], *_checkpoint_call()["tool_calls"]]},
        {"content": "继续自动调查"},
    ])
    router = HumanGuidanceToolRouter(Router())
    result = await BugAnalysisAgent(CONFIG, provider).run("analyze", "system", router)
    assert result.status == "completed"
    assert any("HUMAN_GUIDANCE_MUST_BE_SOLE_CALL" in event.result for event in result.tool_events)


@pytest.mark.anyio
async def test_human_response_resumes_same_conversation_and_is_attributed():
    provider = Provider([
        _calls("search_evidence", "search_evidence"),
        _checkpoint_call(),
        _calls("search_evidence"),
        {"content": "已根据人工给出的复现时间找到对应证据。"},
    ])
    router = HumanGuidanceToolRouter(Router())
    session = ConversationSession(
        agent=BugAnalysisAgent(CONFIG, provider),
        system_prompt="system",
        router=router,
    )

    first = await session.send("分析问题")
    assert first.result.status == "waiting_for_human"
    second = await session.send("第二次复现，大约 10:42")

    assert second.result.status == "completed"
    assert second.human_intervention is not None
    assert second.human_intervention.checkpoint.checkpoint_id == first.result.human_checkpoint.checkpoint_id
    assert second.human_intervention.subsequent_tools == ["search_evidence"]
    assert second.human_intervention.attribution.target == "case_context"
    assert session.pending_checkpoint is None
    system_messages = [
        item["content"] for item in provider.messages_seen[2] if item["role"] == "system"
    ]
    assert any("调查线索而不是已确认事实" in item for item in system_messages)
