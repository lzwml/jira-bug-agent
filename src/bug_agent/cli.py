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

from .chat_store import ChatStore, resolve_chat_session_id, resolve_chat_sessions_dir
from .config import AgentConfig
from .contracts import BugAnalysisResult, BugAnalysisTask
from .conversation import ConversationSession, SavedTurn
from .models import ToolEvent
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
    # ---- 连续问答模式 ----
    chat_local = sub.add_parser("chat-local", help="交互式连续问答，分析本地 Bug Case")
    chat_local.add_argument("case_path")
    chat_local.add_argument("--max-steps-per-turn", type=int, help="每轮最大步数（默认使用配置值）")
    chat_local.add_argument("--objective", default="定位 Bug 根因并给出下一步建议")
    chat_local.add_argument(
        "--skill", dest="skills", action="append",
        help="预先激活 Skill；可重复指定",
    )
    chat_local.add_argument("--no-auto-skills", action="store_true", help="禁止自动激活 Skill")
    chat_jira = sub.add_parser("chat-jira", help="交互式连续问答，分析 Jira Bug")
    chat_jira.add_argument("issue_key", help="例如 APP-42")
    chat_jira.add_argument("--max-steps-per-turn", type=int, help="每轮最大步数（默认使用配置值）")
    chat_jira.add_argument("--objective", default="定位 Bug 根因并给出下一步建议")
    chat_jira.add_argument(
        "--skill", dest="skills", action="append",
        help="预先激活 Skill；可重复指定",
    )
    chat_jira.add_argument("--no-auto-skills", action="store_true", help="禁止自动激活 Skill")
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
    if args.command in ("chat-local", "chat-jira"):
        return await _run_chat(args)
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


def _print_history(session: "ConversationSession") -> None:
    """打印当前会话的对话历史。"""
    print(f"\n已完成 {session.turn_count} 轮对话：")
    for i, t in enumerate(session._turns, 1):
        preview = t.user_message[:80] + ("..." if len(t.user_message) > 80 else "")
        print(f"  [{i}] {preview}")


async def _generate_chat_report(
    worker: BugAnalysisWorker,
    task: BugAnalysisTask,
    session: "ConversationSession",
    store: ChatStore | None,
    session_id: str,
) -> None:
    """将当前对话历史合成为正式 RCA 报告并保存。"""
    if session.turn_count == 0:
        print("\n[系统] 当前没有对话记录，无法生成报告。")
        return

    print("\n[系统] 正在从对话历史生成分析报告，请稍候...")
    turns_data = [
        {"user_message": t.user_message, "assistant_answer": t.result.final_answer}
        for t in session._turns
    ]
    try:
        report, structured = await worker.synthesize_report_from_turns(task, turns_data)
    except Exception as exc:
        print(f"\n[错误] 报告生成失败: {exc}")
        return

    # 构建临时 BugAnalysisResult 用于渲染
    result = BugAnalysisResult(
        task_id=task.task_id,
        status=(
            "completed" if report.conclusion_status == "confirmed"
            else "insufficient_evidence" if report.conclusion_status == "insufficient_evidence"
            else "completed"
        ),
        report=report,
        steps=session._total_steps,
        structured_output=structured,
        applied_skills=task.skills or [],
    )
    markdown = render_markdown(result)
    print(f"\n{'=' * 60}")
    print(markdown)
    print(f"{'=' * 60}")

    # 保存报告到 Case 目录
    saved_path = await _save_report(task, markdown)
    if saved_path is not None:
        print(f"\n报告已保存到: {saved_path}")

    # 持久化报告生成这一轮
    if store is not None:
        store.add_turn(
            session_id, session.turn_count + 1,
            "/report（生成分析报告）",
            markdown[:5000],  # 报告可能很长，截断存储
            session._total_steps, "completed",
        )


