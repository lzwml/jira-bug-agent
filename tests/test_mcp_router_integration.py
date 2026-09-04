"""从完整 Harness 真实启动两个 stdio MCP，验证 workspace 组合边界。"""

from __future__ import annotations

import pytest

from bug_agent.mcp_router import McpToolRouter


@pytest.mark.anyio
async def test_router_discovers_jira_and_log_tools(tmp_path):
    async with McpToolRouter() as router:
        await router.connect_python_server("jira", "jira_bug_mcp.server")
        await router.connect_python_server(
            "log",
            "log_analyzer.server",
            {"LOG_ANALYZER_ALLOWED_ROOTS": str(tmp_path)},
        )
        names = {item["function"]["name"] for item in router.openai_tools()}

        assert names == {
            "test_connection", "get_issue", "collect_issue_context",
            "search_issues", "get_comments", "list_attachments",
            "export_issue_case", "open_case", "inspect_case", "search_evidence",
            "inspect_archive", "probe_archive_members",
            "extract_archive_members", "extract_aee_db", "build_index",
            "prepare_case", "extract_timeline", "parse_diagnostics",
            "get_case_comment",
        }


@pytest.mark.anyio
async def test_router_discovers_video_evidence_tools(tmp_path):
    async with McpToolRouter() as router:
        await router.connect_python_server(
            "video", "video_analysis.server",
            {"VIDEO_ANALYZER_ALLOWED_ROOTS": str(tmp_path)},
        )

        names = {item["function"]["name"] for item in router.openai_tools()}

    assert names == {
        "open_video", "inspect_video", "extract_keyframes",
        "analyze_video", "query_video", "get_clip",
    }
