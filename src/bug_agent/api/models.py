"""API 层任务生命周期契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import BugAnalysisResult, BugAnalysisTask


TaskStatus = Literal["queued", "running", "completed", "failed"]


class TaskRecord(BaseModel):
    task_id: str
    status: TaskStatus
    task: BugAnalysisTask
    result: BugAnalysisResult | None = None
    error: str | None = None
    created_at: str
    updated_at: str


class TaskSubmission(BaseModel):
    task_id: str
    status: TaskStatus
    created: bool


class ContinuationRequest(BaseModel):
    """基于已完成任务追加一轮调查，不允许调用方改变 Case 来源。"""

    task_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    objective: str = Field(min_length=1, max_length=2000)
    max_steps: int | None = Field(default=None, ge=1)
    goal_mode: bool | None = None
    skills: list[str] | None = Field(default=None, min_length=1, max_length=5)
    auto_select_skills: bool | None = None
    include_trace: bool | None = None
    include_analysis_guide: bool | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
