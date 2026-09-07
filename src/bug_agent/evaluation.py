"""把人工复核反馈以旁路记录沉淀为真实稳定性黄金 Case。"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .contracts import IncidentIdentity
from .run_bundle import seal_record, sha256_value, verify_record
from .runstore import _atomic_write


class ReviewScores(BaseModel):
    incident_identity: int = Field(ge=0, le=4)
    evidence_grounding: int = Field(ge=0, le=4)
    causal_correctness: int = Field(ge=0, le=4)
    android_stability_coverage: int = Field(ge=0, le=4)
    actionability: int = Field(ge=0, le=4)


class GoldenExpectation(BaseModel):
    conclusion_status: Literal[
        "confirmed", "hypothesis_only", "insufficient_evidence"
    ] | None = None
    root_cause: str | None = Field(default=None, max_length=10_000)
    incident: IncidentIdentity | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    required_claims: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    required_missing_evidence: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    forbidden_skills: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    max_human_checkpoints: int = Field(default=0, ge=0)


class RunReview(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    verdict: Literal["accepted", "corrected", "rejected"]
    scores: ReviewScores
    labels: list[str] = Field(default_factory=list, max_length=50)
    notes: str = Field(default="", max_length=20_000)
    expectation: GoldenExpectation | None = None

    @model_validator(mode="after")
    def corrected_requires_expectation(self) -> "RunReview":
        if self.verdict == "corrected" and (
            self.expectation is None or not self.expectation.model_fields_set
        ):
            raise ValueError("corrected 复核必须提供非空 expectation")
        if self.verdict != "corrected" and self.expectation is not None:
            raise ValueError("只有 corrected 复核可以覆盖 expectation")
        if self.verdict == "accepted" and min(self.scores.model_dump().values()) < 3:
            raise ValueError("accepted 的五个稳定性维度评分都必须至少为 3")
        return self


def load_run_bundle(path: Path) -> dict:
    resolved = path.expanduser().resolve()
    try:
        bundle = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Run Bundle 不是合法 JSON") from exc
    if bundle.get("schema_version") != 2 or bundle.get("bundle_kind") != "agent_run":
        raise ValueError("只支持 Run Bundle v2")
    if not verify_record(bundle):
        raise ValueError("Run Bundle 完整性校验失败，拒绝写入反馈")
    if bundle.get("result") is None:
        raise ValueError("运行尚未结束，不能进入人工复核")
    return bundle


def _state_root(run_path: Path) -> Path:
    parent = run_path.expanduser().resolve().parent
    if parent.name != "runs" or parent.parent.name != ".bug-agent":
        raise ValueError("Run Bundle 必须位于 <case>/.bug-agent/runs/ 下")
    return parent.parent


def save_review(run_path: Path, review: RunReview) -> Path:
    bundle = load_run_bundle(run_path)
    review_id = str(uuid4())
    record = {
        "schema_version": 1,
        "record_kind": "run_review",
        "review_id": review_id,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "source_run": {
            "task_id": bundle["task"]["task_id"],
            "filename": run_path.name,
            "record_sha256": bundle["integrity"]["record_sha256"],
        },
        # 保留 expectation 的“明确覆盖”语义；未填写字段不能被默认 None/[]
        # 误当成复核者要求清空原值。
        "review": review.model_dump(exclude_unset=True),
    }
    seal_record(record)
    output = _state_root(run_path) / "reviews" / run_path.stem / f"{review_id}.json"
    _atomic_write(output, json.dumps(record, ensure_ascii=False, indent=2))
    return output


def _actual_expectation(bundle: dict) -> dict:
    report = bundle["result"]["report"]
    claims = bundle.get("derived", {}).get("claim_snapshot", {}).get("claims", [])
    return {
        "conclusion_status": report.get("conclusion_status"),
        "root_cause": report.get("root_cause"),
        "incident": report.get("incident"),
        "evidence_ids": list(report.get("root_cause_evidence_ids") or []),
        "required_claims": [
            item["statement"] for item in claims
            if item.get("kind") in {"observed_symptom", "failure_mechanism", "root_cause"}
            and item.get("statement")
        ],
        "forbidden_claims": [],
        "required_missing_evidence": list(report.get("missing_evidence") or []),
        "required_tools": [],
        "forbidden_tools": [],
        "required_skills": [],
        "forbidden_skills": [],
        "max_tool_calls": None,
        "max_human_checkpoints": 0,
    }


def _effective_expectation(bundle: dict, review: RunReview) -> dict:
    expected = _actual_expectation(bundle)
    if review.expectation is not None:
        # 只覆盖复核者明确填写的字段，允许 corrected 保留未修改部分。
        expected.update(review.expectation.model_dump(exclude_unset=True))
    return GoldenExpectation.model_validate(expected).model_dump()


def promote_review(run_path: Path, review_path: Path) -> Path:
    bundle = load_run_bundle(run_path)
    try:
        stored = json.loads(review_path.expanduser().resolve().read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Review 不是合法 JSON") from exc
    if stored.get("record_kind") != "run_review" or not verify_record(stored):
        raise ValueError("Review 完整性校验失败")
    if stored.get("source_run", {}).get("record_sha256") != bundle["integrity"]["record_sha256"]:
        raise ValueError("Review 与 Run Bundle 不匹配")
    review = RunReview.model_validate(stored["review"])
    if review.verdict == "rejected":
        raise ValueError("rejected 运行不能晋升为黄金 Case")
    validation = bundle.get("derived", {}).get("claim_snapshot", {}).get("validation")
    if review.verdict == "accepted" and (
        not isinstance(validation, dict) or validation.get("grounded") is not True
    ):
        raise ValueError("accepted 运行必须已经通过 Evidence grounding 校验")

    fingerprint = bundle.get("derived", {}).get("input_fingerprint", {})
    stable_key = {
        "artifact_manifest_sha256": fingerprint.get("artifact_manifest_sha256"),
        "case_ids": fingerprint.get("case_ids") or [],
        "objective": bundle["task"].get("objective"),
        "source": bundle["task"].get("source"),
        "issue_key": bundle["task"].get("issue_key"),
        "empty_case_fallback": (
            bundle["task"].get("case_path")
            if not fingerprint.get("artifact_count") else None
        ),
    }
    golden_id = "golden-" + sha256_value(stable_key)[:20]
    expected = _effective_expectation(bundle, review)
    evidence_ids = set(expected["evidence_ids"])
    registry = bundle.get("derived", {}).get("evidence_registry", [])
    registered_ids = {item.get("evidence_id") for item in registry}
    incident = expected.get("incident")
    incident_evidence_ids = set((incident or {}).get("evidence_ids") or [])
    unknown_ids = (evidence_ids | incident_evidence_ids) - registered_ids
    if unknown_ids:
        raise ValueError("黄金 Case 引用了本轮不存在的 Evidence ID: " + ", ".join(sorted(unknown_ids)))
    if expected.get("conclusion_status") == "confirmed":
        identity_anchored = bool(
            incident
            and incident_evidence_ids
            and (
                incident.get("verified_window")
                or incident.get("boot_identity")
                or incident.get("process_name")
                or incident.get("build_identity")
            )
        )
        if not expected.get("root_cause") or not evidence_ids or not identity_anchored:
            raise ValueError("confirmed 黄金 Case 必须有根因 Evidence 和事故身份锚点")
    output = (
        _state_root(run_path) / "evals" / "golden" / golden_id
        / f"{stored['review_id']}.json"
    )
    if output.is_file():
        existing = json.loads(output.read_text(encoding="utf-8"))
        if not verify_record(existing):
            raise ValueError("已有黄金 Case 版本完整性校验失败")
        return output
    record = {
        "schema_version": 1,
        "record_kind": "golden_case",
        "golden_case_id": golden_id,
        "revision_id": stored["review_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_run": stored["source_run"],
        "review": {
            "review_id": stored["review_id"],
            "reviewer": review.reviewer,
            "verdict": review.verdict,
            "scores": review.scores.model_dump(),
            "labels": review.labels,
            "notes": review.notes,
        },
        "task": bundle["task"],
        "input_fingerprint": fingerprint,
        "baseline_provenance": bundle.get("provenance"),
        "baseline_agent_metrics": bundle.get("derived", {}).get("agent_metrics"),
        "expected": expected,
        "gold_evidence": [
            item for item in registry
            if item.get("evidence_id") in (evidence_ids | incident_evidence_ids)
        ],
    }
    seal_record(record)
    _atomic_write(output, json.dumps(record, ensure_ascii=False, indent=2))
    return output


def load_review_file(path: Path) -> RunReview:
    try:
        return RunReview.model_validate_json(path.expanduser().read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("反馈文件不是合法 JSON") from exc


def _load_golden(path: Path) -> dict:
    try:
        golden = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("黄金 Case 不是合法 JSON") from exc
    if golden.get("record_kind") != "golden_case" or not verify_record(golden):
        raise ValueError("黄金 Case 完整性校验失败")
    return golden


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _claim_present(expected: str, actual: list[str]) -> bool:
    wanted = _normalized(expected)
    return any(
        wanted == candidate or wanted in candidate or candidate in wanted
        for candidate in map(_normalized, actual)
        if candidate
    )


def _incident_matches(expected: dict | None, actual: dict | None) -> tuple[bool, list[str]]:
    if not expected:
        return True, []
    if not actual:
        return False, ["candidate 缺少事故身份"]
    mismatches = []
    for key in ("boot_identity", "process_name", "pid", "build_identity", "system_domain"):
        if expected.get(key) is not None and expected.get(key) != actual.get(key):
            mismatches.append(key)
    expected_window = expected.get("verified_window")
    actual_window = actual.get("verified_window") or {}
    if expected_window:
        for key, value in expected_window.items():
            if value is not None and value != actual_window.get(key):
                mismatches.append(f"verified_window.{key}")
    return not mismatches, mismatches


def evaluate_run(run_path: Path, golden_path: Path) -> tuple[dict, Path]:
    """执行确定性的结构化 Eval；不使用模型自评或语义裁判。"""

    candidate = load_run_bundle(run_path)
    golden = _load_golden(golden_path)
    expected = golden["expected"]
    report = candidate["result"]["report"]
    claim_items = candidate.get("derived", {}).get("claim_snapshot", {}).get("claims", [])
    actual_claims = [item.get("statement", "") for item in claim_items]
    required_missing = [
        item for item in expected.get("required_claims", [])
        if not _claim_present(item, actual_claims)
    ]
    forbidden_present = [
        item for item in expected.get("forbidden_claims", [])
        if _claim_present(item, actual_claims)
    ]
    expected_evidence = set(expected.get("evidence_ids") or [])
    actual_root_evidence = set(report.get("root_cause_evidence_ids") or [])
    identity_ok, identity_mismatches = _incident_matches(
        expected.get("incident"), report.get("incident"),
    )
    expected_fp = golden.get("input_fingerprint", {})
    actual_fp = candidate.get("derived", {}).get("input_fingerprint", {})
    metadata_match = (
        expected_fp.get("artifact_manifest_sha256")
        == actual_fp.get("artifact_manifest_sha256")
    )
    content_required = expected_fp.get("complete_content_fingerprint") is True
    content_match = (
        expected_fp.get("source_content_sha256")
        == actual_fp.get("source_content_sha256")
        and actual_fp.get("complete_content_fingerprint") is True
    ) if content_required else None
    validation = candidate.get("derived", {}).get("claim_snapshot", {}).get("validation")
    metrics = candidate.get("derived", {}).get("agent_metrics", {})
    used_tools = set(metrics.get("tools", {}))
    used_skills = set(metrics.get("activated_skills", [])) | set(
        candidate.get("result", {}).get("applied_skills", [])
    )
    required_tools = set(expected.get("required_tools") or [])
    forbidden_tools = set(expected.get("forbidden_tools") or [])
    required_skills = set(expected.get("required_skills") or [])
    forbidden_skills = set(expected.get("forbidden_skills") or [])
    max_tool_calls = expected.get("max_tool_calls")
    checks = {
        "input_metadata_match": metadata_match,
        "input_content_match": content_match,
        "grounded": isinstance(validation, dict) and validation.get("grounded") is True,
        "conclusion_status_match": report.get("conclusion_status") == expected.get("conclusion_status"),
        "root_cause_match": _normalized(report.get("root_cause")) == _normalized(expected.get("root_cause")),
        "root_evidence_match": expected_evidence.issubset(actual_root_evidence),
        "incident_identity_match": identity_ok,
        "required_claims_present": not required_missing,
        "forbidden_claims_absent": not forbidden_present,
        "required_missing_evidence_present": all(
            _claim_present(item, report.get("missing_evidence") or [])
            for item in expected.get("required_missing_evidence", [])
        ),
        "required_tools_used": required_tools.issubset(used_tools),
        "forbidden_tools_avoided": not bool(forbidden_tools & used_tools),
        "required_skills_activated": required_skills.issubset(used_skills),
        "forbidden_skills_avoided": not bool(forbidden_skills & used_skills),
        "tool_call_budget_met": (
            metrics.get("analysis_tool_calls", 0) <= max_tool_calls
            if max_tool_calls is not None else None
        ),
        "human_checkpoint_budget_met": (
            metrics.get("human_checkpoint_count", 0)
            <= expected.get("max_human_checkpoints", 0)
        ),
    }
    required_checks = [value for value in checks.values() if value is not None]
    eval_id = str(uuid4())
    record = {
        "schema_version": 1,
        "record_kind": "eval_result",
        "eval_id": eval_id,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "golden_case_id": golden["golden_case_id"],
        "golden_revision_id": golden["revision_id"],
        "candidate_run": {
            "task_id": candidate["task"]["task_id"],
            "record_sha256": candidate["integrity"]["record_sha256"],
        },
        "passed": all(required_checks),
        "checks": checks,
        "details": {
            "missing_required_claims": required_missing,
            "present_forbidden_claims": forbidden_present,
            "missing_root_evidence_ids": sorted(expected_evidence - actual_root_evidence),
            "incident_identity_mismatches": identity_mismatches,
            "reproducibility_limited": not content_required,
            "agent_metrics": metrics,
            "missing_required_tools": sorted(required_tools - used_tools),
            "used_forbidden_tools": sorted(forbidden_tools & used_tools),
            "missing_required_skills": sorted(required_skills - used_skills),
            "used_forbidden_skills": sorted(forbidden_skills & used_skills),
        },
    }
    seal_record(record)
    output = (
        _state_root(run_path) / "evals" / "results" / golden["golden_case_id"]
        / f"{candidate['task']['task_id']}-{eval_id}.json"
    )
    _atomic_write(output, json.dumps(record, ensure_ascii=False, indent=2))
    return record, output
