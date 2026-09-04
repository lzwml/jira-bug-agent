from __future__ import annotations

import gzip
import io
from pathlib import Path
import tarfile
import zipfile

import pytest

from log_analyzer.archive_manager import ArchiveLimits, ArchiveRejected
import log_analyzer.archive_selection as archive_selection
from log_analyzer.archive_selection import (
    extract_archive_members, inventory_archive, parse_path_timestamp, summarize_member_times,
)
from datetime import datetime


LIMITS = ArchiveLimits(
    max_archive_bytes=20 * 1024 * 1024,
    max_members=100,
    max_member_bytes=20 * 1024 * 1024,
    max_expanded_bytes=40 * 1024 * 1024,
    max_compression_ratio=1000,
)


def _by_path(inventory):
    return {item.member_path: item for item in inventory.members}


def test_zip_inventory_and_selective_incremental_extraction(tmp_path: Path):
    path = tmp_path / "logs.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("logs/main.log", "main evidence")
        archive.writestr("logs/radio.log", "radio evidence")

    first_inventory = inventory_archive(path, "artifact-1", LIMITS)
    second_inventory = inventory_archive(path, "artifact-1", LIMITS)
    members = _by_path(first_inventory)
    assert [item.member_id for item in first_inventory.members] == [item.member_id for item in second_inventory.members]
    assert not first_inventory.truncated
    assert all(item.safe for item in first_inventory.members)

    first = extract_archive_members(path, "artifact-1", [members["logs/main.log"].member_id], LIMITS)
    assert (first.destination / "logs/main.log").read_text() == "main evidence"
    assert not (first.destination / "logs/radio.log").exists()
    assert not first.members[0].reused

    second = extract_archive_members(path, "artifact-1", [
        members["logs/main.log"].member_id,
        members["logs/radio.log"].member_id,
    ], LIMITS)
    assert second.members[0].reused
    assert not second.members[1].reused
    assert (second.destination / "logs/radio.log").read_text() == "radio evidence"


def test_incremental_expansion_hard_links_existing_payload(monkeypatch, tmp_path: Path):
    path = tmp_path / "large.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("logs/first.log", "first payload")
        archive.writestr("logs/second.log", "second payload")
    members = _by_path(inventory_archive(path, "artifact-links", LIMITS))
    extract_archive_members(
        path, "artifact-links", [members["logs/first.log"].member_id], LIMITS
    )
    linked_sources = []
    real_link = archive_selection.os.link

    def recording_link(source, target):
        linked_sources.append(Path(source))
        return real_link(source, target)

    monkeypatch.setattr(archive_selection.os, "link", recording_link)
    expanded = extract_archive_members(
        path, "artifact-links", [members["logs/second.log"].member_id], LIMITS
    )

    assert any(source.name == "first.log" for source in linked_sources)
    assert (expanded.destination / "logs/first.log").read_text() == "first payload"
    assert (expanded.destination / "logs/second.log").read_text() == "second payload"


def test_inventory_truncates_without_writing_output(tmp_path: Path):
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.log", "1")
        archive.writestr("two.log", "2")

    result = inventory_archive(path, "artifact-many", LIMITS, max_members=1)

    assert result.truncated
    assert len(result.members) == 1
    assert not (tmp_path / "many.zip.unpacked").exists()


def test_generic_path_timestamp_and_directory_summary_do_not_require_a_product_format(tmp_path: Path):
    path = tmp_path / "mixed.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("logs/round-a_2026-09-01_12-00-00.log", "payload")
        archive.writestr("logs/other_20260901_120100.log", "payload")
        archive.writestr("logs/context.log", "payload")
    inventory = inventory_archive(path, "mixed-time", LIMITS)

    assert parse_path_timestamp("logs/round-a_2026-09-01_12-00-00.log") is None
    # Only documented compact/underscore patterns are facts; unsupported spelling is not guessed.
    assert parse_path_timestamp("logs/other_20260901_120100.log").value == datetime(2026, 9, 1, 12, 1)
    summary = summarize_member_times(inventory.members)
    assert summary == [{
        "path_prefix": "logs/",
        "member_count": 3,
        "timestamped_member_count": 1,
        "untimestamped_member_count": 2,
        "earliest_path_time": "2026-09-01T12:01:00",
        "latest_path_time": "2026-09-01T12:01:00",
    }]


