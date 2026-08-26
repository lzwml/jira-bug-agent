from __future__ import annotations

import pytest
from pydantic import ValidationError

from bug_agent.contracts import BugAnalysisTask, EvidenceReference


def test_task_requires_reference_for_selected_source():
    with pytest.raises(ValidationError):
        BugAnalysisTask(source="jira")
    with pytest.raises(ValidationError):
        BugAnalysisTask(source="local")


def test_task_rejects_mixed_jira_and_local_references():
    with pytest.raises(ValidationError):
        BugAnalysisTask(source="jira", issue_key="APP-1", case_path="C:/case")


def test_evidence_rejects_reversed_line_range():
    with pytest.raises(ValidationError):
        EvidenceReference(
            evidence_id="ev-1", relative_path="logcat.txt",
            line_start=20, line_end=10,
        )

