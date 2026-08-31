from bug_agent.contracts import BugAnalysisResult, BugAnalysisTask, EvidenceReference, Hypothesis, RCAReport
from bug_agent.rca_reconciliation import compare_claims, initialize_state, reconcile_state
from bug_agent.rca_state import Claim


def _result(task_id, root, status="hypothesis_only", evidence="e1"):
    return BugAnalysisResult(task_id=task_id, status="completed", steps=1, structured_output=True, report=RCAReport(
        conclusion_status=status, summary=task_id, root_cause=root,
        hypotheses=[Hypothesis(statement=root, confidence=.8, status="supported", supporting_evidence_ids=[evidence])],
        evidence=[EvidenceReference(evidence_id=evidence, relative_path="log.txt")],
    ))


def test_claim_relations_are_conservative():
    old = Claim(claim_id="a", type="root_cause", statement="A", status="supported", created_run_id="1")
    same = old.model_copy()
    extra = old.model_copy(update={"supporting_evidence_ids": ["e1"]})
    other = Claim(claim_id="b", type="root_cause", statement="B", status="supported", created_run_id="2")
    unrelated = Claim(claim_id="c", type="observed_symptom", statement="B", status="supported", created_run_id="2")
    assert compare_claims(old, same) == "same"
    assert compare_claims(old, extra) == "supports"
    assert compare_claims(old, other) == "contradicts"
    assert compare_claims(old, unrelated) == "unrelated"


def test_confirmed_root_cause_is_not_auto_overwritten():
    task1 = BugAnalysisTask(task_id="r1", source="local", case_path=".")
    state, _ = initialize_state(task1, _result("r1", "A", "confirmed"))
    task2 = BugAnalysisTask(task_id="r2", source="local", case_path=".")
    updated, events = reconcile_state(state, task2, _result("r2", "B"))
    assert updated.root_cause == "A"
    assert any(c.statement == "B" and c.status == "candidate" for c in updated.claims)
    assert events
