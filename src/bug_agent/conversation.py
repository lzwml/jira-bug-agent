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

import inspect
from dataclasses import dataclass
from typing import Callable

from .agent import BugAnalysisAgent, ModelProvider, ToolRouter
from .config import AgentConfig
from .models import (
    AgentRunResult,
    HumanCheckpoint,
    HumanIntervention,
    TokenUsage,
    ToolEvent,
    merge_token_usage,
)
from .prompts import CONVERSATION_FOLLOWUP_SYSTEM_PROMPT
from .human_guidance import infer_attribution
from .tool_catalog import normalize_tool_catalog


@dataclass
class SavedTurn:
    """从持久化存储中恢复的对话轮次。"""

    user_message: str
    assistant_answer: str
    steps: int = 0
    agent_status: str = "completed"
    human_checkpoint: dict | None = None
    human_intervention: dict | None = None
    token_usage: dict | None = None


@dataclass
class ConversationTurn:
    """单轮问答的记录。"""

    user_message: str
    """用户在这一轮输入的内容。"""

    result: AgentRunResult
    """Agent 在这一轮的运行结果。"""

    human_intervention: HumanIntervention | None = None
    """若本轮是对 Agent 检查点的回复，记录提示及其直接产生的行动。"""


@dataclass
class ConversationResult:
    """一次完整会话的最终结果。"""

    turns: list[ConversationTurn]
    """所有轮次的记录，按时间顺序排列。"""

    total_steps: int
    """所有轮次累计的模型推理步数。"""

    initial_token_usage: TokenUsage | None = None
    """进入首轮 Agent 前的模型用量，例如 Jira Comment Compiler。"""

    @property
    def final_answer(self) -> str:
        """最后一轮的最终回答。"""
        return self.turns[-1].result.final_answer if self.turns else ""

    @property
    def human_interventions(self) -> list[HumanIntervention]:
        return [turn.human_intervention for turn in self.turns if turn.human_intervention]

    @property
    def token_usage(self) -> TokenUsage | None:
        return merge_token_usage([
            self.initial_token_usage,
            *(turn.result.token_usage for turn in self.turns),
        ])


