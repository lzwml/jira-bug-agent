from __future__ import annotations

import json

import pytest

from bug_agent.evaluation import (
    ReviewScores, RunReview, evaluate_run, promote_review, save_review,
)
from bug_agent.run_bundle import seal_record, verify_record


def _bundle(path):
    bundle = {
        "schema_version": 2,
        "bundle_kind": "agent_run",
        "task": {
            "task_id": "run-1", "source": "local", "issue_key": None,
            "case_path": str(path.parents[2]), "objective": "定位黑屏根因",
        },
        "result": {"report": {
            "conclusion_status": "hypothesis_only",
            "root_cause": None,
            "root_cause_evidence_ids": [],
            "incident": {
                "process_name": "surfaceflinger",
                "evidence_ids": ["ev-1"],
            },
            "missing_evidence": ["缺少 tombstone"],
        }},
        "provenance": {"model": "model-a"},
        "derived": {
            "input_fingerprint": {"artifact_manifest_sha256": "abc"},
            "claim_snapshot": {
                "claims": [{"kind": "observed_symptom", "statement": "启动黑屏"}],
                "validation": {"grounded": True},
            },
            "evidence_registry": [{
                "evidence_id": "ev-1", "relative_path": "logcat.txt",
            }],
        },
    }
    seal_record(bundle)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bundle), encoding="utf-8")
    return path


def _scores():
    return ReviewScores(
        incident_identity=4,
        evidence_grounding=4,
        causal_correctness=3,
        android_stability_coverage=3,
        actionability=4,
    )


def test_review_is_append_only_sidecar_and_promotes_corrected_golden_case(tmp_path):
    run_path = _bundle(tmp_path / ".bug-agent" / "runs" / "run-1.json")
    review = RunReview.model_validate({
        "reviewer": "android-stability",
        "verdict": "corrected",
        "scores": _scores().model_dump(),
        "labels": ["framework", "black-screen"],
        "notes": "原结果缺少直接根因。",
        "expectation": {
            "conclusion_status": "confirmed",
            "root_cause": "SurfaceFlinger 在启动阶段崩溃",
            "evidence_ids": ["ev-1"],
            "required_claims": ["启动黑屏", "SurfaceFlinger 崩溃"],
            "forbidden_claims": ["应用自身 ANR"],
        },
    })

    review_path = save_review(run_path, review)
    golden_path = promote_review(run_path, review_path)

    stored_review = json.loads(review_path.read_text(encoding="utf-8"))
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    assert verify_record(stored_review)
    assert verify_record(golden)
    assert golden["expected"]["root_cause"] == "SurfaceFlinger 在启动阶段崩溃"
    assert golden["expected"]["incident"]["process_name"] == "surfaceflinger"
    assert golden["gold_evidence"][0]["evidence_id"] == "ev-1"
    assert golden_path.parent.name == golden["golden_case_id"]


def test_rejected_review_cannot_be_promoted(tmp_path):
    run_path = _bundle(tmp_path / ".bug-agent" / "runs" / "run-1.json")
    review_path = save_review(run_path, RunReview(
        reviewer="reviewer", verdict="rejected", scores=_scores(),
    ))
    with pytest.raises(ValueError, match="rejected"):
        promote_review(run_path, review_path)


def test_tampered_bundle_cannot_receive_feedback(tmp_path):
    run_path = _bundle(tmp_path / ".bug-agent" / "runs" / "run-1.json")
    bundle = json.loads(run_path.read_text(encoding="utf-8"))
    bundle["task"]["objective"] = "tampered"
    run_path.write_text(json.dumps(bundle), encoding="utf-8")
    with pytest.raises(ValueError, match="完整性"):
        save_review(run_path, RunReview(
            reviewer="reviewer", verdict="accepted", scores=_scores(),
        ))


def test_accepted_review_requires_engineering_usable_scores():
    scores = _scores().model_copy(update={"causal_correctness": 2})
    with pytest.raises(ValueError, match="至少为 3"):
        RunReview(reviewer="reviewer", verdict="accepted", scores=scores)


def test_deterministic_eval_detects_forbidden_claim(tmp_path):
    run_path = _bundle(tmp_path / ".bug-agent" / "runs" / "run-1.json")
    review = RunReview.model_validate({
        "reviewer": "android-stability",
        "verdict": "corrected",
        "scores": _scores().model_dump(),
        "expectation": {
            "conclusion_status": "hypothesis_only",
            "root_cause": None,
            "evidence_ids": [],
            "required_claims": ["启动黑屏"],
            "forbidden_claims": ["应用自身 ANR"],
        },
    })
    golden_path = promote_review(run_path, save_review(run_path, review))
    candidate = json.loads(run_path.read_text(encoding="utf-8"))
    candidate["derived"]["claim_snapshot"]["claims"].append({
        "kind": "root_cause", "statement": "应用自身 ANR",
    })
    seal_record(candidate)
    run_path.write_text(json.dumps(candidate), encoding="utf-8")

    result, output = evaluate_run(run_path, golden_path)
    assert result["passed"] is False
    assert result["checks"]["required_claims_present"] is True
    assert result["checks"]["forbidden_claims_absent"] is False
    assert output.is_file()
    assert verify_record(json.loads(output.read_text(encoding="utf-8")))
