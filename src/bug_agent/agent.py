"""框架无关的 Agent Loop：Plan/Act 由模型完成，Harness 管理执行与状态。

【学习要点】这个文件是整个 Agent 系统的核心，演示了"ReAct 模式"的实现：
Reasoning(推理) + Acting(行动) 交替进行，直到模型给出最终答案或达到步骤上限。

关键设计理念：
1. Agent 不知道工具的具体实现(MCP/REST/本地函数)，只依赖 ToolRouter 协议；
2. Agent 不知道模型的具体厂商(OpenAI/DeepSeek/本地)，只依赖 ModelProvider 协议；
3. 每次工具调用都被记录为 ToolEvent，形成可追溯的执行轨迹；
4. 工具结果会被截断，防止超长日志撑爆模型上下文。

支持两种模式：
- 单次分析：run() 从头构建消息并执行完整循环
- 连续问答：run_with_messages() 从已有消息继续执行，支持多轮追问

这种解耦让 Agent Loop 可以在测试中用 FakeProvider/FakeRouter 驱动，
完全不依赖真实 LLM 和 MCP Server。见 tests/test_agent.py。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Protocol

from .config import AgentConfig
from .models import AgentRunResult, ToolEvent
from .provider import ProviderError


class ModelProvider(Protocol):
    """模型提供者协议：只需要能执行一次对话补全。

    【学习要点】为什么用 Protocol 而不是 ABC(抽象基类)？
    - Protocol 是"鸭子类型"的正式化：只要对象有 complete() 方法就算实现了协议，
      不需要显式继承，降低了耦合；
    - 测试中的 FakeProvider 不需要继承任何基类，直接定义 complete() 即可；
    - 未来接入 LangChain/LiteLLM 等框架时，包一层适配器就能满足协议。
    """

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]: ...


class ToolRouter(Protocol):
    """工具路由器协议：负责工具的发现和执行。

    【学习要点】Agent 不关心工具从哪来(MCP/本地函数/远程API)，
    只要求两个能力：
    1. openai_tools() → 返回 OpenAI 格式的工具定义列表，给模型看；
    2. call(name, arguments) → 执行指定工具，返回 JSON 字符串结果。

    这种抽象让 Agent 可以同时驱动多个 MCP Server，也可以在不改代码的情况下
    把某些工具换成 mocks 用于测试。
    """

    def openai_tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


def _result_success(raw: str) -> bool:
    """从工具的 JSON 结果字符串中提取 success 标志。

    【学习要点】所有工具返回的都是 JSON 字符串，格式约定为：
    {"success": true/false, "data": ..., "error_code": ..., "error_message": ...}
    这个函数用于快速判断工具调用是否成功，而不需要完全解析整个 payload。
    解析失败(如工具返回了非 JSON)会被视为失败。
    """
    try:
        payload = json.loads(raw)
        return bool(payload.get("success"))
    except (json.JSONDecodeError, AttributeError):
        return False


def _bounded_result(raw: str, limit: int) -> str:
    """截断后仍返回合法 JSON，避免把半截 JSON 作为 Observation 交给模型。

    【学习要点】为什么需要这个函数？
    真实场景：一个 search_evidence 调用可能返回几百行日志(几万字符)，
    直接塞进对话历史会迅速撑爆模型上下文窗口。

    解决思路：
    1. 超长时保留开头部分作为 preview，让模型知道"这里有更多数据"；
    2. 包装成标准 JSON 格式，附带 original_chars 提示原始长度；
    3. 关键：截断后必须仍是合法 JSON，否则模型看到 "..." 或半截字符串会困惑。

    【学习要点】为什么还要循环收缩？
    JSON 序列化时非 ASCII 字符会被转义为 \\uXXXX，一个中文字符从 3 字节变成 6 字符，
    所以预先截断到 limit 后，序列化结果仍可能超限。循环每次砍掉 100 字符，
    直到序列化后的长度符合要求。这是一种保守但可靠的策略。
    """

    if len(raw) <= limit:
        return raw
    payload = {
        "success": _result_success(raw),
        "data": {
            "tool_result_truncated": True,
            "original_chars": len(raw),
            "preview": raw[: max(0, limit - 300)],  # 预留 300 字符给 JSON 包装
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
    """Bug 分析 Agent：驱动模型与工具循环交互，直到得出 RCA 结论。

    【学习要点】这个类实现了经典的 ReAct 模式：
    1. 模型推理(Reasoning)：分析当前状态，决定下一步行动；
    2. 行动(Acting)：调用一个或多个工具；
    3. 观察(Observation)：接收工具返回的结果；
    4. 回到步骤 1，直到模型给出最终答案或达到步骤上限。

    与传统的"一次性问答"不同，Agent 可以主动收集证据、验证假设、修正方向，
    这是处理复杂 Bug 分析任务的关键能力。
    """

    def __init__(self, config: AgentConfig, provider: ModelProvider):
        self.config = config
        self.provider = provider

    async def _complete(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        """Retry transient provider failures without consuming an Agent step."""

        for attempt in range(self.config.llm_max_retries + 1):
            try:
                return await self.provider.complete(messages, tools)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self.config.llm_max_retries:
                    raise
                delay = self.config.llm_retry_base_seconds * (2 ** attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        raise RuntimeError("unreachable")

    async def _run_loop(
        self,
        messages: list[dict],
        tools: list[dict],
        router: ToolRouter,
        on_tool_event: Callable[[ToolEvent], None] | None = None,
        goal_mode: bool = False,
        starting_step: int = 0,
        max_steps_override: int | None = None,
    ) -> tuple[AgentRunResult, list[dict]]:
        """执行 Agent 主循环，从已有消息列表继续执行。

        【学习要点】与 run() 的区别：
        - run() 从零构建 messages 并调用 _run_loop()；
        - _run_loop() 从给定的 messages 继续执行，适用于连续问答场景；
        - 返回 (result, updated_messages)，让调用方可以继续追加消息。
        - max_steps_override：覆盖配置中的步数预算，用于每轮独立预算。

        【学习要点】返回的 messages 包含了循环中所有新增的 assistant/tool 消息，
        以及最终的 assistant 回答。调用方可以继续向这个列表追加 user 消息，
        然后再次调用 _run_loop() 实现连续追问。
        """
        effective_max = starting_step + (
            max_steps_override if max_steps_override is not None else self.config.max_steps
        )
        events: list[ToolEvent] = []
        step = starting_step
        tool_call_count = 0
        started_at = time.monotonic()
        while True:
            if time.monotonic() - started_at >= self.config.max_run_seconds:
                return AgentRunResult(
                    status="max_steps",
                    task="",
                    final_answer="达到 Agent 总运行时间硬上限，尚未形成可靠结论。",
                    steps=max(0, step - starting_step),
                    tool_events=events,
                    error="MAX_RUN_SECONDS_EXCEEDED",
                    error_type="AgentBudgetExceeded",
                ), messages
            step += 1
            if not goal_mode and step > effective_max:
                return AgentRunResult(
                    status="max_steps",
                    task="",
                    final_answer="达到最大步骤数，尚未形成可靠结论。",
                    steps=effective_max - starting_step,
                    tool_events=events,
                ), messages
            try:
                message = await self._complete(messages, tools)
            except ProviderError as exc:
                return AgentRunResult(
                    status="failed", task="", final_answer="", steps=step - 1 - starting_step,
                    tool_events=events, error=str(exc),
                    error_type=type(exc).__name__, retryable=exc.retryable,
                ), messages

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                content = str(message.get("content") or "").strip()
                if content:
                    messages.append({
                        "role": "assistant",
                        "content": content,
                    })
                    return AgentRunResult(
                        status="completed", task="", final_answer=content,
                        steps=step - starting_step, tool_events=events,
                    ), messages
                return AgentRunResult(
                    status="failed", task="", final_answer="", steps=step - starting_step,
                    tool_events=events, error="模型既未输出答案，也未调用工具",
                ), messages

            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })

            for call in tool_calls:
                if tool_call_count >= self.config.max_tool_calls:
                    return AgentRunResult(
                        status="max_steps",
                        task="",
                        final_answer="达到 Agent 工具调用硬上限，尚未形成可靠结论。",
                        steps=step - starting_step,
                        tool_events=events,
                        error="MAX_TOOL_CALLS_EXCEEDED",
                        error_type="AgentBudgetExceeded",
                    ), messages
                tool_call_count += 1
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

                event = ToolEvent(
                    step=step,
                    tool_call_id=call_id,
                    tool_name=name,
                    arguments=arguments,
                    result=result,
                    success=_result_success(result),
                )
                events.append(event)

                if on_tool_event is not None:
                    try:
                        on_tool_event(event)
                    except Exception:
                        pass

                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})

    async def run(
        self,
        task: str,
        system_prompt: str,
        router: ToolRouter,
        on_tool_event: Callable[[ToolEvent], None] | None = None,
        goal_mode: bool = False,
    ) -> AgentRunResult:
        """执行 Agent 主循环（从头开始）。

        【学习要点】参数设计：
        - task: 用户任务描述(如"分析 APP-42 黑屏问题")；
        - system_prompt: 系统提示词，包含领域知识、工作流、输出格式要求；
        - router: 工具路由器，提供可调用的工具；
        - on_tool_event: 可选回调，每次工具调用完成后立即通知（用于实时落盘）；
        - goal_mode: True 时不限制工具调用次数，循环直到模型给出最终答案。

        【学习要点】返回值 AgentRunResult 是内部状态，包含：
        - status: completed/failed/max_steps，表示如何结束；
        - final_answer: 模型的最终输出(通常是 RCA JSON)；
        - tool_events: 完整的工具调用历史，用于调试和审计；
        - steps: 实际执行的步数。

        Worker 会把这个内部结果转换成对外的 BugAnalysisResult 契约。
        """
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        tools = router.openai_tools()
        result, _ = await self._run_loop(
            messages, tools, router, on_tool_event, goal_mode,
        )
        # 把 task 信息回填到结果中，保持与旧接口兼容。
        return AgentRunResult(
            status=result.status,
            task=task,
            final_answer=result.final_answer,
            steps=result.steps,
            tool_events=result.tool_events,
            error=result.error,
            error_type=result.error_type,
            retryable=result.retryable,
        )

    async def run_with_messages(
        self,
        messages: list[dict],
        router: ToolRouter,
        on_tool_event: Callable[[ToolEvent], None] | None = None,
        goal_mode: bool = False,
        starting_step: int = 0,
        max_steps_override: int | None = None,
    ) -> tuple[AgentRunResult, list[dict]]:
        """从已有消息列表继续执行 Agent 循环。

        【学习要点】这是连续问答的核心接口：
        - messages: 包含 system、user、assistant、tool 消息的完整对话历史；
        - starting_step: 当前轮次的起始步号（用于全局限步计数）；
        - max_steps_override: 覆盖本轮步数预算，默认使用 config.max_steps；
        - 返回 (result, updated_messages)，updated_messages 包含本轮的
          新增消息和最终回答，可以直接用于下一轮追问。

        典型用法（连续问答）：
        ```python
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "分析 Bug A"},
        ]
        result1, messages = await agent.run_with_messages(messages, router)
        # 用户追问
        messages.append({"role": "user", "content": "能详细看看 SurfaceFlinger 吗？"})
        result2, messages = await agent.run_with_messages(messages, router)
        ```
        """
        tools = router.openai_tools()
        return await self._run_loop(
            messages, tools, router, on_tool_event, goal_mode, starting_step,
            max_steps_override,
        )
