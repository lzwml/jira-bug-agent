"""Local code MCP Server — 从本地文件系统读取 OpenGrok 定位到的源码。

OpenGrok 负责搜索定位（找到代码在哪），本 Server 负责精确读取（完整源码 + git 信息）。
两个 Server 配合使用：先 opengrok_search_code → 再 locode_read_file。

【学习要点】"先搜索定位，再本地精确读取" 是代码搜索的正确模式：
- OpenGrok 有索引，搜索快，但返回的代码片段有长度限制且无 git 信息
- 本地文件系统有完整源码 + git blame/log，但无法全文搜索
- 两者互补：OpenGrok 做 "where"，本地做 "what"
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

from .config import CodeLocalConfig

server = Server("code-local")

TOOLS = {
    "locode_read_file": {
        "description": "Read local source file content with optional line range. Use after OpenGrok search to get the full file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name from OpenGrok (e.g. 'android_b')."},
                "path": {"type": "string", "description": "File path relative to project root, from OpenGrok search results."},
                "start_line": {"type": "integer", "minimum": 1, "description": "Start line (1-indexed, inclusive)."},
                "end_line": {"type": "integer", "minimum": 1, "description": "End line (1-indexed, inclusive)."},
            },
            "required": ["project", "path"],
        },
    },
    "locode_get_history": {
        "description": "Get git log for a local file. Shows recent commits, authors, and messages.",
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
    "locode_get_blame": {
        "description": "Get line-by-line git blame for a local file. Shows who last modified each line.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["project", "path"],
        },
    },
    "locode_list_roots": {
        "description": "List configured local source roots and their OpenGrok project mappings.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
}


async def handle_list_tools(ctx, params: types.ListToolsRequest) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[
        types.Tool(name=name, **meta) for name, meta in TOOLS.items()
    ])


async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        config = CodeLocalConfig.from_environment()
        result = await _dispatch(config, params.name, params.arguments or {})
    except (ValueError, FileNotFoundError, OSError) as exc:
        result = _fail("FILE_ERROR", str(exc), False)
    except Exception as exc:
        result = _fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", False)
    text = json.dumps(result, ensure_ascii=False, default=str)
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


async def _dispatch(config: CodeLocalConfig, name: str, args: dict) -> dict:
    if name == "locode_read_file":
        local_path = config.resolve(args["project"], args["path"])
        if not local_path.is_file():
            raise FileNotFoundError(f"文件不存在: {local_path}")
        content = local_path.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        total = len(lines) if content else 0
        s = max(0, (args.get("start_line") or 1) - 1)
        e = args.get("end_line") or len(lines)
        selected = "\n".join(lines[s:e])
        return _ok({
            "project": args["project"],
            "path": args["path"],
            "localPath": str(local_path),
            "content": selected,
            "lineCount": total,
            "sizeBytes": local_path.stat().st_size,
            "startLine": args.get("start_line", 1),
            "endLine": args.get("end_line", total),
        })

    if name == "locode_get_history":
        local_path = config.resolve(args["project"], args["path"])
        if not local_path.is_file():
            raise FileNotFoundError(f"文件不存在: {local_path}")
        max_entries = args.get("max_entries", 10)
        entries = await _git_log(local_path, max_entries)
        return _ok({
            "project": args["project"],
            "path": args["path"],
            "localPath": str(local_path),
            "entries": entries,
        })

    if name == "locode_get_blame":
        local_path = config.resolve(args["project"], args["path"])
        if not local_path.is_file():
            raise FileNotFoundError(f"文件不存在: {local_path}")
        start = args.get("start_line")
        end = args.get("end_line")
        lines = await _git_blame(local_path, start, end)
        return _ok({
            "project": args["project"],
            "path": args["path"],
            "localPath": str(local_path),
            "lines": lines,
        })

    if name == "locode_list_roots":
        roots = {
            project: str(root)
            for project, root in config.roots.items()
        }
        return _ok({"roots": roots})

    return _fail("UNKNOWN_TOOL", f"未注册工具: {name}", False)


# ======================================================================
# Git 操作
# ======================================================================

async def _git_log(filepath: Path, max_entries: int) -> list[dict]:
    """执行 git log 获取文件提交历史。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "log",
            f"--max-count={max_entries}",
            "--format=%H%x00%aI%x00%an%x00%s",
            "--", str(filepath),
            cwd=str(filepath.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []
        entries = []
        for line in stdout.decode("utf-8", errors="replace").strip().split("\n"):
            if not line:
                continue
            parts = line.split("\x00")
            if len(parts) >= 4:
                entries.append({
                    "revision": parts[0],
                    "date": parts[1],
                    "author": parts[2],
                    "message": parts[3],
                })
        return entries
    except Exception:
        return []


async def _git_blame(
    filepath: Path,
    start_line: int | None = None,
    end_line: int | None = None,
) -> list[dict]:
    """执行 git blame 获取逐行提交信息。"""
    try:
        args = ["git", "blame", "--line-porcelain"]
        if start_line and end_line:
            args.extend(["-L", f"{start_line},{end_line}"])
        elif start_line:
            args.extend(["-L", f"{start_line},"])
        args.extend(["--", str(filepath)])

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(filepath.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []

        return _parse_git_blame_porcelain(stdout.decode("utf-8", errors="replace"))
    except Exception:
        return []


def _parse_git_blame_porcelain(output: str) -> list[dict]:
    """解析 git blame --line-porcelain 输出。"""
    lines: list[dict] = []
    current: dict = {}
    for line in output.split("\n"):
        if line.startswith("\t"):
            # 实际代码行
            current["content"] = line[1:]
            lines.append(current)
            current = {}
        elif " " in line:
            key, _, value = line.partition(" ")
            if key == "author":
                current["author"] = value
            elif key == "author-time":
                current["date"] = value  # Unix timestamp
            elif key == "summary":
                current["message"] = value
            elif key == "committer":
                current["committer"] = value
    # 添加行号
    for i, entry in enumerate(lines):
        if "lineNumber" not in entry:
            entry["lineNumber"] = i + 1
    return lines


def _ok(data: object) -> dict:
    return {"success": True, "data": data, "error_code": None, "error_message": None, "retryable": False}


def _fail(code: str, message: str, retryable: bool) -> dict:
    return {"success": False, "data": None, "error_code": code, "error_message": message, "retryable": retryable}


server.add_request_handler("tools/list", types.RequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="code-local",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()