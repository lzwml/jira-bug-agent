from __future__ import annotations

import json

from bug_agent.contracts import (
    EvidenceReference,
    Hypothesis,
    IncidentIdentity,
    IncidentWindow,
    RCAReport,
    TimelineEntry,
)
from bug_agent.models import ToolEvent
from bug_agent.report_validation import build_evidence_registry, validate_report


def evidence_event() -> ToolEvent:
    return ToolEvent(
        step=1,
        tool_call_id="call-1",
        tool_name="search_evidence",
        arguments={"query": "FATAL"},
        success=True,
        result=json.dumps({
            "success": True,
            "data": {
                "items": [{
                    "evidence_id": "ev-real",
                    "artifact_id": "artifact-1",
                    "artifact_name": "logcat.txt",
                    "relative_path": "logcat.txt",
                    "line_start": 42,
                    "line_end": 42,
                    "content": "FATAL EXCEPTION: main",
                }],
            },
        }),
    )


def test_build_evidence_registry_uses_tool_output():
    registry = build_evidence_registry([evidence_event()])
    assert registry["ev-real"].relative_path == "logcat.txt"
    assert registry["ev-real"].line_start == 42


def test_valid_confirmed_report_remains_grounded():
    report = RCAReport(
        conclusion_status="confirmed",
        summary="已确认",
        root_cause="主线程发生致命异常",
        root_cause_evidence_ids=["ev-real"],
        incident=IncidentIdentity(
            incident_id="incident-1",
            process_name="system_server",
            verified_window=IncidentWindow(
                start="08-26 10:20:31.123", clock_domain="android", source="log",
            ),
            evidence_ids=["ev-real"],
        ),
        evidence=[EvidenceReference(
            evidence_id="ev-real", artifact_id="artifact-1", relative_path="logcat.txt",
            line_start=42, line_end=42, excerpt="FATAL EXCEPTION",
        )],
        hypotheses=[Hypothesis(
            statement="主线程发生致命异常", confidence=.9, status="supported",
            supporting_evidence_ids=["ev-real"],
        )],
    )

    validated, result = validate_report(report, [evidence_event()])

    assert validated.conclusion_status == "confirmed"
    assert result.grounded is True
    assert result.verified_evidence_count == 1


def test_fabricated_evidence_is_removed_and_confirmed_is_downgraded():
    report = RCAReport(
        conclusion_status="confirmed",
        summary="模型声称已确认",
        root_cause="虚构根因",
        confirmed_facts=["虚构事实"],
        evidence=[EvidenceReference(
            evidence_id="ev-invented", artifact_id="artifact-x",
            relative_path="missing.log", line_start=1, line_end=1,
        )],
        timeline=[TimelineEntry(
            timestamp="12:00", clock_domain="reported", event="声称的事件",
            evidence_ids=["ev-invented"],
        )],
    )

    validated, result = validate_report(report, [evidence_event()])

    assert validated.conclusion_status == "hypothesis_only"
    assert validated.root_cause is None
    assert validated.confirmed_facts == []
    assert validated.evidence == []
    assert validated.timeline[0].evidence_ids == []
    assert result.grounded is False
    assert result.rejected_evidence_ids == ["ev-invented"]


def test_confirmed_report_with_real_evidence_but_no_incident_anchor_is_downgraded():
    report = RCAReport(
        conclusion_status="confirmed",
        summary="证据存在但事故身份未建立",
        root_cause="主线程发生致命异常",
        root_cause_evidence_ids=["ev-real"],
        evidence=[EvidenceReference(
            evidence_id="ev-real", artifact_id="artifact-1", relative_path="logcat.txt",
            line_start=42, line_end=42, excerpt="FATAL EXCEPTION",
        )],
    )

    validated, result = validate_report(report, [evidence_event()])

    assert validated.conclusion_status == "hypothesis_only"
    assert validated.root_cause is None
    assert result.grounded is False
    assert any("事故身份" in issue for issue in result.issues)
