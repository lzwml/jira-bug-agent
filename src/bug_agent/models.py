"""Agent 运行状态；与任何 LLM SDK 或 MCP SDK 解耦。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolEvent(BaseModel):
    step: int = Field(ge=1)
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: str
    success: bool


class CompletionTokenUsage(BaseModel):
    """一次成功模型响应中由 Provider 报告的 token 用量。"""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_prompt_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class TokenUsage(BaseModel):
    """一次 Agent Loop 内多个模型响应的累计 token 用量。"""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_prompt_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    model_calls: int = Field(ge=1)
    reported_calls: int = Field(ge=1)
    complete: bool


class HumanCheckpoint(BaseModel):
    checkpoint_id: str
    category: Literal[
        "evidence_location", "incident_scope", "domain_decision",
        "missing_data", "environment_context", "other",
    ]
    question: str = Field(min_length=1, max_length=1000)
    blocking_reason: str = Field(min_length=1, max_length=2000)
    requested_input: str = Field(min_length=1, max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=8)
    tool_attempts_before: int = Field(ge=0)
    successful_tool_attempts_before: int = Field(ge=0)
    attempted_tools: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class InterventionAttribution(BaseModel):
    target: Literal[
        "tool_selection", "skill_routing", "skill_content", "case_context", "unknown",
    ]
    inferred: bool = True
    rationale: str = Field(min_length=1, max_length=2000)


class HumanIntervention(BaseModel):
    checkpoint: HumanCheckpoint
    response: str = Field(min_length=1, max_length=20_000)
    resumed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    subsequent_tools: list[str] = Field(default_factory=list)
    subsequent_skill_activations: list[str] = Field(default_factory=list)
    attribution: InterventionAttribution | None = None


class AgentRunResult(BaseModel):
    status: Literal["completed", "max_steps", "failed", "waiting_for_human"]
    task: str
    final_answer: str
    steps: int = Field(ge=0)
    tool_events: list[ToolEvent] = Field(default_factory=list)
    error: str | None = None
    error_type: str | None = None
    retryable: bool = False
    human_checkpoint: HumanCheckpoint | None = None
    token_usage: TokenUsage | None = None
