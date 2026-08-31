"""API 层任务生命周期契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

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
