"""归档清单与非递归的选择性解压。

清单阶段不向磁盘展开任何成员。调用方只能把清单返回的 ``member_id``
交回选择性解压器，避免让模型或客户端直接控制输出路径。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import time
import uuid
import zipfile
import re
from collections import defaultdict

from .archive_manager import (
    COPY_CHUNK_BYTES,
    MANIFEST_NAME,
    ArchiveLimits,
    ArchiveExtractor,
    ArchiveRejected,
    ExtractionBudget,
    _safe_member_path,
    _target_path,
    _is_7z_volume,
    _open_7z_archive,
    extraction_destination,
    is_supported_archive,
)

import py7zr


SELECTION_MANIFEST_VERSION = 2


@dataclass(frozen=True)
class SourceFingerprint:
    size_bytes: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class ArchiveMemberInfo:
    member_id: str
    member_path: str
    size_bytes: int | None
    compressed_size: int | None
    kind: str
    is_archive: bool
    safe: bool
    reason: str | None = None


class TimeReliability:
    """路径时间戳的可信度分类。

    RELIABLE: 年份在合理范围内且不像是默认日期，时间戳可参与筛选。
    UNRELIABLE_DEVICE_CLOCK: 设备时钟未同步（年份过旧，或日期为可疑的默认值如 1月1日），时间戳不可用于筛选。
    UNRELIABLE_FUTURE: 年份远在未来（> 2030），时间戳不可用于筛选。
    """

    RELIABLE = "reliable"
    UNRELIABLE_DEVICE_CLOCK = "unreliable_device_clock"
    UNRELIABLE_FUTURE = "unreliable_future"

    # 年份可信范围：2024 年（常见设备出厂年份）到 2030 年（未来合理范围）
    _MIN_RELIABLE_YEAR = 2024
    _MAX_RELIABLE_YEAR = 2030

    @staticmethod
    def assess(parsed: datetime) -> str:
        year = parsed.year
        if year < TimeReliability._MIN_RELIABLE_YEAR:
            return TimeReliability.UNRELIABLE_DEVICE_CLOCK
        if year > TimeReliability._MAX_RELIABLE_YEAR:
            return TimeReliability.UNRELIABLE_FUTURE
        # 年份在合理范围但日期是 1月1日——这是设备时钟重置后的默认日期，
        # 例如 APLog_2025_0101_080037 中 2025-01-01 是设备 RTC 未同步时的值
        if parsed.month == 1 and parsed.day == 1:
            return TimeReliability.UNRELIABLE_DEVICE_CLOCK
        return TimeReliability.RELIABLE


@dataclass(frozen=True)
class ParsedPathTimestamp:
    """从成员路径中直接读出的时间事实。

    clock_domain: 始终为 "path_basename"，表示这是文件名中嵌入的本地时钟，
                  不经过日志内容验证，可能与日志内部时间戳不在同一时钟域。
    reliability: 时间戳的可信度。RELIABLE 表示年份在合理范围；
                 UNRELIABLE_DEVICE_CLOCK 表示设备时钟未同步。
    """

    value: datetime
    pattern: str
    clock_domain: str = "path_basename"
    reliability: str = TimeReliability.RELIABLE


_PATH_TIMESTAMP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("yyyy_mmdd_hhmmss", re.compile(
        r"(?<!\d)(?P<year>\d{4})_(?P<month>\d{2})(?P<day>\d{2})_"
        r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?!\d)"
    )),
    ("yyyy_mm_dd_hh_mm_ss", re.compile(
        r"(?<!\d)(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2})_"
        r"(?P<hour>\d{2})_(?P<minute>\d{2})_(?P<second>\d{2})(?!\d)"
    )),
    ("yyyymmdd_hhmmss", re.compile(
        r"(?<!\d)(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})_"
        r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?!\d)"
    )),
    ("yyyymmdd-hhmmss", re.compile(
        r"(?<!\d)(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})-"
        r"(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})(?!\d)"
    )),
)


def parse_path_timestamp(member_path: str) -> ParsedPathTimestamp | None:
    """从成员路径中解析常见绝对时间，返回带 clock_domain 和 reliability 标记的结果。

    依次检查路径的每个组成部分（从 basename 到最顶层父目录），
    第一个匹配到的时间戳即为结果。这覆盖了 APLog 归档中常见的两种布局：
    - 扁平命名：APLog_2026_0826_131229__8.tar.gz（时间戳在 basename）
    - 目录嵌套：APLog_2026_0826_131229__8/main.log（时间戳在父目录名）

    返回值始终标记 clock_domain="path_basename"：文件名时间戳来自设备本地时钟，
    可能与日志内部时间戳不在同一时钟域，不应直接与日志内容时间比较。

    reliability 标记：
    - "reliable": 年份在 2024-2030 范围且非 1月1日，可参与时间筛选
    - "unreliable_device_clock": 年份 < 2024 或日期为 1月1日，设备时钟未同步
    - "unreliable_future": 年份 > 2030，明显异常
    """

    # 从 basename 开始逐层向上检查路径的每个组成部分
    parts = PurePosixPath(member_path).parts
    # 反转顺序：从 basename 到顶层目录
    for part in reversed(parts):
        for pattern_name, pattern in _PATH_TIMESTAMP_PATTERNS:
            match = pattern.search(part)
            if match is None:
                continue
            try:
                parsed = datetime(
                    int(match["year"]), int(match["month"]), int(match["day"]),
                    int(match["hour"]), int(match["minute"]), int(match["second"]),
                )
                return ParsedPathTimestamp(
                    value=parsed,
                    pattern=pattern_name,
                    clock_domain="path_basename",
                    reliability=TimeReliability.assess(parsed),
                )
            except ValueError:
                continue
    return None


def summarize_member_times(members: list[ArchiveMemberInfo]) -> list[dict[str, object]]:
    """按直接父目录聚合路径时间，包含可靠性标记，供调用方决定领域相关成员。"""

    groups: dict[str, dict[str, object]] = {}
    for member in members:
        parent = str(PurePosixPath(member.member_path).parent)
        group = groups.setdefault(parent, {
            "path_prefix": "" if parent == "." else f"{parent}/",
            "member_count": 0,
            "timestamped_member_count": 0,
            "unreliable_timestamped_member_count": 0,
            "untimestamped_member_count": 0,
            "earliest_path_time": None,
            "earliest_path_time_reliability": None,
            "latest_path_time": None,
            "latest_path_time_reliability": None,
        })
        group["member_count"] = int(group["member_count"]) + 1
        parsed = parse_path_timestamp(member.member_path)
        if parsed is None:
            group["untimestamped_member_count"] = int(group["untimestamped_member_count"]) + 1
            continue
        group["timestamped_member_count"] = int(group["timestamped_member_count"]) + 1
        if parsed.reliability != TimeReliability.RELIABLE:
            group["unreliable_timestamped_member_count"] = int(
                group["unreliable_timestamped_member_count"]
            ) + 1
        value = parsed.value.isoformat()
        earliest = group["earliest_path_time"]
        latest = group["latest_path_time"]
        if earliest is None or value < earliest:
            group["earliest_path_time"] = value
            group["earliest_path_time_reliability"] = parsed.reliability
        if latest is None or value > latest:
            group["latest_path_time"] = value
            group["latest_path_time_reliability"] = parsed.reliability
    return sorted(groups.values(), key=lambda item: str(item["path_prefix"]))


_LOG_DOMAIN_PATTERNS = (
    ("android", re.compile(r"(?:^|\n)\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}|AndroidRuntime|logcat", re.I)),
    ("kernel", re.compile(r"(?:^|\n)\s*\[\s*\d+(?:\.\d+)?\]|kernel:|Call Trace:|Kernel panic", re.I)),
    ("linux", re.compile(r"systemd\[|journal|syslog|daemon\[", re.I)),
    ("mcu", re.compile(r"\b(?:MCU|CAN|A2B)\b", re.I)),
)
_BOOT_ID_RE = re.compile(r"\b(?:boot[_ -]?id|BOOT_ID)\s*[:=]\s*([0-9a-f]{8,}(?:-[0-9a-f-]+)?)", re.I)
_BOOT_MARKER_RE = re.compile(r"\b(?:reboot|boot completed|Linux version|init: starting service)\b", re.I)
_CLOCK_CORRECTION_RE = re.compile(r"\b(?:time (?:has been )?(?:changed|set|updated)|clock.*(?:adjust|sync)|NTP.*(?:sync|set))\b", re.I)
_ANCHOR_RE = re.compile(r"\b(?:FATAL EXCEPTION|ANR in|Watchdog|Kernel panic|Call Trace:|avc: denied|reboot|boot completed)\b", re.I)
_WALL_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)")
_ANDROID_RE = re.compile(r"(?<!\d)(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)")
_KERNEL_RE = re.compile(r"^\s*\[\s*(\d+(?:\.\d+)?)\]", re.M)


def _read_probe_bytes(archive_path: Path, archive_format: str, member_path: str, limit: int) -> bytes:
    """Read a member prefix only; no extraction output is written."""
    if archive_format == "zip":
        with zipfile.ZipFile(archive_path) as archive, archive.open(member_path) as source:
            return source.read(limit)
    if archive_format == "tar":
        with tarfile.open(archive_path, mode="r:*") as archive:
            source = archive.extractfile(member_path)
            if source is None:
                raise ArchiveRejected("ARCHIVE_READ_ERROR", f"无法读取 TAR 成员: {member_path}")
            with source:
                return source.read(limit)
    if archive_format == "gzip":
        with gzip.open(archive_path, "rb") as source:
            return source.read(limit)
    raise ArchiveRejected("PROBE_FORMAT_UNSUPPORTED", "7z 成员探测暂不支持无落盘读取")


def probe_archive_members(archive_path: Path | str, archive_artifact_id: str, member_ids: list[str], limits: ArchiveLimits, *, max_bytes_per_member: int, max_total_bytes: int) -> list[dict[str, object]]:
    """Return content-derived, prefix-only profiles for stable catalog member IDs."""
    inventory = inventory_archive(archive_path, archive_artifact_id, limits)
    safe = {item.member_id: item for item in inventory.members if item.safe}
    requested = list(dict.fromkeys(member_ids))
    invalid = next((item for item in requested if item not in safe), None)
    if invalid is not None:
        raise ArchiveRejected("ARCHIVE_MEMBER_ID_INVALID", "成员 ID 不存在、归档已变化或不允许读取")
    if len(requested) * max_bytes_per_member > max_total_bytes:
        raise ArchiveRejected("PROBE_BUDGET_LIMIT", "成员探测总读取量超过服务端预算")
    results: list[dict[str, object]] = []
    for member_id in requested:
        item = safe[member_id]
        payload = _read_probe_bytes(inventory.archive_path, inventory.format, item.member_path, max_bytes_per_member)
        text = payload.decode("utf-8", errors="replace")
        domains = [domain for domain, pattern in _LOG_DOMAIN_PATTERNS if pattern.search(text)]
        values = {"wall": _WALL_RE.findall(text), "android": _ANDROID_RE.findall(text), "kernel_monotonic": _KERNEL_RE.findall(text)}
        time_ranges = []
        for domain, found in values.items():
            if found:
                ordered = sorted(found, key=lambda value: float(value) if domain == "kernel_monotonic" else value)
                time_ranges.append({"clock_domain": domain, "start": ordered[0], "end": ordered[-1]})
        boot_ids = list(dict.fromkeys(_BOOT_ID_RE.findall(text)))
        anchors = list(dict.fromkeys(match.group(0) for match in _ANCHOR_RE.finditer(text)))[:20]
        corrections = list(dict.fromkeys(match.group(0) for match in _CLOCK_CORRECTION_RE.finditer(text)))[:10]
        limitations = (["只读取了成员前缀；后续内容未探测"] if len(payload) >= max_bytes_per_member else [])
        if not boot_ids:
            limitations.append("样本中未发现显式 Boot ID")
        confidence = "high" if boot_ids and time_ranges else "medium" if (time_ranges or anchors) else "low"
        results.append({"member_id": member_id, "member_path": item.member_path, "bytes_read": len(payload), "log_domains": domains, "content_time_ranges": time_ranges, "boot_identity": f"boot_id:{boot_ids[0]}" if boot_ids else ("boot_marker_observed" if _BOOT_MARKER_RE.search(text) else "unknown"), "anchors": anchors, "clock_corrections": corrections, "coverage_confidence": confidence, "limitations": limitations})
    return results


@dataclass(frozen=True)
class ArchiveInventory:
    archive_path: Path
    archive_artifact_id: str
    format: str
    members: list[ArchiveMemberInfo]
    truncated: bool
    source_fingerprint: SourceFingerprint


@dataclass(frozen=True)
class SelectedMember:
    member_id: str
    member_path: str
    path: Path
    size_bytes: int
    reused: bool


@dataclass(frozen=True)
class SelectiveExtractionResult:
    destination: Path
    members: list[SelectedMember]
    reset: bool


def managed_extraction_matches(
    archive_path: Path | str,
    archive_artifact_id: str,
    source_fingerprint: SourceFingerprint,
) -> bool | None:
    """Return whether an adjacent managed extraction belongs to this source version."""

    destination = extraction_destination(Path(archive_path).resolve())
    if not destination.exists():
        return None
    manifest = _manifest(destination)
    if manifest is None or not _is_pristine(destination, manifest):
        return False
    if manifest.get("version") == 1:
        return (
            manifest.get("source_size") == source_fingerprint.size_bytes
            and manifest.get("source_mtime_ns") == source_fingerprint.mtime_ns
            and manifest.get("source_sha256") == source_fingerprint.sha256
        )
    return (
        manifest.get("archive_artifact_id") == archive_artifact_id
        and manifest.get("source_fingerprint") == asdict(source_fingerprint)
    )


def inspect_reusable_extraction(
    inventory: ArchiveInventory,
) -> tuple[bool | None, list[SelectedMember]]:
    """校验受管目录并返回可跨运行复用的已解压成员。"""

    status = managed_extraction_matches(
        inventory.archive_path, inventory.archive_artifact_id, inventory.source_fingerprint,
    )
    if status is not True:
        return status, []
    destination = extraction_destination(inventory.archive_path)
    manifest = _manifest(destination)
    if manifest is None:
        return False, []
    existing = _existing_members(
        manifest,
        inventory.archive_artifact_id,
        inventory.source_fingerprint.sha256,
    )
    current = {item.member_id: item for item in inventory.members if item.safe}
    reusable: list[SelectedMember] = []
    for member_id, raw in existing.items():
        item = current.get(member_id)
        if item is None:
            continue
        reusable.append(SelectedMember(
            member_id=member_id,
            member_path=item.member_path,
            path=_target_path(destination, item.member_path),
            size_bytes=int(raw["size_bytes"]),
            reused=True,
        ))
    return True, reusable


def _fingerprint(path: Path, limits: ArchiveLimits, started_at: float) -> SourceFingerprint:
    info = path.stat()
    if info.st_size > limits.max_archive_bytes:
        raise ArchiveRejected("ARCHIVE_FILE_LIMIT", "归档文件超过服务端单归档大小预算")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(COPY_CHUNK_BYTES):
            if time.monotonic() - started_at > limits.max_runtime_seconds:
                raise ArchiveRejected("ARCHIVE_TIME_LIMIT", "归档清点超过服务端运行时间预算")
            digest.update(chunk)
    return SourceFingerprint(info.st_size, info.st_mtime_ns, digest.hexdigest())


def _member_id(archive_artifact_id: str, source_sha256: str, normalized_path: str) -> str:
    value = f"{archive_artifact_id}\0{source_sha256}\0{normalized_path}".encode(
        "utf-8", errors="surrogatepass"
    )
    return hashlib.sha256(value).hexdigest()


def _kind(path: str) -> str:
    name = path.casefold()
    if is_supported_archive(Path(path)):
        return "archive"
    if name.endswith((".log", ".txt", ".csv", ".json", ".xml", ".trace")):
        return "text"
    return "file"


def _record(
    archive_artifact_id: str,
    source_sha256: str,
    raw_path: str,
    size_bytes: int | None,
    compressed_size: int | None,
    *,
    unsafe_reason: str | None = None,
) -> dict:
    try:
        normalized = _safe_member_path(raw_path)
        reason = unsafe_reason
    except ArchiveRejected as exc:
        normalized = raw_path.replace("\\", "/")
        reason = exc.code
    return {
        "member_id": _member_id(archive_artifact_id, source_sha256, normalized),
        "member_path": normalized,
        "size_bytes": size_bytes,
        "compressed_size": compressed_size,
        "kind": _kind(normalized),
        "is_archive": is_supported_archive(Path(normalized)),
        "safe": reason is None,
        "reason": reason,
    }


def inventory_archive(
    archive_path: Path | str,
    archive_artifact_id: str,
    limits: ArchiveLimits,
    max_members: int | None = None,
) -> ArchiveInventory:
    """读取归档目录而不落盘，并为每个成员生成稳定 ID。"""

    path = Path(archive_path).resolve()
    if not archive_artifact_id:
        raise ValueError("archive_artifact_id must not be empty")
    if not path.is_file():
        raise ArchiveRejected("ARCHIVE_NOT_FOUND", "归档文件不存在")
    if not is_supported_archive(path):
        raise ArchiveRejected("ARCHIVE_FORMAT_UNSUPPORTED", f"不支持安全清点的归档格式: {path.suffix}")
    started_at = time.monotonic()
    fingerprint = _fingerprint(path, limits, started_at)
    cap = limits.max_members if max_members is None else min(max_members, limits.max_members)
    if cap < 1:
        raise ValueError("max_members must be positive")
    records: list[dict] = []
    truncated = False

    def append(record: dict) -> bool:
        nonlocal truncated
        if len(records) >= cap:
            truncated = True
            return False
        records.append(record)
        return True

    name = path.name.casefold()
    try:
        if name.endswith(".zip"):
            archive_format = "zip"
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > limits.max_members:
                    truncated = True
                    infos = infos[:limits.max_members]
                for info in infos:
                    if time.monotonic() - started_at > limits.max_runtime_seconds:
                        raise ArchiveRejected("ARCHIVE_TIME_LIMIT", "归档清点超过服务端运行时间预算")
                    if info.is_dir():
                        continue
                    mode = (info.external_attr >> 16) & 0xFFFF
                    unsafe = None
                    if info.flag_bits & 0x1:
                        unsafe = "ARCHIVE_ENCRYPTED"
                    elif stat.S_ISLNK(mode):
                        unsafe = "ARCHIVE_LINK_REJECTED"
                    elif stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                        unsafe = "ARCHIVE_SPECIAL_FILE"
                    elif info.file_size < 0 or info.file_size > limits.max_member_bytes:
                        unsafe = "ARCHIVE_MEMBER_SIZE_LIMIT"
                    if not append(_record(archive_artifact_id, fingerprint.sha256, info.filename, info.file_size, info.compress_size, unsafe_reason=unsafe)):
                        break
        elif name.endswith((".tar", ".tar.gz", ".tgz")):
            archive_format = "tar"
            with tarfile.open(path, mode="r:*") as archive:
                entry_count = 0
                for info in archive:
                    entry_count += 1
                    if entry_count > limits.max_members:
                        truncated = True
                        break
                    if time.monotonic() - started_at > limits.max_runtime_seconds:
                        raise ArchiveRejected("ARCHIVE_TIME_LIMIT", "归档清点超过服务端运行时间预算")
                    if info.isdir():
                        continue
                    unsafe = None
                    if info.issym() or info.islnk():
                        unsafe = "ARCHIVE_LINK_REJECTED"
                    elif not info.isfile():
                        unsafe = "ARCHIVE_SPECIAL_FILE"
                    elif info.size < 0 or info.size > limits.max_member_bytes:
                        unsafe = "ARCHIVE_MEMBER_SIZE_LIMIT"
                    if not append(_record(archive_artifact_id, fingerprint.sha256, info.name, info.size, None, unsafe_reason=unsafe)):
                        break
        elif name.endswith(".7z") or _is_7z_volume(path):
            archive_format = "7z"
            archive, merged = _open_7z_archive(path, ExtractionBudget(limits))
            try:
                if archive.needs_password():
                    raise ArchiveRejected("ARCHIVE_ENCRYPTED", "不支持加密 7z 归档")
                infos = archive.list()
                file_infos = [info for info in infos if not info.is_directory]
                if len(file_infos) > limits.max_members:
                    truncated = True
                    file_infos = file_infos[:limits.max_members]
                for info in file_infos:
                    if time.monotonic() - started_at > limits.max_runtime_seconds:
                        raise ArchiveRejected("ARCHIVE_TIME_LIMIT", "归档清点超过服务端运行时间预算")
                    unsafe = None
                    if info.is_symlink:
                        unsafe = "ARCHIVE_LINK_REJECTED"
                    elif not info.is_file:
                        unsafe = "ARCHIVE_SPECIAL_FILE"
                    elif (info.uncompressed or 0) < 0 or (info.uncompressed or 0) > limits.max_member_bytes:
                        unsafe = "ARCHIVE_MEMBER_SIZE_LIMIT"
                    if not append(_record(archive_artifact_id, fingerprint.sha256, info.filename, info.uncompressed, info.compressed, unsafe_reason=unsafe)):
                        break
            finally:
                archive.close()
                if merged is not None:
                    merged.unlink(missing_ok=True)
        else:
            archive_format = "gzip"
            output_name = path.name[:-3] or "content"
            size = 0
            with gzip.open(path, "rb") as stream:
                while chunk := stream.read(COPY_CHUNK_BYTES):
                    if time.monotonic() - started_at > limits.max_runtime_seconds:
                        raise ArchiveRejected("ARCHIVE_TIME_LIMIT", "归档清点超过服务端运行时间预算")
                    size += len(chunk)
                    if size > limits.max_member_bytes:
                        raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_LIMIT", "GZIP 展开内容超过大小预算")
                    if size / max(fingerprint.size_bytes, 1) > limits.max_compression_ratio:
                        raise ArchiveRejected("ARCHIVE_RATIO_LIMIT", "GZIP 实际压缩比超过预算")
            append(_record(archive_artifact_id, fingerprint.sha256, output_name, size, fingerprint.size_bytes))
    except (zipfile.BadZipFile, tarfile.TarError, gzip.BadGzipFile, EOFError, py7zr.Bad7zFile) as exc:
        raise ArchiveRejected("ARCHIVE_INVALID", f"归档损坏或格式无效: {type(exc).__name__}") from exc

    # Windows 目标路径不区分大小写；所有冲突成员都必须禁用。
    counts: dict[str, int] = {}
    for item in records:
        key = item["member_path"].casefold()
        counts[key] = counts.get(key, 0) + 1
    for item in records:
        if counts[item["member_path"].casefold()] > 1:
            item["safe"] = False
            item["reason"] = "ARCHIVE_DUPLICATE_PATH"
    record_keys = {item["member_path"].casefold() for item in records}
    conflicted: set[str] = set()
    for key in record_keys:
        parts = PurePosixPath(key).parts
        for index in range(1, len(parts)):
            parent = PurePosixPath(*parts[:index]).as_posix()
            if parent in record_keys:
                conflicted.update((key, parent))
    for item in records:
        if item["member_path"].casefold() in conflicted:
            item["safe"] = False
            item["reason"] = "ARCHIVE_PATH_CONFLICT"
    members = [ArchiveMemberInfo(**item) for item in records]
    return ArchiveInventory(path, archive_artifact_id, archive_format, members, truncated, fingerprint)


def _manifest(destination: Path) -> dict | None:
    manifest_path = destination / MANIFEST_NAME
    if destination.is_symlink() or not destination.is_dir() or not manifest_path.is_file() or manifest_path.is_symlink():
        return None
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        return value if value.get("version") in (1, SELECTION_MANIFEST_VERSION) else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _is_pristine(destination: Path, manifest: dict) -> bool:
    del manifest
    return ArchiveExtractor._is_managed_destination(destination)


def _existing_members(manifest: dict, archive_artifact_id: str, source_sha256: str) -> dict[str, dict]:
    existing: dict[str, dict] = {}
    for raw in manifest.get("members", []):
        raw_path = raw.get("member_path", raw.get("path"))
        if raw_path is None:
            continue
        member_path = _safe_member_path(str(raw_path))
        member_id = str(
            raw.get("member_id") or _member_id(archive_artifact_id, source_sha256, member_path)
        )
        existing[member_id] = {
            "member_id": member_id,
            "member_path": member_path,
            "size_bytes": int(raw["size_bytes"]),
        }
    return existing


def _copy(source, target: Path, declared_size: int, budget: ExtractionBudget) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    written = 0
    with target.open("xb") as output:
        while chunk := source.read(COPY_CHUNK_BYTES):
            budget.check_runtime()
            written += len(chunk)
            if written > declared_size or written > budget.limits.max_member_bytes:
                raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_MISMATCH", f"归档成员实际大小异常: {target.name}")
            output.write(chunk)
    if written != declared_size:
        raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_MISMATCH", f"归档成员大小与元数据不一致: {target.name}")
    return written


def _clone_managed_destination(destination: Path, staging: Path) -> None:
    """Clone a pristine managed tree without duplicating existing log bytes.

    Both directories are siblings, so hard links normally stay on one filesystem.
    Filesystems or policies that reject links fall back to an ordinary per-file copy.
    The top-level manifest must never be linked because staging writes a replacement
    before the directory swap and the old tree is still the rollback source.
    """

    manifest_path = destination / MANIFEST_NAME

    def clone_file(source: str, target: str):
        source_path = Path(source)
        if source_path == manifest_path:
            return shutil.copy2(source, target)
        try:
            os.link(source, target)
            return target
        except OSError:
            return shutil.copy2(source, target)

    shutil.copytree(destination, staging, copy_function=clone_file)


def extract_archive_members(
    archive_path: Path | str,
    archive_artifact_id: str,
    member_ids: list[str],
    limits: ArchiveLimits,
    *,
    force: bool = False,
) -> SelectiveExtractionResult:
    """把当前归档中选定的安全成员增量解压到相邻位置。

    - ZIP/TAR/TAR.GZ/7z 归档 → 解压到同名目录（如 ``logs.zip`` → ``logs/``）
    - 单个 .gz 文件 → 解压为同名文件，去掉 .gz（如 ``kernel.log.gz`` → ``kernel.log``）
    """

    inventory = inventory_archive(archive_path, archive_artifact_id, limits)
    safe = {item.member_id: item for item in inventory.members if item.safe}
    requested = list(dict.fromkeys(member_ids))

    destination = extraction_destination(inventory.archive_path)

    unknown = [item for item in requested if item not in safe]
    if unknown:
        raise ArchiveRejected("ARCHIVE_MEMBER_ID_INVALID", f"成员 ID 未知或不安全: {unknown[0]}")

    budget = ExtractionBudget(limits)

    # ── Gzip: single-file extraction, no staging directory ──
    if inventory.format == "gzip":
        item = safe[requested[0]]
        if destination.exists():
            if not destination.is_file():
                raise ArchiveRejected("ARCHIVE_DESTINATION_CONFLICT",
                                      f"解压目标已存在且不是文件: {destination.name}")
            # Reuse existing file if source fingerprint matches
            if not force and destination.is_file() and _fingerprint(inventory.archive_path, limits, budget.started_at) == inventory.source_fingerprint:
                return SelectiveExtractionResult(
                    destination,
                    [SelectedMember(item, item.member_path, destination, int(item.size_bytes), True)],
                    reset=False,
                )
        # Write to a temp file then atomically rename
        temp = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        try:
            with gzip.open(inventory.archive_path, "rb") as source:
                _copy(source, temp, int(item.size_bytes), budget)
            if _fingerprint(inventory.archive_path, limits, budget.started_at) != inventory.source_fingerprint:
                temp.unlink(missing_ok=True)
                raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档在清点与解压之间发生变化")
            temp.replace(destination)
        except Exception:
            if temp.exists():
                temp.unlink(missing_ok=True)
            raise
        return SelectiveExtractionResult(
            destination,
            [SelectedMember(item, item.member_path, destination, int(item.size_bytes), False)],
            reset=False,
        )

    # ── Directory archives: use staging directory + manifest ──
    old_manifest = _manifest(destination) if destination.exists() else None
    if destination.exists() and (old_manifest is None or not _is_pristine(destination, old_manifest)):
        raise ArchiveRejected("ARCHIVE_DESTINATION_CONFLICT", f"解压目标已存在且不受选择性解压器管理: {destination.name}")
    current_fingerprint = asdict(inventory.source_fingerprint)
    if old_manifest is None:
        source_matches = False
    elif old_manifest.get("version") == 1:
        source_matches = (
            old_manifest.get("source_size") == inventory.source_fingerprint.size_bytes
            and old_manifest.get("source_mtime_ns") == inventory.source_fingerprint.mtime_ns
            and old_manifest.get("source_sha256") == inventory.source_fingerprint.sha256
        )
    else:
        source_matches = old_manifest.get("source_fingerprint") == current_fingerprint
    reset = old_manifest is not None and (
        not source_matches
        or old_manifest.get("archive_artifact_id", archive_artifact_id) != archive_artifact_id
    )
    unknown = [item for item in requested if item not in safe]
    if unknown:
        if reset:
            raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档版本已变化，请重新读取成员清单")
        raise ArchiveRejected("ARCHIVE_MEMBER_ID_INVALID", f"成员 ID 未知或不安全: {unknown[0]}")
    if inventory.truncated:
        if reset:
            raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档版本已变化且新目录超过清点预算")
        raise ArchiveRejected("ARCHIVE_MEMBER_LIMIT", "归档成员数量超过服务端清点预算")
    existing = (
        {} if reset or old_manifest is None
        else _existing_members(old_manifest, archive_artifact_id, inventory.source_fingerprint.sha256)
    )
    pending = [safe[item] for item in requested if force or item not in existing]

    total = sum(item.size_bytes or 0 for item in pending)
    if any((item.size_bytes or 0) > limits.max_member_bytes for item in pending):
        raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_LIMIT", "选定归档成员超过大小预算")
    if total / max(inventory.source_fingerprint.size_bytes, 1) > limits.max_compression_ratio:
        raise ArchiveRejected("ARCHIVE_RATIO_LIMIT", "选定成员的声明压缩比超过预算")
    projected = {} if reset else dict(existing)
    for item in pending:
        projected[item.member_id] = {
            "member_id": item.member_id,
            "member_path": item.member_path,
            "size_bytes": item.size_bytes,
        }
    projected_bytes = sum(int(item["size_bytes"]) for item in projected.values())
    if len(projected) > limits.max_members:
        raise ArchiveRejected("ARCHIVE_MEMBER_LIMIT", "累计选择成员数量超过服务端预算")
    if projected_bytes > limits.max_expanded_bytes:
        raise ArchiveRejected("ARCHIVE_EXPANDED_LIMIT", "累计选择成员字节数超过服务端预算")
    if projected_bytes / max(inventory.source_fingerprint.size_bytes, 1) > limits.max_compression_ratio:
        raise ArchiveRejected("ARCHIVE_RATIO_LIMIT", "累计选择成员的声明压缩比超过预算")

    # 即使没有待写成员，也先验证累计预算和源版本，再返回复用状态。
    if not pending:
        if _fingerprint(inventory.archive_path, limits, time.monotonic()) != inventory.source_fingerprint:
            raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档在清点与复用之间发生变化")
        return SelectiveExtractionResult(
            destination,
            [
                SelectedMember(
                    item,
                    safe[item].member_path,
                    _target_path(destination, safe[item].member_path),
                    int(existing[item]["size_bytes"]),
                    True,
                )
                for item in requested
            ],
            reset=reset,
        )

    budget.reserve(len(pending), total)
    staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
    backup: Path | None = None
    try:
        if destination.exists() and not reset:
            _clone_managed_destination(destination, staging)
        else:
            staging.mkdir(parents=True)

        name = inventory.archive_path.name.casefold()
        pending_by_path = {item.member_path: item for item in pending}
        if name.endswith(".zip"):
            with zipfile.ZipFile(inventory.archive_path) as archive:
                infos = {}
                remaining = set(pending_by_path)
                for info in archive.infolist():
                    budget.check_runtime()
                    if info.is_dir():
                        continue
                    try:
                        normalized = _safe_member_path(info.filename)
                    except ArchiveRejected:
                        continue
                    if normalized in remaining:
                        infos[normalized] = info
                        remaining.remove(normalized)
                        if not remaining:
                            break
                if remaining:
                    raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档成员在解压前发生变化")
                for member_path, item in pending_by_path.items():
                    info = infos[member_path]
                    with archive.open(info, "r") as source:
                        _copy(source, _target_path(staging, member_path), int(item.size_bytes), budget)
        elif name.endswith((".tar", ".tar.gz", ".tgz")):
            with tarfile.open(inventory.archive_path, mode="r:*") as archive:
                infos = {}
                remaining = set(pending_by_path)
                for info in archive:
                    budget.check_runtime()
                    if not info.isfile():
                        continue
                    try:
                        normalized = _safe_member_path(info.name)
                    except ArchiveRejected:
                        continue
                    if normalized in remaining:
                        infos[normalized] = info
                        remaining.remove(normalized)
                        if not remaining:
                            break
                if remaining:
                    raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档成员在解压前发生变化")
                for member_path, item in pending_by_path.items():
                    source = archive.extractfile(infos[member_path])
                    if source is None:
                        raise ArchiveRejected("ARCHIVE_READ_ERROR", f"无法读取 TAR 成员: {member_path}")
                    with source:
                        _copy(source, _target_path(staging, member_path), int(item.size_bytes), budget)
        elif name.endswith(".7z") or _is_7z_volume(inventory.archive_path):
            archive, merged = _open_7z_archive(inventory.archive_path, budget)
            try:
                if archive.needs_password():
                    raise ArchiveRejected("ARCHIVE_ENCRYPTED", "不支持加密 7z 归档")
                for member_path, item in pending_by_path.items():
                    budget.check_runtime()
                    archive.extract(staging, targets=[member_path])
                for member_path, item in pending_by_path.items():
                    target = _target_path(staging, member_path)
                    if not target.is_file():
                        raise ArchiveRejected("ARCHIVE_READ_ERROR", f"7z 成员未正确提取: {member_path}")
                    actual = target.stat().st_size
                    if actual != int(item.size_bytes):
                        raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_MISMATCH", f"7z 成员大小与元数据不一致: {member_path}")
            finally:
                archive.close()
                if merged is not None:
                    merged.unlink(missing_ok=True)
        else:
            item = pending[0]
            with gzip.open(inventory.archive_path, "rb") as source:
                _copy(source, _target_path(staging, item.member_path), int(item.size_bytes), budget)

        # Bind installed bytes to the exact source version inspected above. A same-path
        # replacement between inventory and extraction invalidates the whole staging tree.
        if _fingerprint(inventory.archive_path, limits, budget.started_at) != inventory.source_fingerprint:
            raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档在清点与解压之间发生变化")

        merged = projected
        manifest = {
            "version": SELECTION_MANIFEST_VERSION,
            "archive_artifact_id": archive_artifact_id,
            "source_fingerprint": current_fingerprint,
            "members": list(merged.values()),
        }
        (staging / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if destination.exists():
            backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
            destination.replace(backup)
        for attempt in range(3):
            try:
                staging.replace(destination)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError:
                # 新目录已经安装完成；保留隐藏备份比回滚有效结果更安全。
                pass
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        if backup is not None and backup.exists() and not destination.exists():
            backup.replace(destination)
        raise

    return SelectiveExtractionResult(
        destination,
        [
            SelectedMember(
                item,
                safe[item].member_path,
                _target_path(destination, safe[item].member_path),
                int(merged[item]["size_bytes"]),
                item in existing and not reset and not force,
            )
            for item in requested
        ],
        reset=reset,
    )
