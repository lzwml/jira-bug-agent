"""可回放、可比较、可校验的 Agent Run Bundle v2。"""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .contracts import BugAnalysisResult, BugAnalysisTask
from .models import AgentRunResult, ToolEvent
from .report_validation import build_evidence_registry


SCHEMA_VERSION = 2


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _package_version() -> str:
    try:
        return metadata.version("jira-bug-agent")
    except metadata.PackageNotFoundError:
        return "unknown"


def _git_revision() -> tuple[str, str]:
    configured = os.getenv("BUG_AGENT_BUILD_REVISION", "").strip()
    if configured:
        return configured, "environment"
    for parent in [Path(__file__).resolve().parent, *Path(__file__).resolve().parents]:
        marker = parent / ".git"
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            line = marker.read_text(encoding="utf-8", errors="replace").strip()
            if not line.startswith("gitdir:"):
                continue
            git_dir = (parent / line.partition(":")[2].strip()).resolve()
        else:
            continue
        try:
            head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
            common_dir = git_dir
            common_marker = git_dir / "commondir"
            if common_marker.is_file():
                common_dir = (
                    git_dir / common_marker.read_text(encoding="ascii").strip()
                ).resolve()
            if head.startswith("ref:"):
                ref = head.partition(":")[2].strip()
                for loose in (git_dir / ref, common_dir / ref):
                    if loose.is_file():
                        return loose.read_text(encoding="ascii").strip(), "git_head"
                packed = common_dir / "packed-refs"
                if packed.is_file():
                    for row in packed.read_text(encoding="ascii", errors="replace").splitlines():
                        if row and not row.startswith(("#", "^")):
                            revision, _, name = row.partition(" ")
                            if name == ref:
                                return revision, "git_head"
            elif head:
                return head, "git_head"
        except OSError:
            pass
    return "unknown", "unavailable"


def build_execution_context(
    *,
    model: str,
    system_prompt: str | None,
    instruction: str | None,
    tool_schema: list[dict[str, Any]],
    skill_documents: Iterable[Any],
    max_steps: int,
    max_tool_calls: int,
    max_run_seconds: float,
    max_tool_result_chars: int,
) -> dict[str, Any]:
    """只保存复现所需的身份和哈希，不落盘 URL、Token 或 Prompt 原文。"""

    skills = []
    for document in skill_documents:
        skills.append({
            "name": document.name,
            "sha256": sha256_text(document.instructions),
        })
    revision, revision_source = _git_revision()
    return {
        "package": {"name": "jira-bug-agent", "version": _package_version()},
        "build_revision": revision,
        "build_revision_source": revision_source,
        "build_revision_authoritative": revision_source == "environment",
        "model": model,
        "prompts": {
            "ready": system_prompt is not None and instruction is not None,
            "system_sha256": sha256_text(system_prompt) if system_prompt is not None else None,
            "instruction_sha256": sha256_text(instruction) if instruction is not None else None,
        },
        "tool_schema_sha256": sha256_value(tool_schema),
        "skills": sorted(skills, key=lambda item: item["name"]),
        "budgets": {
            "max_steps": max_steps,
            "max_tool_calls": max_tool_calls,
            "max_run_seconds": max_run_seconds,
            "max_tool_result_chars": max_tool_result_chars,
        },
    }


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _payloads(events: list[ToolEvent]) -> Iterable[tuple[ToolEvent, Any]]:
    for event in events:
        if not event.success:
            continue
        try:
            yield event, json.loads(event.result)
        except (json.JSONDecodeError, TypeError):
            continue


def _dynamic_skill_hashes(events: list[ToolEvent]) -> list[dict[str, str]]:
    skills: dict[str, str] = {}
    for event, payload in _payloads(events):
        if event.tool_name != "activate_skill":
            continue
        for item in _walk(payload):
            name = item.get("skill") or item.get("name")
            instructions = item.get("instructions")
            if isinstance(name, str) and isinstance(instructions, str):
                skills[name] = sha256_text(instructions)
    return [{"name": name, "sha256": digest} for name, digest in sorted(skills.items())]


