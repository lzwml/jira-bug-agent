from __future__ import annotations

import pytest
from pydantic import ValidationError

from bug_agent.domain.contracts import BugAnalysisTask, EvidenceReference


def test_task_defaults_to_automatic_skill_selection():
    task = BugAnalysisTask(source="jira", issue_key="APP-1")

    assert task.skills is None
    assert task.auto_select_skills is True


def test_task_rejects_empty_explicit_skill_list():
    with pytest.raises(ValidationError):
        BugAnalysisTask(source="jira", issue_key="APP-1", skills=[])


def test_task_requires_reference_for_selected_source():
    with pytest.raises(ValidationError):
        BugAnalysisTask(source="jira")
    with pytest.raises(ValidationError):
        BugAnalysisTask(source="local")


def test_task_rejects_mixed_jira_and_local_references():
    with pytest.raises(ValidationError):
        BugAnalysisTask(source="jira", issue_key="APP-1", case_path="C:/case")


@pytest.mark.parametrize("task_id", ["../escape", "folder/name", r"folder\name", "C:target"])
def test_task_rejects_unsafe_task_id(task_id):
    with pytest.raises(ValidationError):
        BugAnalysisTask(task_id=task_id, source="jira", issue_key="APP-1")


def test_evidence_rejects_reversed_line_range():
    with pytest.raises(ValidationError):
        EvidenceReference(
            evidence_id="ev-1", relative_path="logcat.txt",
            line_start=20, line_end=10,
        )


def test_goal_mode_defaults_to_false():
    task = BugAnalysisTask(source="jira", issue_key="APP-1")
    assert task.goal_mode is False


def test_goal_mode_disables_step_limit():
    """goal_mode=True 时 max_steps 不参与循环控制，但可为零或未设置。"""
    task = BugAnalysisTask(source="jira", issue_key="APP-1", goal_mode=True)
    assert task.goal_mode is True
    assert task.max_steps is None


def test_max_steps_no_longer_capped_at_100():
    """max_steps 不再有 le=100 上限，goal_mode 下可设置为任意正整数。"""
    task = BugAnalysisTask(source="jira", issue_key="APP-1", max_steps=500)
    assert task.max_steps == 500
