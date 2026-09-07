from __future__ import annotations

import json
from pathlib import Path

from bug_agent.chat_store import ChatStore
from bug_agent.contracts import BugAnalysisTask
from bug_agent.models import (
    HumanCheckpoint, HumanIntervention, InterventionAttribution,
)
from bug_agent.run_bundle import verify_record


def test_human_intervention_automatically_creates_reviewable_optimization_candidate(tmp_path):
    sessions = tmp_path / ".bug-agent" / "chat-sessions"
    store = ChatStore(sessions)
    task = BugAnalysisTask(source="local", case_path=str(tmp_path), objective="定位黑屏")
    store.create_session("session-1", task)
    intervention = HumanIntervention(
        checkpoint=HumanCheckpoint(
            checkpoint_id="checkpoint-1",
            category="evidence_location",
            question="关键日志在哪个归档？",
            blocking_reason="已检查两个候选目录但没有事故窗口",
            requested_input="归档名称或大致时间",
            tool_attempts_before=4,
            successful_tool_attempts_before=4,
            attempted_tools=["open_case", "inspect_case", "inspect_archive", "search_evidence"],
        ),
        response="在 APLog_92，约 10:42",
        subsequent_tools=["inspect_archive", "extract_archive_members", "search_evidence"],
        attribution=InterventionAttribution(
            target="tool_selection",
            rationale="人工提示改变了归档选择。",
        ),
    )

    store.add_turn(
        "session-1", 2, intervention.response, "继续调查完成", 3, "completed",
        human_intervention=intervention.model_dump(mode="json"),
    )

    session = store.get_session("session-1")
    candidate_path = session["turns"][0]["optimization_candidate"]
    candidate = json.loads(Path(candidate_path).read_text(encoding="utf-8"))
    assert candidate["target"] == "tool_selection"
    assert candidate["status"] == "pending_review"
    assert candidate["observed_effect"]["subsequent_tools"][-1] == "search_evidence"
    assert candidate["acceptance_gate"]["requires_human_review"] is True
    assert verify_record(candidate)