def build_input_fingerprint(events: list[ToolEvent]) -> dict[str, Any]:
    """记录本次实际可见输入的清单；明确区分内容哈希和元数据哈希。"""

    artifacts: dict[str, dict[str, Any]] = {}
    case_ids: set[str] = set()
    for event, payload in _payloads(events):
        for item in _walk(payload):
            case_id = item.get("case_id")
            if isinstance(case_id, str) and case_id:
                case_ids.add(case_id)
            fingerprint = item.get("source_fingerprint")
            relative_path = item.get("relative_path")
            artifact_id = item.get("artifact_id")
            if not isinstance(relative_path, str) or not relative_path:
                continue
            if artifact_id is None and not any(
                key in item for key in ("size_bytes", "modified_at", "kind", "origin")
            ):
                continue
            descriptor = {
                key: item[key]
                for key in (
                    "artifact_id", "relative_path", "kind", "size_bytes", "modified_at",
                    "origin", "source_archive_id",
                )
                if item.get(key) is not None
            }
            if isinstance(fingerprint, dict) and isinstance(fingerprint.get("sha256"), str):
                descriptor["content_sha256"] = fingerprint["sha256"]
                if fingerprint.get("size_bytes") is not None:
                    descriptor["content_size_bytes"] = fingerprint["size_bytes"]
                if fingerprint.get("mtime_ns") is not None:
                    descriptor["content_mtime_ns"] = fingerprint["mtime_ns"]
            key = str(artifact_id or relative_path.replace("\\", "/"))
            artifacts.setdefault(key, {}).update(descriptor)
    manifest = sorted(artifacts.values(), key=lambda item: (
        str(item.get("relative_path", "")), str(item.get("artifact_id", "")),
    ))
    content_hashes = {
        item["content_sha256"] for item in manifest if item.get("content_sha256")
    }
    content_hashed_artifacts = sum(
        1 for item in manifest if item.get("content_sha256")
    )
    return {
        "case_ids": sorted(case_ids),
        "artifact_manifest": manifest,
        "artifact_manifest_sha256": sha256_value(manifest),
        "artifact_count": len(manifest),
        "content_hashed_source_count": content_hashed_artifacts,
        "source_content_sha256": sorted(content_hashes),
        "complete_content_fingerprint": (
            bool(manifest) and content_hashed_artifacts == len(manifest)
        ),
        "coverage_note": (
            "普通 Artifact 使用路径、大小和修改时间构成元数据指纹；"
            "仅工具明确返回 source_fingerprint 时才计为内容哈希。"
        ),
        "execution_trace_sha256": sha256_value([
            event.model_dump() for event in events
        ]),
    }


def build_claim_snapshot(result: BugAnalysisResult) -> dict[str, Any]:
    report = result.report
    claims: list[dict[str, Any]] = []
    for kind, statement, evidence_ids in (
        ("observed_symptom", report.observed_symptom, []),
        ("failure_mechanism", report.failure_mechanism, []),
        ("root_cause", report.root_cause, report.root_cause_evidence_ids),
    ):
        if statement:
            claims.append({
                "kind": kind,
                "statement": statement,
                "evidence_ids": list(evidence_ids),
            })
    for hypothesis in report.hypotheses:
        claims.append({
            "kind": "hypothesis",
            "statement": hypothesis.statement,
            "status": hypothesis.status,
            "confidence": hypothesis.confidence,
            "supporting_evidence_ids": hypothesis.supporting_evidence_ids,
            "contradicting_evidence_ids": hypothesis.contradicting_evidence_ids,
            "missing_evidence": hypothesis.missing_evidence,
        })
    return {
        "conclusion_status": report.conclusion_status,
        "claims": claims,
        "missing_evidence": report.missing_evidence,
        "validation": (
            result.report_validation.model_dump() if result.report_validation else None
        ),
    }


