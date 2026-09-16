from __future__ import annotations

import json

from bug_agent.domain.contracts import BugAnalysisResult, BugAnalysisTask, IncidentProfile, InvestigationState, RCAReport
from bug_agent.domain.models import AgentRunResult, TokenUsage, ToolEvent
from bug_agent.infrastructure.persistence.run_bundle import build_execution_context, verify_record
from bug_agent.infrastructure.persistence.runstore import build_run_record


class _Skill:
    name = "android-log-triage"
    instructions = "Inspect the incident before making a claim."


def _event() -> ToolEvent:
    return ToolEvent(
        step=1,
        tool_call_id="call-1",
        tool_name="open_case",
        arguments={"case_path": "C:/case"},
        success=True,
        result=json.dumps({
            "success": True,
            "data": {
                "case_id": "case-1",
                "artifacts": [{
                    "artifact_id": "artifact-1",
                    "relative_path": "logcat.txt",
                    "kind": "text_log",
                    "size_bytes": 123,
                    "modified_at": "2026-09-07T00:00:00Z",
                }],
                "matches": [{
                    "evidence_id": "ev-1",
                    "artifact_id": "artifact-1",
                    "relative_path": "logcat.txt",
                    "line_start": 42,
                    "line_end": 42,
                    "content": "FATAL EXCEPTION",
                }],
            },
        }),
    )


def _result() -> BugAnalysisResult:
    return BugAnalysisResult(
        task_id="run-1",
        status="completed",
        structured_output=True,
        steps=1,
        report=RCAReport(
            conclusion_status="hypothesis_only",
            summary="Observed a crash.",
            observed_symptom="Application exits during startup.",
            hypotheses=[],
        ),
    )


def test_run_bundle_v2_captures_provenance_inputs_evidence_and_claims(tmp_path):
    event = _event()
    run = AgentRunResult(
        status="completed", task="analyze", final_answer="{}", steps=1,
        tool_events=[event],
    )
    provenance = build_execution_context(
        model="test-model",
        system_prompt="system prompt",
        instruction="analyze case",
        tool_schema=[{"type": "function", "function": {"name": "open_case"}}],
        skill_documents=[_Skill()],
        max_steps=12,
        max_tool_calls=64,
        max_run_seconds=900,
        max_tool_result_chars=40_000,
    )
    record = build_run_record(
        BugAnalysisTask(task_id="run-1", source="local", case_path=str(tmp_path)),
        run,
        _result(),
        provenance=provenance,
    )

    assert record["schema_version"] == 2
    assert record["provenance"]["model"] == "test-model"
    assert record["provenance"]["prompts"]["ready"] is True
    assert record["provenance"]["skills"][0]["name"] == "android-log-triage"
    assert record["budget"]["actual"] == {
        "steps": 1,
        "tool_calls": 1,
        "total_tool_result_chars": len(event.result),
        "max_single_tool_result_chars": len(event.result),
        "duration_ms": None,
        "token_usage": None,
        "termination": "completed",
        "budget_error": None,
    }
    assert record["derived"]["evidence_registry"][0]["evidence_id"] == "ev-1"
    fingerprint = record["derived"]["input_fingerprint"]
    assert fingerprint["case_ids"] == ["case-1"]
    assert fingerprint["artifact_count"] == 1
    assert fingerprint["complete_content_fingerprint"] is False
    assert record["derived"]["claim_snapshot"]["claims"][0]["kind"] == "observed_symptom"
    assert record["derived"]["agent_metrics"]["analysis_tool_calls"] == 1
    assert record["derived"]["agent_metrics"]["human_checkpoint_count"] == 0
    assert record["derived"]["agent_metrics"]["autonomous_completion"] is True
    assert record["investigation_state"] is None
    assert verify_record(record)


def test_run_bundle_persists_investigation_state(tmp_path):
    record = build_run_record(
        BugAnalysisTask(task_id="run-state", source="local", case_path=str(tmp_path)),
        None, _result(),
        investigation_state=InvestigationState(
            mode="shadow", incident_profile=IncidentProfile(symptom_family="audio"),
        ),
    )
    assert record["investigation_state"]["mode"] == "shadow"
    assert record["investigation_state"]["incident_profile"]["symptom_family"] == "audio"
    assert verify_record(record)


def test_run_bundle_integrity_detects_tampering(tmp_path):
    record = build_run_record(
        BugAnalysisTask(task_id="run-1", source="local", case_path=str(tmp_path)),
        None,
        _result(),
    )
    record["result"]["status"] = "failed"
    assert verify_record(record) is False


def test_run_bundle_persists_agent_token_usage(tmp_path):
    run = AgentRunResult(
        status="completed", task="analyze", final_answer="{}", steps=1,
        token_usage=TokenUsage(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            cached_prompt_tokens=50,
            reasoning_tokens=8,
            model_calls=1,
            reported_calls=1,
            complete=True,
        ),
    )
    record = build_run_record(
        BugAnalysisTask(task_id="run-usage", source="local", case_path=str(tmp_path)),
        run,
        _result(),
    )

    assert record["budget"]["actual"]["token_usage"] == run.token_usage.model_dump()
    assert verify_record(record)


def test_execution_context_never_persists_provider_secrets():
    context = build_execution_context(
        model="test-model",
        system_prompt="secret-free prompt",
        instruction="task",
        tool_schema=[],
        skill_documents=[],
        max_steps=1,
        max_tool_calls=2,
        max_run_seconds=3,
        max_tool_result_chars=1000,
    )
    encoded = json.dumps(context)
    assert "base_url" not in encoded
    assert "api_key" not in encoded
    assert "secret-free prompt" not in encoded
