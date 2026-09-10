"""API 层任务生命周期契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ..contracts import BugAnalysisResult, BugAnalysisTask


TaskStatus = Literal["queued", "running", "completed", "failed"]
ConversationStatus = Literal["active", "closed"]
ConversationRole = Literal["user", "assistant"]
ConversationMessageStatus = Literal["queued", "running", "completed", "failed", "cancelled"]
ConversationEventKind = Literal[
    "message_queued", "message_running", "phase_changed", "tool_started", "tool_completed",
    "assistant_message", "turn_failed", "turn_cancelled",
]


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


class ConversationCreate(BaseModel):
    """创建一个围绕单个 Case 持续协作的会话。"""

    conversation_id: str | None = Field(
        default=None, min_length=1, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    task: BugAnalysisTask


class ConversationMessageCreate(BaseModel):
    """工程师补充的事实、问题或调查指令。"""

    content: str = Field(min_length=1, max_length=20_000)


class ConversationMessage(BaseModel):
    message_id: int
    role: ConversationRole
    content: str
    status: ConversationMessageStatus = "completed"
    error: str | None = None
    created_at: str


class ClientLocation(BaseModel):
    """VS Code 等客户端可稳定消费的 Case 内位置。"""

    relative_path: str
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    identifier: str | None = None
    excerpt: str | None = None
    archive_relative_path: str | None = None
    member_id: str | None = None


class ConversationEvent(BaseModel):
    """持久化的客户端事件；event_id 同时充当断线续接游标。"""

    schema_version: Literal[1] = 1
    event_id: int
    conversation_id: str
    message_id: int | None = None
    kind: ConversationEventKind
    step: int | None = None
    tool_name: str | None = None
    success: bool | None = None
    content: str | None = None
    arguments: dict[str, Any] | None = None
    result: str | None = None
    locations: list[ClientLocation] = Field(default_factory=list)
    created_at: str


class ConversationEventPage(BaseModel):
    schema_version: Literal[1] = 1
    events: list[ConversationEvent] = Field(default_factory=list)
    next_cursor: int = Field(default=0, ge=0)


class ConversationRecord(BaseModel):
    conversation_id: str
    status: ConversationStatus
    task: BugAnalysisTask
    messages: list[ConversationMessage] = Field(default_factory=list)
    created_at: str
    updated_at: str
