"""面向 Agent 的 V2 日志分析服务。

Service 位于 MCP 协议层和文件解析层之间：

    MCP Server → LogAnalyzerService → CaseRegistry / 文件解析

这里不关心 stdio、JSON-RPC 或 MCP Content 类型，只接收 Python 参数并返回
ToolResult，因此可以脱离 MCP 单独测试，也可以被其他 Runtime 复用。
"""

from __future__ import annotations

from collections import Counter, deque
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Callable

from log_analysis_core import (
    classify_diagnostic_line,
    extract_timestamp,
    infer_component,
    stable_id,
)
from pydantic import ValidationError

from .archive_manager import (
    ArchiveExtractor,
    ArchiveRejected,
    ExtractionBudget,
    extraction_destination,
    is_supported_archive,
)
from .archive_selection import extract_archive_members as extract_selected_members
from .archive_selection import (
    TimeReliability,
    inspect_reusable_extraction,
    inventory_archive,
    parse_path_timestamp,
    probe_archive_members,
    summarize_member_times,
)
from .case_registry import CaseRegistry
from .domain import (
    BuildIndexInput,
    DiagnosticFinding,
    Evidence,
    ExtractAeeDbInput,
    ExtractArchiveMembersInput,
    ExtractTimelineInput,
    GetCaseCommentInput,
    InspectCaseInput,
    InspectArchiveInput,
    OpenCaseInput,
    ParseDiagnosticsInput,
    PrepareCaseInput,
    ProbeArchiveMembersInput,
    SearchEvidenceInput,
    TimelineEvent,
)
from .errors import make_error, make_success
from .log_index import IndexMatch, LogIndex
from .models import ToolResult


MAX_SCAN_BYTES_PER_FILE = 128 * 1024 * 1024
MAX_SCAN_BYTES_PER_CALL = 512 * 1024 * 1024


