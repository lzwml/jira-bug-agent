"""把内部 Tool Event 收敛为版本化、可定位的客户端事件。"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from ..models import ToolEvent
from ..contracts import EvidenceReference
from .models import ClientLocation


_PATH_KEYS = (
    "relative_path", "extracted_path", "frame_path", "path",
)
_IDENTIFIER_KEYS = (
    "evidence_id", "artifact_id", "finding_id", "member_id", "name",
)


def _extraction_destination(archive_path: Path) -> Path:
    name = archive_path.name.casefold()
    if name.endswith(".gz") and not name.endswith((".tar.gz", ".tgz")):
        return archive_path.with_suffix("")
    for suffix in (".tar.gz", ".tgz", ".zip", ".tar", ".7z", ".7z.001"):
        if name.endswith(suffix):
            return archive_path.with_name(archive_path.name[:-len(suffix)])
    return archive_path.with_name(archive_path.name + ".unpacked")


def _is_single_gzip(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith(".gz") and not name.endswith((".tar.gz", ".tgz"))


def _safe_case_path(case_root: Path, raw_path: str) -> Path | None:
    if not raw_path or "!/" in raw_path:
        return None
    candidate = Path(raw_path)
    target = candidate.resolve(strict=False) if candidate.is_absolute() else (
        case_root / Path(*PurePosixPath(raw_path.replace("\\", "/")).parts)
    ).resolve(strict=False)
    try:
        target.relative_to(case_root)
    except ValueError:
        return None
    return target


def _resolve_virtual_path(case_root: Path, virtual_path: str) -> Path | None:
    parts = virtual_path.replace("\\", "/").split("!/")
    current = _safe_case_path(case_root, parts[0])
    if current is None:
        return None
    for member in parts[1:]:
        pure = PurePosixPath(member)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            return None
        destination = _extraction_destination(current)
        if _is_single_gzip(current):
            if len(pure.parts) != 1 or pure.name.casefold() != destination.name.casefold():
                return None
            current = destination.resolve(strict=False)
        else:
            current = (destination / Path(*pure.parts)).resolve(strict=False)
        try:
            current.relative_to(case_root)
        except ValueError:
            return None
    return current


def _resolve_location(case_root: Path | None, raw_path: str) -> tuple[str | None, str]:
    if case_root is None:
        return None, "missing"
    target = (
        _resolve_virtual_path(case_root, raw_path)
        if "!/" in raw_path
        else _safe_case_path(case_root, raw_path)
    )
    if target is not None and target.is_file():
        return str(target), "ready"
    return None, "not_extracted" if "!/" in raw_path else "missing"


def locations_from_evidence(
    evidence: list[EvidenceReference], *, case_root: Path | None = None,
) -> list[ClientLocation]:
    resolved_root = case_root.resolve() if case_root is not None else None
    locations: list[ClientLocation] = []
    for item in evidence:
        raw_path = item.frame_path or item.relative_path
        resolved_path, availability = _resolve_location(resolved_root, raw_path)
        virtual_parts = raw_path.replace("\\", "/").split("!/")
        locations.append(ClientLocation(
            relative_path=raw_path,
            line_start=item.line_start,
            line_end=item.line_end,
            identifier=item.evidence_id,
            excerpt=item.excerpt,
            archive_relative_path=(
                "!/".join(virtual_parts[:-1]) if len(virtual_parts) > 1 else None
            ),
            member_path=virtual_parts[-1] if len(virtual_parts) > 1 else None,
            case_root=str(resolved_root) if resolved_root else None,
            resolved_path=resolved_path,
            availability=availability,
        ))
    return locations


def locations_from_tool_event(
    event: ToolEvent, *, case_root: Path | None = None, limit: int = 100,
) -> list[ClientLocation]:
    try:
        payload = json.loads(event.result)
    except (json.JSONDecodeError, TypeError):
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, (dict, list)):
        return []

    found: list[ClientLocation] = []
    seen: set[tuple[str, int | None, str | None]] = set()

    resolved_root = case_root.resolve() if case_root is not None else None

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
        member_path = value.get("member_path")
        member_path = member_path if isinstance(member_path, str) and member_path else None
        if location is None and member_path:
            location = f"{local_archive}!/{member_path}" if local_archive else member_path
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
                resolved_path, availability = _resolve_location(resolved_root, location)
                found.append(ClientLocation(
                    relative_path=location,
                    line_start=line_start,
                    line_end=line_end,
                    identifier=identifier,
                    excerpt=str(excerpt)[:2000] if excerpt else None,
                    archive_relative_path=local_archive,
                    member_id=str(value["member_id"]) if value.get("member_id") else None,
                    member_path=member_path,
                    case_root=str(resolved_root) if resolved_root else None,
                    resolved_path=resolved_path,
                    availability=availability,
                ))
        for child in value.values():
            if isinstance(child, (dict, list)):
                walk(child, local_archive)

    walk(data)
    return found
