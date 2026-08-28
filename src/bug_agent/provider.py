"""最小 OpenAI-compatible Chat Completions Provider。

【学习要点】这个文件演示了 Agent 架构中"Provider 适配层"的职责：
把"调用一次大模型"抽象成一个与厂商无关的接口。Agent Loop 只依赖
``complete(messages, tools) -> message`` 这一个方法，因此换 DeepSeek、
换企业网关、换自建 vLLM，都只需要改这个文件，Agent 逻辑零改动。

这就是架构文档里说的"外部系统差异放在 Provider Adapter"的具体实现。
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import AgentConfig


class ProviderError(RuntimeError):
    """Provider 专用异常，额外携带 retryable 标志。

    【学习要点】为什么不直接用 HTTP 状态码或裸异常？
    因为 Agent Loop 需要做一个策略决策："这次失败值不值得重试？"
    网络抖动(连不上)、限流(429)、服务端崩溃(5xx) → retryable=True，可重试；
    认证失败(401/403)、请求非法(4xx) → retryable=False，重试也是浪费。
    把这个判断封装在异常里，调用方就不用关心 HTTP 细节了。
    """

    def __init__(self, message: str, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


class OpenAICompatibleProvider:
    """兼容 DeepSeek、OpenAI-compatible Gateway 与多数自建模型服务。

    【学习要点】"OpenAI-compatible" 是一个事实标准：只要服务端实现了
    ``POST /v1/chat/completions`` 且接受 ``messages``/``tools``/``tool_choice``
    参数、返回 ``choices[0].message`` 结构，就属于兼容。这个 Provider 因此
    可以对接几十种模型服务，而不用为每个厂商写一个适配器。
    """

    def __init__(self, config: AgentConfig, transport: httpx.AsyncBaseTransport | None = None):
        """初始化 Provider。

        【学习要点】``transport`` 参数是"依赖注入"的测试钩子：
        - 生产环境不传，httpx 走真实网络；
        - 单元测试传入 ``httpx.MockTransport(handler)``，handler 是一个
          拦截请求的函数，直接返回伪造的 Response，全程不发真实网络包。
        这样 Provider 的测试既快又不需要真实模型服务。见 tests/test_provider.py。
        """
        self.config = config
        # 长连接复用的 AsyncClient：连接池、超时、默认 Header 都在这里配置一次，
        # 之后每次 complete() 复用同一个 client，避免每次请求重建 TCP/TLS 连接。
        self.client = httpx.AsyncClient(
            timeout=config.llm_timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {config.llm_api_key}"},
        )

    async def close(self) -> None:
        """显式关闭底层连接池。

        【学习要点】Worker 在 ``finally`` 块里调用这个方法，保证即使分析
        中途出错，HTTP 连接也会被释放，不会泄漏文件描述符。
        """
        await self.client.aclose()

    async def complete(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        """执行一次对话补全，返回助手的 message 对象。

        【学习要点】这是 Agent 循环每一步的核心调用。参数设计要点：
        - ``messages``: 完整对话历史(含 system prompt、工具调用结果)，
          模型是无状态的，每次都要把上下文全量发过去；
        - ``tools``: 本次可用的工具定义(JSON Schema)，让模型知道能调什么；
        - 返回值是 ``choices[0].message``，可能是纯文本回答，
          也可能带 ``tool_calls`` 字段表示模型想调用工具。

        【学习要点】几个关键参数的含义：
        - ``tool_choice="auto"``: 让模型自己决定是回答还是调用工具，
          而不是强制每一步都必须调工具；
        - ``temperature=0.1``: 接近确定性输出。Bug 分析是严肃任务，
          需要稳定、可复现的结论，不希望模型"发挥创意"。
        """
        try:
            response = await self.client.post(
                f"{self.config.llm_base_url}/chat/completions",
                json={
                    "model": self.config.llm_model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "temperature": 0.1,
                },
            )
        except httpx.TransportError as exc:
            # 网络层错误(DNS失败/连接拒绝/超时)：还没收到 HTTP 响应，
            # 属于临时性故障，标记为可重试。
            raise ProviderError(f"模型服务连接失败: {type(exc).__name__}", True) from exc

        # 【学习要点】错误分类是 Provider 层的核心职责之一。
        # 把 HTTP 状态码翻译成"是否可重试"的语义，供上层做策略决策。
        if response.status_code in {401, 403}:
            # 认证/权限失败：重试不会改变结果，反而可能触发账号锁定。
            raise ProviderError("模型服务认证或权限失败", False)
        if response.status_code == 429 or response.status_code >= 500:
            # 429=限流，5xx=服务端故障：都是暂时性的，稍后重试可能成功。
            raise ProviderError(f"模型服务暂时不可用: HTTP {response.status_code}", True)
        if not response.is_success:
            # 其他 4xx(如 400 请求格式错误)：是调用方的问题，重试无意义。
            raise ProviderError(f"模型服务拒绝请求: HTTP {response.status_code}", False)

        try:
            payload = response.json()
            # 只取第一个 choice 的 message；多 choice 场景 Agent 用不上。
            return payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            # 响应 JSON 结构不符合预期：可能是网关或代理篡改了响应，
            # 标记为可重试(也许下次正常)，但同样也可能是持续性故障。
            raise ProviderError("模型服务返回了无效响应", True) from exc