def test_tar_and_single_gzip_are_selectively_extracted(tmp_path: Path):
    tar_path = tmp_path / "logs.tar.gz"
    payload = b"tar evidence"
    with tarfile.open(tar_path, "w:gz") as archive:
        info = tarfile.TarInfo("nested/system.log")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    tar_inventory = inventory_archive(tar_path, "tar-artifact", LIMITS)
    tar_result = extract_archive_members(
        tar_path, "tar-artifact", [tar_inventory.members[0].member_id], LIMITS
    )
    assert (tar_result.destination / "nested/system.log").read_bytes() == payload

    gzip_path = tmp_path / "kernel.log.gz"
    gzip_path.write_bytes(gzip.compress(b"gzip evidence"))
    gzip_inventory = inventory_archive(gzip_path, "gzip-artifact", LIMITS)
    assert gzip_inventory.members[0].member_path == "kernel.log"
    gzip_result = extract_archive_members(
        gzip_path, "gzip-artifact", [gzip_inventory.members[0].member_id], LIMITS
    )
    assert (gzip_result.destination / "kernel.log").read_bytes() == b"gzip evidence"


def test_unknown_and_unsafe_member_ids_are_rejected(tmp_path: Path):
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../escape.log", "bad")
        archive.writestr("safe.log", "good")
    result = inventory_archive(path, "artifact-unsafe", LIMITS)
    unsafe = next(item for item in result.members if not item.safe)
    assert unsafe.reason == "ARCHIVE_PATH_TRAVERSAL"

    for member_id in ("invented-id", unsafe.member_id):
        with pytest.raises(ArchiveRejected) as caught:
            extract_archive_members(path, "artifact-unsafe", [member_id], LIMITS)
        assert caught.value.code == "ARCHIVE_MEMBER_ID_INVALID"
    assert not (tmp_path / "escape.log").exists()


def test_source_change_resets_managed_destination(tmp_path: Path):
    path = tmp_path / "changing.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("same.log", "old")
        archive.writestr("removed.log", "remove me")
    old_inventory = inventory_archive(path, "artifact-change", LIMITS)
    old = _by_path(old_inventory)
    extract_archive_members(path, "artifact-change", [
        old["same.log"].member_id,
        old["removed.log"].member_id,
    ], LIMITS)

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("same.log", "new and different")
    current = inventory_archive(path, "artifact-change", LIMITS)
    result = extract_archive_members(path, "artifact-change", [current.members[0].member_id], LIMITS)

    assert result.reset
    assert (result.destination / "same.log").read_text() == "new and different"
    assert not (result.destination / "removed.log").exists()


def test_member_id_is_bound_to_archive_content(tmp_path: Path):
    path = tmp_path / "changing-id.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("same.log", "old")
    old_id = inventory_archive(path, "artifact-versioned", LIMITS).members[0].member_id

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("same.log", "new content")
    new_id = inventory_archive(path, "artifact-versioned", LIMITS).members[0].member_id

    assert old_id != new_id
    with pytest.raises(ArchiveRejected) as caught:
        extract_archive_members(path, "artifact-versioned", [old_id], LIMITS)
    assert caught.value.code == "ARCHIVE_MEMBER_ID_INVALID"


def test_multi_round_selection_enforces_cumulative_expanded_budget(tmp_path: Path):
    limits = ArchiveLimits(
        max_archive_bytes=1024 * 1024,
        max_members=10,
        max_member_bytes=10,
        max_expanded_bytes=6,
        max_compression_ratio=1000,
    )
    path = tmp_path / "budget.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.log", "1111")
        archive.writestr("two.log", "2222")
    members = _by_path(inventory_archive(path, "artifact-budget", limits))
    extract_archive_members(path, "artifact-budget", [members["one.log"].member_id], limits)

    with pytest.raises(ArchiveRejected) as caught:
        extract_archive_members(path, "artifact-budget", [members["two.log"].member_id], limits)

    assert caught.value.code == "ARCHIVE_EXPANDED_LIMIT"
    assert not (tmp_path / "budget.zip.unpacked" / "two.log").exists()


