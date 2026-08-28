"""Jira MCP 的薄协议适配层；业务规则都位于 Service/Client/Exporter。"""

from __future__ import annotations

import asyncio
import json

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from .client import JiraClient
from .config import JiraConfig
from .domain import (
    CollectIssueContextInput,
    ExportIssueCaseInput,
    GetCommentsInput,
    GetIssueInput,
    ListAttachmentsInput,
    SearchIssuesInput,
)
from .errors import failure
from .exporter import CaseExporter
from .service import JiraService


server = Server("jira-bug")

TOOL_DEFINITIONS = {
    "test_connection": ("检查 Jira 网络、认证和 REST 版本；不读取业务 Issue。", None),
    "get_issue": ("读取 Jira Issue 的结构化详情；不会修改 Jira。", GetIssueInput),
    "collect_issue_context": ("聚合收集 Issue 标准字段、完整分页评论、附件元数据和白名单自定义字段。", CollectIssueContextInput),
    "search_issues": ("使用 JQL 分页搜索 Issue，Cloud cursor 与 Data Center startAt 被统一为 cursor。", SearchIssuesInput),
    "get_comments": ("分页读取 Issue 评论，并将 Cloud ADF 转为纯文本。", GetCommentsInput),
    "list_attachments": ("列出附件元数据，不下载附件，也不暴露带认证语义的 content URL。", ListAttachmentsInput),
    "export_issue_case": ("将 Issue、评论、附件及有边界的关联 Issue 证据导出为本地 Case。", ExportIssueCaseInput),
}


async def handle_list_tools(ctx, params: types.ListToolsRequest) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(
            name=name,
            description=description,
            inputSchema=model.model_json_schema() if model else {
                "type": "object", "properties": {}, "additionalProperties": False,
            },
        )
        for name, (description, model) in TOOL_DEFINITIONS.items()
    ])


async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    # 延迟初始化使 tools/list 不依赖 Jira 环境；真正调用工具时才校验配置。
    try:
        config = JiraConfig.from_environment()
        client = JiraClient(config)
        try:
            service = JiraService(client, CaseExporter(config, client))
            result = service.dispatch(params.name, params.arguments or {})
        finally:
            client.close()
    except (ValueError, TypeError) as exc:
        result = failure("CONFIG_ERROR", str(exc), False)
    except Exception as exc:
        result = failure("INTERNAL_ERROR", f"工具执行失败: {type(exc).__name__}", False)
    text = json.dumps(result.model_dump(), ensure_ascii=False, indent=2, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server.add_request_handler("tools/list", types.RequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="jira-bug",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    """console script 入口必须是普通函数，由它启动 asyncio event loop。"""

    asyncio.run(_run())


if __name__ == "__main__":
    main()