class ConversationSession:
    """单次会话内的连续问答管理器。

    【学习要点】生命周期：
    1. 创建会话：提供初始 System Prompt、Provider 和 Router；
    2. 初始提问：send() 发送第一条用户消息，Agent 执行完整分析；
    3. 追问：send() 发送后续消息，Agent 在已有上下文基础上继续；
    4. 结束：finalize() 返回 ConversationResult。

    轮次上限由调用方控制（CLI 通过 /quit，HTTP API 由调用方决定何时停止发送消息）。
    步数上限由 max_steps_per_turn 控制，防止单轮无限循环。

    使用示例：
    ```python
    session = ConversationSession(
        agent=agent,
        system_prompt="你是 Bug 分析助手...",
        router=router,
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
        max_steps_per_turn: int | None = None,
        goal_mode: bool = False,
        on_tool_event: Callable[[ToolEvent], None] | None = None,
        on_progress: Callable[[dict], None] | None = None,
        on_close: Callable[[], object] | None = None,
        initial_token_usage: TokenUsage | None = None,
    ):
        self._agent = agent
        self._router = router
        self._max_steps_per_turn = max_steps_per_turn
        self._goal_mode = goal_mode
        self._on_tool_event = on_tool_event
        self._on_progress = on_progress
        self._on_close = on_close
        self._closed_resources = False
        self._initial_token_usage = initial_token_usage

        # 消息历史：system prompt 只在初始化时设置一次
        self._messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        self._turns: list[ConversationTurn] = []
        self._total_steps: int = 0
        self._active: bool = True
        self._pending_checkpoint: HumanCheckpoint | None = None

    @property
    def turn_count(self) -> int:
        """已完成的轮次数量。"""
        return len(self._turns)

    @property
    def is_active(self) -> bool:
        """会话是否仍可继续。"""
        return self._active

    @property
    def messages(self) -> list[dict]:
        """当前的消息历史（只读）。"""
        return list(self._messages)

    @property
    def pending_checkpoint(self) -> HumanCheckpoint | None:
        return self._pending_checkpoint

    @property
    def initial_token_usage(self) -> TokenUsage | None:
        return self._initial_token_usage

    @property
    def token_usage(self) -> TokenUsage | None:
        return merge_token_usage([
            self._initial_token_usage,
            *(turn.result.token_usage for turn in self._turns),
        ])

    @property
    def tool_catalog(self) -> list[dict]:
        """本次会话实际发送给模型的工具描述与参数契约快照。"""
        return normalize_tool_catalog(self._router.openai_tools())

    def load_history(self, messages: list[dict[str, str]]) -> None:
        """注入初始消息历史（不含 system prompt）。

        用于 Worker 在创建会话后、send() 之前，将持久化的历史消息
        恢复到会话中。只应在首次 send() 之前调用一次。
        """
        for msg in messages:
            role = msg.get("role", "")
            if role not in {"user", "assistant", "system"}:
                raise ValueError(f"无效的消息角色: {role}")
        self._messages.extend(msg for msg in messages)

    async def send(self, user_message: str) -> ConversationTurn:
        """发送一条用户消息并获取 Agent 回答。

        Args:
            user_message: 用户的消息内容。

        Returns:
            ConversationTurn，包含用户消息和 Agent 运行结果。

        Raises:
            RuntimeError: 会话已结束（已调用 finalize()）。
        """
        if not self.is_active:
            raise RuntimeError("会话已结束")

        checkpoint = self._pending_checkpoint
        intervention: HumanIntervention | None = None
        if checkpoint is not None:
            resolver = getattr(self._router, "resolve", None)
            if resolver is not None:
                resolver(checkpoint.checkpoint_id)
            self._pending_checkpoint = None
            intervention = HumanIntervention(checkpoint=checkpoint, response=user_message)
            self._messages.append({
                "role": "system",
                "content": (
                    "下面的用户消息是对人工检查点 " + checkpoint.checkpoint_id
                    + " 的回复。它是调查线索而不是已确认事实。继续当前调查，优先用工具"
                    "验证这条提示；不要重新从头分析，也不要因为用户这样说就直接确认根因。"
                ),
            })
        # 普通追问才使用通用 follow-up 指令。
        elif self._turns:
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
            on_progress=self._on_progress,
            goal_mode=self._goal_mode,
            starting_step=self._total_steps,
            max_steps_override=self._max_steps_per_turn,
        )

        self._total_steps += result.steps
        if intervention is not None:
            intervention = intervention.model_copy(update={
                "subsequent_tools": [event.tool_name for event in result.tool_events],
                "subsequent_skill_activations": [
                    str(event.arguments.get("name")) for event in result.tool_events
                    if event.tool_name == "activate_skill" and event.success
                ],
            })
            intervention = intervention.model_copy(update={
                "attribution": infer_attribution(
                    intervention.checkpoint,
                    intervention.subsequent_tools,
                    intervention.subsequent_skill_activations,
                ),
            })
        self._pending_checkpoint = result.human_checkpoint
        turn = ConversationTurn(
            user_message=user_message, result=result, human_intervention=intervention,
        )
        self._turns.append(turn)

        return turn

    def restore_turns(self, saved_turns: list[SavedTurn]) -> None:
        """从持久化存储恢复已完成的对话轮次。

        在首次 send() 之前调用，将历史轮次注入到消息列表和轮次记录中。
        这样后续轮次就能看到完整的上下文，包括所有历史 user/assistant 消息。

        注意：恢复后的消息列表中不包含每轮之间的 followup system prompt，
        因为那些是运行时指令，不应持久化。恢复会话时，AI 回答中包含的
        "上一轮分析了什么" 信息已经足够作为上下文。

        Args:
            saved_turns: 按时间顺序排列的已保存轮次，每轮包含 user 消息和
                         assistant 最终回答。
        """
        if self._turns:
            raise RuntimeError("已有实时轮次，不能重复恢复历史")
        for turn_data in saved_turns:
            # 构建消息历史：连续的 user -> assistant
            self._messages.append({"role": "user", "content": turn_data.user_message})
            self._messages.append({"role": "assistant", "content": turn_data.assistant_answer})
            # 构建伪 ConversationTurn（没有完整 tool_events，但保留了最终回答）
            self._turns.append(ConversationTurn(
                user_message=turn_data.user_message,
                result=AgentRunResult(
                    status=turn_data.agent_status,
                    task="",
                    final_answer=turn_data.assistant_answer,
                    steps=turn_data.steps,
                    tool_events=[],
                    human_checkpoint=(
                        HumanCheckpoint.model_validate(turn_data.human_checkpoint)
                        if turn_data.human_checkpoint else None
                    ),
                    token_usage=(
                        TokenUsage.model_validate(turn_data.token_usage)
                        if turn_data.token_usage else None
                    ),
                ),
                human_intervention=(
                    HumanIntervention.model_validate(turn_data.human_intervention)
                    if turn_data.human_intervention else None
                ),
            ))
            self._total_steps += turn_data.steps
        self._pending_checkpoint = None
        if saved_turns and saved_turns[-1].human_checkpoint:
            self._pending_checkpoint = HumanCheckpoint.model_validate(
                saved_turns[-1].human_checkpoint
            )
            restore = getattr(self._router, "restore_pending", None)
            if restore is not None:
                restore(self._pending_checkpoint)

    async def finalize(self) -> ConversationResult:
        """结束会话，返回最终结果。

        调用后会话不可再使用。
        """
        self._active = False
        if self._on_close is not None and not self._closed_resources:
            self._closed_resources = True
            result = self._on_close()
            if inspect.iscoroutine(result):
                await result
        return ConversationResult(
            turns=list(self._turns),
            total_steps=self._total_steps,
            initial_token_usage=self._initial_token_usage,
        )
