from __future__ import annotations

import json

from bug_agent.run_bundle import seal_record
from bug_agent.run_visualization import render_run_visualization


def test_visualization_embeds_verified_bundle_and_feedback_form(tmp_path):
    run_path = tmp_path / ".bug-agent" / "runs" / "run.json"
    run_path.parent.mkdir(parents=True)
    bundle = {
        "schema_version": 2,
        "bundle_kind": "agent_run",
        "task": {"task_id": "run-1", "objective": "检查 </script> 黑屏"},
        "result": {"status": "completed", "report": {"conclusion_status": "confirmed"}},
        "trace": [],
        "budget": {"actual": {"steps": 1, "tool_calls": 0, "duration_ms": 12}},
        "derived": {"evidence_registry": [], "claim_snapshot": {"claims": []}, "input_fingerprint": {}},
        "provenance": {"model": "model-a"},
    }
    seal_record(bundle)
    run_path.write_text(json.dumps(bundle), encoding="utf-8")

    output = render_run_visualization(run_path)
    html = output.read_text(encoding="utf-8")

    assert output.parent == tmp_path / ".bug-agent" / "visualizations"
    assert "稳定性 Agent 执行复盘" in html
    assert "人工复核反馈" in html
    assert "__RUN_BUNDLE_BASE64__" not in html
    assert "检查 </script> 黑屏" not in html
