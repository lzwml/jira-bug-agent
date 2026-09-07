"""把运行时人工介入转换成待评审的 Agent 优化候选。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path

from .models import HumanIntervention
from .run_bundle import seal_record, verify_record
from .runstore import _atomic_write


logger = logging.getLogger(__name__)


_SUGGESTIONS = {
    "tool_selection": "检查工具选择、Artifact 排序、搜索窗口或调用参数策略。",
    "skill_routing": "检查 Skill description、激活信号和 primary/secondary 路由规则。",
    "skill_content": "检查已激活 Skill 是否缺少该领域判定、反证或停止条件。",
    "case_context": "检查 Case 收集契约、事故身份字段和必需附件是否应前置提供。",
    "unknown": "需要人工确定归因后才能进入 Tool、Skill 或数据采集改动。",
}


def write_optimization_candidate(
    sessions_dir: Path,
    *,
    session_id: str,
    turn_index: int,
    task: dict | None,
    intervention_value: dict,
) -> Path | None:
    try:
        intervention = HumanIntervention.model_validate(intervention_value)
        attribution = intervention.attribution
        if attribution is None:
            return None
        state_root = sessions_dir.resolve().parent
        if sessions_dir.name != "chat-sessions" or state_root.name != ".bug-agent":
            return None
        candidate_id = intervention.checkpoint.checkpoint_id
        output = state_root / "optimization" / "candidates" / f"{candidate_id}.json"
        if output.is_file():
            existing = json.loads(output.read_text(encoding="utf-8"))
            return output if verify_record(existing) else None
        record = {
            "schema_version": 1,
            "record_kind": "agent_optimization_candidate",
            "candidate_id": candidate_id,
            "status": "pending_review",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source": {"session_id": session_id, "turn_index": turn_index},
            "case": {
                "source": (task or {}).get("source"),
                "issue_key": (task or {}).get("issue_key"),
                "objective": (task or {}).get("objective"),
            },
            "target": attribution.target,
            "inferred": attribution.inferred,
            "rationale": attribution.rationale,
            "suggested_change": _SUGGESTIONS[attribution.target],
            "checkpoint": intervention.checkpoint.model_dump(mode="json"),
            "human_response": intervention.response,
            "observed_effect": {
                "subsequent_tools": intervention.subsequent_tools,
                "subsequent_skill_activations": intervention.subsequent_skill_activations,
            },
            "acceptance_gate": {
                "requires_human_review": True,
                "required_regression": "在固定模型的同类黄金 Case 上减少该类人工检查点，且不降低 Evidence grounding。",
            },
        }
        seal_record(record)
        _atomic_write(output, json.dumps(record, ensure_ascii=False, indent=2))
        return output
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Agent 优化候选落盘失败: %s", exc)
        return None
