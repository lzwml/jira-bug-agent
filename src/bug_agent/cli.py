"""完整 Agent 的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import AgentConfig
from .contracts import BugAnalysisTask
from .renderer import render_markdown
from .worker import BugAnalysisWorker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bug-agent", description="证据驱动的 Android Bug 分析 Agent")
    parser.add_argument("--json", action="store_true", help="输出完整 BugAnalysisResult JSON（包含 Trace）")
    parser.add_argument("--task-id", help="由上游 Workflow 提供的稳定任务 ID")
    parser.add_argument("--objective", default="定位 Bug 根因并给出下一步建议")
    parser.add_argument("--max-steps", type=int, help="覆盖本次任务的 Agent 步骤预算")
    parser.add_argument(
        "--skill", dest="skills", action="append",
        help="激活项目 Skill；可重复指定，默认 android-log-triage",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    jira = sub.add_parser("analyze-jira", help="从 Jira Issue 开始分析")
    jira.add_argument("issue_key", help="例如 APP-42")
    local = sub.add_parser("analyze-local", help="分析本地 Bug Case 目录")
    local.add_argument("case_path")
    return parser


async def _run(args: argparse.Namespace) -> int:
    config = AgentConfig.from_environment()
    common = {
        "objective": args.objective,
        "max_steps": args.max_steps,
        "include_trace": args.json,
    }
    if args.task_id:
        common["task_id"] = args.task_id
    if args.skills:
        common["skills"] = args.skills
    if args.command == "analyze-jira":
        task = BugAnalysisTask(source="jira", issue_key=args.issue_key.upper(), **common)
    else:
        task = BugAnalysisTask(source="local", case_path=args.case_path, **common)
    result = await BugAnalysisWorker(config).execute(task)

    if args.json:
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
    return 0 if result.status in {"completed", "insufficient_evidence"} else 1


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
