from __future__ import annotations

import json

import pytest

from bug_agent.investigation_state import (
    UPDATE_INVESTIGATION_STATE_TOOL,
    InvestigationStateToolRouter,
)


class DelegateRouter:
    def openai_tools(self):
        return [{"type": "function", "function": {"name": "inspect_case", "parameters": {}}}]

    async def call(self, name, arguments):
        return json.dumps({"success": True, "data": {
            "artifacts": [{"artifact_id": "a-1", "relative_path": "main.txt"}],
            "matches": [{"evidence_id": "ev-1", "artifact_id": "a-1", "relative_path": "main.txt",
                         "line_start": 10, "line_end": 10, "content": "fatal"}],
        }})


@pytest.mark.anyio
async def test_shadow_state_is_evidence_bound_and_persists_plan():
    router = InvestigationStateToolRouter(DelegateRouter(), mode="shadow")
    assert UPDATE_INVESTIGATION_STATE_TOOL in [x["function"]["name"] for x in router.openai_tools()]
    await router.call("inspect_case", {})

    profile = json.loads(await router.call(UPDATE_INVESTIGATION_STATE_TOOL, {
        "operation": "set_incident_profile",
        "value": {"symptom_family": "native_crash", "source_refs": ["jira:description"]},
    }))
    assert profile["success"] is True
    hypothesis = json.loads(await router.call(UPDATE_INVESTIGATION_STATE_TOOL, {
        "operation": "upsert_hypothesis",
        "value": {"hypothesis_id": "hyp-1", "claim": "native process crashed", "role": "primary",
                  "status": "open"},
    }))
    assert hypothesis["success"] is True
    hypothesis = json.loads(await router.call(UPDATE_INVESTIGATION_STATE_TOOL, {
        "operation": "upsert_hypothesis",
        "value": {"hypothesis_id": "hyp-1", "claim": "native process crashed", "role": "primary",
                  "status": "supported", "supporting_evidence_ids": ["ev-1"]},
    }))
    assert hypothesis["success"] is True
    coverage = json.loads(await router.call(UPDATE_INVESTIGATION_STATE_TOOL, {
        "operation": "upsert_coverage",
        "value": {"coverage_id": "android-main", "domain": "android", "stream": "main",
                  "status": "checked", "artifact_ids": ["a-1"], "evidence_ids": ["ev-1"]},
    }))
    assert coverage["success"] is True
    assert router.state.coverage[0].status == "checked"


@pytest.mark.anyio
async def test_state_rejects_invented_evidence_and_illegal_confirmation():
    router = InvestigationStateToolRouter(DelegateRouter(), mode="shadow")
    invalid = json.loads(await router.call(UPDATE_INVESTIGATION_STATE_TOOL, {
        "operation": "upsert_hypothesis",
        "value": {"hypothesis_id": "hyp-1", "claim": "invented", "status": "supported",
                  "supporting_evidence_ids": ["ev-made-up"]},
    }))
    assert invalid["error_code"] == "UNKNOWN_REFERENCE"
    await router.call("inspect_case", {})
    confirmed = json.loads(await router.call(UPDATE_INVESTIGATION_STATE_TOOL, {
        "operation": "upsert_hypothesis",
        "value": {"hypothesis_id": "hyp-2", "claim": "too early", "status": "confirmed",
                  "supporting_evidence_ids": ["ev-1"]},
    }))
    assert confirmed["error_code"] == "INVALID_HYPOTHESIS_TRANSITION"


def test_false_mode_does_not_expose_state_tool():
    router = InvestigationStateToolRouter(DelegateRouter(), mode="false")
    assert [x["function"]["name"] for x in router.openai_tools()] == ["inspect_case"]
