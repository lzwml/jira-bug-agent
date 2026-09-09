"""把每次运行的完整 Trace 落盘，供复盘、Eval 与 Skill 迭代使用。

【学习要点】为什么单独建一个模块，而不是塞进 worker.py？

1. **职责分离**：worker.py 的职责是"组装 Agent 并产出稳定的 BugAnalysisResult
   契约"；trace 落盘是"可观测性/复盘基础设施"，两者关注点不同。
2. **不污染公共契约**：BugAnalysisResult 是给上游 Workflow 的稳定契约，
   不能往里加 run_file 这种实现细节字段。落盘是副作用，独立模块更干净。
3. **失败隔离**：落盘是"锦上添花"，磁盘满、权限不足都不应让分析失败。
   独立模块便于把"落盘失败只警告"的策略集中在一处。

【学习要点】为什么落盘用 run.tool_events 而不是 result.trace？

BugAnalysisResult.trace 默认是空的——只有 task.include_trace=True 时才会
填充（见 worker.py）。如果落盘依赖 result.trace，那默认运行就丢光了决策
轨迹，复盘就无从谈起。所以落盘必须在 worker 内部、拿到完整 run 对象的地方。

【学习要点】RunRecorder 为什么是"实时"的？

之前的方案是整个 run 结束后一次性写入 JSON，从 Agent 开始到结束之间磁盘上
没有任何记录。复盘时如果 Agent 中途挂掉（OOM、进程被杀、无限循环），什么都
看不到。RunRecorder 在运行开始时立即创建文件并写入 running 状态，之后每
发生一次工具调用就原子刷新，最终结束时写入完整结果。无论什么阶段中断，
磁盘上都能看到当前进度的快照。

文件名格式：{local_time_ms}_{task_id}.json
例如：2026-09-01_17-25-39.739_f6474dea-08cb-4085-bdab-5ff5d0f9b391.json
带上本地时间便于按时间排序和快速定位。
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .contracts import BugAnalysisResult, BugAnalysisTask, InvestigationState
from .models import AgentRunResult, ToolEvent
from .run_bundle import (
    SCHEMA_VERSION,
    build_budget_usage,
    build_derived_views,
    enrich_provenance,
    seal_record,
)

logger = logging.getLogger(__name__)
SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


def resolve_run_dir(task: BugAnalysisTask) -> Path | None:
    """根据任务来源解析本次 run 的落盘目录（不创建）。

    【学习要点】位置与 Case 绑定，而不是集中存放：
    - local 模式：直接写到用户给的 case_path 下的 .bug-agent/runs/，
      和该 Case 的索引、解压内容放在一起，复盘时一处就能看全；
    - jira 模式：写到 <export_root>/<ISSUE_KEY>/.bug-agent/runs/，
      与 export_issue_case 实际生成本地 Case 的位置一致。

    返回 None 表示无法确定安全位置（例如 local 模式路径不合法），
    调用方应跳过落盘而不是报错。
    """
    if task.source == "local":
        if not task.case_path:
            return None
        case_path = Path(task.case_path).expanduser().resolve()
        return case_path / ".bug-agent" / "runs"

    # jira 模式：export_issue_case 把 Case 落到 <export_root>/<ISSUE_KEY>/
    from .config import default_export_root

    issue_key = (task.issue_key or "").upper()
    if not issue_key:
        return None
    return default_export_root() / issue_key / ".bug-agent" / "runs"


def build_run_record(
    task: BugAnalysisTask,
    run: AgentRunResult | None,
    result: BugAnalysisResult,
    context_metadata: dict | None = None,
    failure_metadata: dict | None = None,
    provenance: dict | None = None,
    investigation_state: InvestigationState | None = None,
) -> dict:
    """组装要落盘的完整 run 记录。

    【学习要点】这里刻意同时保存三类信息：
    - task：输入（谁发起的、目标是什么、激活了哪些 Skill）；
    - result：对外稳定结果（status、report）；
    - trace：完整工具调用轨迹（来自 run.tool_events，不依赖 include_trace）。

    复盘器据此可以还原"给定输入 → 模型如何一步步决策 → 产出了什么结果"。
    """
    events = run.tool_events if run else []
    final_provenance = enrich_provenance(provenance, events)
    record = {
        "schema_version": SCHEMA_VERSION,
        "bundle_kind": "agent_run",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "lifecycle": {"status": result.status, "phase": None},
        "task": task.model_dump(),
        "result": result.model_dump(exclude={"trace"}),  # trace 单独从 run 取，避免重复/丢失
        "trace": [event.model_dump() for event in (run.tool_events if run else [])],
        "agent_status": run.status if run else None,
        "agent_error": run.error if run else None,
        # 只保存完整性/编译元数据和压缩摘要，不复制 issue.json 中的评论原文。
        "jira_context": context_metadata,
        "failure": failure_metadata,
        "provenance": final_provenance,
        "budget": build_budget_usage(run, final_provenance, result.token_usage),
        "derived": build_derived_views(run, result),
        "investigation_state": investigation_state.model_dump() if investigation_state else None,
    }
    return seal_record(record)


def write_run_record(
    task: BugAnalysisTask,
    run: AgentRunResult | None,
    result: BugAnalysisResult,
    context_metadata: dict | None = None,
    failure_metadata: dict | None = None,
    provenance: dict | None = None,
    investigation_state: InvestigationState | None = None,
) -> Path | None:
    """把 run 记录写入 .bug-agent/runs/<task_id>.json，返回路径。

    【学习要点】失败只警告、不抛异常：
    落盘失败（磁盘满、权限不足、路径非法）绝不能反过来让一次成功的分析
    变成"失败"。可观测性是辅助能力，不能反过来绑架主流程。
    """
    try:
        run_dir = resolve_run_dir(task)
        if run_dir is None:
            logger.warning("无法确定 run 落盘位置，跳过记录 (task_id=%s)", task.task_id)
            return None
        run_dir.mkdir(parents=True, exist_ok=True)
        if SAFE_TASK_ID.fullmatch(task.task_id) is None:
            logger.warning("task_id 不能安全用作文件名，跳过记录")
            return None
        record = build_run_record(
            task, run, result, context_metadata, failure_metadata, provenance, investigation_state,
        )
        resolved_run_dir = run_dir.resolve()
        out_path = (resolved_run_dir / f"{task.task_id}.json").resolve(strict=False)
        try:
            out_path.relative_to(resolved_run_dir)
        except ValueError:
            logger.warning("run 记录路径逃出目标目录，跳过记录")
            return None
        temp_path = resolved_run_dir / f".{task.task_id}.{uuid4().hex}.tmp"
        try:
            with temp_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, indent=2, default=str))
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(out_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return out_path
    except OSError as exc:
        logger.warning("run 记录落盘失败 (task_id=%s): %s", task.task_id, exc)
        return None


def _local_time_ms() -> str:
    """返回本地时间的毫秒级时间戳字符串，用于文件名。

    格式：2026-09-01_17-25-39.739
    注意：Windows 上 datetime.now() 的微秒精度取决于系统时钟，
    实际可能只能到毫秒（三位小数）。
    """
    now = datetime.now()
    return now.strftime("%Y-%m-%d_%H-%M-%S") + f".{now.microsecond // 1000:03d}"


def make_run_filename(task_id: str) -> str:
    """生成带时间戳的 run 文件名。

    >>> make_run_filename("f6474dea-08cb-4085-bdab-5ff5d0f9b391")
    '2026-09-01_17-25-39.739_f6474dea-08cb-4085-bdab-5ff5d0f9b391.json'
    """
    return f"{_local_time_ms()}_{task_id}.json"


def _atomic_write(path: Path, content: str) -> None:
    """原子写入：先写临时文件，再 rename（同目录内是原子操作）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temp_path.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