def test_inventory_rejects_reserved_manifest_and_prefix_conflicts(tmp_path: Path):
    path = tmp_path / "conflicts.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(".extraction-manifest.json", "not metadata")
        archive.writestr("a", "file")
        archive.writestr("a/b.log", "child")

    inventory = inventory_archive(path, "artifact-conflicts", LIMITS)
    by_path = _by_path(inventory)

    assert by_path[".extraction-manifest.json"].reason == "ARCHIVE_RESERVED_PATH"
    assert by_path["a"].reason == "ARCHIVE_PATH_CONFLICT"
    assert by_path["a/b.log"].reason == "ARCHIVE_PATH_CONFLICT"


def test_extra_user_content_makes_destination_a_conflict(tmp_path: Path):
    path = tmp_path / "logs.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("one.log", "one")
        archive.writestr("two.log", "two")
    inventory = inventory_archive(path, "artifact-conflict", LIMITS)
    members = _by_path(inventory)
    result = extract_archive_members(path, "artifact-conflict", [members["one.log"].member_id], LIMITS)
    (result.destination / "notes.txt").write_text("user content")

    with pytest.raises(ArchiveRejected) as caught:
        extract_archive_members(path, "artifact-conflict", [members["two.log"].member_id], LIMITS)

    assert caught.value.code == "ARCHIVE_DESTINATION_CONFLICT"
    assert (result.destination / "notes.txt").read_text() == "user content"


# ---------------------------------------------------------------------------
# parse_path_timestamp reliability tests (pure functions, no tmp_path needed)
# ---------------------------------------------------------------------------


def test_parse_path_timestamp_reliable_year():
    """2026-08-26 is a normal device date — reliable."""
    p = parse_path_timestamp("APLog_2026_0826_131229__8.tar.gz")
    assert p is not None
    assert p.reliability == "reliable"
    assert p.clock_domain == "path_basename"
    assert p.value == datetime(2026, 8, 26, 13, 12, 29)


def test_parse_path_timestamp_jan1_is_unreliable():
    """Jan 1st is a default date when device RTC resets — unreliable."""
    p = parse_path_timestamp("APLog_2025_0101_080037__9.tar.gz")
    assert p is not None
    assert p.reliability == "unreliable_device_clock"


def test_parse_path_timestamp_old_year():
    """Year < 2024 — unreliable device clock."""
    p = parse_path_timestamp("APLog_2023_0615_120000__1.tar.gz")
    assert p is not None
    assert p.reliability == "unreliable_device_clock"


def test_parse_path_timestamp_future_year():
    """Year > 2030 — unreliable future."""
    p = parse_path_timestamp("APLog_2031_0101_120000__1.tar.gz")
    assert p is not None
    assert p.reliability == "unreliable_future"


def test_parse_path_timestamp_march_is_reliable():
    """March 15 in a valid year is not Jan 1 — reliable."""
    p = parse_path_timestamp("APLog_2025_0315_120000__1.tar.gz")
    assert p is not None
    assert p.reliability == "reliable"


def test_parse_path_timestamp_no_timestamp():
    """Names without timestamp patterns return None."""
    p = parse_path_timestamp("anr/traces.txt")
    assert p is None


def test_parse_path_timestamp_all_patterns():
    """All four documented patterns parse correctly."""
    patterns = [
        ("APLog_2026_0826_131229__8.tar.gz", datetime(2026, 8, 26, 13, 12, 29)),
        ("logs/2026_08_26_14_30_00_main.log", datetime(2026, 8, 26, 14, 30)),
        ("log_20260826_153000.txt", datetime(2026, 8, 26, 15, 30)),
        ("trace_20260826-163000.perfetto", datetime(2026, 8, 26, 16, 30)),
    ]
    for path, expected in patterns:
        p = parse_path_timestamp(path)
        assert p is not None, f"Failed to parse: {path}"
        assert p.value == expected, f"{path}: expected {expected}, got {p.value}"
        assert p.clock_domain == "path_basename"
        assert p.reliability == "reliable"


# ---------------------------------------------------------------------------
# summarize_member_times with reliability
# ---------------------------------------------------------------------------


