"""Bug Case 与 Artifact 注册表。

安全根目录由 MCP Server 配置。工具调用者只能在这些根目录内打开 Case；
注册后，后续工具通过 case_id/artifact_id 工作，不再接收裸文件路径。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

from .archive_manager import ArchiveLimits, extraction_destination, is_supported_archive
from .domain import Artifact, ArtifactKind, CaseInfo
from .errors import make_error, make_success
from .log_index import IndexLimits
from .models import ToolResult
from .security import PathGuard


TEXT_SUFFIXES = {
    "", ".log", ".txt", ".curf", ".trace", ".out",
    ".csv", ".json", ".xml", ".prop", ".md", ".cfg", ".conf",
    ".ini", ".yaml", ".yml", ".py", ".c", ".cc", ".cpp", ".h",
    ".hpp", ".java", ".kt", ".sh",
}
ARCHIVE_SUFFIXES = {".zip", ".gz", ".tgz", ".tar", ".7z", ".rar"}
SKIP_DIR_NAMES = {
    ".git", ".venv", "__pycache__", "node_modules", ".log-analyzer", ".bug-agent",
}
SKIP_DIR_NAMES_CASEFOLD = {name.casefold() for name in SKIP_DIR_NAMES}


def _is_generated_part(part: str) -> bool:
    """Recognize Agent-owned directories that must not become raw Case inputs."""

    folded = part.casefold()
    return (
        folded in SKIP_DIR_NAMES_CASEFOLD
        or folded.endswith(".unpacked")
        or ".unpacked.staging-" in folded
        or ".unpacked.backup-" in folded
    )


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 1.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = float(raw)
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


def _stable_id(prefix: str, value: str, length: int = 16) -> str:
    """根据稳定输入生成短 ID，同一路径重复注册会得到相同 ID。"""

    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}_{digest[:length]}"


def infer_artifact_kind(path: Path) -> ArtifactKind:
    """根据文件名语义和扩展名推断附件类型。

    这里优先看文件名，因为 Android debuglogger 经常使用 .curf 或无扩展名，
    仅靠 suffix 无法区分 logcat、kernel 和 SOS 日志。
    """

    name = path.name.lower()
    if is_supported_archive(path):
        return "archive"
    suffix = path.suffix.lower()
    if "tombstone" in name or "native_crash" in name:
        return "tombstone"
    if "anr" in name or name.startswith("traces"):
        return "anr"
    if any(token in name for token in ("kernel", "dmesg", "kmsg", "pstore", "ramoops")):
        return "kernel"
    if any(token in name for token in ("logcat", "main_log", "system_log", "events_log", "radio_log")):
        return "logcat"
    if any(token in name for token in ("perfetto", "systrace", "boottrace")) or suffix == ".trace":
        return "trace"
    if any(token in name for token in ("sos", "hypervisor", "qnx")):
        return "sos"
    if suffix in TEXT_SUFFIXES:
        return "text"
    return "binary"


class CaseRegistry:
    """进程内 Case Registry；路径授权来自服务端配置而非 Tool Call。

    Registry 同时承担 Adapter 与安全引用表的职责：
    - 外部输入：case_path；
    - 内部保存：case_id/artifact_id 到真实 Path 的映射；
    - 对模型输出：相对路径和稳定 ID。

    当前版本是进程内状态，Server 重启后需要重新调用 open_case。后续如果
    需要断点恢复，可以把这层替换为 SQLite，而不修改 Agent 的工具契约。
    """

    def __init__(
        self,
        allowed_roots: list[str],
        *,
        max_artifacts: int = 20_000,
        work_root: str | None = None,
        archive_limits: ArchiveLimits | None = None,
        index_limits: IndexLimits | None = None,
    ):
        if not allowed_roots:
            raise ValueError("allowed_roots 不能为空")
        # allowed_roots 来自 Server 配置。绝不能从 MCP Tool 参数中读取，
        # 否则就变成“调用者自己声明自己可以访问哪里”。
        self.guard = PathGuard(allowed_roots)
        self.allowed_roots = list(self.guard.allowed_dirs)
        self.max_artifacts = max_artifacts
        self._work_root_explicit = work_root is not None
        self.work_root = Path(work_root or (Path(tempfile.gettempdir()) / "jira-bug-agent-log-analyzer")).resolve()
        self.archive_limits = archive_limits or ArchiveLimits()
        self.index_limits = index_limits or IndexLimits()
        self._cases: dict[str, tuple[Path, CaseInfo]] = {}
        self._artifact_paths: dict[tuple[str, str], Path] = {}
        self._case_work_dirs: dict[str, Path] = {}

    @classmethod
    def from_environment(cls, allowed_roots: list[str] | None = None) -> "CaseRegistry":
        """从 Server 环境变量构建 Registry。

        os.pathsep 在 Windows 是分号，在 Linux/macOS 是冒号，因此同一段
        配置解析逻辑可以跨平台工作。
        """

        raw = os.environ.get("LOG_ANALYZER_ALLOWED_ROOTS", "").strip()
        roots = [path for path in raw.split(os.pathsep) if path]
        work_root = os.environ.get("LOG_ANALYZER_WORK_ROOT", "").strip() or None
        archive_limits = ArchiveLimits(
            max_archive_bytes=_env_int("LOG_ANALYZER_MAX_ARCHIVE_BYTES", 4 * 1024**3),
            max_members=_env_int("LOG_ANALYZER_MAX_EXTRACTED_FILES", 20_000),
            max_member_bytes=_env_int("LOG_ANALYZER_MAX_MEMBER_BYTES", 4 * 1024**3),
            max_expanded_bytes=_env_int("LOG_ANALYZER_MAX_EXTRACTED_BYTES", 16 * 1024**3),
            max_compression_ratio=_env_float("LOG_ANALYZER_MAX_COMPRESSION_RATIO", 200.0),
            max_depth=_env_int("LOG_ANALYZER_MAX_ARCHIVE_DEPTH", 2),
            max_runtime_seconds=_env_float("LOG_ANALYZER_MAX_EXTRACT_SECONDS", 300.0),
        )
        index_limits = IndexLimits(
            max_input_bytes=_env_int("LOG_ANALYZER_MAX_INDEX_BYTES", 16 * 1024**3),
            max_storage_bytes=_env_int("LOG_ANALYZER_MAX_INDEX_STORAGE_BYTES", 16 * 1024**3),
            chunk_bytes=_env_int("LOG_ANALYZER_INDEX_CHUNK_BYTES", 1024**2, minimum=64 * 1024),
            max_build_seconds=_env_float("LOG_ANALYZER_MAX_INDEX_SECONDS", 600.0),
            max_search_seconds=_env_float("LOG_ANALYZER_MAX_INDEX_QUERY_SECONDS", 30.0),
        )
        return cls(
            allowed_roots or roots or [os.getcwd()],
            work_root=work_root,
            archive_limits=archive_limits,
            index_limits=index_limits,
        )

    def open_case(self, case_path: str) -> ToolResult:
        """校验并注册 Case，建立 Artifact 清单和内部路径映射。"""

        # 第一层检查：Case 本身必须位于 Server 配置的 allowed roots 内。
        checked = self.guard.validate(case_path)
        if not checked.success:
            return checked
        resolved = Path(checked.data["resolved_path"])
        if not resolved.is_dir():
            return make_error("CASE_NOT_DIRECTORY", "Case 路径必须是已存在的目录")
        internal_work_dir = resolved / ".bug-agent"
        is_junction = getattr(internal_work_dir, "is_junction", lambda: False)
        if internal_work_dir.is_symlink() or is_junction():
            return make_error(
                "WORK_DIR_UNSAFE",
                "Case 内的 .bug-agent 不能是符号链接或 Junction",
            )
        if internal_work_dir.exists() and not internal_work_dir.is_dir():
            return make_error("WORK_DIR_UNSAFE", "Case 内的 .bug-agent 必须是目录")
        if self._work_root_explicit:
            try:
                relative_work_root = self.work_root.relative_to(resolved)
            except ValueError:
                relative_work_root = None
            if relative_work_root is not None and relative_work_root.parts != (".bug-agent",):
                return make_error(
                    "WORK_ROOT_INSIDE_CASE",
                    "Case 内部只允许使用 .bug-agent 作为 Log Analyzer 工作区",
                )

        case_id = _stable_id("case", str(resolved).casefold())
        for key in [key for key in self._artifact_paths if key[0] == case_id]:
            del self._artifact_paths[key]
        artifacts: list[Artifact] = []
        total_size = 0
        # 注册阶段只收集元数据，不读取文件内容。真正读取由具体分析工具按需完成。
        for root, dirs, files in os.walk(resolved, topdown=True, followlinks=False):
            root_path = Path(root)
            # Prune generated trees before traversal. Path.rglob would still enumerate
            # every extracted log even if registration later skipped it.
            dirs[:] = [
                name for name in dirs
                if not _is_generated_part(name) and not (root_path / name).is_symlink()
            ]
            for filename in files:
                if len(artifacts) >= self.max_artifacts:
                    return make_error("CASE_TOO_LARGE", f"Case 文件数量超过上限 {self.max_artifacts}")
                path = root_path / filename
                if path.is_symlink() or not path.is_file():
                    continue
                # 第二层检查：解析真实路径后仍必须位于 Case 内。
                # 即使未来平台出现 junction 等重定向，也不能借此逃出 Case。
                try:
                    real_path = path.resolve(strict=True)
                    real_path.relative_to(resolved)
                    stat = real_path.stat()
                except (OSError, ValueError):
                    continue

                relative_path = real_path.relative_to(resolved).as_posix()
                kind = infer_artifact_kind(real_path)
                # ID 只依赖 Case 与相对路径；模型无需持有真实绝对路径。
                artifact_id = _stable_id("artifact", f"{case_id}:{relative_path.casefold()}")
                artifact = Artifact(
                    artifact_id=artifact_id,
                    name=real_path.name,
                    relative_path=relative_path,
                    kind=kind,
                    size_bytes=stat.st_size,
                    modified_at=str(int(stat.st_mtime)),
                    readable_text=kind not in ("archive", "binary", "trace"),
                )
                artifacts.append(artifact)
                self._artifact_paths[(case_id, artifact_id)] = real_path
                total_size += stat.st_size

        artifacts.sort(key=lambda item: item.relative_path.casefold())
        info = CaseInfo(
            case_id=case_id,
            name=resolved.name,
            artifact_count=len(artifacts),
            total_size_bytes=total_size,
            artifacts=artifacts,
        )
        self._cases[case_id] = (resolved, info)
        return make_success({"case": info.model_dump()})

    def get_work_dir(self, case_id: str) -> Path | None:
        entry = self._cases.get(case_id)
        if entry is None:
            return None
        case_root, _ = entry
        default = self.work_root / case_id if self._work_root_explicit else case_root / ".bug-agent"
        candidate = self._case_work_dirs.get(case_id, default)
        if not self._work_root_explicit and case_id not in self._case_work_dirs:
            is_junction = getattr(candidate, "is_junction", lambda: False)
            if candidate.is_symlink() or is_junction():
                raise ValueError("Case 内的 .bug-agent 不能是符号链接或 Junction")
            resolved_candidate = candidate.resolve(strict=False)
            try:
                resolved_candidate.relative_to(case_root)
            except ValueError as exc:
                raise ValueError("Case 内的 .bug-agent 逃出 Case 目录") from exc
            return resolved_candidate
        return candidate

    def set_case_work_dir(self, case_id: str, work_dir: Path) -> None:
        """为受信任的宿主 CLI 指定单 Case 工作区；MCP Tool 不暴露此入口。"""

        entry = self._cases.get(case_id)
        if entry is None:
            raise ValueError("Case 尚未注册")
        case_root, _ = entry
        resolved = work_dir.expanduser().resolve()
        try:
            relative = resolved.relative_to(case_root)
        except ValueError:
            relative = None
        if relative is not None and relative.parts != (".bug-agent",):
            raise ValueError("Case 内部只允许使用 .bug-agent 作为 Log Analyzer 工作区")
        self._case_work_dirs[case_id] = resolved

    def register_extracted_artifacts(
        self,
        case_id: str,
        *,
        source_archive: Artifact,
        members: list[tuple[Path, str]],
    ) -> list[Artifact]:
        """注册由受控 Extractor 生成的文件，使用 archive!/member 虚拟相对路径。"""

        entry = self._cases.get(case_id)
        if entry is None:
            return []
        _, info = entry
        source_path = self.get_artifact_path(case_id, source_archive.artifact_id)
        if source_path is None:
            return []
        managed_root = extraction_destination(source_path).resolve(strict=False)
        existing = {item.artifact_id: item for item in info.artifacts}
        candidates: list[tuple[Artifact, Path]] = []
        for real_path, member_path in members:
            if real_path.is_symlink():
                continue
            try:
                checked_path = real_path.resolve(strict=True)
                checked_path.relative_to(managed_root)
            except (OSError, ValueError):
                continue
            if checked_path.is_symlink() or not checked_path.is_file():
                continue
            virtual_path = f"{source_archive.relative_path}!/{member_path}"
            artifact_id = _stable_id("artifact", f"{case_id}:{virtual_path.casefold()}")
            stat_result = checked_path.stat()
            kind = infer_artifact_kind(checked_path)
            artifact = Artifact(
                artifact_id=artifact_id,
                name=checked_path.name,
                relative_path=virtual_path,
                kind=kind,
                size_bytes=stat_result.st_size,
                modified_at=str(int(stat_result.st_mtime)),
                readable_text=kind not in ("archive", "binary", "trace"),
                origin="archive",
                source_archive_id=source_archive.artifact_id,
            )
            candidates.append((artifact, checked_path))

        new_ids = {artifact.artifact_id for artifact, _ in candidates if artifact.artifact_id not in existing}
        if len(existing) + len(new_ids) > self.max_artifacts:
            raise ValueError(f"Case 文件数量超过上限 {self.max_artifacts}")

        registered: list[Artifact] = []
        for artifact, checked_path in candidates:
            artifact_id = artifact.artifact_id
            if artifact_id not in existing:
                info.artifacts.append(artifact)
                info.total_size_bytes += artifact.size_bytes
                info.artifact_count += 1
                existing[artifact_id] = artifact
            else:
                artifact = existing[artifact_id]
            self._artifact_paths[(case_id, artifact_id)] = checked_path
            registered.append(artifact)
        info.artifacts.sort(key=lambda item: item.relative_path.casefold())
        return registered

    def remove_archive_descendants(self, case_id: str, source_archive_id: str) -> None:
        """在成功重建某个归档后移除旧的派生 Artifact 映射。"""

        entry = self._cases.get(case_id)
        if entry is None:
            return
        _, info = entry
        removed_ids = {source_archive_id}
        changed = True
        while changed:
            changed = False
            for artifact in info.artifacts:
                if artifact.source_archive_id in removed_ids and artifact.artifact_id not in removed_ids:
                    removed_ids.add(artifact.artifact_id)
                    changed = True
        removed_ids.discard(source_archive_id)
        if not removed_ids:
            return
        removed = [artifact for artifact in info.artifacts if artifact.artifact_id in removed_ids]
        info.artifacts = [artifact for artifact in info.artifacts if artifact.artifact_id not in removed_ids]
        info.artifact_count = len(info.artifacts)
        info.total_size_bytes -= sum(artifact.size_bytes for artifact in removed)
        for artifact_id in removed_ids:
            self._artifact_paths.pop((case_id, artifact_id), None)

    def artifact_paths(self, case_id: str, artifacts: list[Artifact]) -> list[tuple[Artifact, Path]]:
        result = []
        for artifact in artifacts:
            path = self.get_artifact_path(case_id, artifact.artifact_id)
            if path is not None:
                result.append((artifact, path))
        return result

    def get_case(self, case_id: str) -> tuple[Path, CaseInfo] | None:
        return self._cases.get(case_id)

    def get_artifact_path(self, case_id: str, artifact_id: str) -> Path | None:
        stored = self._artifact_paths.get((case_id, artifact_id))
        entry = self._cases.get(case_id)
        if stored is None or entry is None or stored.is_symlink():
            return None
        case_root, _ = entry
        try:
            is_root_junction = getattr(case_root, "is_junction", lambda: False)
            if case_root.is_symlink() or is_root_junction():
                return None
            current_case_root = case_root.resolve(strict=True)
            if current_case_root != case_root:
                return None
            current = stored.resolve(strict=True)
            current.relative_to(current_case_root)
        except (OSError, ValueError):
            return None
        if current.is_symlink() or not current.is_file():
            return None
        _, info = entry
        artifact = next((item for item in info.artifacts if item.artifact_id == artifact_id), None)
        if artifact is not None and artifact.origin == "archive" and artifact.source_archive_id:
            source_path = self.get_artifact_path(case_id, artifact.source_archive_id)
            if source_path is None:
                return None
            managed_root = extraction_destination(source_path).resolve(strict=False)
            try:
                current.relative_to(managed_root)
                manifest = json.loads((managed_root / ".extraction-manifest.json").read_text(encoding="utf-8"))
                source_stat = source_path.stat()
                if manifest.get("version") == 1:
                    source_matches = (
                        manifest.get("source_size") == source_stat.st_size
                        and manifest.get("source_mtime_ns") == source_stat.st_mtime_ns
                    )
                elif manifest.get("version") == 2:
                    fingerprint = manifest.get("source_fingerprint", {})
                    source_matches = (
                        fingerprint.get("size_bytes") == source_stat.st_size
                        and fingerprint.get("mtime_ns") == source_stat.st_mtime_ns
                    )
                else:
                    source_matches = False
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            if not source_matches:
                return None
        return current

    def select_artifacts(
        self,
        case_id: str,
        *,
        artifact_ids: list[str] | None = None,
        artifact_kinds: list[str] | None = None,
        text_only: bool = True,
    ) -> list[Artifact]:
        """按 ID/类型选择附件；默认排除不可安全当作文本读取的文件。"""

        entry = self.get_case(case_id)
        if entry is None:
            return []
        _, info = entry
        ids = set(artifact_ids or [])
        kinds = set(artifact_kinds or [])
        return [
            artifact for artifact in info.artifacts
            if (not ids or artifact.artifact_id in ids)
            and (not kinds or artifact.kind in kinds)
            and (not text_only or artifact.readable_text)
            and self.get_artifact_path(case_id, artifact.artifact_id) is not None
        ]