class RunRecorder:
    """实时运行记录器：启动时创建文件，每步刷新，结束时终写。

    【学习要点】为什么不用 JSONL 追加？
    - JSONL 适合 append-only 流，但复盘时需要一个完整的结构化快照。
    - 用原子覆盖写入同一个 JSON 文件，每次刷新都是完整快照，
      复盘时直接打开就能看到最新状态，不需要拼接多行。
    - 原子写入（tmp + rename）保证不会读到半截文件。

    生命周期：
    1. start()         → 创建 {time}_{task_id}.json，status=running
    2. on_tool_event() → 追加事件到 trace，刷新文件
    3. on_phase()      → 更新当前阶段，刷新文件
    4. finish()        → 写入完整结果，status=completed/failed/max_steps
    """

    def __init__(self, task: BugAnalysisTask):
        self._task = task
        self._run_dir = resolve_run_dir(task)
        self._filename = make_run_filename(task.task_id)
        self._file_path: Path | None = None
        self._events: list[dict] = []
        self._phase: str = "initializing"
        self._started_at: str = ""
        self._context_metadata: dict | None = None
        self._failure_metadata: dict | None = None
        self._provenance: dict | None = None
        self._investigation_state: InvestigationState | None = None

    @property
    def file_path(self) -> Path | None:
        """最终落盘路径（仅在 start() 成功后可用）。"""
        return self._file_path

    def start(self) -> bool:
        """创建运行记录文件，写入 running 状态。

        返回 False 表示落盘位置不可用（调用方应跳过所有后续记录）。
        不会创建目录——目录在首次 _flush() 时懒创建，避免在 case_path 校验前
        意外创建目录。
        """
        if self._run_dir is None:
            logger.warning("无法确定 run 落盘位置，跳过实时记录 (task_id=%s)", self._task.task_id)
            return False
        if SAFE_TASK_ID.fullmatch(self._task.task_id) is None:
            logger.warning("task_id 不能安全用作文件名，跳过实时记录")
            return False
        try:
            resolved = self._run_dir.resolve()
            self._file_path = (resolved / self._filename).resolve(strict=False)
            try:
                self._file_path.relative_to(resolved)
            except ValueError:
                logger.warning("run 记录路径逃出目标目录，跳过实时记录")
                self._file_path = None
                return False
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._phase = "initializing"
            # 不在此处 _flush()——目录创建推迟到首次 on_phase/on_tool_event，
            # 避免在 case_path 校验前意外创建目录。
            return True
        except OSError as exc:
            logger.warning("run 实时记录启动失败 (task_id=%s): %s", self._task.task_id, exc)
            self._file_path = None
            return False

    def on_tool_event(self, event: ToolEvent) -> None:
        """Agent 每完成一次工具调用后立即调用，刷新文件。"""
        self._events.append(event.model_dump())
        self._flush()

    def on_phase(self, phase: str) -> None:
        """Worker 切换阶段时调用（如 local_validation → agent）。"""
        self._phase = phase
        self._flush()

    def set_context(self, context_metadata: dict | None) -> None:
        """设置 Jira 上下文元数据（在 finish 前调用）。"""
        self._context_metadata = context_metadata

    def set_failure(self, failure_metadata: dict | None) -> None:
        """设置失败元数据（在 finish 前调用）。"""
        self._failure_metadata = failure_metadata

    def set_provenance(self, provenance: dict | None) -> None:
        """保存可复现身份；调用方必须确保其中不含凭据和 Prompt 原文。"""
        self._provenance = provenance

    def set_investigation_state(self, state: InvestigationState | None) -> None:
        """Persist a copy of the local planning state with the final bundle."""
        self._investigation_state = state

    def finish(
        self,
        run: AgentRunResult | None,
        result: BugAnalysisResult,
    ) -> Path | None:
        """写入最终完整记录，返回文件路径。

        start() 失败时此方法为 no-op，返回 None。
        """
        if self._file_path is None:
            return None
        try:
            record = build_run_record(
                self._task, run, result,
                self._context_metadata,
                self._failure_metadata,
                self._provenance,
                self._investigation_state,
            )
            # 用实时收集的 trace 覆盖 build_run_record 中的 trace，
            # 确保 finish 时 trace 是最完整的（与逐步刷新的一致）。
            if self._events:
                record["trace"] = list(self._events)
            record["phase"] = self._phase
            record["started_at"] = self._started_at
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            record["budget"]["actual"]["duration_ms"] = max(0, round(
                (
                    datetime.fromisoformat(record["finished_at"])
                    - datetime.fromisoformat(self._started_at)
                ).total_seconds() * 1000
            ))
            record["lifecycle"] = {
                "status": result.status,
                "phase": self._phase,
                "started_at": self._started_at,
                "finished_at": record["finished_at"],
            }
            seal_record(record)
            _atomic_write(self._file_path, json.dumps(record, ensure_ascii=False, indent=2, default=str))
            return self._file_path
        except OSError as exc:
            logger.warning("run 记录终写失败 (task_id=%s): %s", self._task.task_id, exc)
            return None

    def _flush(self) -> None:
        """原子刷新当前快照到文件。失败只警告，不抛异常。"""
        if self._file_path is None:
            return
        try:
            snapshot = {
                "schema_version": SCHEMA_VERSION,
                "bundle_kind": "agent_run",
                "status": "running",
                "phase": self._phase,
                "started_at": self._started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "lifecycle": {
                    "status": "running",
                    "phase": self._phase,
                    "started_at": self._started_at,
                },
                "task": self._task.model_dump(),
                "trace": list(self._events),
                "steps": len(self._events),
                "result": None,
                "agent_status": None,
                "agent_error": None,
                "jira_context": self._context_metadata,
                "failure": self._failure_metadata,
                "provenance": self._provenance,
            }
            seal_record(snapshot)
            _atomic_write(self._file_path, json.dumps(snapshot, ensure_ascii=False, indent=2, default=str))
        except OSError as exc:
            logger.warning("run 实时记录刷新失败 (task_id=%s): %s", self._task.task_id, exc)


def write_analysis_guide(task: BugAnalysisTask, markdown: str) -> Path | None:
    """把讲解写成与 RCA 运行记录并列、但独立的 Markdown 文件。"""

    try:
        run_dir = resolve_run_dir(task)
        if run_dir is None or SAFE_TASK_ID.fullmatch(task.task_id) is None:
            return None
        guide_dir = run_dir.parent / "analysis-guides"
        guide_dir.mkdir(parents=True, exist_ok=True)
        output = (guide_dir / f"{task.task_id}.md").resolve(strict=False)
        if output.parent != guide_dir.resolve():
            return None
        temp_path = guide_dir / f".{task.task_id}.{uuid4().hex}.tmp"
        try:
            with temp_path.open("x", encoding="utf-8") as stream:
                stream.write(markdown)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(output)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return output
    except OSError as exc:
        logger.warning("问题分析讲解落盘失败 (task_id=%s): %s", task.task_id, exc)
        return None