def test_summarize_member_times_reliability():
    from log_analyzer.archive_selection import ArchiveMemberInfo

    members = [
        ArchiveMemberInfo("id1", "APLog_2025_0101_080037__9.tar.gz", 100, None, "archive", True, True, None),
        ArchiveMemberInfo("id2", "APLog_2026_0826_131229__8.tar.gz", 200, None, "archive", True, True, None),
        ArchiveMemberInfo("id3", "anr/traces.txt", 50, None, "text", False, True, None),
    ]
    groups = summarize_member_times(members)
    assert len(groups) == 2

    # Root group: both APLogs
    root = groups[0]
    assert root["timestamped_member_count"] == 2
    assert root["unreliable_timestamped_member_count"] == 1
    assert root["untimestamped_member_count"] == 0
    # Earliest is 2025-01-01 (unreliable)
    assert root["earliest_path_time"] == "2025-01-01T08:00:37"
    assert root["earliest_path_time_reliability"] == "unreliable_device_clock"
    # Latest is 2026-08-26 (reliable)
    assert root["latest_path_time"] == "2026-08-26T13:12:29"
    assert root["latest_path_time_reliability"] == "reliable"

    # anr/ group: no timestamps
    anr = groups[1]
    assert anr["timestamped_member_count"] == 0
    assert anr["unreliable_timestamped_member_count"] == 0
    assert anr["untimestamped_member_count"] == 1
    assert anr["earliest_path_time"] is None
    assert anr["earliest_path_time_reliability"] is None


def test_summarize_member_times_all_reliable():
    from log_analyzer.archive_selection import ArchiveMemberInfo

    members = [
        ArchiveMemberInfo("id1", "APLog_2026_0826_120000__1.tar.gz", 100, None, "archive", True, True, None),
        ArchiveMemberInfo("id2", "APLog_2026_0826_131229__8.tar.gz", 200, None, "archive", True, True, None),
    ]
    groups = summarize_member_times(members)
    assert groups[0]["timestamped_member_count"] == 2
    assert groups[0]["unreliable_timestamped_member_count"] == 0
    assert groups[0]["earliest_path_time_reliability"] == "reliable"
    assert groups[0]["latest_path_time_reliability"] == "reliable"


# ---------------------------------------------------------------------------
# probe_archive_members tests
# ---------------------------------------------------------------------------


def _make_syslog_content(timestamp: str = "2026-09-04 10:30:45.123", uptime: str = "123.456") -> str:
    return (
        f"[{timestamp}][{uptime}][I][1][IPCL][Main][PID:1234][ipcl.c:42 main]Service started\n"
        + f"[{timestamp}][{uptime}][W][2][Screen][Display][PID:5678][screen.c:99 init]Resolution: 1920x1080\n"
        + f"[{timestamp}][{uptime}][E][3][Network][TCP][PID:9012][net.c:10 connect]Connection failed: timeout\n"
    )


def test_probe_zip_members_returns_content_profiles(tmp_path: Path):
    """probe_archive_members reads member prefixes without writing to disk."""
    path = tmp_path / "probe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Linux_Log/log00/syslog.log.0001.log", _make_syslog_content())
        archive.writestr("Linux_Log/log01/syslog.log.0001.log", _make_syslog_content(
            "2026-09-04 11:00:00.000", "4000.000"
        ))

    inventory = inventory_archive(path, "artifact-probe", LIMITS)
    by_path = _by_path(inventory)

    profiles = archive_selection.probe_archive_members(
        path, "artifact-probe",
        [by_path[p].member_id for p in by_path],
        LIMITS,
        max_bytes_per_member=64 * 1024,
        max_total_bytes=10 * 1024 * 1024,
    )

    assert len(profiles) == 2
    for profile in profiles:
        assert "log_domains" in profile
        assert "content_time_ranges" in profile
        assert "boot_identity" in profile
        assert "coverage_confidence" in profile
        assert profile["bytes_read"] > 0
        assert "linux" in profile["log_domains"]  # syslog pattern matches

    # log00: 10:30 wall time
    log00 = next(p for p in profiles if "log00" in p["member_path"])
    wall_times = next(r for r in log00["content_time_ranges"] if r["clock_domain"] == "wall")
    assert "10:30:45" in wall_times["start"]
    # Kernel monotonic
    kernel_times = next(r for r in log00["content_time_ranges"] if r["clock_domain"] == "kernel_monotonic")
    assert kernel_times["start"] == "123.456"

    # log01: different time
    log01 = next(p for p in profiles if "log01" in p["member_path"])
    wall_times = next(r for r in log01["content_time_ranges"] if r["clock_domain"] == "wall")
    assert "11:00:00" in wall_times["start"]


