from __future__ import annotations

import json

from bug_agent.presentation import parse_report_output, present_conversation_answer


def hypothesis_report() -> dict:
    return {
        "conclusion_status": "hypothesis_only",
        "summary": "DVR 卡死由资源竞争引发。",
        "observed_symptom": "DVR 画面卡死",
        "failure_mechanism": None,
        "root_cause": None,
        "hypotheses": [{
            "statement": "新旧 Task 可能竞争相机资源",
            "confidence": 0.62,
            "status": "candidate",
            "missing_evidence": ["CameraService 客户端归属"],
            "falsification": "核对事故窗口 CameraService 日志",
        }],
        "missing_evidence": ["重启直接触发证据"],
    }


def test_parser_accepts_model_preamble_and_mislabeled_code_fence():
    raw = "以下是完整 RCA：\n```arduino\n" + json.dumps(hypothesis_report()) + "\n```"

    report, structured = parse_report_output(raw)

    assert structured is True
    assert report.conclusion_status == "hypothesis_only"


def test_structured_rca_has_separate_human_and_machine_representations():
    raw = json.dumps(hypothesis_report(), ensure_ascii=False)

    presented = present_conversation_answer(raw, task_id="BAIC-47248", tool_events=[])

    assert presented.content_format == "markdown"
    assert presented.report is not None
    assert presented.report.summary == "DVR 卡死由资源竞争引发。"
    assert "# 阶段性 RCA：BAIC-47248" in presented.content
    assert "当前没有已验证的技术根因" in presented.content
    assert "## 5. 假设与证伪" in presented.content


def test_ordinary_chat_answer_remains_plain_text():
    presented = present_conversation_answer(
        "还需要核对 CameraService 日志。",
        task_id="BAIC-47248",
        tool_events=[],
    )

    assert presented.content_format == "plain_text"
    assert presented.report is None
    assert presented.content == "还需要核对 CameraService 日志。"
