"""把一次 Agent 结果合并进 Case 级 RCA 状态。"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .contracts import ActionItem, BugAnalysisResult, BugAnalysisTask, EvidenceReference, Hypothesis
from .rca_state import Claim, RCAEvent, RCAEventDetail, RCAState, RCAMetadata, stable_claim_id


def _confidence(value: float) -> str:
    if value >= .85:
        return "high"
    if value >= .70:
        return "medium_high"
    if value >= .45:
        return "medium"
    return "low"


def _status(h: Hypothesis, conclusion: str) -> str:
    if h.status == "rejected":
        return "rejected"
    if h.status == "supported":
        return "supported"
    return "confirmed" if conclusion == "confirmed" and h.confidence >= .85 else "candidate"


def _claim(claim_type: str, statement: str | None, run_id: str, *, status="candidate", confidence="low", evidence=None, missing=None, falsification=None) -> Claim | None:
    if not statement or not statement.strip():
        return None
    return Claim(
        claim_id=stable_claim_id(claim_type, statement), type=claim_type, statement=statement.strip(),
        status=status, confidence=confidence, supporting_evidence_ids=list(evidence or []),
        missing_evidence=list(missing or []), falsification=falsification, created_run_id=run_id,
    )


def claims_from_result(task: BugAnalysisTask, result: BugAnalysisResult) -> list[Claim]:
    report = result.report
    claims: list[Claim] = []
    for typ, text in (("observed_symptom", report.observed_symptom), ("failure_mechanism", report.failure_mechanism)):
        claim = _claim(typ, text, task.task_id, status="supported" if text else "candidate", confidence="medium", evidence=[e.evidence_id for e in report.evidence])
        if claim:
            claims.append(claim)
    root = _claim("root_cause", report.root_cause, task.task_id, status="confirmed" if report.conclusion_status == "confirmed" else "supported", confidence="high" if report.conclusion_status == "confirmed" else "medium_high", evidence=report.root_cause_evidence_ids)
    if root:
        claims.append(root)
    for hypothesis in report.hypotheses:
        claim = _claim("root_cause", hypothesis.statement, task.task_id, status=_status(hypothesis, report.conclusion_status), confidence=_confidence(hypothesis.confidence), evidence=hypothesis.supporting_evidence_ids, missing=hypothesis.missing_evidence, falsification=hypothesis.falsification)
        if claim:
            claim.contradicting_evidence_ids = list(hypothesis.contradicting_evidence_ids)
            claims.append(claim)
    # A single run can mention the same claim in root_cause and hypotheses.
    unique: dict[str, Claim] = {}
    for claim in claims:
        if claim.claim_id not in unique:
            unique[claim.claim_id] = claim
        else:
            old = unique[claim.claim_id]
            old.supporting_evidence_ids = sorted(set(old.supporting_evidence_ids + claim.supporting_evidence_ids))
            if claim.status == "confirmed":
                old.status, old.confidence = claim.status, claim.confidence
    return list(unique.values())


def compare_claims(old: Claim, new: Claim) -> str:
    """有限规则匹配：不做自然语言语义推断。"""
    if old.type != new.type:
        return "unrelated"
    if old.component and new.component and old.component != new.component:
        return "unrelated"
    normalize = lambda value: re.sub(r"\s+", " ", value.strip().casefold())
    if normalize(old.statement) == normalize(new.statement):
        return "supports" if set(new.supporting_evidence_ids) - set(old.supporting_evidence_ids) else "same"
    if old.type == "root_cause" and (old.status in {"supported", "confirmed"} or new.status in {"supported", "confirmed"}):
        return "contradicts"
    return "extends"


def _merge_list(old: list, new: list, key) -> list:
    result = list(old)
    seen = {key(item) for item in result}
    for item in new:
        if key(item) not in seen:
            result.append(item)
            seen.add(key(item))
    return result


_UNVERIFIED_ACTION_MARKER = "（⚠ 引用未在证据中出现，需人工确认）"
_APLOG_REFERENCE_RE = re.compile(r"\bAPLog(?:[_\s-]+)(\d{1,3})(?![\d_])", re.IGNORECASE)
_APLOG_MEMBER_RE = re.compile(
    r"(?:^|[!/])APLog_\d{4}_\d{4}_\d{6}__(\d+)(?:\.[^/]+)*(?:$|/)", re.IGNORECASE,
)
_FILE_REFERENCE_RE = re.compile(
    r"(?<![\w.])([\w-]+(?:[\\/][\w.-]+)*\.(?:tar\.gz|zip|tar|tgz|gz|log|txt|trace))(?![\w.])",
    re.IGNORECASE,
)


def _extract_aplog_refs(text: str) -> set[str]:
    """Extract short APLog sequence references, never the YYYY portion of a full name."""

    return {match.group(1).lstrip("0") or "0" for match in _APLOG_REFERENCE_RE.finditer(text)}


def _evidence_aplog_sequences(evidence: list[EvidenceReference]) -> set[str]:
    sequences: set[str] = set()
    for item in evidence:
        sequences.update(match.group(1).lstrip("0") or "0" for match in _APLOG_MEMBER_RE.finditer(item.relative_path))
    return sequences


def _extract_file_refs(text: str) -> set[str]:
    return {match.group(1).replace("\\", "/").casefold() for match in _FILE_REFERENCE_RE.finditer(text)}


def _validate_actions(actions: list[ActionItem], evidence: list[EvidenceReference]) -> list[ActionItem]:
    """Demote actions that name APLog volumes or files absent from registered evidence.

    This is intentionally a lexical guard, not a natural-language truth detector.  Actions
    with no identifiable resource reference remain actionable; an action that explicitly
    names a resource must be grounded in a tool-returned relative path.
    """

    evidence_paths = {item.relative_path.replace("\\", "/").casefold() for item in evidence}
    aplog_sequences = _evidence_aplog_sequences(evidence)
    validated: list[ActionItem] = []
    for item in actions:
        aplog_refs = _extract_aplog_refs(item.action)
        file_refs = _extract_file_refs(item.action)
        missing_aplogs = aplog_refs - aplog_sequences
        missing_files = {
            ref for ref in file_refs
            if not any(ref in path for path in evidence_paths)
        }
        if missing_aplogs or missing_files:
            action = item.action if _UNVERIFIED_ACTION_MARKER in item.action else item.action + _UNVERIFIED_ACTION_MARKER
            item = item.model_copy(update={"action": action, "priority": "P3"})
        validated.append(item)
    return validated


def _action_key(item: ActionItem) -> tuple[str, str]:
    return item.priority, re.sub(r"\s+", " ", item.action.strip()).casefold()


def _event(revision: int, kind: str, run_id: str, claim_id=None, **kwargs) -> RCAEvent:
    return RCAEvent(revision=revision, event=kind, run_id=run_id, detail=RCAEventDetail(claim_id=claim_id, **kwargs))


def initialize_state(task: BugAnalysisTask, result: BugAnalysisResult) -> tuple[RCAState, list[RCAEvent]]:
    report = result.report
    now = datetime.now(timezone.utc).isoformat()
    state = RCAState(
        revision=1, updated_at=now, based_on_runs=[task.task_id], conclusion_status=report.conclusion_status,
        summary=report.summary, observed_symptom=report.observed_symptom, failure_mechanism=report.failure_mechanism,
        root_cause=report.root_cause if report.conclusion_status == "confirmed" else None,
        trigger_conditions=list(report.trigger_conditions), timeline=list(report.timeline), coverage=list(report.coverage),
        confirmed_facts=list(report.confirmed_facts), claims=claims_from_result(task, result),
        negative_findings=list(report.negative_findings), missing_evidence=list(report.missing_evidence),
        actions=_validate_actions(report.actions, report.evidence), evidence=list(report.evidence), metadata=RCAMetadata(task_id=task.task_id, steps=result.steps, skills=list(result.applied_skills)),
    )
    return state, [_event(1, "state_initialized", task.task_id, reason="首次运行初始化 RCA 状态")]


def reconcile_state(current: RCAState, task: BugAnalysisTask, result: BugAnalysisResult) -> tuple[RCAState, list[RCAEvent]]:
    incoming = result.report
    revision = current.revision + 1
    now = datetime.now(timezone.utc).isoformat()
    state = current.model_copy(deep=True)
    state.revision, state.updated_at = revision, now
    if task.task_id not in state.based_on_runs:
        state.based_on_runs.append(task.task_id)
    state.metadata = RCAMetadata(task_id=task.task_id, steps=result.steps, skills=list(result.applied_skills))
    events: list[RCAEvent] = []

    old_evidence = {e.evidence_id for e in state.evidence}
    state.evidence = _merge_list(state.evidence, incoming.evidence, lambda e: e.evidence_id)
    added = [e.evidence_id for e in incoming.evidence if e.evidence_id not in old_evidence]
    if added:
        events.append(_event(revision, "evidence_added", task.task_id, evidence_ids=added, reason="新增证据"))

    old_claims = {c.claim_id: c for c in state.claims}
    strong_conflict = False
    for new in claims_from_result(task, result):
        matches = [old for old in old_claims.values() if compare_claims(old, new) != "unrelated"]
        exact = old_claims.get(new.claim_id)
        if exact:
            relation = compare_claims(exact, new)
            before = exact.status
            exact.supporting_evidence_ids = sorted(set(exact.supporting_evidence_ids + new.supporting_evidence_ids))
            exact.contradicting_evidence_ids = sorted(set(exact.contradicting_evidence_ids + new.contradicting_evidence_ids))
            exact.missing_evidence = sorted(set(exact.missing_evidence + new.missing_evidence))
            rank = {"candidate": 0, "supported": 1, "confirmed": 2, "disputed": 1, "rejected": 0, "superseded": 0}
            if rank[new.status] > rank[exact.status] and exact.status not in {"disputed", "rejected", "superseded"}:
                exact.status, exact.confidence = new.status, new.confidence
            events.append(_event(revision, "claim_updated", task.task_id, exact.claim_id, reason=relation, evidence_ids=new.supporting_evidence_ids))
            if before != exact.status:
                events.append(_event(revision, "claim_status_changed", task.task_id, exact.claim_id, from_status=before, to_status=exact.status, reason="新运行提供更强支持"))
            continue
        conflicts = [old for old in matches if compare_claims(old, new) == "contradicts"]
        if conflicts:
            for old in conflicts:
                if old.status == "confirmed":
                    # 已确认根因不能被自动覆盖或降级。
                    new.status = "candidate"
                    new.missing_evidence.append("需人工裁决与已确认根因的冲突")
                    strong_conflict = True
                elif new.status in {"supported", "confirmed"}:
                    before = old.status
                    old.status = "disputed"
                    old.missing_evidence.append("存在后续运行提出的冲突根因")
                    events.append(_event(revision, "claim_status_changed", task.task_id, old.claim_id, from_status=before, to_status="disputed", reason="新运行提出冲突根因"))
                    strong_conflict = True
        state.claims.append(new)
        old_claims[new.claim_id] = new
        events.append(_event(revision, "claim_created", task.task_id, new.claim_id, reason="新运行产生 Claim", evidence_ids=new.supporting_evidence_ids))

    # 未覆盖的层不能推翻已有结论；新结论只在有明确值时补充。
    state.observed_symptom = state.observed_symptom or incoming.observed_symptom
    state.failure_mechanism = state.failure_mechanism or incoming.failure_mechanism
    if incoming.root_cause and state.root_cause is None and incoming.conclusion_status == "confirmed" and not strong_conflict:
        state.root_cause = incoming.root_cause
        events.append(_event(revision, "root_cause_changed", task.task_id, reason="确认根因"))
    state.trigger_conditions = _merge_list(state.trigger_conditions, incoming.trigger_conditions, lambda x: x)
    state.confirmed_facts = _merge_list(state.confirmed_facts, incoming.confirmed_facts, lambda x: x)
    state.missing_evidence = _merge_list(state.missing_evidence, incoming.missing_evidence, lambda x: x)
    state.negative_findings = _merge_list(state.negative_findings, incoming.negative_findings, lambda x: (x.statement, x.scope))
    incoming_actions = _validate_actions(incoming.actions, state.evidence)
    state.actions = _merge_list(state.actions, incoming_actions, _action_key)
    state.timeline = _merge_list(state.timeline, incoming.timeline, lambda x: (x.timestamp, x.event))
    coverage_by_layer = {item.layer: item for item in state.coverage}
    coverage_rank = {"not_applicable": 0, "not_covered": 1, "partial": 2, "covered": 3}
    for item in incoming.coverage:
        previous = coverage_by_layer.get(item.layer)
        if previous is None:
            state.coverage.append(item)
            coverage_by_layer[item.layer] = item
            continue
        previous.evidence_ids = sorted(set(previous.evidence_ids + item.evidence_ids))
        if coverage_rank[item.status] >= coverage_rank[previous.status]:
            previous.status, previous.finding = item.status, item.finding
        previous.gap = item.gap or previous.gap
    confirmed = any(c.type == "root_cause" and c.status == "confirmed" for c in state.claims)
    disputed = any(c.type == "root_cause" and c.status == "disputed" for c in state.claims)
    if confirmed and not disputed:
        state.conclusion_status = "confirmed"
    elif state.claims:
        state.conclusion_status = "hypothesis_only"
    else:
        state.conclusion_status = "insufficient_evidence"
    if result.status != "failed" and not strong_conflict and incoming.summary:
        state.summary = incoming.summary
    elif strong_conflict:
        state.missing_evidence = _merge_list(state.missing_evidence, ["后续运行提出了与既有根因冲突的 Claim，需人工裁决"], lambda x: x)
    return state, events


def reconcile(task: BugAnalysisTask, result: BugAnalysisResult, run=None, config=None):
    """Worker 边界调用的容错入口；存储失败只记录 warning。"""
    from .rca_store import RCAStore
    import logging
    try:
        store = RCAStore(task)
        with store.lock:
            current = store.load_state()
            state, events = initialize_state(task, result) if current is None else reconcile_state(current, task, result)
            store.commit(state, events)
            return state
    except Exception as exc:  # RCA 是辅助能力，不能让 Worker 失败
        logging.getLogger(__name__).warning("RCA reconciliation failed (task_id=%s): %s", task.task_id, exc)
        return None
