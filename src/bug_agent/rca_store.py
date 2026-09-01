"""Case 级 RCA 状态、Markdown 快照和 append-only 事件存储。"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from uuid import uuid4

from .contracts import BugAnalysisTask
from .rca_state import RCAEvent, RCAState
from .runstore import resolve_run_dir

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def render_state_markdown(state: RCAState) -> str:
    def bullets(values):
        return "\n".join(f"- {v}" for v in values) if values else "- 无"
    title = state.metadata.task_id if state.metadata else (state.based_on_runs[0] if state.based_on_runs else "Case")
    lines = [
        f"# 阶段性 RCA：{title}", "", f"- Revision: {state.revision}", f"- 更新时间: {state.updated_at}",
        f"- 覆盖运行: {', '.join(state.based_on_runs) or '无'}", f"- 结论状态: **{state.conclusion_status}**", "",
        "## 摘要", "", state.summary or "暂无", "",
        "## 关键结论", "", f"- 观察症状：{state.observed_symptom or '尚未确认'}", f"- 故障机制：{state.failure_mechanism or '尚未确认'}", f"- 技术根因：{state.root_cause or '尚未确认'}", "",
        "## Claims", "", "| 类型 | 状态 | 置信度 | 陈述 | 证据 |", "|---|---|---|---|---|",
    ]
    for claim in state.claims:
        lines.append(f"| {claim.type} | {claim.status} | {claim.confidence} | {claim.statement} | {', '.join(claim.supporting_evidence_ids) or '无'} |")
    lines += ["", "## 已确认事实", "", bullets(state.confirmed_facts), "", "## 负向结果", ""]
    for finding in state.negative_findings:
        lines.append(f"- {finding.statement}（范围：{finding.scope}；限制：{finding.limitation}）")
    if not state.negative_findings:
        lines.append("- 无")
    lines += ["", "## 缺失证据", "", bullets(state.missing_evidence), "", "## 时间线", ""]
    for item in state.timeline:
        lines.append(f"- `{item.timestamp}` [{item.clock_domain}] {item.event} — {item.interpretation or ''}")
    if not state.timeline:
        lines.append("- 无")
    lines += ["", "## 分层覆盖", "", "| 层 | 状态 | 发现 | 缺口 |", "|---|---|---|---|"]
    for item in state.coverage:
        lines.append(f"| {item.layer} | {item.status} | {item.finding} | {item.gap or ''} |")
    if not state.coverage:
        lines.append("| - | - | 无 | - |")
    lines += ["", "## 行动项", "", "| 优先级 | 行动 | Owner | 预期产物 |", "|---|---|---|---|"]
    for item in state.actions:
        lines.append(f"| {item.priority} | {item.action} | {item.owner or ''} | {item.expected_artifact} |")
    if not state.actions:
        lines.append("| - | 无 | - | - |")
    lines += ["", "## 证据索引", ""]
    for evidence in state.evidence:
        lines.append(f"- **{evidence.evidence_id}** — `{evidence.relative_path}`{f':{evidence.line_start}' if evidence.line_start else ''} {evidence.excerpt or ''}")
    if not state.evidence:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def continuation_context(state: RCAState) -> str:
    """给后续 Agent 的精简 Case 状态，不暴露旧运行的完整 trace。"""
    payload = {
        "revision": state.revision,
        "conclusion_status": state.conclusion_status,
        "summary": state.summary,
        "root_cause": state.root_cause,
        "claims": [claim.model_dump() for claim in state.claims],
        "missing_evidence": state.missing_evidence,
        "actions": [action.model_dump() for action in state.actions],
        "evidence": [evidence.model_dump() for evidence in state.evidence],
        "based_on_runs": state.based_on_runs,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


class RCAStore:
    def __init__(self, task: BugAnalysisTask):
        run_dir = resolve_run_dir(task)
        if run_dir is None:
            raise ValueError("无法确定 RCA 落盘位置")
        self.root = run_dir.parent.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with _locks_guard:
            self.lock = _locks.setdefault(str(self.root), threading.RLock())

    @property
    def state_path(self) -> Path:
        return self.root / "rca-state.json"

    @property
    def markdown_path(self) -> Path:
        return self.root / "RCA.md"

    @property
    def events_path(self) -> Path:
        return self.root / "rca-events.jsonl"

    def load_state(self) -> RCAState | None:
        if not self.state_path.exists():
            return None
        return RCAState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def _stage(self, path: Path, content: str) -> Path:
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        with temp.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temp

    def commit(self, state: RCAState, events: list[RCAEvent]) -> None:
        # 先完成两个快照的 staging，再 replace；任何 staging 失败都不会污染旧快照。
        state_json = json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
        markdown = render_state_markdown(state)
        state_tmp = markdown_tmp = None
        try:
            state_tmp = self._stage(self.state_path, state_json)
            markdown_tmp = self._stage(self.markdown_path, markdown)
            state_tmp.replace(self.state_path)
            markdown_tmp.replace(self.markdown_path)
            if events:
                with self.events_path.open("a", encoding="utf-8") as stream:
                    for event in events:
                        stream.write(json.dumps(event.model_dump(mode="json"), ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
        finally:
            for temp in (state_tmp, markdown_tmp):
                if temp is not None and temp.exists():
                    temp.unlink()
