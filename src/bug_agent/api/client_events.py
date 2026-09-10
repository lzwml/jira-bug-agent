"""把内部 Tool Event 收敛为版本化、可定位的客户端事件。"""

from __future__ import annotations

import json
from typing import Any

from ..models import ToolEvent
from .models import ClientLocation


_PATH_KEYS = (
    "relative_path", "extracted_path", "frame_path", "path", "member_path",
)
_IDENTIFIER_KEYS = (
    "evidence_id", "artifact_id", "finding_id", "member_id", "name",
)


def locations_from_tool_event(event: ToolEvent, *, limit: int = 100) -> list[ClientLocation]:
    try:
        payload = json.loads(event.result)
    except (json.JSONDecodeError, TypeError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, (dict, list)):
        return []

    found: list[ClientLocation] = []
    seen: set[tuple[str, int | None, str | None]] = set()

    def walk(value: Any, archive_path: str | None = None) -> None:
        if len(found) >= limit:
            return
        if isinstance(value, list):
            for child in value:
                walk(child, archive_path)
            return
        if not isinstance(value, dict):
            return
        local_archive = archive_path
        candidate_archive = value.get("archive_relative_path")
        if isinstance(candidate_archive, str) and candidate_archive:
            local_archive = candidate_archive
        location = next((
            value.get(key) for key in _PATH_KEYS
            if isinstance(value.get(key), str) and value.get(key)
        ), None)
        if location:
            line_start = value.get("line_start")
            line_start = line_start if isinstance(line_start, int) and line_start > 0 else None
            identifier = next((
                str(value[key]) for key in _IDENTIFIER_KEYS if value.get(key) is not None
            ), None)
            identity = (location, line_start, identifier)
            if identity not in seen:
                seen.add(identity)
                line_end = value.get("line_end")
                line_end = line_end if isinstance(line_end, int) and line_end > 0 else None
                excerpt = value.get("excerpt") or value.get("content")
                found.append(ClientLocation(
                    relative_path=location,
                    line_start=line_start,
                    line_end=line_end,
                    identifier=identifier,
                    excerpt=str(excerpt)[:2000] if excerpt else None,
                    archive_relative_path=local_archive,
                    member_id=str(value["member_id"]) if value.get("member_id") else None,
                ))
        for child in value.values():
            if isinstance(child, (dict, list)):
                walk(child, local_archive)

    walk(data)
    return found