def build_derived_views(
    run: AgentRunResult | None,
    result: BugAnalysisResult,
) -> dict[str, Any]:
    events = run.tool_events if run else []
    registry = build_evidence_registry(events)
    return {
        "evidence_registry": [
            registry[key].model_dump() for key in sorted(registry)
        ],
        "claim_snapshot": build_claim_snapshot(result),
        "input_fingerprint": build_input_fingerprint(events),
        "agent_metrics": build_agent_metrics(events, result),
    }


def build_agent_metrics(
    events: list[ToolEvent],
    result: BugAnalysisResult,
) -> dict[str, Any]:
    """只度量 Agent 控制面，不把模型文风或知识能力混入指标。"""

    analysis_events = [
        event for event in events if event.tool_name != "request_human_guidance"
    ]
    signatures: dict[str, int] = {}
    tools: dict[str, dict[str, int]] = {}
    activated_skills: list[str] = []
    checkpoints: list[dict[str, Any]] = []
    for event in events:
        if event.tool_name == "request_human_guidance" and event.success:
            try:
                checkpoint = json.loads(event.result)["data"]["human_checkpoint"]
                checkpoints.append(checkpoint)
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            continue
        signature = event.tool_name + ":" + sha256_value(event.arguments)
        signatures[signature] = signatures.get(signature, 0) + 1
        item = tools.setdefault(event.tool_name, {"calls": 0, "successes": 0, "failures": 0})
        item["calls"] += 1
        item["successes" if event.success else "failures"] += 1
        if event.tool_name == "activate_skill" and event.success:
            name = event.arguments.get("name")
            if isinstance(name, str):
                activated_skills.append(name)
    return {
        "analysis_tool_calls": len(analysis_events),
        "successful_tool_calls": sum(event.success for event in analysis_events),
        "failed_tool_calls": sum(not event.success for event in analysis_events),
        "repeated_identical_calls": sum(max(0, count - 1) for count in signatures.values()),
        "tools": tools,
        "activated_skills": list(dict.fromkeys(activated_skills)),
        "human_checkpoint_count": len(checkpoints),
        "human_checkpoint_categories": [item.get("category") for item in checkpoints],
        "autonomous_completion": result.status in {"completed", "insufficient_evidence"},
    }


def seal_record(record: dict[str, Any]) -> dict[str, Any]:
    record.pop("integrity", None)
    record["integrity"] = {
        "algorithm": "sha256",
        "record_sha256": sha256_value(record),
    }
    return record


def verify_record(record: dict[str, Any]) -> bool:
    integrity = record.get("integrity")
    if not isinstance(integrity, dict) or integrity.get("algorithm") != "sha256":
        return False
    expected = integrity.get("record_sha256")
    payload = dict(record)
    payload.pop("integrity", None)
    return isinstance(expected, str) and expected == sha256_value(payload)


def enrich_provenance(
    provenance: dict[str, Any] | None,
    events: list[ToolEvent],
) -> dict[str, Any] | None:
    if provenance is None:
        return None
    merged = dict(provenance)
    skills = {
        item["name"]: item["sha256"]
        for item in merged.get("skills", [])
        if isinstance(item, dict) and "name" in item and "sha256" in item
    }
    for item in _dynamic_skill_hashes(events):
        skills[item["name"]] = item["sha256"]
    merged["skills"] = [
        {"name": name, "sha256": digest} for name, digest in sorted(skills.items())
    ]
    return merged


def build_budget_usage(
    run: AgentRunResult | None,
    provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    configured = (provenance or {}).get("budgets", {})
    return {
        "configured": configured,
        "actual": {
            "steps": run.steps if run else 0,
            "tool_calls": len(run.tool_events) if run else 0,
            "total_tool_result_chars": (
                sum(len(event.result) for event in run.tool_events) if run else 0
            ),
            "max_single_tool_result_chars": (
                max((len(event.result) for event in run.tool_events), default=0) if run else 0
            ),
            "duration_ms": None,
            "token_usage": None,
            "termination": run.status if run else "preparation_failed",
            "budget_error": run.error if run and run.error_type == "AgentBudgetExceeded" else None,
        },
    }
