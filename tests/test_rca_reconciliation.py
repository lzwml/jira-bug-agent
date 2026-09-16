from bug_agent.application.rca_reconciliation import _validate_actions, compare_claims, initialize_state, reconcile_state
from bug_agent.domain.contracts import ActionItem, BugAnalysisResult, BugAnalysisTask, EvidenceReference, Hypothesis, RCAReport
from bug_agent.domain.rca_state import Claim


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


def _action(text: str, priority="P1"):
    return ActionItem(priority=priority, action=text, expected_artifact="日志", completion_criteria="完成")


def test_validate_actions_demotes_hallucinated_aplog_refs():
    evidence = [EvidenceReference(
        evidence_id="e1", relative_path="outer.zip!/APLog_2026_0831_061532__79.tar.gz",
    )]

    result = _validate_actions([_action("展开 APLog_88~APLog_92 嵌套 tar.gz")], evidence)

    assert result[0].priority == "P3"
    assert "引用未在证据中出现" in result[0].action


def test_validate_actions_preserves_valid_aplog_refs():
    evidence = [EvidenceReference(
        evidence_id="e1", relative_path="outer.zip!/APLog_2026_0831_061532__79.tar.gz",
    )]

    result = _validate_actions([_action("复核 APLog_79 中的 SuspendAll 证据")], evidence)

    assert result[0].priority == "P1"
    assert "引用未在证据中出现" not in result[0].action


def test_validate_actions_demotes_unverifiable_file_refs_but_keeps_general_actions():
    evidence = [EvidenceReference(evidence_id="e1", relative_path="logs/main_log.txt")]

    result = _validate_actions([
        _action("复核 logs/main_log.txt 的异常"),
        _action("展开 missing/crash.tar.gz"),
        _action("补充 GC SuspendAll 的线程栈"),
    ], evidence)

    assert [item.priority for item in result] == ["P1", "P3", "P1"]
