"""完整 Agent 的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .agent import BugAnalysisAgent
from .config import AgentConfig, default_export_root
from .mcp_router import McpToolRouter
from .prompts import JIRA_WORKFLOW_PROMPT, LOCAL_WORKFLOW_PROMPT
from .provider import OpenAICompatibleProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bug-agent", description="证据驱动的 Android Bug 分析 Agent")
    parser.add_argument("--json", action="store_true", help="输出完整 AgentRunResult JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    jira = sub.add_parser("analyze-jira", help="从 Jira Issue 开始分析")
    jira.add_argument("issue_key", help="例如 APP-42")
    local = sub.add_parser("analyze-local", help="分析本地 Bug Case 目录")
    local.add_argument("case_path")
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = AgentConfig.from_environment()
    provider = OpenAICompatibleProvider(config)
    try:
        async with McpToolRouter() as router:
            if args.command == "analyze-jira":
                export_root = default_export_root()
                export_root.mkdir(parents=True, exist_ok=True)
                await router.connect_python_server("jira", "jira_bug_mcp.server")
                await router.connect_python_server(
                    "log", "log_analyzer.server",
                    {"LOG_ANALYZER_ALLOWED_ROOTS": str(export_root)},
                )
                task = f"请分析 Jira Bug {args.issue_key.upper()}，输出证据驱动的 RCA 报告。"
                prompt = JIRA_WORKFLOW_PROMPT
            else:
                case_path = Path(args.case_path).expanduser().resolve()
                if not case_path.is_dir():
                    raise ValueError(f"Case 目录不存在: {case_path}")
                await router.connect_python_server(
                    "log", "log_analyzer.server",
                    {"LOG_ANALYZER_ALLOWED_ROOTS": str(case_path)},
                )
                task = f"请分析本地 Bug Case：{case_path}，输出证据驱动的 RCA 报告。"
                prompt = LOCAL_WORKFLOW_PROMPT
            result = await BugAnalysisAgent(config, provider).run(task, prompt, router)
    finally:
        await provider.close()

    if args.json:
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    else:
        print(result.final_answer or f"分析失败：{result.error}")
    return 0 if result.status == "completed" else 1


def main() -> None:
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        code = asyncio.run(_run(_parser().parse_args()))
    except (ValueError, OSError) as exc:
        print(f"配置或输入错误：{exc}", file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