class LogAnalyzerService:
    """V2 工具的领域实现与统一分发入口。"""

    def __init__(self, registry: CaseRegistry):
        self.registry = registry
        self._indexes: dict[str, LogIndex] = {}
        self.handlers: dict[str, Callable[..., ToolResult]] = {
            "open_case": self.open_case,
            "inspect_case": self.inspect_case,
            "inspect_archive": self.inspect_archive,
            "probe_archive_members": self.probe_archive_members,
            "extract_archive_members": self.extract_archive_members,
            "extract_aee_db": self.extract_aee_db,
            "build_index": self.build_index,
            "prepare_case": self.prepare_case,
            "search_evidence": self.search_evidence,
            "extract_timeline": self.extract_timeline,
            "parse_diagnostics": self.parse_diagnostics,
            "get_case_comment": self.get_case_comment,
        }

    def dispatch(self, name: str, arguments: dict) -> ToolResult:
        """校验并执行工具，将可预期错误转换为统一 ToolResult。

        MCP Server 只需要调用这一个入口。参数验证失败是调用错误，文件读取
        失败可能可重试；未知编程错误交给最外层协议边界兜底。
        """

        handler = self.handlers.get(name)
        if handler is None:
            return make_error("NOT_IMPLEMENTED", f"未知工具: {name}")
        try:
            return handler(**arguments)
        except ValidationError as exc:
            return make_error("INVALID_PARAMS", exc.json(include_url=False))
        except sqlite3.Error as exc:
            return make_error("INDEX_ERROR", str(exc), retryable=True)
        except (OSError, UnicodeError) as exc:
            return make_error("FILE_READ_ERROR", str(exc), retryable=True)

    def open_case(self, **kwargs) -> ToolResult:
        """注册 Case；它是其他四个工具的前置步骤。"""

        params = OpenCaseInput.model_validate(kwargs)
        return self.registry.open_case(params.case_path)

    def inspect_case(self, **kwargs) -> ToolResult:
        """返回 Case 概览，帮助 Agent 在读取大日志前先制定计划。

        当附件数量可能超过 sample_limit 时，会在摘要中高亮归档文件，
        确保 Agent 始终能看到需要解压的压缩包，避免被截断遮蔽。
        """

        params = InspectCaseInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        case_root, info = entry
        kind_counts = Counter(artifact.kind for artifact in info.artifacts)
        text_bytes = sum(a.size_bytes for a in info.artifacts if a.readable_text)
        index = self._get_index(info.case_id)
        indexed_ids = index.indexed_artifact_ids() if index else set()

        # 按类型分组排序：archive 在最前，然后按大小降序，确保模型优先看到大文件。
        sorted_artifacts = sorted(info.artifacts, key=lambda a: (
            {"archive": 0, "aee_db": 1, "text": 2, "anr": 3, "tombstone": 4,
             "trace": 5, "kernel": 6, "logcat": 7, "sos": 8, "binary": 9}.get(a.kind, 10),
            -a.size_bytes,
        ))
        sample = [a.model_dump() for a in sorted_artifacts[: params.sample_limit]]
        truncated = info.artifact_count > params.sample_limit

        # 摘要：高亮所有归档（模型首选目标），并列出大文件。
        archive_summary = [
            {
                "artifact_id": a.artifact_id,
                "name": a.name,
                "relative_path": a.relative_path,
                "size_bytes": a.size_bytes,
                "kind": a.kind,
            }
            for a in sorted_artifacts if a.kind == "archive"
        ]

        def aee_is_decoded(item) -> bool:
            source = self.registry.get_artifact_path(params.case_id, item.artifact_id)
            if source is None:
                return False
            target = Path(f"{source}.DEC")
            is_junction = getattr(target, "is_junction", lambda: False)
            return (
                target.is_dir()
                and not target.is_symlink()
                and not is_junction()
                and any(path.is_file() and not path.is_symlink() for path in target.rglob("*"))
            )

        aee_db_summary = [
            {
                "artifact_id": a.artifact_id,
                "name": a.name,
                "relative_path": a.relative_path,
                "size_bytes": a.size_bytes,
                "kind": a.kind,
                "decoded": aee_is_decoded(a),
                "activation_skill": "aee-db-extract",
            }
            for a in sorted_artifacts if a.kind == "aee_db"
        ]
        large_text = [
            {
                "artifact_id": a.artifact_id,
                "name": a.name,
                "size_bytes": a.size_bytes,
                "kind": a.kind,
            }
            for a in sorted_artifacts
            if a.readable_text and a.size_bytes > 50_000
        ][:20]

        return make_success({
            "case_id": info.case_id,
            "name": info.name,
            "artifact_count": info.artifact_count,
            "total_size_bytes": info.total_size_bytes,
            "text_size_bytes": text_bytes,
            "kinds": dict(sorted(kind_counts.items())),
            "summary": {
                "archives": archive_summary,
                "aee_databases": aee_db_summary,
                "large_text_files": large_text,
                "total_archives": len(archive_summary),
                "total_aee_databases": len(aee_db_summary),
                "required_skill_activations": (
                    [{
                        "name": "aee-db-extract",
                        "reason": "Case contains one or more undecoded aee_db artifacts",
                    }]
                    if any(not item["decoded"] for item in aee_db_summary)
                    else []
                ),
                "total_artifacts": info.artifact_count,
                "artifacts_truncated": truncated,
            },
            "artifacts": sample,
            "artifacts_truncated": truncated,
            "preparation": {
                "archive_count": kind_counts.get("archive", 0),
                "aee_db_count": kind_counts.get("aee_db", 0),
                "extracted_artifact_count": sum(a.origin == "archive" for a in info.artifacts),
                "index_available": bool(indexed_ids),
                "indexed_artifact_count": len(indexed_ids),
            },
        })

    def extract_aee_db(self, **kwargs) -> ToolResult:
        """Decode one registered MTK AEE DB and register its text outputs."""

        params = ExtractAeeDbInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        case_root, info = entry
        artifact = next((item for item in info.artifacts if item.artifact_id == params.artifact_id), None)
        if artifact is None:
            return make_error("ARTIFACT_NOT_FOUND", "指定 Artifact 不存在")
        if artifact.kind != "aee_db":
            return make_error("ARTIFACT_NOT_AEE_DB", "指定 Artifact 不是 MTK AEE DB")
        source_path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
        if source_path is None:
            return make_error("ARTIFACT_NOT_FOUND", "AEE DB 路径不可用")

        output_dir = Path(f"{source_path}.DEC")
        is_junction = getattr(output_dir, "is_junction", lambda: False)
        if output_dir.is_symlink() or is_junction():
            return make_error("AEE_OUTPUT_UNSAFE", "AEE 解码目录不能是符号链接或 Junction")

        existing_files = list(output_dir.rglob("*")) if output_dir.is_dir() else []
        reused = any(path.is_file() and not path.is_symlink() for path in existing_files)
        if output_dir.exists() and not output_dir.is_dir():
            return make_error("AEE_OUTPUT_CONFLICT", "AEE 解码目标已存在且不是目录")
        if output_dir.is_dir() and not reused:
            return make_error("AEE_OUTPUT_CONFLICT", "AEE 解码目录已存在但没有可注册文件")

        if not reused:
            configured = os.getenv("AEE_EXTRACT_BIN")
            extractor = (
                Path(configured).expanduser()
                if configured
                else Path(__file__).resolve().parents[4] / "tools" / "aee_extract.exe"
            ).resolve(strict=False)
            if not extractor.is_file():
                return make_error(
                    "AEE_EXTRACTOR_NOT_FOUND",
                    "未找到受控 AEE 解码器；请配置 AEE_EXTRACT_BIN",
                )
            try:
                completed = subprocess.run(
                    [str(extractor), str(source_path)],
                    cwd=str(extractor.parent),
                    check=False,
                    capture_output=True,
                    timeout=self.registry.archive_limits.max_runtime_seconds,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                return make_error("AEE_EXTRACT_TIMEOUT", "AEE 解码超过服务端运行时间预算", retryable=True)
            if completed.returncode != 0:
                return make_error("AEE_EXTRACT_FAILED", f"AEE 解码器退出码: {completed.returncode}")

        is_junction = getattr(output_dir, "is_junction", lambda: False)
        if not output_dir.is_dir() or output_dir.is_symlink() or is_junction():
            return make_error("AEE_OUTPUT_MISSING", "AEE 解码器未生成安全的 .DEC 目录")

        decoded_paths: list[Path] = []
        expanded_bytes = 0
        for candidate in output_dir.rglob("*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                checked = candidate.resolve(strict=True)
                checked.relative_to(output_dir.resolve(strict=True))
                checked.relative_to(case_root)
            except (OSError, ValueError):
                return make_error("AEE_OUTPUT_UNSAFE", "AEE 解码结果逃出受控目录")
            size = checked.stat().st_size
            if size > self.registry.archive_limits.max_member_bytes:
                return make_error("AEE_MEMBER_SIZE_LIMIT", "AEE 解码单文件超过服务端预算")
            decoded_paths.append(checked)
            expanded_bytes += size
            if len(decoded_paths) > self.registry.archive_limits.max_members:
                return make_error("AEE_MEMBER_LIMIT", "AEE 解码文件数量超过服务端预算")
            if expanded_bytes > self.registry.archive_limits.max_expanded_bytes:
                return make_error("AEE_EXPANDED_LIMIT", "AEE 解码总字节数超过服务端预算")
        if not decoded_paths:
            return make_error("AEE_OUTPUT_EMPTY", "AEE 解码结果为空")

        refreshed = self.registry.open_case(str(case_root))
        if not refreshed.success:
            return refreshed
        refreshed_entry = self.registry.get_case(params.case_id)
        if refreshed_entry is None:
            return make_error("CASE_NOT_OPEN", "AEE 解码后 Case 刷新失败")
        _, refreshed_info = refreshed_entry
        source_relative_path = source_path.relative_to(case_root).as_posix()
        prefix = f"{source_relative_path}.DEC/".casefold()
        decoded = [
            item for item in refreshed_info.artifacts
            if item.relative_path.casefold().startswith(prefix)
        ]
        refreshed_source = next(
            (item for item in refreshed_info.artifacts if item.relative_path == source_relative_path),
            None,
        )
        return make_success({
            "case_id": params.case_id,
            "aee_db_artifact_id": (
                refreshed_source.artifact_id if refreshed_source is not None else artifact.artifact_id
            ),
            "aee_db_relative_path": artifact.relative_path,
            "output_relative_path": f"{artifact.relative_path}.DEC",
            "reused": reused,
            "decoded_file_count": len(decoded),
            "decoded_size_bytes": expanded_bytes,
            "artifacts": [item.model_dump() for item in decoded],
            "recommended_first": next(
                (item.artifact_id for item in decoded if item.name == "__exp_main.txt"),
                None,
            ),
        })

    def _get_index(self, case_id: str) -> LogIndex | None:
        existing = self._indexes.get(case_id)
        if existing is not None:
            return existing
        work_dir = self.registry.get_work_dir(case_id)
        if work_dir is None:
            return None
        candidate = LogIndex(work_dir / "index" / "logs.sqlite3", self.registry.index_limits)
        if candidate.path.is_file():
            self._indexes[case_id] = candidate
            return candidate
        return None

    @staticmethod
    def _archive_depth(info, artifact) -> int:
        """Count archive ancestry and reject malformed cycles conservatively."""

        artifacts = {item.artifact_id: item for item in info.artifacts}
        depth = 1
        seen = {artifact.artifact_id}
        current = artifact
        while current.source_archive_id:
            parent_id = current.source_archive_id
            if parent_id in seen:
                return 1_000_000
            seen.add(parent_id)
            parent = artifacts.get(parent_id)
            if parent is None:
                break
            depth += 1
            current = parent
        return depth

    def inspect_archive(self, **kwargs) -> ToolResult:
        """Return a bounded member catalog without writing extracted files."""

        params = InspectArchiveInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        _, info = entry
        artifact = next((item for item in info.artifacts if item.artifact_id == params.artifact_id), None)
        if artifact is None:
            return make_error("ARTIFACT_NOT_FOUND", "指定 Artifact 不存在")
        if artifact.kind != "archive":
            return make_error("ARTIFACT_NOT_ARCHIVE", "指定 Artifact 不是归档文件")
        if self._archive_depth(info, artifact) > self.registry.archive_limits.max_depth:
            return make_error("ARCHIVE_DEPTH_LIMIT", "归档嵌套深度超过服务端预算")
        path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
        if path is None:
            return make_error("ARTIFACT_NOT_FOUND", "指定 Artifact 路径不可用")
        try:
            inventory = inventory_archive(
                path,
                artifact.artifact_id,
                self.registry.archive_limits,
            )
        except ArchiveRejected as exc:
            if exc.code in {"ARCHIVE_INVALID", "ARCHIVE_NOT_FOUND", "ARCHIVE_SOURCE_CHANGED"}:
                self.registry.remove_archive_descendants(params.case_id, artifact.artifact_id)
            return make_error(exc.code, exc.message)
        if params.member_offset > 0 and not params.source_sha256:
            return make_error("ARCHIVE_CURSOR_REQUIRED", "翻页必须携带上一页 source_fingerprint.sha256")
        if params.source_sha256 and params.source_sha256 != inventory.source_fingerprint.sha256:
            self.registry.remove_archive_descendants(params.case_id, artifact.artifact_id)
            return make_error("ARCHIVE_SOURCE_CHANGED", "归档版本已变化，请从第一页重新读取清单")
        extraction_current, reusable = inspect_reusable_extraction(inventory)
        if extraction_current is False:
            self.registry.remove_archive_descendants(params.case_id, artifact.artifact_id)
        selection_payload = None
        time_relations: dict[str, str] = {}
        members = inventory.members
        if params.path_prefix is not None:
            members = [item for item in members if item.member_path.startswith(params.path_prefix)]
        if params.time_range is not None:
            # 分离有时间戳和无时间戳的成员
            timestamped = sorted(
                ((item, parsed) for item in members if (parsed := parse_path_timestamp(item.member_path))),
                key=lambda item: (item[1].value, item[0].member_path),
            )
            untimed = [item for item in members if item not in {t[0] for t in timestamped}]
            untimed.sort(key=lambda item: item.member_path.casefold())

            in_range = [item for item in timestamped if params.time_range.start <= item[1].value <= params.time_range.end]
            before = [item for item in timestamped if item[1].value < params.time_range.start]
            after = [item for item in timestamped if item[1].value > params.time_range.end]
            selected = [
                *[(item, "predecessor") for item, _ in (before[-params.time_neighbor_count:] if params.time_neighbor_count > 0 else [])],
                *[(item, "in_range") for item, _ in in_range],
                *[(item, "successor") for item, _ in (after[:params.time_neighbor_count] if params.time_neighbor_count > 0 else [])],
            ]
            # 保留无法依据路径时间判断的成员；ANR、AEE 等诊断文件通常正是
            # 这类成员，调用方可结合路径和类型继续缩小范围。
            members = [item for item, _ in selected]
            if untimed:
                members.extend(untimed)
            time_relations = {item.member_id: relation for item, relation in selected}
            for item in untimed:
                time_relations[item.member_id] = "untimed"

            # 评估时间戳可信度：检查是否有不可靠的时钟
            all_reliabilities = [
                parsed.reliability for _, parsed in timestamped
            ]
            unreliable_count = sum(
                1 for r in all_reliabilities if r != TimeReliability.RELIABLE
            )
            if unreliable_count > 0:
                time_reliability = "unreliable_device_clock"
                time_reliability_detail = (
                    f"{unreliable_count}/{len(timestamped)} 个成员的文件名时间戳不受信任"
                    f"（设备时钟未同步），时间筛选结果可能不可靠"
                )
            else:
                time_reliability = TimeReliability.RELIABLE
                time_reliability_detail = None

            selection_payload = {
                "mode": "path_timestamp",
                "requested_start": params.time_range.start.isoformat(),
                "requested_end": params.time_range.end.isoformat(),
                "path_prefix": params.path_prefix,
                "time_neighbor_count": params.time_neighbor_count,
                "timestamped_member_count": len(timestamped),
                "matched_member_count": len(selected),
                "selected_member_count": len(selected),
                "untimed_member_count": len(untimed),
                "time_reliability": time_reliability,
                "time_reliability_detail": time_reliability_detail,
            }
        elif params.path_prefix is not None:
            selection_payload = {"mode": "path_prefix", "path_prefix": params.path_prefix}
        page_end = min(params.member_offset + params.max_members, len(members))
        page = members[params.member_offset:page_end]
        reusable_by_id = {item.member_id: item for item in reusable}
        page_reusable = [
            reusable_by_id[item.member_id]
            for item in page
            if item.member_id in reusable_by_id
        ]
        try:
            registered = self.registry.register_extracted_artifacts(
                params.case_id,
                source_archive=artifact,
                members=[(item.path, item.member_path) for item in page_reusable],
            )
        except ValueError as exc:
            return make_error("CASE_TOO_LARGE", str(exc))
        artifacts_by_path = {item.relative_path: item for item in registered}
        member_payloads = []
        for item in page:
            parsed_time = parse_path_timestamp(item.member_path)
            virtual_path = f"{artifact.relative_path}!/{item.member_path}"
            registered_artifact = artifacts_by_path.get(virtual_path)
            member_payloads.append({
                "member_id": item.member_id,
                "member_path": item.member_path,
                "size_bytes": item.size_bytes,
                "compressed_size": item.compressed_size,
                "kind": item.kind,
                "is_archive": item.is_archive,
                "safe": item.safe,
                "reason": item.reason,
                "extracted": registered_artifact is not None,
                "artifact_id": (
                    registered_artifact.artifact_id if registered_artifact else None
                ),
                "relative_path": virtual_path if registered_artifact else None,
                "path_timestamp": parsed_time.value.isoformat() if parsed_time else None,
                "path_timestamp_pattern": parsed_time.pattern if parsed_time else None,
                "path_timestamp_reliability": parsed_time.reliability if parsed_time else None,
                "path_timestamp_clock_domain": parsed_time.clock_domain if parsed_time else None,
                "path_time_relation": time_relations.get(item.member_id),
            })
        has_more = page_end < len(members)
        time_groups = summarize_member_times(inventory.members)
        returned_time_groups = time_groups[:500]
        return make_success({
            "case_id": params.case_id,
            "artifact_id": artifact.artifact_id,
            "relative_path": artifact.relative_path,
            "format": inventory.format,
            "member_offset": params.member_offset,
            "member_count": len(page),
            "cataloged_member_count": len(inventory.members),
            "truncated": inventory.truncated or has_more,
            "next_offset": page_end if has_more else None,
            "source_fingerprint": {
                "size_bytes": inventory.source_fingerprint.size_bytes,
                "mtime_ns": inventory.source_fingerprint.mtime_ns,
                "sha256": inventory.source_fingerprint.sha256,
            },
            "extraction": {
                "reusable": extraction_current is True,
                "extracted_member_count": len(reusable),
            },
            "selection": selection_payload,
            "time_groups": returned_time_groups,
            "time_group_count": len(time_groups),
            "time_groups_truncated": len(time_groups) > len(returned_time_groups),
            "members": member_payloads,
            "required_skill_activations": (
                [{
                    "name": "aee-db-extract",
                    "reason": "Archive selection contains one or more aee_db members",
                }]
                if any(item.kind == "aee_db" for item in page)
                else []
            ),
        })

    def extract_archive_members(self, **kwargs) -> ToolResult:
        """Incrementally extract only member IDs returned by inspect_archive."""

        params = ExtractArchiveMembersInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        _, info = entry
        archive = next((item for item in info.artifacts if item.artifact_id == params.artifact_id), None)
        if archive is None:
            return make_error("ARTIFACT_NOT_FOUND", "指定 Artifact 不存在")
        if archive.kind != "archive":
            return make_error("ARTIFACT_NOT_ARCHIVE", "指定 Artifact 不是归档文件")
        if self._archive_depth(info, archive) > self.registry.archive_limits.max_depth:
            return make_error("ARCHIVE_DEPTH_LIMIT", "归档嵌套深度超过服务端预算")
        path = self.registry.get_artifact_path(params.case_id, archive.artifact_id)
        if path is None:
            return make_error("ARTIFACT_NOT_FOUND", "指定 Artifact 路径不可用")
        try:
            result = extract_selected_members(
                path,
                archive.artifact_id,
                params.member_ids,
                self.registry.archive_limits,
                force=params.force_rebuild,
            )
            if result.reset:
                self.registry.remove_archive_descendants(params.case_id, archive.artifact_id)
            registered = self.registry.register_extracted_artifacts(
                params.case_id,
                source_archive=archive,
                members=[(item.path, item.member_path) for item in result.members],
            )
        except ArchiveRejected as exc:
            if exc.code in {
                "ARCHIVE_INVALID", "ARCHIVE_SOURCE_CHANGED", "ARCHIVE_DESTINATION_CONFLICT",
                "ARCHIVE_FILE_LIMIT", "ARCHIVE_MEMBER_SIZE_MISMATCH",
            }:
                self.registry.remove_archive_descendants(params.case_id, archive.artifact_id)
            return make_error(exc.code, exc.message)
        except ValueError as exc:
            return make_error("CASE_TOO_LARGE", str(exc))
        except (OSError, UnicodeError) as exc:
            if self.registry.get_artifact_path(params.case_id, archive.artifact_id) is None:
                self.registry.remove_archive_descendants(params.case_id, archive.artifact_id)
            return make_error("FILE_WRITE_ERROR", str(exc), retryable=True)

        artifacts_by_path = {item.relative_path: item for item in registered}
        items = []
        for selected in result.members:
            virtual_path = f"{archive.relative_path}!/{selected.member_path}"
            artifact = artifacts_by_path.get(virtual_path)
            items.append({
                "member_id": selected.member_id,
                "member_path": selected.member_path,
                "artifact_id": artifact.artifact_id if artifact else None,
                "relative_path": virtual_path,
                "size_bytes": selected.size_bytes,
                "reused": selected.reused,
            })
        return make_success({
            "case_id": params.case_id,
            "archive_artifact_id": archive.artifact_id,
            "archive_relative_path": archive.relative_path,
            "reset": result.reset,
            "member_count": len(items),
            "members": items,
        })

    def probe_archive_members(self, **kwargs) -> ToolResult:
        """在不落盘、不展开整个归档的前提下，读取成员的有界内容样本。

        返回每个成员的内容画像：日志域、内容时间范围、boot 身份、
        诊断锚点、时钟校正记录和覆盖置信度。
        """
        params = ProbeArchiveMembersInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        _, info = entry
        archive = next((item for item in info.artifacts if item.artifact_id == params.artifact_id), None)
        if archive is None:
            return make_error("ARTIFACT_NOT_FOUND", "指定 Artifact 不存在")
        if archive.kind != "archive":
            return make_error("ARTIFACT_NOT_ARCHIVE", "指定 Artifact 不是归档文件")
        path = self.registry.get_artifact_path(params.case_id, archive.artifact_id)
        if path is None:
            return make_error("ARTIFACT_NOT_FOUND", "指定 Artifact 路径不可用")
        try:
            profiles = probe_archive_members(
                path,
                archive.artifact_id,
                params.member_ids,
                self.registry.archive_limits,
                max_bytes_per_member=params.max_bytes_per_member,
                max_total_bytes=self.registry.archive_limits.max_archive_bytes,
            )
        except ArchiveRejected as exc:
            return make_error(exc.code, exc.message)
        return make_success({
            "case_id": params.case_id,
            "artifact_id": archive.artifact_id,
            "relative_path": archive.relative_path,
            "incident_time_range": (
                {
                    "start": params.incident_time_range.start.isoformat(),
                    "end": params.incident_time_range.end.isoformat(),
                    "clock_domain": "reported_local_time",
                    "interpretation": (
                        "调查目标时间窗；必须与 profiles.content_time_ranges 分开核对，"
                        "不能据此声称日志已经覆盖事故。"
                    ),
                }
                if params.incident_time_range is not None else None
            ),
            "member_count": len(profiles),
            "profiles": profiles,
        })

    def build_index(self, **kwargs) -> ToolResult:
        """Build a reusable index for a selected, cumulatively growing Artifact set."""

        params = BuildIndexInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        _, info = entry
        artifact_map = {item.artifact_id: item for item in info.artifacts}
        unknown = [artifact_id for artifact_id in params.artifact_ids if artifact_id not in artifact_map]
        if unknown:
            return make_error("ARTIFACT_NOT_FOUND", f"指定 Artifact 不存在: {unknown[0]}")
        invalid = [artifact_id for artifact_id in params.artifact_ids if not artifact_map[artifact_id].readable_text]
        if invalid:
            return make_error("ARTIFACT_NOT_TEXT", f"指定 Artifact 不能作为文本索引: {invalid[0]}")
        unavailable = [
            artifact_id for artifact_id in params.artifact_ids
            if self.registry.get_artifact_path(params.case_id, artifact_id) is None
        ]
        if unavailable:
            return make_error("ARTIFACT_UNAVAILABLE", f"指定 Artifact 文件不可用: {unavailable[0]}")
        work_dir = self.registry.get_work_dir(params.case_id)
        if work_dir is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        index = LogIndex(work_dir / "index" / "logs.sqlite3", self.registry.index_limits)
        retained_ids = index.indexed_artifact_ids() & set(artifact_map)
        selected_ids = retained_ids | set(params.artifact_ids)
        # Rebuild is currently transactional rather than in-place incremental. Put
        # previously indexed artifacts first so an expansion that hits a budget cannot
        # evict already searchable evidence in favor of newly requested files.
        retained = [artifact for artifact in info.artifacts if artifact.artifact_id in retained_ids]
        additions = [
            artifact for artifact in info.artifacts
            if artifact.artifact_id in selected_ids and artifact.artifact_id not in retained_ids
        ]
        selected = retained + additions
        build = index.build(
            self.registry.artifact_paths(params.case_id, selected),
            force=params.force_rebuild,
        )
        self._indexes[params.case_id] = index
        indexed_ids = sorted(index.indexed_artifact_ids())
        return make_success({
            "case_id": params.case_id,
            "requested_artifact_ids": list(dict.fromkeys(params.artifact_ids)),
            "indexed_artifact_ids": indexed_ids,
            "artifact_count": build.artifact_count,
            "chunk_count": build.chunk_count,
            "indexed_bytes": build.indexed_bytes,
            "reused": build.reused,
            "truncated": build.truncated,
            "warnings": build.warnings,
        })

    def prepare_case(self, **kwargs) -> ToolResult:
        """安全展开支持的归档，并为文本 Artifact 建立可复用分块索引。"""

        params = PrepareCaseInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        _, info = entry
        work_dir = self.registry.get_work_dir(params.case_id)
        if work_dir is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")

        selected_ids = set(params.artifact_ids)
        archives = [
            artifact for artifact in info.artifacts
            if artifact.kind == "archive" and (not selected_ids or artifact.artifact_id in selected_ids)
        ]
        extraction_items: list[dict] = []
        skipped_archives: list[dict] = []
        generated_ids: set[str] = set()
        if params.extract_archives:
            extractor = ArchiveExtractor(self.registry.archive_limits)
            budget = ExtractionBudget(self.registry.archive_limits)
            queue = deque((artifact, 1) for artifact in archives)
            processed: set[str] = set()
            while queue:
                archive, depth = queue.popleft()
                if archive.artifact_id in processed:
                    continue
                processed.add(archive.artifact_id)
                path = self.registry.get_artifact_path(params.case_id, archive.artifact_id)
                if path is None:
                    continue
                if not is_supported_archive(path):
                    skipped_archives.append({
                        "artifact_id": archive.artifact_id,
                        "relative_path": archive.relative_path,
                        "reason": "ARCHIVE_FORMAT_UNSUPPORTED",
                    })
                    continue
                try:
                    result = extractor.extract(
                        path,
                        extraction_destination(path),
                        budget,
                        force=params.force_rebuild,
                    )
                    self.registry.remove_archive_descendants(params.case_id, archive.artifact_id)
                    registered = self.registry.register_extracted_artifacts(
                        params.case_id,
                        source_archive=archive,
                        members=[(item.path, item.member_path) for item in result.members],
                    )
                except ArchiveRejected as exc:
                    # A previously extracted version must never remain searchable after
                    # the current archive can no longer be validated or expanded.
                    self.registry.remove_archive_descendants(params.case_id, archive.artifact_id)
                    skipped_archives.append({
                        "artifact_id": archive.artifact_id,
                        "relative_path": archive.relative_path,
                        "reason": exc.code,
                        "message": exc.message,
                    })
                    continue
                except ValueError as exc:
                    return make_error("CASE_TOO_LARGE", str(exc))

                generated_ids.update(item.artifact_id for item in registered)
                extraction_items.append({
                    "artifact_id": archive.artifact_id,
                    "relative_path": archive.relative_path,
                    "depth": depth,
                    "member_count": len(registered),
                    "expanded_bytes": sum(item.size_bytes for item in registered),
                    "reused": result.reused,
                })
                nested = [item for item in registered if item.kind == "archive"]
                if depth < self.registry.archive_limits.max_depth:
                    queue.extend((item, depth + 1) for item in nested)
                else:
                    skipped_archives.extend({
                        "artifact_id": item.artifact_id,
                        "relative_path": item.relative_path,
                        "reason": "ARCHIVE_DEPTH_LIMIT",
                    } for item in nested)

        index_data = None
        if params.build_index:
            current_artifacts = self.registry.select_artifacts(params.case_id)
            if selected_ids:
                allowed_index_ids = selected_ids | generated_ids
                current_artifacts = [item for item in current_artifacts if item.artifact_id in allowed_index_ids]
            index = LogIndex(work_dir / "index" / "logs.sqlite3", self.registry.index_limits)
            build = index.build(
                self.registry.artifact_paths(params.case_id, current_artifacts),
                force=params.force_rebuild,
            )
            self._indexes[params.case_id] = index
            index_data = {
                "artifact_count": build.artifact_count,
                "chunk_count": build.chunk_count,
                "indexed_bytes": build.indexed_bytes,
                "reused": build.reused,
                "truncated": build.truncated,
                "warnings": build.warnings,
            }

        return make_success({
            "case_id": params.case_id,
            "extraction": {
                "archives": extraction_items,
                "skipped": skipped_archives,
                "expanded_file_count": len(generated_ids),
            },
            "index": index_data,
            "artifact_count": info.artifact_count,
        })

    def _evidence_from_pending(self, pending: dict) -> Evidence:
        """把流式搜索过程中暂存的前后文组装为 Evidence。"""

        lines = pending["before"] + [pending["line"]] + pending["after"]
        timestamp = extract_timestamp(pending["line"])
        return Evidence(
            evidence_id=stable_id(
                "evidence",
                f"{pending['artifact'].artifact_id}:{pending['line_number']}:{pending['query']}",
            ),
            artifact_id=pending["artifact"].artifact_id,
            artifact_name=pending["artifact"].name,
            relative_path=pending["artifact"].relative_path,
            line_start=max(1, pending["line_number"] - len(pending["before"])),
            line_end=pending["line_number"] + len(pending["after"]),
            content="\n".join(lines),
            timestamp_raw=timestamp.raw if timestamp else None,
            query=pending["query"],
        )

    def _evidence_from_index_match(self, artifact, match: IndexMatch, query: str) -> Evidence:
        pending = {
            "artifact": artifact,
            "line_number": match.line_number,
            "line": match.line,
            "before": match.before,
            "after": match.after,
            "query": query,
        }
        return self._evidence_from_pending(pending)

    def search_evidence(self, **kwargs) -> ToolResult:
        """在 Case 文本附件中进行有预算的流式字面量搜索。

        算法使用一个 before deque 保存前文，并用 pending 列表等待后文到齐，
        因而无需把整个日志读入内存。返回结果始终包含文件与行号，供 Agent
        在最终结论中引用。
        """

        params = SearchEvidenceInput.model_validate(kwargs)
        if self.registry.get_case(params.case_id) is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")

        artifacts = self.registry.select_artifacts(
            params.case_id,
            artifact_ids=params.artifact_ids,
            artifact_kinds=params.artifact_kinds,
        )
        # 只做字面量匹配，避免让不受信任的模型输入触发灾难性正则回溯。
        needle = params.query if params.case_sensitive else params.query.casefold()
        results: list[Evidence] = []
        warnings: list[str] = []
        scanned_bytes = 0
        truncated = False

        index = self._get_index(params.case_id)
        indexed_ids: set[str] = set()
        selected_indexed_ids: set[str] = set()
        candidate_chunks = 0
        if index is not None:
            indexed = index.search(
                params.query,
                artifact_ids=[item.artifact_id for item in artifacts],
                case_sensitive=params.case_sensitive,
                context_before=params.context_before,
                context_after=params.context_after,
                max_results=params.max_results,
            )
            indexed_ids = indexed.indexed_artifact_ids
            selected_indexed_ids = indexed_ids & {item.artifact_id for item in artifacts}
            candidate_chunks = indexed.candidate_chunks
            if indexed.truncated:
                warnings.append("索引查询达到运行时间预算，结果已截断")
                truncated = True
            artifact_map = {item.artifact_id: item for item in artifacts}
            results.extend(
                self._evidence_from_index_match(artifact_map[item.artifact_id], item, params.query)
                for item in indexed.matches
                if item.artifact_id in artifact_map
            )
            if len(results) >= params.max_results:
                truncated = True
        artifacts = [item for item in artifacts if item.artifact_id not in indexed_ids]

        for artifact in artifacts if len(results) < params.max_results else []:
            path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
            if path is None:
                continue
            if scanned_bytes >= MAX_SCAN_BYTES_PER_CALL:
                warnings.append("达到单次调用总扫描预算，部分文件未扫描")
                truncated = True
                break

            # before 保存最近 N 行；pending 保存已经命中但仍在收集 after 的证据。
            before: deque[str] = deque(maxlen=params.context_before)
            pending: list[dict] = []
            file_bytes = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    file_bytes += len(raw_line.encode("utf-8", errors="replace"))
                    if file_bytes > MAX_SCAN_BYTES_PER_FILE:
                        warnings.append(f"{artifact.relative_path} 超过单文件扫描预算，结果已截断")
                        truncated = True
                        break
                    line = raw_line.rstrip("\r\n")

                    # 当前行先作为之前命中的“后文”，再判断它本身是否命中新证据。
                    completed = []
                    for item in pending:
                        item["after"].append(line)
                        item["remaining"] -= 1
                        if item["remaining"] <= 0:
                            completed.append(item)
                    for item in completed:
                        pending.remove(item)
                        results.append(self._evidence_from_pending(item))

                    haystack = line if params.case_sensitive else line.casefold()
                    if needle in haystack and len(results) + len(pending) < params.max_results:
                        item = {
                            "artifact": artifact,
                            "line_number": line_number,
                            "line": line,
                            "before": list(before),
                            "after": [],
                            "remaining": params.context_after,
                            "query": params.query,
                        }
                        if params.context_after == 0:
                            results.append(self._evidence_from_pending(item))
                        else:
                            pending.append(item)

                    before.append(line)
                    if len(results) >= params.max_results and not pending:
                        truncated = True
                        break

            scanned_bytes += min(file_bytes, MAX_SCAN_BYTES_PER_FILE)
            for item in pending:
                results.append(self._evidence_from_pending(item))
            if len(results) >= params.max_results:
                results = results[: params.max_results]
                truncated = True
                break

        # 零匹配也是成功：它表示“没有发现该证据”，不是工具执行失败。
        return make_success({
            "case_id": params.case_id,
            "query": params.query,
            "items": [item.model_dump() for item in results],
            "match_count": len(results),
            "scanned_bytes": scanned_bytes,
            "truncated": truncated,
            "warnings": warnings,
            "search_mode": "hybrid" if selected_indexed_ids and artifacts else ("index" if selected_indexed_ids else "stream"),
            "candidate_chunks": candidate_chunks,
        })

    def extract_timeline(self, **kwargs) -> ToolResult:
        """从多个附件中提取锚点事件，并在各自时钟域内排序。

        anchors 必须由当前 Skill/Agent 显式提供。不同 clock_domain 的事件被
        保留，但不会被伪造为同一时间轴。
        """

        params = ExtractTimelineInput.model_validate(kwargs)
        if self.registry.get_case(params.case_id) is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        anchors = params.anchors
        anchors_folded = [anchor.casefold() for anchor in anchors]
        artifacts = self.registry.select_artifacts(params.case_id, artifact_ids=params.artifact_ids)
        events: list[TimelineEvent] = []
        scanned_bytes = 0
        truncated = False

        index = self._get_index(params.case_id)
        indexed_ids: set[str] = set()
        selected_indexed_ids: set[str] = set()
        if index is not None:
            indexed_ids = index.indexed_artifact_ids()
            artifact_map = {item.artifact_id: item for item in artifacts}
            selected_indexed_ids = indexed_ids & set(artifact_map)
            seen_events: set[tuple[str, int]] = set()
            for anchor in anchors:
                indexed = index.search(
                    anchor,
                    artifact_ids=list(artifact_map),
                    case_sensitive=False,
                    context_before=0,
                    context_after=0,
                    max_results=params.max_events,
                )
                if indexed.truncated:
                    truncated = True
                for match in indexed.matches:
                    key = (match.artifact_id, match.line_number)
                    if key in seen_events:
                        continue
                    timestamp = extract_timestamp(match.line, params.year_hint)
                    if timestamp is None:
                        continue
                    seen_events.add(key)
                    events.append(TimelineEvent(
                        event_id=stable_id("event", f"{match.artifact_id}:{match.line_number}:{match.line}"),
                        artifact_id=match.artifact_id,
                        relative_path=match.relative_path,
                        line_number=match.line_number,
                        clock_domain=timestamp.clock_domain,
                        timestamp_raw=timestamp.raw,
                        timestamp_normalized=timestamp.normalized,
                        relative_seconds=timestamp.relative_seconds,
                        component=infer_component(match.line, anchors),
                        event_type=anchor,
                        content=match.line,
                    ))
                    if len(events) >= params.max_events:
                        truncated = True
                        break
                if len(events) >= params.max_events:
                    break
        artifacts = [item for item in artifacts if item.artifact_id not in indexed_ids]

        for artifact in artifacts if len(events) < params.max_events else []:
            path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
            if path is None:
                continue
            file_bytes = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    file_bytes += len(raw_line.encode("utf-8", errors="replace"))
                    if file_bytes > MAX_SCAN_BYTES_PER_FILE:
                        truncated = True
                        break
                    line = raw_line.rstrip("\r\n")
                    lowered = line.casefold()
                    matched_anchor = next(
                        (anchor for anchor, folded in zip(anchors, anchors_folded) if folded in lowered),
                        None,
                    )
                    if matched_anchor is None:
                        continue
                    timestamp = extract_timestamp(line, params.year_hint)
                    if timestamp is None:
                        continue
                    events.append(TimelineEvent(
                        event_id=stable_id("event", f"{artifact.artifact_id}:{line_number}:{line}"),
                        artifact_id=artifact.artifact_id,
                        relative_path=artifact.relative_path,
                        line_number=line_number,
                        clock_domain=timestamp.clock_domain,
                        timestamp_raw=timestamp.raw,
                        timestamp_normalized=timestamp.normalized,
                        relative_seconds=timestamp.relative_seconds,
                        component=infer_component(line, anchors),
                        event_type=matched_anchor,
                        content=line,
                    ))
                    if len(events) >= params.max_events:
                        truncated = True
                        break
            scanned_bytes += min(file_bytes, MAX_SCAN_BYTES_PER_FILE)
            if len(events) >= params.max_events or scanned_bytes >= MAX_SCAN_BYTES_PER_CALL:
                truncated = True
                break

        # 先排列可归一化的 wall/android 时间，再排列 kernel 相对时间。
        # 这只是稳定展示顺序，不代表两个时钟域已经完成对齐。
        def sort_key(event: TimelineEvent):
            if event.timestamp_normalized:
                return (0, event.timestamp_normalized, 0.0, event.relative_path, event.line_number)
            if event.relative_seconds is not None:
                return (1, "", event.relative_seconds, event.relative_path, event.line_number)
            return (2, "", 0.0, event.relative_path, event.line_number)

        events.sort(key=sort_key)
        return make_success({
            "case_id": params.case_id,
            "anchors": anchors,
            "events": [event.model_dump() for event in events],
            "event_count": len(events),
            "clock_domains": sorted({event.clock_domain for event in events}),
            "truncated": truncated,
            "search_mode": "hybrid" if selected_indexed_ids and artifacts else ("index" if selected_indexed_ids else "stream"),
        })

    def _make_line_evidence(self, artifact, line_number: int, content: str) -> Evidence:
        """为确定性诊断结果创建可追溯的 Evidence。"""

        timestamp = extract_timestamp(content)
        return Evidence(
            evidence_id=stable_id("evidence", f"{artifact.artifact_id}:{line_number}:{content}"),
            artifact_id=artifact.artifact_id,
            artifact_name=artifact.name,
            relative_path=artifact.relative_path,
            line_start=line_number,
            line_end=line_number,
            content=content,
            timestamp_raw=timestamp.raw if timestamp else None,
        )

    def parse_diagnostics(self, **kwargs) -> ToolResult:
        """使用确定性规则提取常见诊断信号。

        这里输出 Finding 而不是 Root Cause：看到 Fatal/AVC/Call Trace 是事实，
        这些事实是否导致当前 Bug 仍需 Agent 结合时间线、代码和历史案例判断。
        """

        params = ParseDiagnosticsInput.model_validate(kwargs)
        if self.registry.get_case(params.case_id) is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        wanted = set(params.diagnostic_types)
        artifacts = self.registry.select_artifacts(params.case_id, artifact_ids=params.artifact_ids)
        findings: list[DiagnosticFinding] = []
        truncated = False
        index = self._get_index(params.case_id)
        indexed_ids: set[str] = set()
        selected_indexed_ids: set[str] = set()
        if index is not None:
            indexed_ids = index.indexed_artifact_ids()
            artifact_map = {item.artifact_id: item for item in artifacts}
            selected_indexed_ids = indexed_ids & set(artifact_map)
            marker_queries = {
                "avc": ["avc: denied"],
                "fatal": ["FATAL EXCEPTION", "Fatal signal"],
                "anr": ["ANR in"],
                "kernel_stack": ["Call Trace:"],
                "watchdog": [
                    "WATCHDOG KILLING SYSTEM PROCESS", "watchdog bite", "watchdog bark",
                    "watchdog detected", "watchdog timeout",
                ],
                "kernel_panic": [
                    "Kernel panic - not syncing", "Unable to handle kernel",
                    "Fatal exception in interrupt",
                ],
                "hung_task": [
                    "blocked for more than", "soft lockup", "hard lockup", "RCU stall",
                ],
                "lmk_oom": [
                    "lowmemorykiller", "lmkd", "Out of memory: Kill process",
                    "oom_reaper", "Memory cgroup out of memory",
                ],
                "binder_stall": [
                    "binder thread pool starved", "binder thread pool starvation",
                    "binder transaction failed", "binder_alloc_buf", "undelivered transaction",
                ],
            }
            seen_findings: set[tuple[str, int, str]] = set()
            for diagnostic_type in params.diagnostic_types:
                for marker in marker_queries[diagnostic_type]:
                    indexed = index.search(
                        marker,
                        artifact_ids=list(artifact_map),
                        case_sensitive=False,
                        context_before=0,
                        context_after=63 if diagnostic_type == "kernel_stack" else 0,
                        max_results=params.max_findings,
                    )
                    if indexed.truncated:
                        truncated = True
                    for match in indexed.matches:
                        diagnostic = classify_diagnostic_line(match.line, wanted)
                        if diagnostic is None:
                            continue
                        key = (match.artifact_id, match.line_number, diagnostic.diagnostic_type)
                        if key in seen_findings:
                            continue
                        seen_findings.add(key)
                        artifact = artifact_map.get(match.artifact_id)
                        if artifact is None:
                            continue
                        content = match.line
                        end_line = match.line_number
                        attributes = diagnostic.attributes
                        if diagnostic.diagnostic_type == "kernel_stack":
                            stack_lines = [match.line]
                            for offset, next_line in enumerate(match.after, 1):
                                if not next_line.strip() or len(stack_lines) >= 64:
                                    break
                                stack_lines.append(next_line)
                                end_line = match.line_number + offset
                                if "</TASK>" in next_line:
                                    break
                            content = "\n".join(stack_lines)
                            attributes = {"frame_lines": max(0, len(stack_lines) - 1)}
                        evidence = self._make_line_evidence(artifact, match.line_number, content)
                        evidence.line_end = end_line
                        findings.append(DiagnosticFinding(
                            finding_id=stable_id("finding", evidence.evidence_id),
                            diagnostic_type=diagnostic.diagnostic_type,
                            severity=diagnostic.severity,
                            summary=diagnostic.summary,
                            evidence=evidence,
                            attributes=attributes,
                        ))
                        if len(findings) >= params.max_findings:
                            truncated = True
                            break
                    if len(findings) >= params.max_findings:
                        break
                if len(findings) >= params.max_findings:
                    break
        artifacts = [item for item in artifacts if item.artifact_id not in indexed_ids]

        for artifact in artifacts if len(findings) < params.max_findings else []:
            path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
            if path is None:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = iter(enumerate(handle, 1))
                for line_number, raw_line in lines:
                    line = raw_line.rstrip("\r\n")
                    diagnostic = classify_diagnostic_line(line, wanted)

                    if diagnostic and diagnostic.diagnostic_type == "kernel_stack":
                        # Call Trace 是多行结构；在同一个流中向后收集，限制 64 行
                        # 防止缺少结束标记的损坏日志吞掉整个文件。
                        stack_lines = [line]
                        end_line = line_number
                        for next_line_number, next_raw in lines:
                            next_line = next_raw.rstrip("\r\n")
                            if not next_line.strip() or len(stack_lines) >= 64:
                                break
                            stack_lines.append(next_line)
                            end_line = next_line_number
                            if "</TASK>" in next_line:
                                break
                        evidence = self._make_line_evidence(artifact, line_number, "\n".join(stack_lines))
                        evidence.line_end = end_line
                        findings.append(DiagnosticFinding(
                            finding_id=stable_id("finding", evidence.evidence_id),
                            diagnostic_type=diagnostic.diagnostic_type,
                            severity=diagnostic.severity,
                            summary=diagnostic.summary,
                            evidence=evidence,
                            attributes={"frame_lines": max(0, len(stack_lines) - 1)},
                        ))
                        if len(findings) >= params.max_findings:
                            truncated = True
                            break
                        continue

                    if diagnostic:
                        evidence = self._make_line_evidence(artifact, line_number, line)
                        findings.append(DiagnosticFinding(
                            finding_id=stable_id("finding", evidence.evidence_id),
                            diagnostic_type=diagnostic.diagnostic_type,
                            severity=diagnostic.severity,
                            summary=diagnostic.summary,
                            evidence=evidence,
                            attributes=diagnostic.attributes,
                        ))
                        if len(findings) >= params.max_findings:
                            truncated = True
                            break
            if len(findings) >= params.max_findings:
                break

        return make_success({
            "case_id": params.case_id,
            "findings": [finding.model_dump() for finding in findings],
            "finding_count": len(findings),
            "truncated": truncated,
            "search_mode": "hybrid" if selected_indexed_ids and artifacts else ("index" if selected_indexed_ids else "stream"),
        })

    # ------------------------------------------------------------------
    # get_case_comment —— 按 comment_id 精读 Jira 评论原文
    # ------------------------------------------------------------------

    def get_case_comment(self, **kwargs) -> ToolResult:
        """从已注册 Case 的 issue.json 读取一条评论，并按字符分页返回。"""
        params = GetCaseCommentInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        case_root = entry[0]
        issue_json_path = case_root / "issue.json"
        manifest_path = case_root / "collection-manifest.json"
        if not issue_json_path.is_file():
            return make_error("ISSUE_JSON_NOT_FOUND", "Case 根目录下不存在 issue.json")
        if not manifest_path.is_file():
            return make_error(
                "JIRA_CASE_REEXPORT_REQUIRED",
                "Case 缺少评论完整性证明，请重新导出 Jira Case",
            )
        try:
            resolved_root = case_root.resolve(strict=True)
            resolved_issue = issue_json_path.resolve(strict=True)
            resolved_manifest = manifest_path.resolve(strict=True)
            resolved_issue.relative_to(resolved_root)
            resolved_manifest.relative_to(resolved_root)
            manifest = json.loads(resolved_manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return make_error("JIRA_CASE_MANIFEST_INVALID", "Manifest 无法安全读取或解析")
        if not isinstance(manifest, dict):
            return make_error("JIRA_CASE_MANIFEST_INVALID", "Manifest 必须是 JSON 对象")
        if manifest.get("source") != "jira" or manifest.get("schema_version") != 2:
            return make_error(
                "JIRA_CASE_REEXPORT_REQUIRED",
                "Case 缺少受支持的评论完整性证明，请重新导出 Jira Case",
            )
        root_context = manifest.get("root_issue_context")
        if not isinstance(root_context, dict) or root_context.get("version") != 1:
            return make_error(
                "JIRA_CASE_REEXPORT_REQUIRED",
                "Case 缺少受支持的根 Issue 上下文证明，请重新导出 Jira Case",
            )
        collection = root_context.get("comments")
        total = collection.get("total") if isinstance(collection, dict) else None
        collected = collection.get("collected") if isinstance(collection, dict) else None
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or not isinstance(collected, int)
            or isinstance(collected, bool)
            or total < 0
            or collected < 0
            or collection.get("complete") is not True
            or collection.get("truncated") is not False
            or collected != total
        ):
            return make_error(
                "JIRA_COMMENTS_INCOMPLETE",
                f"Jira 评论未完整收集 (total={total}, collected={collected})",
            )
        try:
            issue_bytes = resolved_issue.read_bytes()
            expected_hash = manifest.get("issue_json_sha256")
            if (
                not isinstance(expected_hash, str)
                or len(expected_hash) != 64
                or any(char not in "0123456789abcdef" for char in expected_hash.lower())
            ):
                return make_error("JIRA_CASE_MANIFEST_INVALID", "Manifest 缺少 issue.json 哈希")
            if hashlib.sha256(issue_bytes).hexdigest() != expected_hash:
                return make_error("JIRA_CASE_CONTEXT_TAMPERED", "issue.json 与 Manifest 哈希不一致")
            issue_data = json.loads(issue_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return make_error("ISSUE_JSON_INVALID", "issue.json 无法安全读取或解析")
        actual_key = str(issue_data.get("key") or "").upper() if isinstance(issue_data, dict) else ""
        expected_key = str(manifest.get("root_issue") or "").upper()
        raw_comments = issue_data.get("comments") if isinstance(issue_data, dict) else None
        if not isinstance(raw_comments, list):
            return make_error("ISSUE_JSON_NO_COMMENTS", "issue.json 中未找到 comments 数组")
        comment_ids = [
            str(item.get("comment_id") or "") if isinstance(item, dict) else ""
            for item in raw_comments
        ]
        if (
            actual_key != expected_key
            or len(raw_comments) != collected
            or any(not comment_id for comment_id in comment_ids)
            or len(set(comment_ids)) != len(comment_ids)
        ):
            return make_error(
                "JIRA_CASE_CONTEXT_MISMATCH",
                "Issue Key、评论数或 comment_id 与 Manifest 不一致",
            )
        matched = next((
            item for item in raw_comments
            if isinstance(item, dict) and str(item.get("comment_id") or item.get("id") or "") == params.comment_id
        ), None)
        if matched is None:
            return make_error("COMMENT_NOT_FOUND", f"未找到 comment_id={params.comment_id} 的评论")
        body = str(matched.get("body") or matched.get("comment") or "")
        fragment = body[params.offset:params.offset + params.limit]
        next_offset = params.offset + len(fragment)
        has_more = next_offset < len(body)
        author = matched.get("author")
        if isinstance(author, dict):
            author = author.get("displayName") or author.get("display_name") or author.get("name")
        return make_success({
            "case_id": params.case_id,
            "comment_id": params.comment_id,
            "author": author,
            "created_at": matched.get("created_at") or matched.get("created"),
            "updated_at": matched.get("updated_at") or matched.get("updated"),
            "body": fragment,
            "offset": params.offset,
            "returned_chars": len(fragment),
            "total_chars": len(body),
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
        })
