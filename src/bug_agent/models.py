"""Agent 运行状态；与任何 LLM SDK 或 MCP SDK 解耦。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolEvent(BaseModel):
    step: int = Field(ge=1)
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: str
    success: bool


class AgentRunResult(BaseModel):
    status: Literal["completed", "max_steps", "failed"]
    task: str
    final_answer: str
    steps: int = Field(ge=0)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    retryable: bool = False
