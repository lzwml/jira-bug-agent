"""单次会话内的连续问答管理。

【学习要点】为什么需要这个模块？

Bug 分析场景中，一次 Agent 运行往往拿不到完整信息：
1. 初始分析可能证据不足，需要追问特定日志路径；
2. 模型可能遗漏某个模块，需要提示补充调查；
3. 工程师可能想对某个假设做深入验证。

传统的一次性 run() → 返回结果 的模式不支持这种交互。
ConversationSession 在单次会话内维护消息历史，支持多轮追问，
同时保持工具路由和模型连接的复用。

设计原则：
1. 会话级消息历史：所有轮次共享同一个 messages 列表，模型能看到完整上下文；
2. 每轮独立步数预算：避免单轮消耗过多预算导致后续轮次无法执行；
3. 会话级步数上限：防止无限追问导致 Token 耗尽；
4. 工具路由复用：所有轮次共享同一个 ToolRouter，避免重复创建 MCP 连接。

与 RCA 迭代（reconciliation）的关系：
- ConversationSession 是"单次会话内的连续问答"，不持久化到 rca-state.json；
- RCA 迭代是"跨运行的结论合并"，每次执行 produce 一份独立的 Run Record；
- 两者互补：会话内多轮追问帮助收集更完整的证据，然后一次性产出更好的 RCA。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .agent import BugAnalysisAgent, ModelProvider, ToolRouter
from .config import AgentConfig
from .models import AgentRunResult, ToolEvent
from .prompts import CONVERSATION_FOLLOWUP_SYSTEM_PROMPT


@dataclass
class ConversationTurn:
    """单轮问答的记录。"""

    user_message: str
    """用户在这一轮输入的内容。"""

    result: AgentRunResult
    """Agent 在这一轮的运行结果。"""


@dataclass
class ConversationResult:
    """一次完整会话的最终结果。"""

    turns: list[ConversationTurn]
    """所有轮次的记录，按时间顺序排列。"""

    total_steps: int
    """所有轮次累计的模型推理步数。"""

    @property
    def final_answer(self) -> str:
        """最后一轮的最终回答。"""
        return self.turns[-1].result.final_answer if self.turns else ""


class ConversationSession:
    """单次会话内的连续问答管理器。

    【学习要点】生命周期：
    1. 创建会话：提供初始 System Prompt、Provider 和 Router；
    2. 初始提问：send() 发送第一条用户消息，Agent 执行完整分析；
    3. 追问：send() 发送后续消息，Agent 在已有上下文基础上继续；
    4. 结束：finalize() 返回 ConversationResult。

    使用示例：
    ```python
    session = ConversationSession(
        agent=agent,
        system_prompt="你是 Bug 分析助手...",
        router=router,
        max_turns=5,
        max_steps_per_turn=8,
    )
    # 初始分析
    turn1 = await session.send("分析 APP-42 黑屏问题")
    # 追问
    turn2 = await session.send("能详细看看 SurfaceFlinger 的日志吗？")
    # 结束
    result = await session.finalize()
    ```
    """

    def __init__(
        self,
        agent: BugAnalysisAgent,
        system_prompt: str,
        router: ToolRouter,
        max_turns: int = 5,
        max_steps_per_turn: int | None = None,
        goal_mode: bool = False,
        on_tool_event: Callable[[ToolEvent], None] | None = None,
    ):
        self._agent = agent
        self._router = router
        self._max_turns = max_turns
        self._max_steps_per_turn = max_steps_per_turn
        self._goal_mode = goal_mode
        self._on_tool_event = on_tool_event

        # 消息历史：system prompt 只在初始化时设置一次
        self._messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        self._turns: list[ConversationTurn] = []
        self._total_steps: int = 0
        self._active: bool = True

    @property
    def turn_count(self) -> int:
        """已完成的轮次数量。"""
        return len(self._turns)

    @property
    def remaining_turns(self) -> int:
        """剩余可用的轮次数量。"""
        return max(0, self._max_turns - len(self._turns))

    @property
    def is_active(self) -> bool:
        """会话是否仍可继续。"""
        return self._active and self.remaining_turns > 0

    @property
    def messages(self) -> list[dict]:
        """当前的消息历史（只读）。"""
        return list(self._messages)

    async def send(self, user_message: str) -> ConversationTurn:
        """发送一条用户消息并获取 Agent 回答。

        Args:
            user_message: 用户的消息内容。

        Returns:
            ConversationTurn，包含用户消息和 Agent 运行结果。

        Raises:
            RuntimeError: 会话已结束（达到最大轮次或已调用 finalize()）。
        """
        if not self.is_active:
            raise RuntimeError(
                f"会话已结束（已完成 {len(self._turns)}/{self._max_turns} 轮）"
            )

        # 如果是第一轮之后的追问，先追加追问的系统提示
        if self._turns:
            self._messages.append({
                "role": "system",
                "content": CONVERSATION_FOLLOWUP_SYSTEM_PROMPT,
            })

        # 追加用户消息
        self._messages.append({"role": "user", "content": user_message})

        # 执行 Agent 循环
        result, self._messages = await self._agent.run_with_messages(
            messages=self._messages,
            router=self._router,
            on_tool_event=self._on_tool_event,
            goal_mode=self._goal_mode,
            starting_step=self._total_steps,
            max_steps_override=self._max_steps_per_turn,
        )

        self._total_steps += result.steps
        turn = ConversationTurn(user_message=user_message, result=result)
        self._turns.append(turn)

        # 检查是否达到最大轮次
        if len(self._turns) >= self._max_turns:
            self._active = False

        return turn

    async def finalize(self) -> ConversationResult:
        """结束会话，返回最终结果。

        调用后会话不可再使用。
        """
        self._active = False
        return ConversationResult(
            turns=list(self._turns),
            total_steps=self._total_steps,
        )