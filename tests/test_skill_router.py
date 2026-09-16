from __future__ import annotations

import json
from pathlib import Path

import pytest

from bug_agent.application.skill_router import ACTIVATE_SKILL_TOOL, SkillAwareToolRouter
from bug_agent.infrastructure.skills import SkillRegistry


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"


class DelegateRouter:
    def __init__(self):
        self.calls = []

    def openai_tools(self):
        return [{
            "type": "function",
            "function": {"name": "inspect_case", "parameters": {"type": "object"}},
        }]

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"success": True, "data": {"delegated": True}})


def make_router(*, initial=None, source="default", enabled=True):
    return SkillAwareToolRouter(
        DelegateRouter(),
        SkillRegistry(SKILLS_ROOT),
        initial or ["android-log-triage"],
        initial_source=source,
        auto_enabled=enabled,
    )


def test_router_exposes_trusted_skill_catalog_and_activation_tool():
    router = make_router()

    tool_names = [item["function"]["name"] for item in router.openai_tools()]
    activation_tool = router.openai_tools()[-1]["function"]

    assert tool_names == ["inspect_case", ACTIVATE_SKILL_TOOL]
    assert "android-black-screen" in activation_tool["parameters"]["properties"]["name"]["enum"]
    assert "android-black-screen [symptom]" in router.catalog_prompt()
    assert "inspect_case 返回 aee_db" in router.catalog_prompt()
    assert "android-log-triage [base]（已激活）" in router.catalog_prompt()


@pytest.mark.anyio
async def test_agent_can_activate_symptom_skill_and_receive_instructions():
    router = make_router()

    raw = await router.call(ACTIVATE_SKILL_TOOL, {
        "name": "android-black-screen",
        "reason": "Issue 报告启动后黑屏，且日志存在 SurfaceFlinger 事件",
    })
    payload = json.loads(raw)

    assert payload["success"] is True
    assert "SurfaceFlinger" in payload["data"]["instructions"]
    assert payload["data"]["symptom_family"] == "display"
    assert payload["data"]["required_coverage_contract"] == "display-v1"
    assert payload["data"]["coverage_requirements"]
    assert router.activated_names == ["android-log-triage", "android-black-screen"]
    assert router.activations[-1].source == "agent"
    assert "启动后黑屏" in router.activations[-1].reason


@pytest.mark.anyio
async def test_router_rejects_two_primary_symptom_skills():
    router = make_router(initial=["android-black-screen"], source="explicit")

    raw = await router.call(ACTIVATE_SKILL_TOOL, {
        "name": "android-anr-ui-freeze",
        "reason": "同时看到 ANR",
    })
    payload = json.loads(raw)

    assert payload["success"] is False
    assert payload["error_code"] == "PRIMARY_SKILL_CONFLICT"
    assert router.activated_names == ["android-black-screen"]


@pytest.mark.anyio
async def test_router_allows_one_explicit_secondary_cascade_skill():
    router = make_router(initial=["android-native-crash"], source="explicit")

    raw = await router.call(ACTIVATE_SKILL_TOOL, {
        "name": "android-black-screen",
        "role": "secondary",
        "reason": "已确认 native service death 后出现黑屏，需要验证下游显示影响",
    })
    payload = json.loads(raw)

    assert payload["success"] is True
    assert payload["data"]["role"] == "secondary"
    assert router.activations[-1].role == "secondary"
    assert router.activated_names == ["android-native-crash", "android-black-screen"]


@pytest.mark.anyio
async def test_router_can_stack_platform_skill_with_symptom_skill():
    router = make_router(initial=["android-black-screen"], source="explicit")

    raw = await router.call(ACTIVATE_SKILL_TOOL, {
        "name": "mtk-ivi-log-analysis",
        "reason": "Case 含 MTK IVI、Linux VM 和 SCP 日志",
    })

    assert json.loads(raw)["success"] is True
    assert router.activated_names == ["android-black-screen", "mtk-ivi-log-analysis"]


@pytest.mark.anyio
async def test_router_normalizes_symptom_role_for_platform_skill():
    router = make_router(initial=["android-black-screen"], source="explicit")

    raw = await router.call(ACTIVATE_SKILL_TOOL, {
        "name": "mtk-ivi-log-analysis",
        "role": "primary",
        "reason": "Case 含 MTK IVI、Linux VM 和 SCP 日志",
    })
    payload = json.loads(raw)

    assert payload["success"] is True
    assert payload["data"]["role"] == "supporting"
    assert payload["data"]["role_adjusted_from"] == "primary"
    assert router.activations[-1].role == "supporting"


@pytest.mark.anyio
async def test_router_can_activate_aee_supplemental_skill_from_dbg_evidence():
    router = make_router(initial=["android-native-crash", "mtk-ivi-log-analysis"], source="explicit")

    raw = await router.call(ACTIVATE_SKILL_TOOL, {
        "name": "aee-db-extract",
        "reason": "inspect_case 返回 kind=aee_db 的 db.00.NE.dbg",
    })
    payload = json.loads(raw)

    assert payload["success"] is True
    assert "extract_aee_db" in payload["data"]["instructions"]
    assert router.activated_names == [
        "android-native-crash", "mtk-ivi-log-analysis", "aee-db-extract",
    ]


def test_auto_disabled_router_does_not_advertise_activation_tool():
    router = make_router(enabled=False)

    assert [item["function"]["name"] for item in router.openai_tools()] == ["inspect_case"]
    assert router.catalog_prompt() == ""
