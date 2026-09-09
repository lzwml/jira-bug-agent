"""OpenGrok MCP Server — 薄协议适配层，业务逻辑在 Client 中。"""

from __future__ import annotations

import asyncio
import json

import httpx

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from .client import OpenGrokClient, OpenGrokProjectDiscoveryError
from .config import OpenGrokConfig


server = Server("opengrok-code-search")
_shared_client: OpenGrokClient | None = None
_shared_config: OpenGrokConfig | None = None
_client_lock = asyncio.Lock()

TOOLS = {
    "opengrok_search_code": {
        "description": "Search codebase: full-text, definitions, references, path, or history. Supports file_type filtering.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query. Supports +required -excluded \"exact phrase\"."},
                "search_type": {
                    "type": "string",
                    "enum": ["full", "defs", "refs", "path", "hist"],
                    "default": "full",
                    "description": "Search type: full=full-text, defs=definitions, refs=references, path=file path, hist=history.",
                },
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by project names. For defs/refs, omit to automatically discover indexed projects when no default is configured.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10,
                },
                "start_index": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                },
                "file_type": {
                    "type": "string",
                    "description": "Filter by language: c, cxx (C++), java, python, javascript, kotlin, rust, etc.",
                },
            },
            "required": ["query"],
        },
    },
    "opengrok_find_file": {
        "description": "Find files by name or directory pattern.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path_pattern": {"type": "string", "description": "Path pattern, e.g. 'SurfaceFlinger.cpp' or '*/services/*.cpp'."},
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by project names.",
                },
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            },
            "required": ["path_pattern"],
        },
    },
    "opengrok_get_file_content": {
        "description": "Read source code. Requires project and path. Use start_line/end_line for large files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name from search results."},
                "path": {"type": "string", "description": "File path relative to project root."},
                "start_line": {"type": "integer", "minimum": 1, "description": "Start line (1-indexed, inclusive)."},
                "end_line": {"type": "integer", "minimum": 1, "description": "End line (1-indexed, inclusive)."},
            },
            "required": ["project", "path"],
        },
    },
    "opengrok_read_local_file": {
        "description": "Read a local source file previously located by OpenGrok. Only configured project roots are accessible.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "OpenGrok project name with a configured local root."},
                "path": {"type": "string", "description": "Repository-relative path returned by OpenGrok."},
                "start_line": {"type": "integer", "minimum": 1, "description": "Start line (1-indexed, inclusive)."},
                "end_line": {"type": "integer", "minimum": 1, "description": "End line (1-indexed, inclusive)."},
            },
            "required": ["project", "path"],
        },
    },
    "opengrok_get_file_history": {
        "description": "Get commit history for a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "path": {"type": "string"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "required": ["project", "path"],
        },
    },
    "opengrok_list_projects": {
        "description": "List all indexed projects/repositories.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    "opengrok_browse_directory": {
        "description": "Browse directory structure within a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "path": {"type": "string", "default": "", "description": "Directory path relative to project root. Empty for root."},
            },
            "required": ["project"],
        },
    },
    "opengrok_search_suggest": {
        "description": "Get query autocomplete suggestions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": "string", "description": "Project name (optional)."},
                "field": {"type": "string", "enum": ["full", "defs", "refs", "path"], "default": "full"},
            },
            "required": ["query"],
        },
    },
    "opengrok_get_file_annotate": {
        "description": "Get line-by-line git blame information for a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["project", "path"],
        },
    },
    "opengrok_get_file_symbols": {
        "description": "List indexed classes, functions, macros, and other symbols in a source file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "path": {"type": "string", "description": "File path relative to the project root."},
            },
            "required": ["project", "path"],
        },
    },
    "opengrok_get_symbol_context": {
        "description": "Find a symbol definition, read its surrounding code, and return references in one call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "project": {"type": "string", "description": "Optional project scope."},
                "context_lines": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            "required": ["symbol"],
        },
    },
    "opengrok_search_and_read": {
        "description": "Search code and read contextual snippets from the five most relevant files in one call.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "search_type": {"type": "string", "enum": ["full", "defs", "refs", "path", "hist"], "default": "full"},
                "projects": {"type": "array", "items": {"type": "string"}},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                "context_lines": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "file_type": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    "opengrok_what_changed": {
        "description": "Show recent line changes in a file grouped by commit. Combines file history and blame.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "path": {"type": "string"},
                "since_days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 14, "description": "Look-back window in days."},
            },
            "required": ["project", "path"],
        },
    },
    "opengrok_index_health": {
        "description": "Check OpenGrok server connectivity and latency.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
}


async def handle_list_tools(ctx, params: types.ListToolsRequest) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, **meta)
        for name, meta in TOOLS.items()
    ])


