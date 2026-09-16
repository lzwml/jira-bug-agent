"""Agent 运行时的结构化人工检查点工具。"""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import uuid4

from ..agent_core.human_checkpoint import (
    REQUEST_HUMAN_GUIDANCE_TOOL,
    checkpoint_from_result,
)
from ..domain.models import HumanCheckpoint, InterventionAttribution


class DelegateRouter(Protocol):
    def openai_tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


class HumanGuidanceToolRouter:
    """只在真实工具调查后允许 Agent 请求一个可归因的人工输入。"""

    def __init__(
        self, delegate: DelegateRouter, *, min_tool_attempts: int = 2,
        max_checkpoints: int = 2,
    ):
        self.delegate = delegate
        self.min_tool_attempts = min_tool_attempts
        self.max_checkpoints = max_checkpoints
        self.tool_attempts = 0
        self.successful_tool_attempts = 0
        self.pending_checkpoint: HumanCheckpoint | None = None
        self.checkpoint_count = 0
        self.attempted_tools: list[str] = []

    def openai_tools(self) -> list[dict[str, Any]]:
        tools = list(self.delegate.openai_tools())
        if any(
            item.get("function", {}).get("name") == REQUEST_HUMAN_GUIDANCE_TOOL
            for item in tools
        ):
            raise ValueError(f"工具名称冲突: {REQUEST_HUMAN_GUIDANCE_TOOL}")
        tools.append({
            "type": "function",
            "function": {
                "name": REQUEST_HUMAN_GUIDANCE_TOOL,
                "description": (
                    "Request one specific human input only when available tools cannot resolve "
                    "a blocking ambiguity after investigation. This pauses the current run."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": [
                                "evidence_location", "incident_scope", "domain_decision",
                                "missing_data", "environment_context", "other",
                            ],
                        },
                        "question": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "blocking_reason": {"type": "string", "minLength": 1, "maxLength": 2000},
                        "requested_input": {"type": "string", "minLength": 1, "maxLength": 1000},
                        "options": {
                            "type": "array", "items": {"type": "string", "maxLength": 500},
                            "maxItems": 8,
                        },
                    },
                    "required": ["category", "question", "blocking_reason", "requested_input"],
                    "additionalProperties": False,
                },
            },
        })
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name != REQUEST_HUMAN_GUIDANCE_TOOL:
            counts_as_investigation = name != "activate_skill"
            if counts_as_investigation:
                self.tool_attempts += 1
                self.attempted_tools.append(name)
            result = await self.delegate.call(name, arguments)
            try:
                if counts_as_investigation and json.loads(result).get("success") is True:
                    self.successful_tool_attempts += 1
            except (json.JSONDecodeError, AttributeError):
                pass
            return result
        if self.pending_checkpoint is not None:
            return _error("HUMAN_GUIDANCE_ALREADY_PENDING", "已有待回复的人工检查点")
        if self.checkpoint_count >= self.max_checkpoints:
            return _error("HUMAN_GUIDANCE_LIMIT_REACHED", "本次会话人工检查点已达到上限")
        if (
            self.tool_attempts < self.min_tool_attempts
            or self.successful_tool_attempts < 1
        ):
            return _error(
                "HUMAN_GUIDANCE_PREMATURE",
                f"至少完成 {self.min_tool_attempts} 次真实工具调查后才能请求人工提示",
            )
        try:
            checkpoint = HumanCheckpoint(
                checkpoint_id=str(uuid4()),
                category=arguments.get("category"),
                question=arguments.get("question"),
                blocking_reason=arguments.get("blocking_reason"),
                requested_input=arguments.get("requested_input"),
                options=arguments.get("options") or [],
                tool_attempts_before=self.tool_attempts,
                successful_tool_attempts_before=self.successful_tool_attempts,
                attempted_tools=list(self.attempted_tools),
            )
        except ValueError as exc:
            return _error("INVALID_HUMAN_GUIDANCE_REQUEST", str(exc))
        self.pending_checkpoint = checkpoint
        self.checkpoint_count += 1
        return json.dumps({
            "success": True,
            "data": {"human_checkpoint": checkpoint.model_dump(mode="json")},
            "error_code": None,
            "error_message": None,
            "retryable": False,
        }, ensure_ascii=False)

    def resolve(self, checkpoint_id: str) -> HumanCheckpoint:
        checkpoint = self.pending_checkpoint
        if checkpoint is None or checkpoint.checkpoint_id != checkpoint_id:
            raise ValueError("人工提示与当前待回复检查点不匹配")
        self.pending_checkpoint = None
        return checkpoint

    def restore_pending(self, checkpoint: HumanCheckpoint) -> None:
        """恢复本地会话的待回复检查点；用于进程重启后的继续调查。"""
        self.pending_checkpoint = checkpoint
        self.checkpoint_count = max(self.checkpoint_count, 1)
        self.tool_attempts = max(self.tool_attempts, checkpoint.tool_attempts_before)
        self.successful_tool_attempts = max(
            self.successful_tool_attempts, checkpoint.successful_tool_attempts_before,
        )
        self.attempted_tools = list(checkpoint.attempted_tools)


def infer_attribution(
    checkpoint: HumanCheckpoint,
    subsequent_tools: list[str],
    subsequent_skill_activations: list[str],
) -> InterventionAttribution:
    """根据提示类型和提示后的真实动作形成可复核归因，不自动修改规则。"""

    if subsequent_skill_activations:
        return InterventionAttribution(
            target="skill_routing",
            rationale="人工提示后首次激活了专项 Skill，优先检查自动 Skill 路由信号。",
        )
    if checkpoint.category == "domain_decision":
        return InterventionAttribution(
            target="skill_content",
            rationale="阻塞点属于领域判断，需检查已激活 Skill 是否缺少判定或证伪规则。",
        )
    if checkpoint.category in {"incident_scope", "missing_data", "environment_context"}:
        return InterventionAttribution(
            target="case_context",
            rationale="人工补充的是事故范围、缺失数据或环境事实，优先改进 Case 输入契约。",
        )
    if checkpoint.category == "evidence_location" or subsequent_tools:
        return InterventionAttribution(
            target="tool_selection",
            rationale="人工提示改变了后续取证工具路径，需检查工具选择、排序或参数策略。",
        )
    return InterventionAttribution(
        target="unknown",
        rationale="现有结构不足以可靠归因，保留给人工评审，不自动进入 Tool/Skill。",
    )


def _error(code: str, message: str) -> str:
    return json.dumps({
        "success": False,
        "data": None,
        "error_code": code,
        "error_message": message,
        "retryable": False,
    }, ensure_ascii=False)
