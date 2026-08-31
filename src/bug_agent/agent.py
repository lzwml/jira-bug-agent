"""框架无关的 Agent Loop：Plan/Act 由模型完成，Harness 管理执行与状态。

【学习要点】这个文件是整个 Agent 系统的核心，演示了"ReAct 模式"的实现：
Reasoning(推理) + Acting(行动) 交替进行，直到模型给出最终答案或达到步骤上限。

关键设计理念：
1. Agent 不知道工具的具体实现(MCP/REST/本地函数)，只依赖 ToolRouter 协议；
2. Agent 不知道模型的具体厂商(OpenAI/DeepSeek/本地)，只依赖 ModelProvider 协议；
3. 每次工具调用都被记录为 ToolEvent，形成可追溯的执行轨迹；
4. 工具结果会被截断，防止超长日志撑爆模型上下文。

这种解耦让 Agent Loop 可以在测试中用 FakeProvider/FakeRouter 驱动，
完全不依赖真实 LLM 和 MCP Server。见 tests/test_agent.py。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

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

    async def run(self, task: str, system_prompt: str, router: ToolRouter) -> AgentRunResult:
        """执行 Agent 主循环。

        【学习要点】参数设计：
        - task: 用户任务描述(如"分析 APP-42 黑屏问题")；
        - system_prompt: 系统提示词，包含领域知识、工作流、输出格式要求；
        - router: 工具路由器，提供可调用的工具。

        【学习要点】返回值 AgentRunResult 是内部状态，包含：
        - status: completed/failed/max_steps，表示如何结束；
        - final_answer: 模型的最终输出(通常是 RCA JSON)；
        - tool_events: 完整的工具调用历史，用于调试和审计；
        - steps: 实际执行的步数。

        Worker 会把这个内部结果转换成对外的 BugAnalysisResult 契约。
        """
        # 初始化对话历史：system + user 是标准开局。
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        # 记录每次工具调用，形成执行轨迹(trace)。
        events: list[ToolEvent] = []
        # 获取所有可用工具的 OpenAI 格式定义。
        tools = router.openai_tools()

        # 【学习要点】主循环：最多执行 max_steps 步。
        # 每一步：调用模型 → 处理工具调用 → 将结果追加到消息历史。
        # 模型看到历史后会决定是继续调工具还是给出最终答案。
        for step in range(1, self.config.max_steps + 1):
            try:
                message = await self._complete(messages, tools)
            except ProviderError as exc:
                # 认证和参数错误立即失败；限流、5xx 和网络错误已在
                # _complete 中做有限退避，耗尽预算后保留已有 Trace 返回。
                return AgentRunResult(
                    status="failed", task=task, final_answer="", steps=step - 1,
                    tool_events=events, error=str(exc),
                )

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                # 模型没有调用工具 → 这是最终答案。
                content = str(message.get("content") or "").strip()
                if content:
                    # 【学习要点】正常结束：模型给出了文本回答。
                    # Worker 层会尝试解析为 RCAReport JSON。
                    return AgentRunResult(
                        status="completed", task=task, final_answer=content,
                        steps=step, tool_events=events,
                    )
                # 异常情况：模型既没调工具也没回答，通常是模型故障或提示词问题。
                return AgentRunResult(
                    status="failed", task=task, final_answer="", steps=step,
                    tool_events=events, error="模型既未输出答案，也未调用工具",
                )

            # 模型要调用工具：先把 assistant 消息加入历史。
            # 【学习要点】这条消息必须保留 tool_calls 字段，
            # 因为后面的 tool 消息要通过 tool_call_id 关联到这次调用。
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })

            # 执行所有工具调用(可能并行多个)。
            for call in tool_calls:
                # 提取工具调用 ID，用于关联后续的 tool 消息。
                # 如果模型没提供 ID，生成一个备用 ID。
                call_id = str(call.get("id") or f"step-{step}-{len(events)}")
                function = call.get("function") or {}
                name = str(function.get("name") or "")

                # 【学习要点】错误处理策略：工具调用的失败不应该导致 Agent 崩溃，
                # 而是转换成结构化的错误结果，作为"观察"反馈给模型，
                # 让模型决定是修正参数重试，还是放弃这条路径。
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("工具参数必须是 JSON 对象")
                    result = await router.call(name, arguments)
                except (json.JSONDecodeError, ValueError) as exc:
                    # 模型生成了无效的 JSON 参数：不执行工具，返回错误观察。
                    # retryable=False 告诉模型"参数格式错了，重试前先修正"。
                    arguments = {}
                    result = json.dumps({
                        "success": False,
                        "error_code": "INVALID_TOOL_ARGUMENTS",
                        "error_message": str(exc),
                        "retryable": False,
                    }, ensure_ascii=False)
                except Exception as exc:
                    # 工具执行过程中抛出异常(如 MCP Server 崩溃、文件不存在)。
                    # retryable=True 表示这是暂时性故障，模型可以选择重试。
                    result = json.dumps({
                        "success": False,
                        "error_code": "TOOL_EXECUTION_ERROR",
                        "error_message": type(exc).__name__,
                        "retryable": True,
                    }, ensure_ascii=False)

                # 截断超长结果，保护模型上下文。
                result = _bounded_result(result, self.config.max_tool_result_chars)

                # 记录这次工具调用事件。
                events.append(ToolEvent(
                    step=step,
                    tool_call_id=call_id,
                    tool_name=name,
                    arguments=arguments,
                    result=result,
                    success=_result_success(result),
                ))

                # 将工具结果作为 tool 消息追加到对话历史。
                # 【学习要点】tool_call_id 是关键：它告诉模型"这个结果对应你刚才的哪次调用"。
                # 模型在下一轮会看到这个结果，并据此决定下一步行动。
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})

        # 达到步骤上限：模型还没给出最终答案。
        # 【学习要点】这不是"失败"，而是"预算耗尽"。
        # Bug 分析可能需要多轮证据收集，如果步骤太少会频繁触发这个状态。
        # Worker 会把这个状态映射为 BugAnalysisResult.status="max_steps"。
        return AgentRunResult(
            status="max_steps",
            task=task,
            final_answer="达到最大步骤数，尚未形成可靠结论。",
            steps=self.config.max_steps,
            tool_events=events,
        )
