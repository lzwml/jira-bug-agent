import json

from bug_agent.contracts import BugAnalysisTask
from bug_agent.rca_reconciliation import reconcile
from bug_agent.contracts import BugAnalysisResult, RCAReport


def test_store_writes_one_case_snapshot_and_append_only_events(tmp_path):
    task = BugAnalysisTask(task_id="run-1", source="local", case_path=str(tmp_path))
    result = BugAnalysisResult(task_id="run-1", status="completed", steps=1, structured_output=True, report=RCAReport(conclusion_status="insufficient_evidence", summary="insufficient"))
    assert reconcile(task, result) is not None
    root = tmp_path / ".bug-agent"
    assert (root / "rca-state.json").exists()
    assert (root / "RCA.md").exists()
    events = [json.loads(line) for line in (root / "rca-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0]["event"] == "state_initialized"
    assert not list((root / "runs").glob("*.md"))
