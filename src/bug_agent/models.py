"""Agent 运行状态；与任何 LLM SDK 或 MCP SDK 解耦。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Literal

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


class TokenUsageAccumulator:
    """累计成功模型调用及其可选 usage，供 Provider 与 Agent 共用。"""

    def __init__(self) -> None:
        self.model_calls = 0
        self.samples: list[CompletionTokenUsage] = []

    def add(self, usage: CompletionTokenUsage | None) -> None:
        self.model_calls += 1
        if usage is not None:
            self.samples.append(usage)

    def summary(self) -> TokenUsage | None:
        if not self.samples:
            return None

        def optional_sum(field: str) -> int | None:
            values = [getattr(item, field) for item in self.samples]
            known = [value for value in values if value is not None]
            return sum(known) if known else None

        return TokenUsage(
            prompt_tokens=sum(item.prompt_tokens for item in self.samples),
            completion_tokens=sum(item.completion_tokens for item in self.samples),
            total_tokens=sum(item.total_tokens for item in self.samples),
            cached_prompt_tokens=optional_sum("cached_prompt_tokens"),
            reasoning_tokens=optional_sum("reasoning_tokens"),
            model_calls=self.model_calls,
            reported_calls=len(self.samples),
            complete=len(self.samples) == self.model_calls,
        )


def merge_token_usage(usages: Iterable[TokenUsage | None]) -> TokenUsage | None:
    """汇总多个分析阶段；所有阶段都未知时保持 ``None``。"""

    items = [item for item in usages if item is not None]
    if not items:
        return None

    def optional_sum(field: str) -> int | None:
        values = [getattr(item, field) for item in items]
        known = [value for value in values if value is not None]
        return sum(known) if known else None

    model_calls = sum(item.model_calls for item in items)
    reported_calls = sum(item.reported_calls for item in items)
    return TokenUsage(
        prompt_tokens=sum(item.prompt_tokens for item in items),
        completion_tokens=sum(item.completion_tokens for item in items),
        total_tokens=sum(item.total_tokens for item in items),
        cached_prompt_tokens=optional_sum("cached_prompt_tokens"),
        reasoning_tokens=optional_sum("reasoning_tokens"),
        model_calls=model_calls,
        reported_calls=reported_calls,
        complete=all(item.complete for item in items) and reported_calls == model_calls,
    )


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
