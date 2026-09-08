"""log-analyzer-mcp V2 的 MCP 协议适配层。

【学习要点】这个文件演示了如何编写一个 MCP Server：
把领域服务(这里就是日志分析能力)通过 MCP 协议暴露给 Agent。

分层架构：
```
MCP Client (Agent)
    ↓ JSON-RPC over stdio
本文件 (server.py) — 协议适配层
    ↓ Python 函数调用
LogAnalyzerService — 领域服务层
    ↓ 文件 I/O、正则解析
CaseRegistry / log_analysis_core — 核心实现层
```

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
    BuildIndexInput,
    ExtractAeeDbInput,
    ExtractArchiveMembersInput,
    ExtractTimelineInput,
    GetCaseCommentInput,
    InspectArchiveInput,
    InspectCaseInput,
    OpenCaseInput,
    ParseDiagnosticsInput,
    PrepareCaseInput,
    ProbeArchiveMembersInput,
    SearchEvidenceInput,
)
from .errors import make_error
from .service import LogAnalyzerService


# Server 与 Service 生命周期相同，因此 Registry 中已注册的 Case 可以在同一
# 个 MCP 会话的多轮 Tool Call 之间复用。
# 【学习要点】Server 和 Service 是全局单例：
# - MCP Server 在 stdio 上持续运行，处理多个 Client 请求；
# - Service 持有 CaseRegistry，维护所有已注册的 Case 状态；
# - 这使得 Agent 可以先调用 open_case 注册 Case，然后在后续多轮工具调用中
#   通过 case_id 引用同一个 Case，而不需要每次重新打开。
server = Server("log-analyzer")
service = LogAnalyzerService(CaseRegistry.from_environment())

# 每个工具绑定一个 Input Model。tools/list 时直接调用 model_json_schema()，
# 让 Pydantic 成为参数约束的单一事实来源，避免手写 JSON Schema 与代码漂移。
#
# 【学习要点】TOOL_DEFINITIONS 的设计模式：
# - key: 工具名(Agent 调用时使用)
# - value: (描述, Pydantic Input Model)
#
# 为什么用 Pydantic Model 而不是手写 JSON Schema？
# 1. 类型安全：Pydantic 自动验证参数类型和约束；
# 2. 文档即代码：Field(description=...) 会自动出现在 Schema 中；
# 3. 单一事实来源：修改 Model 字段，Schema 自动更新，不会忘记同步。
#
# 【学习要点】工具描述是写给模型看的：
# Agent 会根据 description 决定何时调用这个工具，所以描述要清晰说明：
# - 这个工具做什么
# - 什么时候应该用(什么时候不应该用)
# - 输入输出的关键约束(如"后续工具只使用 ID，不接收裸路径")
TOOL_DEFINITIONS = {
    "open_case": (
        "在服务端允许根目录内注册一个 Bug 案例目录，返回 case_id 和 Artifact 清单。后续工具只使用 ID，不接收裸路径。",
        OpenCaseInput,
    ),
    "inspect_case": (
        "查看已注册 Case 的附件类型、大小和样本清单；发现未解码 aee_db 时返回 required_skill_activations，要求激活 aee-db-extract。",
        InspectCaseInput,
    ),
    "prepare_case": (
        "显式全量准备或最终兜底：按预算递归展开 ZIP/TAR/GZIP 并建立索引；Agent 默认应先使用 inspect_archive 和选择性工具。",
        PrepareCaseInput,
    ),
    "inspect_archive": (
        "只读检查归档成员清单，不解压、不将成员内容落盘；返回通用路径时间、目录摘要和稳定 member_id，发现 aee_db 成员时返回 required_skill_activations。",
        InspectArchiveInput,
    ),
    "probe_archive_members": (
        "不落盘、不展开整个归档（包括 7z），只读取指定成员的前缀内容样本。调用时应传入已知的 incident_time_range；返回调查目标时间窗，以及每个成员独立观测到的内容时间范围、boot 身份、诊断锚点和覆盖置信度，便于人工确认所选 boot round 是否真的覆盖事故。",
        ProbeArchiveMembersInput,
    ),
    "extract_archive_members": (
        "使用 inspect_archive 或 probe_archive_members 返回的稳定 member_id 选择性解压归档成员，避免默认全量展开。",
        ExtractArchiveMembersInput,
    ),
    "extract_aee_db": (
        "使用服务端受控的 aee_extract.exe 解码已注册的 aee_db Artifact。如 .dbg 位于归档内，必须先用 extract_archive_members 解压，再传入其返回的 members[].artifact_id；不能直接传 member_id。",
        ExtractAeeDbInput,
    ),
    "build_index": (
        "把指定 artifact_id 增量加入 Case 的持久化分块索引集合，用于逐轮扩大日志范围。",
        BuildIndexInput,
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
    "get_case_comment": (
        "按 comment_id 从已注册 Jira Case 的 issue.json 读取一条评论原文；offset/limit 用于字符分页，不接收裸文件路径。",
        GetCaseCommentInput,
    ),
}


async def handle_list_tools(ctx, params: types.ListToolsRequest) -> types.ListToolsResult:
    """响应 MCP tools/list，向 Client 公布当前领域工具。

    【学习要点】MCP 协议的工具发现流程：
    1. Client 连接后发送 tools/list 请求；
    2. Server 返回所有可用工具的定义(name、description、inputSchema)；
    3. Client 把这些定义转换成模型能理解的格式(如 OpenAI Function Calling)。

    inputSchema 由 Pydantic Model 自动生成，包含：
    - 字段类型(string、int、list 等)
    - 约束(min_length、max_length、ge、le 等)
    - 描述(来自 Field(description=...))

    模型会根据这些信息生成正确的工具调用参数。
    """

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
    """响应 MCP tools/call，并维持统一的 JSON ToolResult 外壳。

    【学习要点】这是 MCP Server 的核心入口，处理流程：
    1. 从 params 中提取工具名和参数；
    2. 调用 service.dispatch() 路由到对应的领域方法；
    3. 把返回的 ToolResult 序列化成 JSON 字符串；
    4. 包装成 MCP TextContent 返回给 Client。

    【学习要点】为什么需要 try/except 兜底？
    - service.dispatch() 已经捕获了大部分可预期错误(参数验证、文件不存在等)；
    - 但仍可能有未预料的 bug(如代码逻辑错误、资源耗尽)；
    - MCP 边界是最后一道防线：把异常转换成标准 ToolResult，
      避免异常传播到 MCP 协议层导致连接中断。

    【学习要点】ToolResult 是统一的结果格式：
    {
      "success": true/false,
      "data": {...},           // 成功时的业务数据
      "error_code": "...",     // 失败时的错误码
      "error_message": "...",  // 人类可读的错误描述
      "retryable": true/false  // 是否值得重试
    }
    Agent 根据 success 判断结果，根据 retryable 决定是否重试。
    """

    try:
        result = service.dispatch(params.name, params.arguments or {})
    except Exception as exc:  # MCP 边界兜底；领域错误应由 service 转为 ToolResult。
        result = make_error(
            "INTERNAL_ERROR",
            f"工具执行失败: {type(exc).__name__}",
            retryable=False,
        )

    # 【学习要点】序列化为 JSON 字符串：
    # - model_dump(): Pydantic 模型转字典；
    # - ensure_ascii=False: 保留中文等非 ASCII 字符(不转义成 \\uXXXX)；
    # - indent=2: 格式化输出，便于调试和日志查看；
    # - default=str: 处理无法序列化的类型(如 datetime、Path)。
    text = json.dumps(result.model_dump(), ensure_ascii=False, indent=2, default=str)

    # 【学习要点】MCP 响应格式：
    # content 是一个列表，可以包含多种类型的内容(文本、图像、资源等)。
    # 当前项目只使用 TextContent，把所有结果序列化成 JSON 字符串。
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


# add_request_handler 是 MCP SDK 的协议注册点；params 类型由 SDK 在进入
# handler 前完成第一层协议校验。
#
# 【学习要点】MCP 协议的核心消息类型：
# - tools/list: Client 查询 Server 提供哪些工具；
# - tools/call: Client 调用指定工具；
# - 其他还有 resources/list、prompts/get 等(本项目未使用)。
server.add_request_handler("tools/list", types.RequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)


async def main():
    """以 stdio transport 运行 Server，由 Codex、DSH 或手写 Client 拉起。

    【学习要点】MCP stdio 传输的工作原理：
    1. Client(如 McpToolRouter)启动本进程作为子进程；
    2. Client 通过子进程的 stdin 发送 JSON-RPC 请求；
    3. Server 处理请求后，通过 stdout 返回 JSON-RPC 响应；
    4. stderr 用于日志和调试信息(不会影响协议通信)。

    【学习要点】为什么用 stdio 而不是 HTTP？
    - 简单：不需要端口管理、防火墙配置；
    - 安全：子进程天然隔离，无法被外部访问；
    - 适合工具场景：Agent 需要工具时启动，用完即销毁。
    """

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="log-analyzer",
                server_version="0.3.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    # 【学习要点】python -m log_analyzer.server 启动入口
    asyncio.run(main())
