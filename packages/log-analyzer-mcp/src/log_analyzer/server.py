"""log-analyzer-mcp V2 的 MCP 协议适配层。

使用 LOG_ANALYZER_ALLOWED_ROOTS 配置允许读取的根目录。多个目录使用操作
系统 path separator 分隔（Windows 为分号，Linux/macOS 为冒号）。未配置时
仅允许当前工作目录。

本文件只负责三件事：
1. 把领域服务声明成 MCP Tools；
2. 把 MCP tools/call 转发给 LogAnalyzerService；
3. 把 ToolResult 序列化为 MCP TextContent。

文件扫描和诊断规则不应写在协议层中。
"""

from __future__ import annotations

import asyncio
import json

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from .case_registry import CaseRegistry
from .domain import (
    ExtractTimelineInput,
    InspectCaseInput,
    OpenCaseInput,
    ParseDiagnosticsInput,
    SearchEvidenceInput,
)
from .errors import make_error
from .service import LogAnalyzerService


# Server 与 Service 生命周期相同，因此 Registry 中已注册的 Case 可以在同一
# 个 MCP 会话的多轮 Tool Call 之间复用。
server = Server("log-analyzer")
service = LogAnalyzerService(CaseRegistry.from_environment())

# 每个工具绑定一个 Input Model。tools/list 时直接调用 model_json_schema()，
# 让 Pydantic 成为参数约束的单一事实来源，避免手写 JSON Schema 与代码漂移。
TOOL_DEFINITIONS = {
    "open_case": (
        "在服务端允许根目录内注册一个 Bug 案例目录，返回 case_id 和 Artifact 清单。后续工具只使用 ID，不接收裸路径。",
        OpenCaseInput,
    ),
    "inspect_case": (
        "查看已注册 Case 的附件类型、大小和样本清单，用于制定分析计划。",
        InspectCaseInput,
    ),
    "search_evidence": (
        "在 Case 文本附件中执行安全的字面量搜索，返回带前后文和稳定引用的 Evidence；零匹配仍是成功结果。",
        SearchEvidenceInput,
    ),
    "extract_timeline": (
        "从 Android、Wall Clock 和 Kernel monotonic 日志中提取指定锚点事件并形成跨文件时间线。",
        ExtractTimelineInput,
    ),
    "parse_diagnostics": (
        "从 Case 中提取 SELinux AVC、Kernel Call Trace、Fatal 和 ANR 等结构化诊断发现。",
        ParseDiagnosticsInput,
    ),
}


async def handle_list_tools(ctx, params: types.ListToolsRequest) -> types.ListToolsResult:
    """响应 MCP tools/list，向 Client 公布当前五个领域工具。"""

    return types.ListToolsResult(
        tools=[
            types.Tool(
                name=name,
                description=description,
                inputSchema=input_model.model_json_schema(),
            )
            for name, (description, input_model) in TOOL_DEFINITIONS.items()
        ]
    )


async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    """响应 MCP tools/call，并维持统一的 JSON ToolResult 外壳。"""

    try:
        result = service.dispatch(params.name, params.arguments or {})
    except Exception as exc:  # MCP 边界兜底；领域错误应由 service 转为 ToolResult。
        result = make_error(
            "INTERNAL_ERROR",
            f"工具执行失败: {type(exc).__name__}",
            retryable=False,
        )
    text = json.dumps(result.model_dump(), ensure_ascii=False, indent=2, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


# add_request_handler 是 MCP SDK 的协议注册点；params 类型由 SDK 在进入
# handler 前完成第一层协议校验。
server.add_request_handler("tools/list", types.RequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)


async def main():
    """以 stdio transport 运行 Server，由 Codex、DSH 或手写 Client 拉起。"""

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="log-analyzer",
                server_version="0.2.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
