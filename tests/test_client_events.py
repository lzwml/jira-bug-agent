from __future__ import annotations

import json

from bug_agent.api.client_events import locations_from_tool_event
from bug_agent.models import ToolEvent


def _event(data: dict) -> ToolEvent:
    return ToolEvent(
        step=1,
        tool_call_id="call-1",
        tool_name="inspect_archive",
        arguments={},
        result=json.dumps({"success": True, "data": data}),
        success=True,
    )


def test_archive_member_location_reports_not_extracted(tmp_path):
    archive = tmp_path / "attachments" / "android.zip"
    archive.parent.mkdir()
    archive.write_bytes(b"archive")

    locations = locations_from_tool_event(_event({
        "archive_relative_path": "attachments/android.zip",
        "members": [{
            "member_id": "member-1",
            "member_path": "APLog/main_log.txt",
            "extracted": False,
        }],
    }), case_root=tmp_path)

    member = next(item for item in locations if item.member_id == "member-1")
    assert member.relative_path == "attachments/android.zip!/APLog/main_log.txt"
    assert member.archive_relative_path == "attachments/android.zip"
    assert member.member_path == "APLog/main_log.txt"
    assert member.resolved_path is None
    assert member.availability == "not_extracted"


def test_nested_archive_virtual_path_resolves_to_extracted_file(tmp_path):
    extracted = tmp_path / "attachments" / "android" / "APLog" / "main_log.txt"
    extracted.parent.mkdir(parents=True)
    extracted.write_text("fatal", encoding="utf-8")
    virtual = "attachments/android.zip!/APLog.tar.gz!/main_log.txt"

    locations = locations_from_tool_event(_event({
        "evidence": [{
            "evidence_id": "ev-1",
            "relative_path": virtual,
            "line_start": 9,
        }],
    }), case_root=tmp_path)

    assert locations[0].relative_path == virtual
    assert locations[0].resolved_path == str(extracted.resolve())
    assert locations[0].availability == "ready"


def test_location_cannot_escape_case_root(tmp_path):
    outside = tmp_path.parent / "outside.log"
    outside.write_text("secret", encoding="utf-8")

    locations = locations_from_tool_event(_event({
        "relative_path": "../outside.log",
    }), case_root=tmp_path)

    assert locations[0].resolved_path is None
    assert locations[0].availability == "missing"


def test_single_gzip_virtual_path_resolves_to_output_file(tmp_path):
    extracted = tmp_path / "kernel.log"
    extracted.write_text("panic", encoding="utf-8")

    locations = locations_from_tool_event(_event({
        "relative_path": "kernel.log.gz!/kernel.log",
    }), case_root=tmp_path)

    assert locations[0].resolved_path == str(extracted.resolve())
    assert locations[0].availability == "ready"
