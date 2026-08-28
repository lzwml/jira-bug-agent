"""多个 MCP Server 的生命周期管理和 Tool 路由。

【学习要点】这个文件演示了 MCP(Model Context Protocol) 的客户端实现：
如何启动多个 MCP Server 子进程、发现它们提供的工具、并把工具调用路由到正确的 Server。

核心概念：
1. **MCP 是一种协议**，不是库：它定义了 Client 和 Server 之间如何通过 JSON-RPC 通信；
2. **stdio 传输**：Server 作为子进程运行，通过标准输入/输出交换消息；
3. **工具发现**：连接后 Client 调用 `tools/list` 获取 Server 提供的所有工具定义；
4. **工具路由**：多个 Server 可能提供不同的工具，Router 维护"工具名 → Server"的映射。

为什么需要 Router 而不是直接调用 MCP？
- Agent 只看到一个统一的工具列表，不需要关心工具来自哪个 Server；
- 可以同时连接 Jira MCP、Log MCP、未来还会有 Code Search MCP 等；
- 工具名冲突能在启动时被发现，而不是等到运行时才报错。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
import json
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpToolRouter:
    """MCP 工具路由器：管理多个 MCP Server 的连接和工具分发。

    【学习要点】这个类实现了 ToolRouter 协议(见 agent.py)，
    向 Agent 提供统一的工具接口，隐藏了底层多个 MCP Server 的复杂性。

    职责：
    1. 启动/停止 MCP Server 子进程；
    2. 发现每个 Server 的工具并合并成统一列表；
    3. 把工具调用路由到正确的 Server；
    4. 检测工具名冲突(两个 Server 提供同名工具)。
    """

    def __init__(self):
        # 【学习要点】AsyncExitStack 是异步上下文管理器的"堆栈"：
        # 可以动态注册多个需要清理的资源(这里是 MCP 子进程和会话)，
        # 在退出时按照 LIFO(后进先出)顺序自动清理。
        # 好处：即使中途某个 Server 连接失败，已建立的连接也会被正确关闭。
        self._stack = AsyncExitStack()

        # server_name → ClientSession：活跃的 MCP 会话
        self._sessions: dict[str, ClientSession] = {}

        # tool_name → server_name：工具路由表，记录每个工具属于哪个 Server
        self._tool_routes: dict[str, str] = {}

        # 合并后的工具定义列表(OpenAI 格式)，提供给 Agent
        self._tools: list[dict[str, Any]] = []

    async def __aenter__(self) -> "McpToolRouter":
        """进入异步上下文，通常配合 `async with` 使用。

        【学习要点】Worker 中的典型用法：
        ```python
        async with McpToolRouter() as router:
            await router.connect_python_server(...)
            result = await agent.run(task, prompt, router)
        # 退出时自动关闭所有 MCP Server 子进程
        ```
        """
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """退出异步上下文，清理所有已注册的资源。

        【学习要点】AsyncExitStack 会按照注册的反序清理：
        1. 关闭所有 ClientSession(发送 MCP 关闭消息)；
        2. 终止所有子进程(关闭 stdin/stdout 管道)。
        即使发生异常，清理也会执行，避免僵尸进程。
        """
        await self._stack.__aexit__(exc_type, exc, tb)

    async def connect_python_server(
        self,
        server_name: str,
        module: str,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        """启动一个 Python stdio MCP，并把发现的工具注册到统一命名空间。

        【学习要点】参数说明：
        - server_name: 给这个 Server 起的名字(如 "jira"、"log")，用于路由和错误提示；
        - module: Python 模块路径(如 "jira_bug_mcp.server")，相当于 `python -m module`；
        - env_overrides: 要传递给子进程的环境变量(如 LOG_ANALYZER_ALLOWED_ROOTS)。

        【学习要点】为什么用子进程而不是直接 import？
        1. **隔离**：MCP Server 的崩溃不会影响 Agent 主进程；
        2. **环境隔离**：每个 Server 可以有自己的环境变量、工作目录；
        3. **语言无关**：理论上 MCP Server 可以用任何语言实现(这里恰好是 Python)。
        """

        if server_name in self._sessions:
            raise ValueError(f"MCP Server 已连接: {server_name}")

        # 【学习要点】StdioServerParameters 描述如何启动子进程：
        # - command=sys.executable：使用当前 Python 解释器(虚拟环境中)；
        # - args=["-m", module]：相当于 `python -m module`，会执行模块的 __main__；
        # - env：合并当前环境变量和覆盖变量，让子进程继承配置。
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", module],
            env={**os.environ, **(env_overrides or {})},
        )

        # 【学习要点】stdio_client 是一个异步上下文管理器：
        # 1. 启动子进程；
        # 2. 创建 stdin/stdout 管道；
        # 3. 返回 (read_stream, write_stream) 用于收发消息。
        # 通过 `self._stack.enter_async_context` 注册到 ExitStack，
        # 确保 Router 退出时子进程会被终止。
        read, write = await self._stack.enter_async_context(stdio_client(params))

        # 【学习要点】ClientSession 是 MCP 协议的客户端会话：
        # 封装了 JSON-RPC 消息的发送/接收、请求/响应匹配、错误处理。
        # 它把底层的"管道字节流"抽象成"调用方法、等待结果"的接口。
        session = await self._stack.enter_async_context(ClientSession(read, write))

        # 【学习要点】MCP 握手流程：
        # 1. Client 发送 initialize 请求(包含协议版本、Client 信息)；
        # 2. Server 响应 initialize(包含 Server 信息、支持的能力)；
        # 3. Client 发送 initialized 通知，握手完成。
        await session.initialize()

        # 【学习要点】工具发现：向 Server 查询它提供哪些工具。
        # 返回的每个 tool 包含：name、description、inputSchema(JSON Schema)。
        discovered = await session.list_tools()

        # 把发现的工具注册到路由表和统一工具列表。
        for tool in discovered.tools:
            # 【学习要点】工具名冲突检测：
            # 如果两个 Server 都提供了 "search" 工具，Agent 就不知道该调哪个。
            # 在启动时发现问题，比运行时才报错要好得多。
            if tool.name in self._tool_routes:
                owner = self._tool_routes[tool.name]
                raise ValueError(f"工具名冲突: {tool.name} 同时来自 {owner} 和 {server_name}")

            self._tool_routes[tool.name] = server_name

            # 【学习要点】把 MCP 工具定义转换成 OpenAI Function Calling 格式。
            # MCP 和 OpenAI 的工具描述格式非常相似，但字段名略有不同：
            # - MCP: tool.inputSchema
            # - OpenAI: function.parameters
            self._tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            })

        self._sessions[server_name] = session

    def openai_tools(self) -> list[dict[str, Any]]:
        """返回所有已发现工具的 OpenAI 格式定义列表。

        【学习要点】这个方法会被 Agent Loop 调用，
        返回的列表会直接作为 `tools` 参数传给模型的 chat/completions API。
        模型会根据这些定义决定调用哪个工具、传什么参数。
        """
        return list(self._tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """执行工具调用，返回 JSON 字符串结果。

        【学习要点】Agent Loop 调用这个方法的时机：
        模型在响应中返回 tool_calls，Agent 解析出工具名和参数，
        然后通过这个方法实际执行。

        路由逻辑：根据工具名查找它属于哪个 Server，然后转发调用。
        """
        server_name = self._tool_routes.get(name)
        if server_name is None:
            # 工具不存在：返回标准错误格式，让模型知道它幻觉了一个工具。
            # 【学习要点】这种情况通常发生在：
            # 1. 模型混淆了工具名(尤其是多个工具功能相似时)；
            # 2. System Prompt 中提到了某个工具，但 MCP Server 没有提供。
            return json.dumps({
                "success": False,
                "error_code": "UNKNOWN_TOOL",
                "error_message": f"未注册工具: {name}",
                "retryable": False,
            }, ensure_ascii=False)

        # 【学习要点】MCP 工具调用的返回格式：
        # result.content 是一个列表，可能包含多种类型的内容(文本、图像、资源等)。
        # 当前项目中的 MCP Server 都只返回文本，所以简单地提取 text 字段。
        result = await self._sessions[server_name].call_tool(name, arguments=arguments)
        texts = [getattr(content, "text", "") for content in result.content]

        # 【学习要点】合并多个文本块：
        # 某些 MCP Server 可能把长结果分成多个 TextContent 返回，
        # 这里用换行符合并它们，形成一个完整的 JSON 字符串。
        # Agent 期望的是 JSON 格式，所以 MCP Server 通常会返回序列化后的 JSON。
        return "\n".join(text for text in texts if text)

