"""MCP protocol boundary for secure video evidence analysis."""

from __future__ import annotations

import asyncio
import json

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from pydantic import BaseModel

from .config import VideoAnalyzerConfig
from .domain import AnalyzeVideoInput, ExtractKeyframesInput, GetClipInput, InspectVideoInput, OpenVideoInput, QueryVideoInput
from .service import VideoAnalyzerError, VideoAnalyzerService

server = Server("video-analysis")
service = VideoAnalyzerService(VideoAnalyzerConfig.from_environment())
TOOLS: dict[str, tuple[str, type[BaseModel]]] = {
    "open_video": ("Register a screen recording inside the allowed Bug Case root. Subsequent calls use video_id only.", OpenVideoInput),
    "inspect_video": ("Read safe metadata for an already registered video.", InspectVideoInput),
    "extract_keyframes": ("Extract bounded, timestamped keyframes for human review or downstream evidence.", ExtractKeyframesInput),
    "analyze_video": ("Use the configured vision model to produce timeline-grounded JSON evidence from a screen recording.", AnalyzeVideoInput),
    "query_video": ("Ask a focused question about a video; returns the same structured evidence contract as analyze_video.", QueryVideoInput),
    "get_clip": ("Create a review clip for a bounded timestamp interval (maximum 120 seconds).", GetClipInput),
}

async def handle_list_tools(ctx, params: types.ListToolsRequest) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[types.Tool(name=name, description=description, inputSchema=model.model_json_schema()) for name, (description, model) in TOOLS.items()])

async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    try:
        _, model = TOOLS[params.name]
        args = model.model_validate(params.arguments or {})
        if params.name == "open_video": data = service.open_video(args.video_path)
        elif params.name == "inspect_video": data = service.inspect_video(args.video_id)
        elif params.name == "extract_keyframes": data = {"frames": [item.model_dump() for item in service.extract_keyframes(args.video_id, args.interval_seconds, args.max_frames)]}
        elif params.name in {"analyze_video", "query_video"}: data = service.analyze_video(args.video_id, args.objective, args.interval_seconds, args.max_frames)
        else: data = service.get_clip(args.video_id, args.start_ms, args.end_ms)
        payload = {"success": True, "data": data, "error_code": None, "error_message": None, "retryable": False}
    except KeyError:
        payload = {"success": False, "data": None, "error_code": "UNKNOWN_TOOL", "error_message": "unknown tool", "retryable": False}
    except (VideoAnalyzerError, ValueError) as exc:
        payload = {"success": False, "data": None, "error_code": "VIDEO_ANALYSIS_ERROR", "error_message": str(exc), "retryable": False}
    except Exception as exc:
        payload = {"success": False, "data": None, "error_code": "INTERNAL_ERROR", "error_message": type(exc).__name__, "retryable": False}
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))])

server.add_request_handler("tools/list", types.RequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)

async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(server_name="video-analysis", server_version="0.1.0", capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={})))

if __name__ == "__main__":
    asyncio.run(main())
