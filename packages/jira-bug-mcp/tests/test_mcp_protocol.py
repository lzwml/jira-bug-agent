"""真正通过 stdio 握手，防止领域测试通过但 MCP 协议层不可用。"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.anyio
async def test_stdio_initialize_and_list_tools():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "jira_bug_mcp.server"],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    assert {tool.name for tool in tools.tools} == {
        "test_connection", "get_issue", "collect_issue_context",
        "search_issues", "get_comments",
        "list_attachments", "export_issue_case",
    }
