"""受预算约束的归档展开。

归档内容是不可信输入。本模块只使用 Python 标准库处理 ZIP、TAR、TAR.GZ、
TGZ 和单文件 GZIP，并在写入前拒绝路径穿越、链接、特殊文件、加密 ZIP、
重复目标、异常压缩比和超预算成员。RAR/7z 不会退回到 shell 命令。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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


COPY_CHUNK_BYTES = 1024 * 1024
MANIFEST_NAME = ".extraction-manifest.json"
UNPACKED_SUFFIX = ".unpacked"
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 4 * 1024 * 1024 * 1024
    max_members: int = 20_000
    max_member_bytes: int = 4 * 1024 * 1024 * 1024
    max_expanded_bytes: int = 16 * 1024 * 1024 * 1024
    max_compression_ratio: float = 200.0
    max_depth: int = 2
    max_runtime_seconds: float = 300.0


@dataclass
class ExtractionBudget:
    limits: ArchiveLimits
    members: int = 0
    expanded_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def check_runtime(self) -> None:
        if time.monotonic() - self.started_at > self.limits.max_runtime_seconds:
            raise ArchiveRejected("ARCHIVE_TIME_LIMIT", "归档展开超过服务端运行时间预算")

    def reserve(self, member_count: int, expanded_bytes: int) -> None:
        self.check_runtime()
        if self.members + member_count > self.limits.max_members:
            raise ArchiveRejected("ARCHIVE_MEMBER_LIMIT", "归档成员数量超过服务端预算")
        if self.expanded_bytes + expanded_bytes > self.limits.max_expanded_bytes:
            raise ArchiveRejected("ARCHIVE_EXPANDED_LIMIT", "归档展开总字节数超过服务端预算")
        self.members += member_count
        self.expanded_bytes += expanded_bytes


@dataclass(frozen=True)
class ExtractedMember:
    path: Path
    member_path: str
    size_bytes: int


@dataclass(frozen=True)
class ExtractionResult:
    members: list[ExtractedMember]
    reused: bool


class ArchiveRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def is_supported_archive(path: Path) -> bool:
    name = path.name.casefold()
    return name.endswith((".zip", ".tar", ".tar.gz", ".tgz", ".gz"))


def extraction_destination(archive_path: Path) -> Path:
    """Return the visible, archive-adjacent directory managed by the extractor."""

    return archive_path.with_name(f"{archive_path.name}{UNPACKED_SUFFIX}")


def _safe_member_path(raw_name: str) -> str:
    normalized = raw_name.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    parts = tuple(part for part in candidate.parts if part not in ("", "."))
    if (
        not parts
        or candidate.is_absolute()
        or any(part == ".." for part in parts)
        or any(":" in part for part in parts)
    ):
        raise ArchiveRejected("ARCHIVE_PATH_TRAVERSAL", f"归档成员路径不安全: {raw_name}")
    for part in parts:
        stem = part.split(".", 1)[0].rstrip(" ").casefold()
        if part != part.rstrip(" .") or stem in WINDOWS_RESERVED_NAMES:
            raise ArchiveRejected("ARCHIVE_PATH_UNSAFE_WINDOWS", f"归档成员路径不兼容 Windows: {raw_name}")
    safe = PurePosixPath(*parts).as_posix()
    if safe.casefold() == MANIFEST_NAME.casefold():
        raise ArchiveRejected("ARCHIVE_RESERVED_PATH", f"归档成员占用内部管理路径: {raw_name}")
    return safe


def _target_path(root: Path, member_path: str) -> Path:
    target = (root / Path(*PurePosixPath(member_path).parts)).resolve(strict=False)
    try:
        target.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise ArchiveRejected("ARCHIVE_PATH_TRAVERSAL", f"归档成员逃出目标目录: {member_path}") from exc
    return target


def _source_sha256(path: Path, budget: ExtractionBudget) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            budget.check_runtime()
            chunk = handle.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class ArchiveExtractor:
    def __init__(self, limits: ArchiveLimits):
        self.limits = limits

    def extract(
        self,
        archive_path: Path,
        destination: Path,
        budget: ExtractionBudget,
        *,
        force: bool = False,
    ) -> ExtractionResult:
        stat_result = archive_path.stat()
        if stat_result.st_size > self.limits.max_archive_bytes:
            raise ArchiveRejected("ARCHIVE_FILE_LIMIT", "归档文件超过服务端单归档大小预算")
        if not is_supported_archive(archive_path):
            raise ArchiveRejected("ARCHIVE_FORMAT_UNSUPPORTED", f"不支持安全展开的归档格式: {archive_path.suffix}")

        cached = None if force else self._load_cached(archive_path, destination, budget)
        if cached is not None:
            return ExtractionResult(cached, reused=True)

        source_sha256 = _source_sha256(archive_path, budget)

        # The destination now lives beside the source archive and is visible to users.
        # Never replace a pre-existing directory unless it was created by this extractor.
        if destination.exists() and not self._is_managed_destination(destination):
            raise ArchiveRejected(
                "ARCHIVE_DESTINATION_CONFLICT",
                f"解压目标已存在且不受 Bug Agent 管理: {destination.name}",
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir(parents=False, exist_ok=False)
        try:
            name = archive_path.name.casefold()
            if name.endswith(".zip"):
                members = self._extract_zip(archive_path, staging, budget)
            elif name.endswith((".tar", ".tar.gz", ".tgz")):
                members = self._extract_tar(archive_path, staging, budget)
            else:
                members = self._extract_gzip(archive_path, staging, budget)

            if _source_sha256(archive_path, budget) != source_sha256:
                raise ArchiveRejected("ARCHIVE_SOURCE_CHANGED", "归档在解压过程中发生变化")

            manifest = {
                "version": 1,
                "mode": "full",
                "source_size": stat_result.st_size,
                "source_mtime_ns": stat_result.st_mtime_ns,
                "source_sha256": source_sha256,
                "members": [
                    {"path": item.member_path, "size_bytes": item.size_bytes}
                    for item in members
                ],
            }
            (staging / MANIFEST_NAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            backup = None
            if destination.exists():
                backup = destination.parent / f".{destination.name}.backup-{uuid.uuid4().hex}"
                destination.replace(backup)
            # Windows 上杀毒/索引程序可能短暂持有刚写完的目录句柄。
            # 只对可恢复的 sharing violation 做有限重试。
            try:
                for attempt in range(3):
                    try:
                        staging.replace(destination)
                        break
                    except PermissionError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05 * (attempt + 1))
            except Exception:
                if backup is not None and not destination.exists():
                    try:
                        backup.replace(destination)
                    except OSError:
                        # The backup remains beside the archive for manual recovery.
                        pass
                raise
            if backup is not None:
                try:
                    shutil.rmtree(backup)
                except OSError:
                    # 新目录已经原子安装完成；保留隐藏备份比误删或宣告失败更安全。
                    pass
            return ExtractionResult(
                [
                    ExtractedMember(destination / Path(*PurePosixPath(item.member_path).parts), item.member_path, item.size_bytes)
                    for item in members
                ],
                reused=False,
            )
        except ArchiveRejected:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        except (zipfile.BadZipFile, tarfile.TarError, gzip.BadGzipFile, EOFError) as exc:
            if staging.exists():
                shutil.rmtree(staging)
            raise ArchiveRejected("ARCHIVE_INVALID", f"归档损坏或格式无效: {type(exc).__name__}") from exc
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    @staticmethod
    def _is_managed_destination(destination: Path) -> bool:
        if destination.is_symlink() or not destination.is_dir():
            return False
        manifest_path = destination / MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        version = manifest.get("version")
        if version not in (1, 2) or not isinstance(manifest.get("members"), list):
            return False
        if version == 1:
            if not isinstance(manifest.get("source_size"), int) or not isinstance(manifest.get("source_mtime_ns"), int):
                return False
        else:
            fingerprint = manifest.get("source_fingerprint")
            if (
                not isinstance(fingerprint, dict)
                or not isinstance(fingerprint.get("size_bytes"), int)
                or not isinstance(fingerprint.get("mtime_ns"), int)
                or not isinstance(fingerprint.get("sha256"), str)
            ):
                return False

        expected: dict[str, int] = {}
        try:
            for raw in manifest["members"]:
                raw_path = raw.get("path", raw.get("member_path"))
                if raw_path is None:
                    return False
                member_path = _safe_member_path(str(raw_path))
                size_bytes = int(raw["size_bytes"])
                key = member_path.casefold()
                if size_bytes < 0 or key in expected:
                    return False
                expected[key] = size_bytes
        except (ArchiveRejected, KeyError, TypeError, ValueError):
            return False

        actual: dict[str, int] = {}
        expected_dirs: set[str] = set()
        for member_key in expected:
            parts = PurePosixPath(member_key).parts[:-1]
            for index in range(1, len(parts) + 1):
                expected_dirs.add(PurePosixPath(*parts[:index]).as_posix().casefold())
        for root, dirs, files in os.walk(destination, topdown=True, followlinks=False):
            root_path = Path(root)
            if root_path.is_symlink():
                return False
            for dirname in list(dirs):
                child = root_path / dirname
                if child.is_symlink():
                    return False
                if dirname.casefold().endswith(UNPACKED_SUFFIX):
                    sibling_name = dirname[:-len(UNPACKED_SUFFIX)]
                    sibling = root_path / sibling_name
                    sibling_rel = sibling.relative_to(destination).as_posix().casefold()
                    if sibling_rel not in expected or not ArchiveExtractor._is_managed_destination(child):
                        return False
                    dirs.remove(dirname)
                    continue
                child_rel = child.relative_to(destination).as_posix().casefold()
                if child_rel not in expected_dirs:
                    return False
            for filename in files:
                candidate = root_path / filename
                if candidate == manifest_path:
                    continue
                if candidate.is_symlink() or not candidate.is_file():
                    return False
                relative = candidate.relative_to(destination).as_posix()
                key = relative.casefold()
                if key in actual:
                    return False
                actual[key] = candidate.stat().st_size
        return actual == expected

    def _load_cached(
        self,
        archive_path: Path,
        destination: Path,
        budget: ExtractionBudget,
    ) -> list[ExtractedMember] | None:
        manifest_path = destination / MANIFEST_NAME
        if not manifest_path.is_file():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_stat = archive_path.stat()
            if (
                manifest.get("version") != 1
                or manifest.get("mode", "full") != "full"
                or manifest.get("source_size") != source_stat.st_size
                or manifest.get("source_mtime_ns") != source_stat.st_mtime_ns
                or not isinstance(manifest.get("source_sha256"), str)
            ):
                return None
            if _source_sha256(archive_path, budget) != manifest["source_sha256"]:
                return None
            members: list[ExtractedMember] = []
            seen: set[str] = set()
            for raw in manifest.get("members", []):
                member_path = _safe_member_path(str(raw["path"]))
                key = member_path.casefold()
                if key in seen:
                    return None
                seen.add(key)
                target = _target_path(destination, member_path)
                if target.is_symlink() or not target.is_file():
                    return None
                actual_size = target.stat().st_size
                if actual_size != int(raw["size_bytes"]):
                    return None
                members.append(ExtractedMember(target, member_path, actual_size))
            budget.reserve(len(members), sum(item.size_bytes for item in members))
            return members
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _validate_plan(self, archive_size: int, entries: list[tuple[str, int]]) -> None:
        total = 0
        seen: set[str] = set()
        for member_path, size_bytes in entries:
            key = member_path.casefold()
            if key in seen:
                raise ArchiveRejected("ARCHIVE_DUPLICATE_PATH", f"归档包含重复目标路径: {member_path}")
            seen.add(key)
            if size_bytes < 0 or size_bytes > self.limits.max_member_bytes:
                raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_LIMIT", f"归档成员超过大小预算: {member_path}")
            total += size_bytes
        keys = {member_path.casefold() for member_path, _ in entries}
        for member_path, _ in entries:
            parts = PurePosixPath(member_path.casefold()).parts
            for index in range(1, len(parts)):
                parent = PurePosixPath(*parts[:index]).as_posix()
                if parent in keys:
                    raise ArchiveRejected(
                        "ARCHIVE_PATH_CONFLICT",
                        f"归档成员存在文件/目录前缀冲突: {member_path}",
                    )
        ratio = total / max(archive_size, 1)
        if ratio > self.limits.max_compression_ratio:
            raise ArchiveRejected("ARCHIVE_RATIO_LIMIT", f"归档声明压缩比 {ratio:.1f} 超过预算")

    def _copy_member(self, source, target: Path, declared_size: int, budget: ExtractionBudget) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with target.open("xb") as output:
            while True:
                budget.check_runtime()
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > declared_size or written > self.limits.max_member_bytes:
                    raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_MISMATCH", f"归档成员实际大小异常: {target.name}")
                output.write(chunk)
        if written != declared_size:
            raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_MISMATCH", f"归档成员大小与元数据不一致: {target.name}")
        return written

    def _extract_zip(self, archive_path: Path, root: Path, budget: ExtractionBudget) -> list[ExtractedMember]:
        with zipfile.ZipFile(archive_path) as archive:
            planned: list[tuple[zipfile.ZipInfo, str]] = []
            infos = archive.infolist()
            if len(infos) + budget.members > self.limits.max_members:
                raise ArchiveRejected("ARCHIVE_MEMBER_LIMIT", "ZIP 条目数量超过服务端预算")
            for info in infos:
                member_path = _safe_member_path(info.filename)
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.flag_bits & 0x1:
                    raise ArchiveRejected("ARCHIVE_ENCRYPTED", f"不支持加密 ZIP 成员: {member_path}")
                if stat.S_ISLNK(mode):
                    raise ArchiveRejected("ARCHIVE_LINK_REJECTED", f"拒绝 ZIP 符号链接: {member_path}")
                if info.is_dir():
                    continue
                # 一些 ZIP writer 只记录 0600/0644 权限而不记录 S_IFREG 类型位。
                # 类型位为 0 时按普通文件处理；已声明的其他类型仍全部拒绝。
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ArchiveRejected("ARCHIVE_SPECIAL_FILE", f"拒绝 ZIP 特殊文件: {member_path}")
                planned.append((info, member_path))
            entries = [(name, info.file_size) for info, name in planned]
            self._validate_plan(archive_path.stat().st_size, entries)
            budget.reserve(len(entries), sum(size for _, size in entries))
            result: list[ExtractedMember] = []
            for info, member_path in planned:
                target = _target_path(root, member_path)
                with archive.open(info, "r") as source:
                    written = self._copy_member(source, target, info.file_size, budget)
                result.append(ExtractedMember(target, member_path, written))
            return result

    def _extract_tar(self, archive_path: Path, root: Path, budget: ExtractionBudget) -> list[ExtractedMember]:
        with tarfile.open(archive_path, mode="r:*") as archive:
            planned: list[tuple[tarfile.TarInfo, str]] = []
            entry_count = 0
            for info in archive:
                entry_count += 1
                if entry_count + budget.members > self.limits.max_members:
                    raise ArchiveRejected("ARCHIVE_MEMBER_LIMIT", "TAR 条目数量超过服务端预算")
                member_path = _safe_member_path(info.name)
                if info.isdir():
                    continue
                if info.issym() or info.islnk():
                    raise ArchiveRejected("ARCHIVE_LINK_REJECTED", f"拒绝 TAR 链接: {member_path}")
                if not info.isfile():
                    raise ArchiveRejected("ARCHIVE_SPECIAL_FILE", f"拒绝 TAR 特殊文件: {member_path}")
                planned.append((info, member_path))
            entries = [(name, info.size) for info, name in planned]
            self._validate_plan(archive_path.stat().st_size, entries)
            budget.reserve(len(entries), sum(size for _, size in entries))
            result: list[ExtractedMember] = []
            for info, member_path in planned:
                source = archive.extractfile(info)
                if source is None:
                    raise ArchiveRejected("ARCHIVE_READ_ERROR", f"无法读取 TAR 成员: {member_path}")
                with source:
                    target = _target_path(root, member_path)
                    written = self._copy_member(source, target, info.size, budget)
                result.append(ExtractedMember(target, member_path, written))
            return result

    def _extract_gzip(self, archive_path: Path, root: Path, budget: ExtractionBudget) -> list[ExtractedMember]:
        output_name = archive_path.name[:-3] or "content"
        member_path = _safe_member_path(output_name)
        target = _target_path(root, member_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with gzip.open(archive_path, "rb") as source, target.open("xb") as output:
            while True:
                budget.check_runtime()
                chunk = source.read(COPY_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > self.limits.max_member_bytes:
                    raise ArchiveRejected("ARCHIVE_MEMBER_SIZE_LIMIT", f"GZIP 展开内容超过大小预算: {member_path}")
                if budget.expanded_bytes + written > self.limits.max_expanded_bytes:
                    raise ArchiveRejected("ARCHIVE_EXPANDED_LIMIT", "GZIP 展开总字节数超过服务端预算")
                ratio = written / max(archive_path.stat().st_size, 1)
                if ratio > self.limits.max_compression_ratio:
                    raise ArchiveRejected("ARCHIVE_RATIO_LIMIT", f"GZIP 实际压缩比 {ratio:.1f} 超过预算")
                output.write(chunk)
        budget.reserve(1, written)
        return [ExtractedMember(target, member_path, written)]
