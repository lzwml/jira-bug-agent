"""多个 MCP Server 的生命周期管理和 Tool 路由。"""

from __future__ import annotations

from contextlib import AsyncExitStack
import json
import os
import sys
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpToolRouter:
    def __init__(self):
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}
        self._tool_routes: dict[str, str] = {}
        self._tools: list[dict[str, Any]] = []

    async def __aenter__(self) -> "McpToolRouter":
        await self._stack.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._stack.__aexit__(exc_type, exc, tb)

    async def connect_python_server(
        self,
        server_name: str,
        module: str,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        """启动一个 Python stdio MCP，并把发现的工具注册到统一命名空间。"""

        if server_name in self._sessions:
            raise ValueError(f"MCP Server 已连接: {server_name}")
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", module],
            env={**os.environ, **(env_overrides or {})},
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        discovered = await session.list_tools()
        for tool in discovered.tools:
            if tool.name in self._tool_routes:
                owner = self._tool_routes[tool.name]
                raise ValueError(f"工具名冲突: {tool.name} 同时来自 {owner} 和 {server_name}")
            self._tool_routes[tool.name] = server_name
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
        return list(self._tools)

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        server_name = self._tool_routes.get(name)
        if server_name is None:
            return json.dumps({
                "success": False,
                "error_code": "UNKNOWN_TOOL",
                "error_message": f"未注册工具: {name}",
                "retryable": False,
            }, ensure_ascii=False)
        result = await self._sessions[server_name].call_tool(name, arguments=arguments)
        texts = [getattr(content, "text", "") for content in result.content]
        return "\n".join(text for text in texts if text)