async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        config = OpenGrokConfig.from_environment()
    except ValueError as exc:
        result = _fail("CONFIG_ERROR", str(exc), False)
    else:
        try:
            client = await _get_shared_client(config)
            result = await _dispatch(client, params.name, params.arguments or {})
        except (KeyError, TypeError, ValueError) as exc:
            result = _fail("INVALID_ARGUMENT", str(exc), False)
        except OpenGrokProjectDiscoveryError as exc:
            result = _fail("PROJECT_DISCOVERY_FAILED", str(exc), False)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            result = _fail(
                f"HTTP_{status}",
                f"OpenGrok 返回 HTTP {status}",
                status == 429 or status >= 500,
            )
        except httpx.HTTPError as exc:
            result = _fail("NETWORK_ERROR", str(exc), True)
        except Exception as exc:
            result = _fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", False)
    text = json.dumps(result, ensure_ascii=False, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


async def _dispatch(client: OpenGrokClient, name: str, args: dict) -> dict:
    if name == "opengrok_search_code":
        return _ok(await client.search(
            query=args["query"],
            search_type=args.get("search_type", "full"),
            projects=args.get("projects"),
            max_results=args.get("max_results", 10),
            start=args.get("start_index", 0),
            file_type=args.get("file_type"),
        ))
    if name == "opengrok_find_file":
        return _ok(await client.search(
            query=args["path_pattern"],
            search_type="path",
            projects=args.get("projects"),
            max_results=args.get("max_results", 10),
        ))
    if name == "opengrok_get_file_content":
        return _ok(await client.get_file_content(
            project=args["project"],
            path=args["path"],
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        ))
    if name == "opengrok_read_local_file":
        return _ok(await client.get_local_file_content(
            project=args["project"],
            path=args["path"],
            start_line=args.get("start_line"),
            end_line=args.get("end_line"),
        ))
    if name == "opengrok_get_file_history":
        return _ok(await client.get_file_history(
            project=args["project"],
            path=args["path"],
            max_entries=args.get("max_entries", 10),
        ))
    if name == "opengrok_list_projects":
        return _ok(await client.list_projects())
    if name == "opengrok_browse_directory":
        return _ok(await client.browse_directory(
            project=args["project"],
            path=args.get("path", ""),
        ))
    if name == "opengrok_search_suggest":
        return _ok(await client.suggest(
            query=args["query"],
            project=args.get("project"),
            field=args.get("field", "full"),
        ))
    if name == "opengrok_get_file_annotate":
        return _ok(await client.get_annotate(
            project=args["project"],
            path=args["path"],
        ))
    if name == "opengrok_get_file_symbols":
        return _ok(await client.get_file_symbols(
            project=args["project"],
            path=args["path"],
        ))
    if name == "opengrok_get_symbol_context":
        return _ok(await client.get_symbol_context(
            symbol=args["symbol"],
            project=args.get("project"),
            context_lines=args.get("context_lines", 30),
        ))
    if name == "opengrok_search_and_read":
        return _ok(await client.search_and_read(
            query=args["query"],
            search_type=args.get("search_type", "full"),
            projects=args.get("projects"),
            max_results=args.get("max_results", 10),
            context_lines=args.get("context_lines", 20),
            file_type=args.get("file_type"),
        ))
    if name == "opengrok_what_changed":
        return _ok(await client.what_changed(
            project=args["project"],
            path=args["path"],
            since_days=args.get("since_days", 14),
        ))
    if name == "opengrok_index_health":
        import time
        start = time.monotonic()
        connected = await client.test_connection()
        return _ok({
            "connected": connected,
            "latencyMs": round((time.monotonic() - start) * 1000),
            "baseUrl": client._base,
        })
    return _fail("UNKNOWN_TOOL", f"未注册工具: {name}", False)


def _ok(data: object) -> dict:
    return {"success": True, "data": data, "error_code": None, "error_message": None, "retryable": False}


def _fail(code: str, message: str, retryable: bool) -> dict:
    return {"success": False, "data": None, "error_code": code, "error_message": message, "retryable": retryable}


async def _get_shared_client(config: OpenGrokConfig) -> OpenGrokClient:
    """在 MCP 进程中复用连接池、缓存和并发控制；配置变更时安全替换。"""
    global _shared_client, _shared_config
    async with _client_lock:
        if _shared_client is None or _shared_config != config:
            if _shared_client is not None:
                await _shared_client.close()
            _shared_client = OpenGrokClient(config)
            _shared_config = config
        return _shared_client


server.add_request_handler("tools/list", types.RequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)


async def _run() -> None:
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="opengrok-code-search",
                    server_version="0.1.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        if _shared_client is not None:
            await _shared_client.close()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
