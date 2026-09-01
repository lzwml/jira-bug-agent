"""完整 Agent 的命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import getpass
import json
import os
from pathlib import Path
import sys

from jira_bug_mcp.client import JiraClient
from jira_bug_mcp.config import JiraConfig, load_local_env
from jira_bug_mcp.exporter import CaseExporter
from jira_bug_mcp.service import JiraService
from log_analyzer.case_registry import CaseRegistry
from log_analyzer.service import LogAnalyzerService

from .config import AgentConfig
from .contracts import BugAnalysisTask
from .renderer import render_analysis_guide, render_markdown
from .runstore import resolve_run_dir, write_analysis_guide
from .worker import BugAnalysisWorker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bug-agent", description="证据驱动的 Android Bug 分析 Agent")
    parser.add_argument("--json", action="store_true", help="输出完整 BugAnalysisResult JSON（包含 Trace）")
    parser.add_argument("--task-id", help="由上游 Workflow 提供的稳定任务 ID")
    parser.add_argument("--objective", default="定位 Bug 根因并给出下一步建议")
    parser.add_argument("--max-steps", type=int, help="覆盖本次任务的 Agent 步骤预算")
    parser.add_argument(
        "--goal", action="store_true",
        help="Goal 模式：不限制工具调用次数，Agent 循环直到给出最终答案或发生不可恢复错误",
    )
    parser.add_argument(
        "--analysis-guide", action="store_true",
        help="额外生成独立的问题分析讲解，不写入正式 RCA",
    )
    parser.add_argument(
        "--skill", dest="skills", action="append",
        help="预先激活项目 Skill；可重复指定，未指定时默认 android-log-triage",
    )
    parser.add_argument(
        "--no-auto-skills", action="store_true",
        help="禁止 Agent 根据 Issue 和日志证据自动激活专项 Skill",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    jira = sub.add_parser("analyze-jira", help="从 Jira Issue 开始分析")
    jira.add_argument("issue_key", help="例如 APP-42")
    check = sub.add_parser("jira-check", help="验证 Jira 网络、PAT 和当前账号，不读取 Issue")
    _add_jira_connection_args(check)
    collect = sub.add_parser("collect-jira", help="只收集 Jira 信息，不调用大模型")
    collect.add_argument("issue_key", help="例如 APP-42")
    _add_jira_connection_args(collect)
    collect.add_argument("--max-comments", type=int, default=1000)
    collect.add_argument("--export-case", action="store_true", help="同时生成本地 Case 并下载附件")
    collect.add_argument(
        "--prepare", action="store_true",
        help="下载后安全解压并建立日志索引；隐含 --export-case，不调用大模型",
    )
    collect.add_argument("--no-index", action="store_true", help="与 --prepare 配合：只解压，不建立索引")
    collect.add_argument(
        "--work-dir",
        help="索引/内部状态目录；归档始终解压到其旁边的 <归档名>.unpacked，默认状态目录为 Case/.bug-agent",
    )
    collect.add_argument("--no-attachments", action="store_true", help="导出 Case 时不下载附件")
    collect.add_argument("--no-related", action="store_true", help="不展开评论和 Issue Link 中的关联 Jira")
    local = sub.add_parser("analyze-local", help="分析本地 Bug Case 目录")
    local.add_argument("case_path")
    prepare_local = sub.add_parser("prepare-local", help="只解压并索引本地 Case，不调用大模型")
    prepare_local.add_argument("case_path")
    prepare_local.add_argument("--force-rebuild", action="store_true", help="忽略已有解压/索引缓存")
    prepare_local.add_argument("--no-extract", action="store_true", help="不展开归档，只建立文本索引")
    prepare_local.add_argument("--no-index", action="store_true", help="只展开归档，不建立文本索引")
    prepare_local.add_argument(
        "--work-dir",
        help="索引/内部状态目录；归档始终解压到其旁边的 <归档名>.unpacked，默认状态目录为 Case/.bug-agent",
    )
    return parser


def _add_jira_connection_args(parser: argparse.ArgumentParser) -> None:
    """为只读 Jira CLI 提供统一连接参数。"""

    parser.add_argument("--base-url", help="Jira 根地址；默认读取 JIRA_BASE_URL")
    parser.add_argument(
        "--deployment", choices=("cloud", "datacenter"),
        help="Jira 部署类型；默认读取 JIRA_DEPLOYMENT",
    )
    parser.add_argument(
        "--auth-mode", choices=("basic", "bearer", "none"),
        help="认证方式；Data Center PAT 使用 bearer",
    )
    parser.add_argument("--user", help="Basic Auth 用户名；默认读取 JIRA_USER")
    parser.add_argument("--export-root", help="Case 导出目录；默认读取 JIRA_EXPORT_ROOT")


def _jira_config_for_cli(args: argparse.Namespace) -> JiraConfig:
    """合并 CLI 与环境配置，必要时在终端隐藏读取 PAT。"""

    load_local_env()
    base_url = (args.base_url or os.getenv("JIRA_BASE_URL", "")).strip().rstrip("/")
    if not base_url:
        raise ValueError("缺少 Jira 地址：使用 --base-url 或 JIRA_BASE_URL")
    deployment = args.deployment or os.getenv("JIRA_DEPLOYMENT", "cloud").strip().lower()
    auth_mode = args.auth_mode or os.getenv("JIRA_AUTH_MODE", "basic").strip().lower()
    if deployment not in {"cloud", "datacenter"}:
        raise ValueError("Jira deployment 只能是 cloud 或 datacenter")
    if auth_mode not in {"basic", "bearer", "none"}:
        raise ValueError("Jira auth mode 只能是 basic、bearer 或 none")

    # 先复用环境配置中的预算、SSL 和自定义字段规则。临时
    # 补齐 base URL，使纯 CLI 调用不要求用户先修改父 Shell。
    previous_base_url = os.environ.get("JIRA_BASE_URL")
    os.environ["JIRA_BASE_URL"] = base_url
    try:
        config = JiraConfig.from_environment()
    finally:
        if previous_base_url is None:
            os.environ.pop("JIRA_BASE_URL", None)
        else:
            os.environ["JIRA_BASE_URL"] = previous_base_url

    token = os.getenv("JIRA_TOKEN")
    user = args.user or os.getenv("JIRA_USER")
    if auth_mode != "none" and not token:
        if not sys.stdin.isatty():
            raise ValueError("缺少 JIRA_TOKEN；非交互运行时必须通过环境变量提供")
        token = getpass.getpass("Jira PAT/Token（输入不回显）: ").strip()
        if not token:
            raise ValueError("Jira PAT/Token 不能为空")
    if auth_mode == "basic" and not user:
        if not sys.stdin.isatty():
            raise ValueError("Basic Auth 缺少 JIRA_USER")
        user = input("Jira 用户名/邮箱: ").strip()

    return replace(
        config,
        base_url=base_url,
        deployment=deployment,  # type: ignore[arg-type]
        auth_mode=auth_mode,  # type: ignore[arg-type]
        user=user,
        token=token,
        export_root=Path(args.export_root).expanduser() if args.export_root else config.export_root,
    )


def _jira_service(config: JiraConfig) -> tuple[JiraClient, JiraService]:
    client = JiraClient(config)
    return client, JiraService(client, CaseExporter(config, client))


def _run_jira_check(args: argparse.Namespace) -> int:
    config = _jira_config_for_cli(args)
    client, service = _jira_service(config)
    try:
        result = service.dispatch("test_connection", {})
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
        return 0 if result.success else 1
    finally:
        client.close()


def _prepare_case_without_agent(
    case_path: Path,
    *,
    allowed_root: Path,
    extract_archives: bool = True,
    build_index: bool = True,
    force_rebuild: bool = False,
    work_dir: Path | None = None,
) -> dict:
    """复用 Log MCP 领域服务完成确定性 Case 准备，不创建模型 Provider。"""

    registry = CaseRegistry.from_environment([str(allowed_root.resolve())])
    service = LogAnalyzerService(registry)
    opened = service.open_case(case_path=str(case_path.resolve()))
    payload: dict[str, object] = {"open_case": opened.model_dump()}
    if not opened.success:
        return payload
    case_id = opened.data["case"]["case_id"]
    registry.set_case_work_dir(case_id, work_dir or (case_path / ".bug-agent"))
    effective_work_dir = registry.get_work_dir(case_id)
    if effective_work_dir is None:
        raise ValueError("Case 尚未注册")
    before = service.inspect_case(case_id=case_id)
    prepared = service.prepare_case(
        case_id=case_id,
        extract_archives=extract_archives,
        build_index=build_index,
        force_rebuild=force_rebuild,
    )
    after = service.inspect_case(case_id=case_id)
    payload.update({
        "work_dir": str(effective_work_dir),
        "extraction_layout": "archive_sibling",
        "extracted_root": str(case_path),
        "index_path": str(effective_work_dir / "index" / "logs.sqlite3") if build_index else None,
        "inspect_before": before.model_dump(),
        "prepare": prepared.model_dump(),
        "inspect_after": after.model_dump(),
    })
    return payload


def _run_jira_collection(args: argparse.Namespace) -> int:
    """独立执行 Jira 接入验证和信息收集，不需要 LLM 配置。"""

    config = _jira_config_for_cli(args)
    client, service = _jira_service(config)
    try:
        connection = service.dispatch("test_connection", {})
        if not connection.success:
            print(json.dumps({"connection": connection.model_dump()}, ensure_ascii=False, indent=2))
            return 1
        context = service.dispatch("collect_issue_context", {
            "issue_key": args.issue_key.upper(),
            "max_comments": args.max_comments,
        })
        payload: dict[str, object] = {
            "connection": connection.model_dump(),
            "context": context.model_dump(),
        }
        export_success = True
        if (args.export_case or args.prepare) and context.success:
            exported = service.dispatch("export_issue_case", {
                "issue_key": args.issue_key.upper(),
                # 本地 Jira Case 必须带有可验证的完整评论快照。
                "max_comments": args.max_comments,
                "include_attachments": not args.no_attachments,
                "include_related_issues": not args.no_related,
                "include_related_attachments": not args.no_attachments,
            })
            payload["export"] = exported.model_dump()
            export_success = exported.success
            if args.prepare and exported.success:
                preparation = _prepare_case_without_agent(
                    Path(exported.data["case_path"]),
                    allowed_root=config.export_root,
                    build_index=not args.no_index,
                    work_dir=Path(args.work_dir) if args.work_dir else None,
                )
                payload["preparation"] = preparation
                prepare_result = preparation.get("prepare", {})
                export_success = bool(
                    isinstance(prepare_result, dict) and prepare_result.get("success")
                )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0 if context.success and export_success else 1
    finally:
        client.close()


async def _run(args: argparse.Namespace) -> int:
    if args.command == "jira-check":
        return _run_jira_check(args)
    if args.command == "collect-jira":
        return _run_jira_collection(args)
    if args.command == "prepare-local":
        case_path = Path(args.case_path).expanduser().resolve()
        payload = _prepare_case_without_agent(
            case_path,
            allowed_root=case_path,
            extract_archives=not args.no_extract,
            build_index=not args.no_index,
            force_rebuild=args.force_rebuild,
            work_dir=Path(args.work_dir) if args.work_dir else None,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        prepare_result = payload.get("prepare", {})
        return 0 if isinstance(prepare_result, dict) and prepare_result.get("success") else 1
    config = AgentConfig.from_environment()
    common = {
        "objective": args.objective,
        "max_steps": args.max_steps,
        "goal_mode": args.goal,
        "include_trace": args.json,
        "auto_select_skills": not args.no_auto_skills,
        "include_analysis_guide": args.analysis_guide,
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
        # --json 模式可能被管道/程序消费，保持输出纯净，不混入额外行。
        print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
        if result.analysis_guide is not None:
            guide_path = write_analysis_guide(
                task, render_analysis_guide(result.analysis_guide, result.task_id),
            )
            if guide_path is not None:
                print(f"\n问题分析讲解已保存: {guide_path}")
        elif args.analysis_guide and result.analysis_guide_error:
            print(f"\n问题分析讲解未生成: {result.analysis_guide_error}")
        # 人类可读模式下提示完整 trace 的落盘位置，便于复盘与 Skill 迭代。
        run_dir = resolve_run_dir(task)
        if run_dir is not None:
            # 文件名格式：{local_time}_{task_id}.json（由 RunRecorder 实时生成）
            print(f"\n完整执行轨迹已保存到: {run_dir}")
            print(f"  （文件名格式：<时间戳>_{task.task_id}.json）")
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
