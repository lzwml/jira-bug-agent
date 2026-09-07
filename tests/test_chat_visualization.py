from __future__ import annotations

import json

import pytest

from bug_agent.chat_visualization import (
    build_return_preview,
    load_chat_session,
    render_chat_visualization,
    render_safe_markdown,
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
    site_dir = tmp_path / ".bug-agent" / "visualizations" / "chat-CASE-1"

    assert output == site_dir / "index.html"
    assert (tmp_path / ".bug-agent" / "visualizations" / "chat-CASE-1.html").is_file()
    assert {item.name for item in site_dir.iterdir()} == {
        "index.html", "conversation.html", "tools.html", "optimization.html",
        "session-data.js", "common.js", "site.css",
    }
    assert "复盘概览" in (site_dir / "index.html").read_text(encoding="utf-8")
    assert "会话过程" in (site_dir / "conversation.html").read_text(encoding="utf-8")
    tools_html = (site_dir / "tools.html").read_text(encoding="utf-8")
    assert "核对工具" in tools_html
    optimization_html = (site_dir / "optimization.html").read_text(encoding="utf-8")
    assert "导出复盘结论" in optimization_html
    common_js = (site_dir / "common.js").read_text(encoding="utf-8")
    assert "当前版本后补" in common_js
    assert "可核对的文件/证据位置" in common_js
    assert "返回是否满足需要" in common_js
    data_js = (site_dir / "session-data.js").read_text(encoding="utf-8")
    assert "检查 </script> 重启" not in data_js


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


def test_safe_markdown_renders_reports_without_allowing_html_injection():
    rendered = render_safe_markdown(
        "## 总结\n\n**根因**：`system_server`\n\n"
        "| 时间 | 事件 |\n|---|---|\n| 09:32 | crash |\n\n"
        "```text\nFatal signal\n```\n<script>alert(1)</script>"
    )

    assert "<h2>总结</h2>" in rendered
    assert "<strong>根因</strong>" in rendered
    assert "<table>" in rendered
    assert '<code class="language-text">Fatal signal</code>' in rendered
    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
