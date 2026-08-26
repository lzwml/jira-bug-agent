"""框架无关的 Agent Loop：Plan/Act 由模型完成，Harness 管理执行与状态。"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .config import AgentConfig
from .models import AgentRunResult, ToolEvent
from .provider import ProviderError


class ModelProvider(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]: ...


class ToolRouter(Protocol):
    def openai_tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


def _result_success(raw: str) -> bool:
    try:
        payload = json.loads(raw)
        return bool(payload.get("success"))
    except (json.JSONDecodeError, AttributeError):
        return False


def _bounded_result(raw: str, limit: int) -> str:
    """截断后仍返回合法 JSON，避免把半截 JSON 作为 Observation 交给模型。"""

    if len(raw) <= limit:
        return raw
    payload = {
        "success": _result_success(raw),
        "data": {
            "tool_result_truncated": True,
            "original_chars": len(raw),
            "preview": raw[: max(0, limit - 300)],
        },
        "error_code": None,
        "error_message": None,
        "retryable": False,
    }
    encoded = json.dumps(payload, ensure_ascii=False)
    # config 保证 limit >= 1000；这里循环收缩以适应非 ASCII JSON 转义差异。
    while len(encoded) > limit and payload["data"]["preview"]:
        payload["data"]["preview"] = payload["data"]["preview"][:-100]
        encoded = json.dumps(payload, ensure_ascii=False)
    return encoded


class BugAnalysisAgent:
    def __init__(self, config: AgentConfig, provider: ModelProvider):
        self.config = config
        self.provider = provider

    async def run(self, task: str, system_prompt: str, router: ToolRouter) -> AgentRunResult:
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        events: list[ToolEvent] = []
        tools = router.openai_tools()

        for step in range(1, self.config.max_steps + 1):
            try:
                message = await self.provider.complete(messages, tools)
            except ProviderError as exc:
                return AgentRunResult(
                    status="failed", task=task, final_answer="", steps=step - 1,
                    tool_events=events, error=str(exc),
                )

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = str(message.get("content") or "").strip()
                if content:
                    return AgentRunResult(
                        status="completed", task=task, final_answer=content,
                        steps=step, tool_events=events,
                    )
                return AgentRunResult(
                    status="failed", task=task, final_answer="", steps=step,
                    tool_events=events, error="模型既未输出答案，也未调用工具",
                )

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for call in tool_calls:
                call_id = str(call.get("id") or f"step-{step}-{len(events)}")
                function = call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是 JSON 对象")
                    result = await router.call(name, arguments)
                except (json.JSONDecodeError, ValueError) as exc:
                    arguments = {}
                    result = json.dumps({
                        "success": False,
                        "error_code": "INVALID_TOOL_ARGUMENTS",
                        "error_message": str(exc),
                        "retryable": False,
                    }, ensure_ascii=False)
                except Exception as exc:
                    result = json.dumps({
                        "success": False,
                        "error_code": "TOOL_EXECUTION_ERROR",
                        "error_message": type(exc).__name__,
                        "retryable": True,
                    }, ensure_ascii=False)
                result = _bounded_result(result, self.config.max_tool_result_chars)
                events.append(ToolEvent(
                    step=step,
                    tool_call_id=call_id,
                    tool_name=name,
                    arguments=arguments,
                    result=result,
                    success=_result_success(result),
                ))
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})

        return AgentRunResult(
            status="max_steps",
            task=task,
            final_answer="达到最大步骤数，尚未形成可靠结论。",
            steps=self.config.max_steps,
            tool_events=events,
        )
