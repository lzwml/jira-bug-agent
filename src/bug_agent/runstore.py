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
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .contracts import BugAnalysisResult, BugAnalysisTask
from .models import AgentRunResult

logger = logging.getLogger(__name__)


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
) -> dict:
    """组装要落盘的完整 run 记录。

    【学习要点】这里刻意同时保存三类信息：
    - task：输入（谁发起的、目标是什么、激活了哪些 Skill）；
    - result：对外稳定结果（status、report）；
    - trace：完整工具调用轨迹（来自 run.tool_events，不依赖 include_trace）。

    复盘器据此可以还原"给定输入 → 模型如何一步步决策 → 产出了什么结果"。
    """
    return {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "task": task.model_dump(),
        "result": result.model_dump(exclude={"trace"}),  # trace 单独从 run 取，避免重复/丢失
        "trace": [event.model_dump() for event in (run.tool_events if run else [])],
        "agent_status": run.status if run else None,
        "agent_error": run.error if run else None,
    }


def write_run_record(
    task: BugAnalysisTask,
    run: AgentRunResult | None,
    result: BugAnalysisResult,
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
        record = build_run_record(task, run, result)
        out_path = run_dir / f"{task.task_id}.json"
        out_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return out_path
    except OSError as exc:
        logger.warning("run 记录落盘失败 (task_id=%s): %s", task.task_id, exc)
        return None
