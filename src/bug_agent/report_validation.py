"""把模型生成的 RCA 引用反向绑定到本次运行的真实工具证据。"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .contracts import EvidenceReference, Hypothesis, RCAReport, ReportValidation
from .models import ToolEvent


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _to_reference(item: dict[str, Any]) -> EvidenceReference | None:
    """识别 Log、Diagnostic、Timeline 与 Video MCP 返回的证据对象。"""

    evidence_id = item.get("evidence_id") or item.get("event_id")
    relative_path = item.get("relative_path")
    if not isinstance(evidence_id, str) or not evidence_id:
        return None
    if not isinstance(relative_path, str) or not relative_path:
        return None

    line_start = item.get("line_start")
    line_end = item.get("line_end")
    if line_start is None and isinstance(item.get("line_number"), int):
        line_start = line_end = item["line_number"]
    excerpt = item.get("content")
    if excerpt is None:
        excerpt = item.get("description")
    if excerpt is not None:
        excerpt = str(excerpt)[:4000]
    try:
        return EvidenceReference(
            evidence_id=evidence_id,
            artifact_id=(
                str(item["artifact_id"])
                if item.get("artifact_id") is not None
                else str(item["video_id"]) if item.get("video_id") is not None else None
            ),
            relative_path=relative_path,
            line_start=line_start,
            line_end=line_end,
            timestamp_ms=item.get("timestamp_ms"),
            frame_path=item.get("frame_path"),
            excerpt=excerpt,
        )
    except ValueError:
        return None


def build_evidence_registry(events: list[ToolEvent]) -> dict[str, EvidenceReference]:
    """只从成功、可完整解析的工具结果构建可信 Evidence Registry。"""

    registry: dict[str, EvidenceReference] = {}
    for event in events:
        if not event.success:
            continue
        try:
            payload = json.loads(event.result)
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk(payload):
            reference = _to_reference(item)
            if reference is not None:
                registry.setdefault(reference.evidence_id, reference)
    return registry


def _reference_matches(
    reported: EvidenceReference,
    actual: EvidenceReference,
) -> bool:
    if reported.relative_path.replace("\\", "/") != actual.relative_path.replace("\\", "/"):
        return False
    if reported.artifact_id is not None and reported.artifact_id != actual.artifact_id:
        return False
    for field in ("line_start", "line_end", "timestamp_ms", "frame_path"):
        value = getattr(reported, field)
        if value is not None and value != getattr(actual, field):
            return False
    if reported.excerpt and actual.excerpt and reported.excerpt not in actual.excerpt:
        return False
    return True


def _filter_ids(values: list[str], valid_ids: set[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value in valid_ids))


def _declared_reference_ids(report: RCAReport) -> set[str]:
    values = set(report.root_cause_evidence_ids)
    if report.incident is not None:
        values.update(report.incident.evidence_ids)
    for item in report.timeline:
        values.update(item.evidence_ids)
    for item in report.coverage:
        values.update(item.evidence_ids)
    for item in report.negative_findings:
        values.update(item.evidence_ids)
    for item in report.hypotheses:
        values.update(item.supporting_evidence_ids)
        values.update(item.contradicting_evidence_ids)
    return values


def validate_report(
    report: RCAReport,
    events: list[ToolEvent],
    *,
    strict: bool = True,
) -> tuple[RCAReport, ReportValidation]:
    """校验 RCA 引用；严格模式下移除伪造引用并降级无证据 confirmed。"""

    registry = build_evidence_registry(events)
    rejected: list[str] = []
    verified: list[EvidenceReference] = []
    for reported in report.evidence:
        actual = registry.get(reported.evidence_id)
        if actual is None or not _reference_matches(reported, actual):
            rejected.append(reported.evidence_id)
            continue
        # 使用工具记录的身份字段；只保留确实出现在原始证据中的模型短摘录。
        excerpt = reported.excerpt if reported.excerpt else actual.excerpt
        verified.append(actual.model_copy(update={"excerpt": excerpt}))

    valid_ids = {item.evidence_id for item in verified}
    issues: list[str] = []
    if rejected:
        issues.append("报告包含无法从本次工具轨迹验证的证据引用")
    dangling_ids = _declared_reference_ids(report) - valid_ids
    if dangling_ids:
        rejected.extend(sorted(dangling_ids))
        issues.append("报告字段引用了未进入可信 Evidence Registry 的证据 ID")

    incident = report.incident
    if incident is not None:
        incident = incident.model_copy(update={
            "evidence_ids": _filter_ids(incident.evidence_ids, valid_ids),
        })
    timeline = [item.model_copy(update={
        "evidence_ids": _filter_ids(item.evidence_ids, valid_ids),
    }) for item in report.timeline]
    coverage = [item.model_copy(update={
        "evidence_ids": _filter_ids(item.evidence_ids, valid_ids),
    }) for item in report.coverage]
    negative = [item.model_copy(update={
        "evidence_ids": _filter_ids(item.evidence_ids, valid_ids),
    }) for item in report.negative_findings]
    hypotheses = [item.model_copy(update={
        "supporting_evidence_ids": _filter_ids(item.supporting_evidence_ids, valid_ids),
        "contradicting_evidence_ids": _filter_ids(item.contradicting_evidence_ids, valid_ids),
    }) for item in report.hypotheses]

    conclusion = report.conclusion_status
    root_cause = report.root_cause
    root_cause_evidence_ids = _filter_ids(report.root_cause_evidence_ids, valid_ids)
    confirmed_facts = list(report.confirmed_facts)
    missing = list(report.missing_evidence)
    incident_anchored = bool(
        incident is not None
        and incident.evidence_ids
        and (
            incident.verified_window is not None
            or incident.boot_identity
            or incident.process_name
            or incident.build_identity
        )
    )
    if conclusion == "confirmed" and (
        not root_cause or not root_cause_evidence_ids or not incident_anchored
    ):
        issues.append("confirmed 结论缺少已验证的根因引用或事故身份锚点")
        if root_cause:
            hypotheses.append(Hypothesis(
                statement=root_cause,
                confidence=0.0,
                status="candidate",
                missing_evidence=["根因缺少可从本次工具轨迹验证的 Evidence 引用"],
                falsification="补充并引用能够直接验证该根因的日志或诊断证据",
            ))
        conclusion = "hypothesis_only"
        root_cause = None
        confirmed_facts = []
        missing.append("报告的 confirmed 结论未通过工具证据校验")

    grounded = not issues
    validation = ReportValidation(
        grounded=grounded,
        verified_evidence_count=len(verified),
        rejected_evidence_ids=list(dict.fromkeys(rejected)),
        issues=issues,
    )
    if not strict:
        return report, validation
    return report.model_copy(update={
        "incident": incident,
        "timeline": timeline,
        "coverage": coverage,
        "negative_findings": negative,
        "hypotheses": hypotheses,
        "evidence": verified,
        "conclusion_status": conclusion,
        "root_cause": root_cause,
        "root_cause_evidence_ids": root_cause_evidence_ids,
        "confirmed_facts": confirmed_facts,
        "missing_evidence": list(dict.fromkeys(missing)),
    }), validation