def test_probe_rejects_member_ids_not_in_inventory(tmp_path: Path):
    path = tmp_path / "probe_reject.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("a.log", "content")

    with pytest.raises(ArchiveRejected) as caught:
        archive_selection.probe_archive_members(
            path, "artifact-bad", ["fake-id"],
            LIMITS, max_bytes_per_member=4096, max_total_bytes=10 * 1024 * 1024,
        )
    assert caught.value.code == "ARCHIVE_MEMBER_ID_INVALID"


def test_probe_respects_budget(tmp_path: Path):
    path = tmp_path / "probe_budget.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("a.log", "x" * 5000)
        archive.writestr("b.log", "y" * 5000)

    inventory = inventory_archive(path, "artifact-budget", LIMITS)
    by_path = _by_path(inventory)

    # 2 members * 5000 bytes each = 10000 > budget of 5000
    with pytest.raises(ArchiveRejected) as caught:
        archive_selection.probe_archive_members(
            path, "artifact-budget",
            [by_path[p].member_id for p in by_path],
            LIMITS,
            max_bytes_per_member=5000,
            max_total_bytes=5000,
        )
    assert caught.value.code == "PROBE_BUDGET_LIMIT"


def test_probe_tar_members(tmp_path: Path):
    tar_path = tmp_path / "probe.tar.gz"
    payload = _make_syslog_content().encode()
    with tarfile.open(tar_path, "w:gz") as archive:
        info = tarfile.TarInfo("log00/syslog.log")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    inventory = inventory_archive(tar_path, "artifact-tar", LIMITS)
    profiles = archive_selection.probe_archive_members(
        tar_path, "artifact-tar",
        [inventory.members[0].member_id],
        LIMITS,
        max_bytes_per_member=64 * 1024,
        max_total_bytes=10 * 1024 * 1024,
    )

    assert len(profiles) == 1
    assert profiles[0]["bytes_read"] == len(payload)


def test_probe_anchors_and_diagnostics(tmp_path: Path):
    path = tmp_path / "probe_anchors.zip"
    content = (
        "[2026-09-04 10:30:45.000][100.000][E][1][APP][Main][PID:1][app.c:1 main]FATAL EXCEPTION: main\n"
        + "[2026-09-04 10:30:46.000][101.000][I][2][APP][Main][PID:1][app.c:2 main]ANR in com.example\n"
        + "[2026-09-04 10:31:00.000][115.000][W][3][KERNEL][K][PID:0][k.c:1 k]Kernel panic - not syncing\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("crash.log", content)

    inventory = inventory_archive(path, "artifact-anchors", LIMITS)
    profiles = archive_selection.probe_archive_members(
        path, "artifact-anchors",
        [inventory.members[0].member_id],
        LIMITS,
        max_bytes_per_member=64 * 1024,
        max_total_bytes=10 * 1024 * 1024,
    )

    assert len(profiles) == 1
    anchors = profiles[0]["anchors"]
    assert "FATAL EXCEPTION" in anchors
    assert "ANR in" in anchors
    assert "Kernel panic" in anchors


def test_probe_coverage_confidence_levels(tmp_path: Path):
    """low=no time/no boot, medium=time or anchors, high=time+boot."""
    path = tmp_path / "probe_conf.zip"
    # Content with time + boot_id → high
    rich = (
        "boot_id=abc123-def456-7890\n"
        + _make_syslog_content()
    )
    # Content with only time → medium
    medium = _make_syslog_content()
    # Content with no time, no boot → low
    bare = "just some random text without any timestamp or boot marker\n"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("rich.log", rich)
        archive.writestr("medium.log", medium)
        archive.writestr("bare.log", bare)

    inventory = inventory_archive(path, "artifact-conf", LIMITS)
    by_path = _by_path(inventory)
    profiles = archive_selection.probe_archive_members(
        path, "artifact-conf",
        [by_path[p].member_id for p in sorted(by_path)],
        LIMITS,
        max_bytes_per_member=64 * 1024,
        max_total_bytes=10 * 1024 * 1024,
    )

    conf_map = {p["member_path"]: p["coverage_confidence"] for p in profiles}
    assert conf_map["bare.log"] == "low"
    assert conf_map["medium.log"] == "medium"
    assert conf_map["rich.log"] == "high"