async def _maybe_generate_report(
    worker: BugAnalysisWorker,
    task: BugAnalysisTask,
    session: "ConversationSession",
    store: ChatStore | None,
    session_id: str,
    reason: str,
) -> None:
    """退出前询问是否生成报告。"""
    if session.turn_count == 0:
        return
    print(f"\n[系统] {reason}，是否需要生成分析报告？")
    try:
        choice = input("  [y] 生成报告  [n] 直接退出（默认 n）: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return
    if choice in ("y", "yes"):
        await _generate_chat_report(worker, task, session, store, session_id)


async def _save_report(task: BugAnalysisTask, markdown: str) -> Path | None:
    """保存报告到 Case 的 .bug-agent/reports/ 目录。"""
    try:
        from .runstore import resolve_run_dir
        run_dir = resolve_run_dir(task)
        if run_dir is None:
            return None
        reports_dir = run_dir.parent / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        filename = f"chat-report-{task.task_id}.md"
        output = reports_dir / filename
        output.write_text(markdown, encoding="utf-8")
        return output
    except OSError:
        return None


def _tool_progress(event: ToolEvent) -> None:
    """打印工具调用进度，让用户看到 Agent 正在做什么。"""
    name = event.tool_name
    label = _TOOL_LABELS.get(name, name)
    status = "✓" if event.success else "✗"
    detail = _tool_detail(event)
    print(f"  [{status}] {label}{detail}")


def _tool_detail(event: ToolEvent) -> str:
    """从工具参数中提取一行可读的摘要。"""
    args = event.arguments
    if not args:
        return ""
    if "query" in args:
        q = str(args["query"])[:60]
        return f" — {q}"
    if "relative_path" in args:
        return f" — {args['relative_path']}"
    if "case_path" in args:
        return f" — {args['case_path']}"
    if "artifact_id" in args:
        return f" — {args['artifact_id']}"
    if "archive_id" in args:
        info = f"archive={args['archive_id']}"
        if "member_ids" in args:
            info += f" members={len(args['member_ids'])}"
        return f" — {info}"
    return ""


_TOOL_LABELS: dict[str, str] = {
    "open_case": "打开 Case",
    "inspect_case": "检查 Case 结构",
    "inspect_archive": "检查归档",
    "extract_archive_members": "解压归档成员",
    "build_index": "建立文本索引",
    "search_evidence": "搜索证据",
    "extract_timeline": "提取时间线",
    "parse_diagnostics": "解析诊断信息",
    "get_case_comment": "获取 Jira 评论",
    "prepare_case": "准备 Case",
}


async def _run_chat(args: argparse.Namespace) -> int:
    """运行交互式连续问答模式。

    支持两种模式：
    - chat-local：分析本地 Bug Case；
    - chat-jira：从 Jira 导出并分析。

    每轮对话自动持久化到 Case 的 .bug-agent/chat.db，退出后下次执行同一
    命令时自动检测已有会话并提示 resume。

    交互式终端中，用户可以连续追问，输入 /quit 或 /q 退出，
    输入 /history 查看当前对话轮次。
    """
    config = AgentConfig.from_environment()
    worker = BugAnalysisWorker(config)

    common = {
        "objective": args.objective,
        "goal_mode": args.goal,
        "auto_select_skills": not args.no_auto_skills,
    }
    if args.skills:
        common["skills"] = args.skills

    if args.command == "chat-jira":
        task = BugAnalysisTask(source="jira", issue_key=args.issue_key.upper(), **common)
    else:
        task = BugAnalysisTask(source="local", case_path=args.case_path, **common)

    # ---------- 持久化：检测已有会话 ----------
    sessions_dir = resolve_chat_sessions_dir(task)
    session_id = resolve_chat_session_id(task)
    store = ChatStore(sessions_dir) if sessions_dir is not None else None
    saved_turns: list[SavedTurn] = []

    if store is not None:
        existing = store.get_session(session_id)
        if existing is not None and existing["status"] == "active" and existing["turns"]:
            saved_turns = [
                SavedTurn(
                    user_message=t["user_message"],
                    assistant_answer=t["assistant_answer"],
                    steps=t["steps"],
                    agent_status=t["agent_status"],
                )
                for t in existing["turns"]
            ]
            print("=" * 60)
            print(f"发现已有会话记录: {len(saved_turns)} 轮")
            print(f"创建时间: {existing['created_at']}")
            print(f"更新时间: {existing['updated_at']}")
            print()
            print("  [r] 恢复会话，继续追问")
            print("  [n] 放弃旧会话，重新开始")
            print("=" * 60)
            try:
                choice = input("请选择 (r/n，默认 r): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n[系统] 已取消。")
                return 0
            if choice in ("n", "no", "new"):
                store.close_session(session_id)
                store.create_session(session_id, task)
                saved_turns = []
                print("[系统] 已创建新会话。\n")
            else:
                print(f"[系统] 恢复 {len(saved_turns)} 轮历史会话。\n")
        else:
            store.create_session(session_id, task)

    print("=" * 60)
    print("Bug Analysis Agent — 交互式连续问答模式")
    print("=" * 60)
    print(f"Case: {task.issue_key or task.case_path}")
    print(f"目标: {task.objective}")
    if args.max_steps_per_turn:
        print(f"每轮步数: {args.max_steps_per_turn}")
    print()
    print("输入 /quit 或 /q 退出，输入 /history 查看对话轮次，输入 /report 生成分析报告")
    if store is not None and saved_turns:
        print("（会话已恢复，可直接追问，无需重新执行初始分析）")
    print("=" * 60)
    print()

    try:
        session, first_instruction = await worker.create_conversation(
            task,
            max_steps_per_turn=args.max_steps_per_turn,
            on_tool_event=_tool_progress,
        )
    except (ValueError, OSError) as exc:
        print(f"\n会话创建失败: {exc}", file=sys.stderr)
        return 2

    try:
        if saved_turns:
            # 恢复历史轮次到会话中
            session.restore_turns(saved_turns)
            print(f"[系统] 已恢复 {len(saved_turns)} 轮历史对话。")
            print(f"直接输入追问即可继续，或输入 /quit 退出。\n")
        else:
            # 全新会话：执行初始分析
            print("[系统] 正在执行初始分析，请稍候...\n")
            turn = await session.send(first_instruction)
            print(f"[第 1 轮回答]")
            print("-" * 40)
            print(turn.result.final_answer)
            print("-" * 40)
            if turn.result.status == "failed":
                print(f"\n[错误] {turn.result.error}")
                return 1
            if turn.result.status == "max_steps":
                print(f"\n[提示] 本轮达到步数上限，部分证据可能未收集完整。")
            # 持久化第一轮
            if store is not None:
                store.add_turn(
                    session_id, 1, first_instruction,
                    turn.result.final_answer, turn.result.steps,
                    turn.result.status,
                    tool_events=[e.model_dump() for e in turn.result.tool_events],
                )

        # 后续轮次：交互式追问
        while session.is_active:
            print(f"\n[轮次 {session.turn_count + 1}] ", end="")
            try:
                user_input = input("请输入追问（或 /quit 退出）: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n")
                await _maybe_generate_report(
                    worker, task, session, store, session_id, "Ctrl+C/EOF",
                )
                break

            if not user_input:
                continue
            if user_input.lower() in ("/quit", "/q", "/exit"):
                await _maybe_generate_report(
                    worker, task, session, store, session_id, "主动退出",
                )
                print("\n[系统] 会话结束。")
                break
            if user_input.lower() == "/history":
                _print_history(session)
                continue
            if user_input.lower() == "/report":
                await _generate_chat_report(worker, task, session, store, session_id)
                continue

            print(f"\n[系统] 正在分析追问...")
            try:
                turn = await session.send(user_input)
            except RuntimeError as exc:
                print(f"\n[系统] 无法继续: {exc}")
                break

            print(f"\n[第 {session.turn_count} 轮回答]")
            print("-" * 40)
            print(turn.result.final_answer)
            print("-" * 40)
            if turn.result.status == "max_steps":
                print(f"\n[提示] 本轮达到步数上限。")

            # 持久化本轮
            if store is not None:
                store.add_turn(
                    session_id, session.turn_count, user_input,
                    turn.result.final_answer, turn.result.steps,
                    turn.result.status,
                    tool_events=[e.model_dump() for e in turn.result.tool_events],
                )

    finally:
        result = await session.finalize()
        if store is not None:
            store.close_session(session_id)
        print(f"\n{'=' * 60}")
        print(f"会话结束。共 {len(result.turns)} 轮，{result.total_steps} 步。")
        if store is not None and sessions_dir is not None:
            print(f"会话记录已保存到: {sessions_dir / f'{session_id}.json'}")
        print(f"{'=' * 60}")

    return 0


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
