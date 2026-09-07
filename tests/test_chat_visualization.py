from __future__ import annotations

import json

import pytest

from bug_agent.chat_visualization import (
    build_return_preview,
    load_chat_session,
    render_chat_visualization,
)


def test_chat_visualization_embeds_session_and_feedback_controls(tmp_path):
    path = tmp_path / ".bug-agent" / "chat-sessions" / "chat-CASE-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 2,
        "session_id": "chat-CASE-1",
        "status": "closed",
        "task": {"issue_key": "CASE-1", "objective": "检查 </script> 重启"},
        "turns": [{
            "turn_index": 1,
            "user_message": "这个判断不对",
            "assistant_answer": "你说得对，需要纠正",
            "steps": 1,
            "agent_status": "completed",
            "tool_events": [],
        }],
    }), encoding="utf-8")

    output = render_chat_visualization(path)
    html = output.read_text(encoding="utf-8")

    assert output.parent == tmp_path / ".bug-agent" / "visualizations"
    assert "Agent 问题复盘" in html
    assert "导出复盘结论" in html
    assert "先看结果" in html
    assert "每轮都是一次“人工输入 → Agent 响应”" in html
    assert "工具调用与参数契约" in html
    assert "当前版本后补" in html
    assert "返回是否满足需要" in html
    assert "tool_reviews" in html
    assert "可人工核对的文件/证据位置" in html
    assert "__CHAT_SESSION_BASE64__" not in html
    assert "检查 </script> 重启" not in html


def test_chat_visualization_rejects_non_session_json(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="缺少 turns"):
        load_chat_session(path)


def test_return_preview_surfaces_artifact_paths_and_identifiers():
    preview = build_return_preview(json.dumps({
        "success": True,
        "data": {
            "artifact_count": 1,
            "artifacts": [{
                "artifact_id": "artifact-1",
                "relative_path": "attachments/android.zip",
                "kind": "archive",
                "size_bytes": 123,
            }],
        },
    }))

    assert preview["location_count"] == 1
    assert preview["locations"] == [{
        "location": "attachments/android.zip",
        "identifier": "artifact-1",
        "details": "archive · 123 bytes",
    }]
    assert preview["facts"] == [{"name": "artifact_count", "value": "1"}]
