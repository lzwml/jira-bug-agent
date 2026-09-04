from __future__ import annotations

import gzip
import io
from pathlib import Path
import tarfile
import zipfile

from log_analyzer.archive_manager import ArchiveLimits
from log_analyzer.case_registry import CaseRegistry
from log_analyzer.log_index import IndexLimits
from log_analyzer.service import LogAnalyzerService


def _service(tmp_path: Path, *, archive_limits: ArchiveLimits | None = None):
    case_dir = tmp_path / "CASE-1"
    case_dir.mkdir()
    registry = CaseRegistry(
        [str(tmp_path)],
        work_root=str(tmp_path / "work"),
        archive_limits=archive_limits or ArchiveLimits(
            max_archive_bytes=20 * 1024 * 1024,
            max_members=100,
            max_member_bytes=20 * 1024 * 1024,
            max_expanded_bytes=40 * 1024 * 1024,
            max_compression_ratio=1000,
            max_depth=2,
        ),
        index_limits=IndexLimits(
            max_input_bytes=40 * 1024 * 1024,
            max_storage_bytes=80 * 1024 * 1024,
            chunk_bytes=64 * 1024,
        ),
    )
    return case_dir, registry, LogAnalyzerService(registry)


def _open(service: LogAnalyzerService, case_dir: Path) -> str:
    opened = service.open_case(case_path=str(case_dir))
    assert opened.success
    return opened.data["case"]["case_id"]


def test_prepare_extracts_nested_archives_and_searches_index(tmp_path):
    case_dir, _, service = _service(tmp_path)
    inner_content = b"first line\n08-27 10:00:00.000 E Demo: nested failure marker\n"
    inner_gzip = gzip.compress(inner_content)
    with zipfile.ZipFile(case_dir / "android.zip", "w") as archive:
        archive.writestr("logs/inner.log.gz", inner_gzip)
        archive.writestr("logs/readme.txt", "ordinary text\n")

    case_id = _open(service, case_dir)
    prepared = service.prepare_case(case_id=case_id)

    assert prepared.success
    assert len(prepared.data["extraction"]["archives"]) == 2
    assert prepared.data["extraction"]["expanded_file_count"] == 3
    assert prepared.data["index"]["artifact_count"] == 2

    searched = service.search_evidence(case_id=case_id, query="nested failure marker")
    assert searched.success
    assert searched.data["search_mode"] == "index"
    assert searched.data["match_count"] == 1
    assert "android.zip!/logs/inner.log.gz!/inner.log" in searched.data["items"][0]["relative_path"]

    forced = service.prepare_case(case_id=case_id, force_rebuild=True)
    assert forced.success
    assert len(forced.data["extraction"]["archives"]) == 2
    assert all(item["reused"] is False for item in forced.data["extraction"]["archives"])


def test_agent_flow_inspects_extracts_and_indexes_only_selected_member(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "android.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("logs/main.log", "14:32 SurfaceFlinger selected evidence\n")
        archive.writestr("logs/radio.log", "unrelated radio evidence\n")
    case_id = _open(service, case_dir)
    archive_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]

    inventory = service.inspect_archive(case_id=case_id, artifact_id=archive_id)

    assert inventory.success
    assert not (case_dir / "android").exists()
    by_path = {item["member_path"]: item for item in inventory.data["members"]}
    extracted = service.extract_archive_members(
        case_id=case_id,
        artifact_id=archive_id,
        member_ids=[by_path["logs/main.log"]["member_id"]],
    )
    selected_artifact_id = extracted.data["members"][0]["artifact_id"]
    indexed = service.build_index(case_id=case_id, artifact_ids=[selected_artifact_id])

    assert extracted.success
    assert (case_dir / "android" / "logs" / "main.log").is_file()
    assert not (case_dir / "android" / "logs" / "radio.log").exists()
    assert indexed.success
    assert indexed.data["indexed_artifact_ids"] == [selected_artifact_id]
    assert service.search_evidence(case_id=case_id, query="selected evidence").data["match_count"] == 1
    assert service.search_evidence(case_id=case_id, query="radio evidence").data["match_count"] == 0


def test_new_analysis_reuses_extracted_member_without_extract_call(tmp_path):
    case_dir, registry, first_service = _service(tmp_path)
    with zipfile.ZipFile(case_dir / "android.zip", "w") as archive:
        archive.writestr("logs/main.log", "SurfaceFlinger reusable evidence\n")
        archive.writestr("logs/radio.log", "not selected\n")
    first_case_id = _open(first_service, case_dir)
    archive_id = first_service.inspect_case(case_id=first_case_id).data["artifacts"][0]["artifact_id"]
    first_inventory = first_service.inspect_archive(
        case_id=first_case_id, artifact_id=archive_id,
    )
    main_member = next(
        item for item in first_inventory.data["members"]
        if item["member_path"] == "logs/main.log"
    )
    extracted = first_service.extract_archive_members(
        case_id=first_case_id,
        artifact_id=archive_id,
        member_ids=[main_member["member_id"]],
    )
    assert extracted.success

    # 模拟续分析启动新的 MCP 进程：内存注册表为空，只保留 Case 磁盘状态。
    second_registry = CaseRegistry(
        [str(tmp_path)],
        work_root=str(tmp_path / "work"),
        archive_limits=registry.archive_limits,
        index_limits=registry.index_limits,
    )
    second_service = LogAnalyzerService(second_registry)
    second_case_id = _open(second_service, case_dir)
    second_archive_id = second_service.inspect_case(
        case_id=second_case_id,
    ).data["artifacts"][0]["artifact_id"]

    reused_inventory = second_service.inspect_archive(
        case_id=second_case_id, artifact_id=second_archive_id,
    )
    reused_main = next(
        item for item in reused_inventory.data["members"]
        if item["member_path"] == "logs/main.log"
    )

    assert reused_inventory.success
    assert reused_inventory.data["extraction"] == {
        "reusable": True,
        "extracted_member_count": 1,
    }
    assert reused_main["extracted"] is True
    assert reused_main["artifact_id"] is not None
    assert reused_main["relative_path"] == "android.zip!/logs/main.log"
    indexed = second_service.build_index(
        case_id=second_case_id, artifact_ids=[reused_main["artifact_id"]],
    )
    assert indexed.success
    assert second_service.search_evidence(
        case_id=second_case_id, query="reusable evidence",
    ).data["match_count"] == 1


def test_archive_inventory_supports_bounded_pagination(tmp_path):
    case_dir, _, service = _service(tmp_path)
    with zipfile.ZipFile(case_dir / "many.zip", "w") as archive:
        for number in range(6):
            archive.writestr(f"logs/{number}.log", str(number))
    case_id = _open(service, case_dir)
    archive_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]

    first = service.inspect_archive(
        case_id=case_id, artifact_id=archive_id, member_offset=0, max_members=2,
    )
    second = service.inspect_archive(
        case_id=case_id, artifact_id=archive_id,
        member_offset=first.data["next_offset"],
        source_sha256=first.data["source_fingerprint"]["sha256"],
        max_members=2,
    )

    assert [item["member_path"] for item in first.data["members"]] == ["logs/0.log", "logs/1.log"]
    assert [item["member_path"] for item in second.data["members"]] == ["logs/2.log", "logs/3.log"]
    assert first.data["truncated"] is True
    assert second.data["next_offset"] == 4


def test_inspect_archive_filters_generic_path_times_and_neighbors_before_pagination(tmp_path):
    case_dir, _, service = _service(tmp_path)
    with zipfile.ZipFile(case_dir / "aplogs.zip", "w") as archive:
        # Deliberately reverse the ZIP order; selection must use parsed start time.
        for minute in reversed(range(44)):
            archive.writestr(f"APLog_2026_0831_06{minute:02d}00__{minute}.tar.gz", "payload")
        archive.writestr("unrelated.txt", "not an APLog")
    case_id = _open(service, case_dir)
    archive_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]

    result = service.inspect_archive(
        case_id=case_id,
        artifact_id=archive_id,
        time_range={"start": "2026-08-31T06:15:00", "end": "2026-08-31T06:30:00"},
        time_neighbor_count=1,
        max_members=100,
    )

    assert result.success
    assert result.data["selection"] == {
        "mode": "path_timestamp",
        "requested_start": "2026-08-31T06:15:00",
        "requested_end": "2026-08-31T06:30:00",
        "path_prefix": None,
        "time_neighbor_count": 1,
        "matched_member_count": 18,
    }
    assert len(result.data["members"]) == 18
    assert [item["path_time_relation"] for item in result.data["members"]] == [
        "predecessor", *(["in_range"] * 16), "successor",
    ]
    assert result.data["members"][0]["path_timestamp"] == "2026-08-31T06:14:00"
    assert result.data["members"][-1]["path_timestamp"] == "2026-08-31T06:31:00"
    assert all(item["member_id"] for item in result.data["members"])
    assert "unrelated.txt" not in {item["member_path"] for item in result.data["members"]}


def test_archive_inventory_rejects_stale_pagination_fingerprint(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "paged.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("logs/0.log", "old zero")
        archive.writestr("logs/1.log", "old one")
        archive.writestr("logs/2.log", "old two")
    case_id = _open(service, case_dir)
    archive_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]
    first = service.inspect_archive(
        case_id=case_id, artifact_id=archive_id, member_offset=0, max_members=1,
    )

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("logs/0.log", "new zero with a different size")
        archive.writestr("logs/1.log", "new one with a different size")
        archive.writestr("logs/2.log", "new two with a different size")
    stale_page = service.inspect_archive(
        case_id=case_id,
        artifact_id=archive_id,
        member_offset=first.data["next_offset"],
        source_sha256=first.data["source_fingerprint"]["sha256"],
        max_members=1,
    )

    assert not stale_page.success
    assert stale_page.error_code == "ARCHIVE_SOURCE_CHANGED"


def test_inspect_changed_archive_unregisters_old_derived_evidence(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.log", "old selectively extracted evidence\n")
    case_id = _open(service, case_dir)
    archive_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]
    inventory = service.inspect_archive(case_id=case_id, artifact_id=archive_id)
    extracted = service.extract_archive_members(
        case_id=case_id,
        artifact_id=archive_id,
        member_ids=[inventory.data["members"][0]["member_id"]],
    )
    derived_id = extracted.data["members"][0]["artifact_id"]
    assert service.build_index(case_id=case_id, artifact_ids=[derived_id]).success
    assert service.search_evidence(
        case_id=case_id, query="selectively extracted evidence",
    ).data["match_count"] == 1

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.log", "replacement archive evidence with a different size\n")
    inspected = service.inspect_archive(case_id=case_id, artifact_id=archive_id)
    case_after_inspect = service.inspect_case(case_id=case_id)
    searched = service.search_evidence(
        case_id=case_id, query="selectively extracted evidence",
    )

    assert inspected.success
    assert case_after_inspect.data["preparation"]["extracted_artifact_count"] == 0
    assert all(
        item["artifact_id"] != derived_id for item in case_after_inspect.data["artifacts"]
    )
    assert searched.success
    assert searched.data["match_count"] == 0


def test_build_index_expansion_preserves_previous_artifacts(tmp_path):
    case_dir, _, service = _service(tmp_path)
    (case_dir / "one.log").write_text("first marker\n", encoding="utf-8")
    (case_dir / "two.log").write_text("second marker\n", encoding="utf-8")
    case_id = _open(service, case_dir)
    artifacts = service.inspect_case(case_id=case_id).data["artifacts"]
    by_name = {item["name"]: item["artifact_id"] for item in artifacts}

    first = service.build_index(case_id=case_id, artifact_ids=[by_name["one.log"]])
    second = service.build_index(case_id=case_id, artifact_ids=[by_name["two.log"]])

    assert first.success and second.success
    assert set(second.data["indexed_artifact_ids"]) == {by_name["one.log"], by_name["two.log"]}
    assert service.search_evidence(case_id=case_id, query="first marker").data["match_count"] == 1
    assert service.search_evidence(case_id=case_id, query="second marker").data["match_count"] == 1


def test_build_index_budget_keeps_previously_indexed_artifact(tmp_path):
    case_dir = tmp_path / "CASE-INDEX-BUDGET"
    case_dir.mkdir()
    (case_dir / "one.log").write_text("1111", encoding="utf-8")
    (case_dir / "two.log").write_text("2222", encoding="utf-8")
    registry = CaseRegistry(
        [str(tmp_path)],
        work_root=str(tmp_path / "budget-work"),
        index_limits=IndexLimits(
            max_input_bytes=5,
            max_storage_bytes=10 * 1024 * 1024,
            chunk_bytes=64 * 1024,
        ),
    )
    service = LogAnalyzerService(registry)
    case_id = _open(service, case_dir)
    artifacts = service.inspect_case(case_id=case_id).data["artifacts"]
    by_name = {item["name"]: item["artifact_id"] for item in artifacts}
    assert service.build_index(case_id=case_id, artifact_ids=[by_name["one.log"]]).success

    expanded = service.build_index(case_id=case_id, artifact_ids=[by_name["two.log"]])

    assert expanded.success
    assert expanded.data["indexed_artifact_ids"] == [by_name["one.log"]]
    assert expanded.data["truncated"] is True


def test_build_index_reports_registered_file_that_disappeared(tmp_path):
    case_dir, _, service = _service(tmp_path)
    log_path = case_dir / "gone.log"
    log_path.write_text("temporary\n", encoding="utf-8")
    case_id = _open(service, case_dir)
    artifact_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]
    log_path.unlink()

    result = service.build_index(case_id=case_id, artifact_ids=[artifact_id])

    assert not result.success
    assert result.error_code == "ARTIFACT_UNAVAILABLE"


def test_registered_archive_replaced_by_external_symlink_is_rejected(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.log", "inside\n")
    outside = tmp_path / "outside.zip"
    with zipfile.ZipFile(outside, "w") as archive:
        archive.writestr("main.log", "outside\n")
    case_id = _open(service, case_dir)
    artifact_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]
    archive_path.unlink()
    try:
        archive_path.symlink_to(outside)
    except OSError:
        pytest.skip("当前 Windows 环境不允许创建测试符号链接")

    result = service.inspect_archive(case_id=case_id, artifact_id=artifact_id)

    assert not result.success
    assert result.error_code == "ARTIFACT_NOT_FOUND"
    assert not (tmp_path / "outside").exists()


def test_selective_nested_archive_obeys_depth_limit(tmp_path):
    limits = ArchiveLimits(
        max_archive_bytes=20 * 1024 * 1024,
        max_members=100,
        max_member_bytes=20 * 1024 * 1024,
        max_expanded_bytes=40 * 1024 * 1024,
        max_compression_ratio=1000,
        max_depth=1,
    )
    case_dir, _, service = _service(tmp_path, archive_limits=limits)
    with zipfile.ZipFile(case_dir / "outer.zip", "w") as archive:
        archive.writestr("inner.log.gz", gzip.compress(b"nested\n"))
    case_id = _open(service, case_dir)
    outer_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]
    outer_inventory = service.inspect_archive(case_id=case_id, artifact_id=outer_id)
    extracted = service.extract_archive_members(
        case_id=case_id,
        artifact_id=outer_id,
        member_ids=[outer_inventory.data["members"][0]["member_id"]],
    )
    nested_id = extracted.data["members"][0]["artifact_id"]

    nested_inventory = service.inspect_archive(case_id=case_id, artifact_id=nested_id)

    assert not nested_inventory.success
    assert nested_inventory.error_code == "ARCHIVE_DEPTH_LIMIT"


def test_failed_selection_expansion_preserves_previous_evidence(tmp_path):
    limits = ArchiveLimits(
        max_archive_bytes=1024 * 1024,
        max_members=10,
        max_member_bytes=10,
        max_expanded_bytes=6,
        max_compression_ratio=1000,
        max_depth=2,
    )
    case_dir, _, service = _service(tmp_path, archive_limits=limits)
    with zipfile.ZipFile(case_dir / "budget.zip", "w") as archive:
        archive.writestr("one.log", "1111")
        archive.writestr("two.log", "2222")
    case_id = _open(service, case_dir)
    archive_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]
    inventory = service.inspect_archive(case_id=case_id, artifact_id=archive_id)
    by_path = {item["member_path"]: item for item in inventory.data["members"]}
    first = service.extract_archive_members(
        case_id=case_id,
        artifact_id=archive_id,
        member_ids=[by_path["one.log"]["member_id"]],
    )
    assert first.success

    rejected = service.extract_archive_members(
        case_id=case_id,
        artifact_id=archive_id,
        member_ids=[by_path["two.log"]["member_id"]],
    )

    assert not rejected.success
    assert rejected.error_code == "ARCHIVE_EXPANDED_LIMIT"
    assert service.search_evidence(case_id=case_id, query="1111").data["match_count"] == 1
    assert service.inspect_case(case_id=case_id).data["preparation"]["extracted_artifact_count"] == 1


def test_selective_nested_archive_requires_explicit_second_step(tmp_path):
    case_dir, _, service = _service(tmp_path)
    inner = gzip.compress(b"nested explicit evidence\n")
    with zipfile.ZipFile(case_dir / "outer.zip", "w") as archive:
        archive.writestr("logs/inner.log.gz", inner)
        archive.writestr("logs/unused.log", "must stay packed\n")
    case_id = _open(service, case_dir)
    outer_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]
    outer_inventory = service.inspect_archive(case_id=case_id, artifact_id=outer_id)
    inner_member = next(item for item in outer_inventory.data["members"] if item["is_archive"])

    outer_extract = service.extract_archive_members(
        case_id=case_id, artifact_id=outer_id, member_ids=[inner_member["member_id"]],
    )
    inner_artifact_id = outer_extract.data["members"][0]["artifact_id"]

    assert outer_extract.success
    assert not (case_dir / "outer" / "logs" / "inner.log").exists()
    inner_inventory = service.inspect_archive(case_id=case_id, artifact_id=inner_artifact_id)
    inner_extract = service.extract_archive_members(
        case_id=case_id,
        artifact_id=inner_artifact_id,
        member_ids=[inner_inventory.data["members"][0]["member_id"]],
    )
    assert inner_extract.success
    assert inner_extract.data["members"][0]["relative_path"].endswith(
        "outer.zip!/logs/inner.log.gz!/inner.log"
    )
    assert not (case_dir / "outer" / "logs" / "unused.log").exists()


def test_full_and_selective_extraction_modes_are_compatible(tmp_path):
    case_dir, _, service = _service(tmp_path)
    with zipfile.ZipFile(case_dir / "logs.zip", "w") as archive:
        archive.writestr("one.log", "one\n")
        archive.writestr("two.log", "two\n")
    case_id = _open(service, case_dir)
    archive_id = service.inspect_case(case_id=case_id).data["artifacts"][0]["artifact_id"]

    assert service.prepare_case(case_id=case_id, build_index=False).success
    inventory = service.inspect_archive(case_id=case_id, artifact_id=archive_id)
    selected = service.extract_archive_members(
        case_id=case_id,
        artifact_id=archive_id,
        member_ids=[inventory.data["members"][0]["member_id"]],
    )
    assert selected.success
    assert selected.data["members"][0]["reused"] is True

    selective_case = tmp_path / "CASE-SELECTIVE-FIRST"
    selective_case.mkdir()
    with zipfile.ZipFile(selective_case / "logs.zip", "w") as archive:
        archive.writestr("one.log", "one\n")
        archive.writestr("two.log", "two\n")
    second_registry = CaseRegistry([str(tmp_path)], work_root=str(tmp_path / "second-work"))
    second_service = LogAnalyzerService(second_registry)
    second_case_id = _open(second_service, selective_case)
    second_archive_id = second_service.inspect_case(case_id=second_case_id).data["artifacts"][0]["artifact_id"]
    second_inventory = second_service.inspect_archive(case_id=second_case_id, artifact_id=second_archive_id)
    assert second_service.extract_archive_members(
        case_id=second_case_id,
        artifact_id=second_archive_id,
        member_ids=[second_inventory.data["members"][0]["member_id"]],
    ).success
    full = second_service.prepare_case(case_id=second_case_id, build_index=False)
    assert full.success
    assert (selective_case / "logs" / "two.log").is_file()


def test_prepare_reuses_extraction_and_index(tmp_path):
    case_dir, _, service = _service(tmp_path)
    with zipfile.ZipFile(case_dir / "logs.zip", "w") as archive:
        archive.writestr("main_log.txt", "FATAL EXCEPTION: main\n")
    case_id = _open(service, case_dir)

    first = service.prepare_case(case_id=case_id)
    second = service.prepare_case(case_id=case_id)

    assert first.success and second.success
    assert second.data["extraction"]["archives"][0]["reused"] is True
    assert second.data["index"]["reused"] is True

    forced = service.prepare_case(case_id=case_id, force_rebuild=True)
    assert forced.data["extraction"]["archives"][0]["reused"] is False
    assert forced.data["index"]["reused"] is False


def test_prepare_writes_next_to_archive_and_reopen_ignores_generated_files(tmp_path):
    case_dir, registry, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("nested/main.log", "visible evidence\n")
    case_id = _open(service, case_dir)

    prepared = service.prepare_case(case_id=case_id)

    assert prepared.success
    assert (case_dir / "logs" / "nested" / "main.log").is_file()
    reopened = registry.open_case(str(case_dir))
    assert reopened.success
    assert reopened.data["case"]["artifact_count"] == 1
    assert reopened.data["case"]["artifacts"][0]["relative_path"] == "logs.zip"


def test_prepare_does_not_replace_unmanaged_sibling_directory(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.log", "archive content\n")
    conflict = case_dir / "logs"
    conflict.mkdir()
    user_file = conflict / "user-notes.txt"
    user_file.write_text("keep me", encoding="utf-8")
    case_id = _open(service, case_dir)

    prepared = service.prepare_case(case_id=case_id)

    assert prepared.success
    assert prepared.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_DESTINATION_CONFLICT"
    assert user_file.read_text(encoding="utf-8") == "keep me"
    assert not (conflict / "main.log").exists()


def test_prepare_does_not_delete_user_file_added_to_managed_directory(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.log", "archive content\n")
    case_id = _open(service, case_dir)
    assert service.prepare_case(case_id=case_id).success
    destination = case_dir / "logs"
    user_file = destination / "my-notes.txt"
    user_file.write_text("do not delete", encoding="utf-8")

    prepared = service.prepare_case(case_id=case_id, force_rebuild=True)

    assert prepared.success
    assert prepared.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_DESTINATION_CONFLICT"
    assert user_file.read_text(encoding="utf-8") == "do not delete"


def test_prepare_does_not_delete_extra_empty_directory(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.log", "archive content\n")
    case_id = _open(service, case_dir)
    assert service.prepare_case(case_id=case_id).success
    extra_dir = case_dir / "logs" / "manual-empty-folder"
    extra_dir.mkdir()

    prepared = service.prepare_case(case_id=case_id, force_rebuild=True)

    assert prepared.success
    assert prepared.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_DESTINATION_CONFLICT"
    assert extra_dir.is_dir()


def test_rejected_archive_update_invalidates_old_derived_evidence(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("main.log", "old evidence must disappear\n")
    case_id = _open(service, case_dir)
    assert service.prepare_case(case_id=case_id).success
    assert service.search_evidence(case_id=case_id, query="old evidence").data["match_count"] == 1

    archive_path.write_bytes(b"corrupt replacement")
    prepared = service.prepare_case(case_id=case_id)
    searched = service.search_evidence(case_id=case_id, query="old evidence")

    assert prepared.success
    assert prepared.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_INVALID"
    assert searched.success
    assert searched.data["match_count"] == 0


def test_prepare_replaces_stale_archive_members_and_rebuilds_index(tmp_path):
    case_dir, _, service = _service(tmp_path)
    archive_path = case_dir / "logs.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("old.log", "old archive marker\n")
    case_id = _open(service, case_dir)
    assert service.prepare_case(case_id=case_id).success

    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("new.log", "new archive marker with different size\n")
    prepared = service.prepare_case(case_id=case_id)
    old_search = service.search_evidence(case_id=case_id, query="old archive marker")
    new_search = service.search_evidence(case_id=case_id, query="new archive marker")

    assert prepared.success
    assert prepared.data["index"]["reused"] is False
    assert old_search.data["match_count"] == 0
    assert new_search.data["match_count"] == 1


def test_prepare_rejects_zip_path_traversal_without_writing_outside(tmp_path):
    case_dir, _, service = _service(tmp_path)
    with zipfile.ZipFile(case_dir / "malicious.zip", "w") as archive:
        archive.writestr("../../escaped.txt", "must not escape")
    case_id = _open(service, case_dir)

    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert result.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_PATH_TRAVERSAL"
    assert not (tmp_path / "escaped.txt").exists()


def test_prepare_reports_corrupt_archive(tmp_path):
    case_dir, _, service = _service(tmp_path)
    (case_dir / "broken.zip").write_bytes(b"this is not a zip")
    case_id = _open(service, case_dir)

    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert result.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_INVALID"


def test_prepare_rejects_zip_symlink(tmp_path):
    case_dir, _, service = _service(tmp_path)
    link = zipfile.ZipInfo("link.log")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    with zipfile.ZipFile(case_dir / "link.zip", "w") as archive:
        archive.writestr(link, "../../outside")
    case_id = _open(service, case_dir)

    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert result.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_LINK_REJECTED"


def test_prepare_supports_tar_gz_and_rejects_tar_links(tmp_path):
    case_dir, _, service = _service(tmp_path)
    payload = b"08-27 10:00:00.000 I Demo: tar evidence\n"
    with tarfile.open(case_dir / "good.tar.gz", "w:gz") as archive:
        info = tarfile.TarInfo("logs/linux.log")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    with tarfile.open(case_dir / "bad.tar", "w") as archive:
        link = tarfile.TarInfo("logs/link.log")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
    case_id = _open(service, case_dir)

    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert any(item["relative_path"] == "good.tar.gz" for item in result.data["extraction"]["archives"])
    assert any(item["reason"] == "ARCHIVE_LINK_REJECTED" for item in result.data["extraction"]["skipped"])
    searched = service.search_evidence(case_id=case_id, query="tar evidence")
    assert searched.data["match_count"] == 1


def test_prepare_enforces_compression_ratio_budget(tmp_path):
    limits = ArchiveLimits(
        max_archive_bytes=10 * 1024 * 1024,
        max_members=10,
        max_member_bytes=10 * 1024 * 1024,
        max_expanded_bytes=10 * 1024 * 1024,
        max_compression_ratio=2,
        max_depth=1,
    )
    case_dir, _, service = _service(tmp_path, archive_limits=limits)
    with zipfile.ZipFile(case_dir / "bomb.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("huge.log", "A" * 1024 * 1024)
    case_id = _open(service, case_dir)

    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert result.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_RATIO_LIMIT"


def test_chunk_index_finds_match_near_end_and_preserves_context(tmp_path):
    case_dir, _, service = _service(tmp_path)
    log_path = case_dir / "main_log.txt"
    lines = [f"line {number:06d} normal payload" for number in range(12_000)]
    lines[-3] = "08-27 10:00:00.000 I SurfaceFlinger: indexed timeline marker"
    lines[-2] = "before target"
    lines[-1] = "FATAL EXCEPTION near file tail"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    case_id = _open(service, case_dir)

    prepared = service.prepare_case(case_id=case_id, extract_archives=False)
    searched = service.search_evidence(
        case_id=case_id,
        query="EXCEPTION near file tail",
        context_before=1,
    )

    assert prepared.success
    assert prepared.data["index"]["chunk_count"] > 1
    assert searched.data["search_mode"] == "index"
    assert searched.data["items"][0]["line_start"] == 11_999
    assert "before target" in searched.data["items"][0]["content"]

    timeline = service.extract_timeline(case_id=case_id, anchors=["SurfaceFlinger"], year_hint=2026)
    diagnostics = service.parse_diagnostics(case_id=case_id, diagnostic_types=["fatal"])
    assert timeline.data["search_mode"] == "index"
    assert timeline.data["event_count"] == 1
    assert diagnostics.data["search_mode"] == "index"
    assert diagnostics.data["finding_count"] == 1


def test_chunk_index_detects_utf16_text(tmp_path):
    case_dir, _, service = _service(tmp_path)
    (case_dir / "utf16.log").write_text(
        "08-27 10:00:00.000 I Demo: 中文编码证据\n", encoding="utf-16"
    )
    case_id = _open(service, case_dir)

    prepared = service.prepare_case(case_id=case_id, extract_archives=False)
    searched = service.search_evidence(case_id=case_id, query="中文编码证据")

    assert prepared.success
    assert searched.data["search_mode"] == "index"
    assert searched.data["match_count"] == 1


def test_unsupported_archive_is_reported_without_external_fallback(tmp_path):
    case_dir, _, service = _service(tmp_path)
    (case_dir / "vendor.xz").write_bytes(b"not really xz")
    case_id = _open(service, case_dir)

    result = service.prepare_case(case_id=case_id)

    assert result.success
    # .xz 不在支持的归档格式中，不会被识别为 archive
    assert result.data["extraction"]["expanded_file_count"] == 0


def test_registry_reads_server_side_preparation_limits(monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_ANALYZER_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("LOG_ANALYZER_WORK_ROOT", str(tmp_path / "isolated-work"))
    monkeypatch.setenv("LOG_ANALYZER_MAX_EXTRACTED_FILES", "321")
    monkeypatch.setenv("LOG_ANALYZER_MAX_ARCHIVE_DEPTH", "3")
    monkeypatch.setenv("LOG_ANALYZER_INDEX_CHUNK_BYTES", "131072")

    registry = CaseRegistry.from_environment()

    assert registry.work_root == (tmp_path / "isolated-work").resolve()
    assert registry.archive_limits.max_members == 321
    assert registry.archive_limits.max_depth == 3
    assert registry.index_limits.chunk_bytes == 131072


def test_registry_rejects_work_root_inside_case(tmp_path):
    case_dir = tmp_path / "CASE-INSIDE"
    case_dir.mkdir()
    registry = CaseRegistry([str(tmp_path)], work_root=str(case_dir / ".cache"))

    result = registry.open_case(str(case_dir))

    assert not result.success
    assert result.error_code == "WORK_ROOT_INSIDE_CASE"


def test_registry_defaults_internal_state_to_case_bug_agent_directory(tmp_path):
    case_dir = tmp_path / "CASE-DEFAULT-WORK"
    case_dir.mkdir()
    registry = CaseRegistry([str(tmp_path)])

    opened = registry.open_case(str(case_dir))

    assert opened.success
    assert registry.get_work_dir(opened.data["case"]["case_id"]) == case_dir / ".bug-agent"


def test_empty_work_root_environment_keeps_case_internal_default(monkeypatch, tmp_path):
    case_dir = tmp_path / "CASE-EMPTY-WORK"
    case_dir.mkdir()
    monkeypatch.setenv("LOG_ANALYZER_WORK_ROOT", "")
    registry = CaseRegistry.from_environment([str(tmp_path)])

    opened = registry.open_case(str(case_dir))

    assert opened.success
    assert registry.get_work_dir(opened.data["case"]["case_id"]) == case_dir / ".bug-agent"


def test_open_case_prunes_bug_agent_directory_case_insensitively(tmp_path):
    case_dir = tmp_path / "CASE-STATE"
    internal = case_dir / ".Bug-Agent" / "index"
    internal.mkdir(parents=True)
    (internal / "logs.sqlite3").write_bytes(b"not an artifact")
    (case_dir / "main.log").write_text("real input\n", encoding="utf-8")
    registry = CaseRegistry([str(tmp_path)])

    opened = registry.open_case(str(case_dir))

    assert opened.success
    assert opened.data["case"]["artifact_count"] == 1
    assert opened.data["case"]["artifacts"][0]["relative_path"] == "main.log"


def test_is_supported_archive_includes_7z(tmp_path):
    from log_analyzer.archive_manager import is_supported_archive

    assert is_supported_archive(Path("test.7z"))
    assert is_supported_archive(Path("test.7z.001"))
    assert is_supported_archive(Path("archive.7z.001"))
    # 非 .001 的后续分卷本身不直接支持，但 _is_7z_volume 会识别
    assert is_supported_archive(Path("test.zip"))
    assert is_supported_archive(Path("test.tar.gz"))
    assert not is_supported_archive(Path("test.rar"))
    assert not is_supported_archive(Path("test.txt"))


def test_prepare_extracts_7z_archive(tmp_path):
    import py7zr

    case_dir, _, service = _service(tmp_path)
    with py7zr.SevenZipFile(str(case_dir / "logs.7z"), "w") as z:
        z.writestr(b"kernel panic\n", "kernel.log")
        z.writestr(b"main crash\n", "main.log")

    case_id = _open(service, case_dir)
    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert result.data["extraction"]["expanded_file_count"] == 2
    dest = case_dir / "logs"
    assert dest.is_dir()
    assert (dest / "kernel.log").read_text() == "kernel panic\n"
    assert (dest / "main.log").read_text() == "main crash\n"


def test_7z_volume_extension_is_supported(tmp_path):
    """以 .7z.001 结尾的文件被 is_supported_archive 识别。"""
    from log_analyzer.archive_manager import is_supported_archive

    assert is_supported_archive(Path("logs.7z.001"))
    assert is_supported_archive(Path("data.7z"))
    assert not is_supported_archive(Path("data.7z.002"))  # 非 .001 后缀不直接支持


def test_prepare_rejects_encrypted_7z(tmp_path):
    import py7zr

    case_dir, _, service = _service(tmp_path)
    with py7zr.SevenZipFile(str(case_dir / "secret.7z"), "w", password="1234") as z:
        z.writestr(b"secret\n", "secret.txt")

    case_id = _open(service, case_dir)
    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert result.data["extraction"]["expanded_file_count"] == 0
    skipped = result.data["extraction"]["skipped"]
    assert len(skipped) >= 1
    assert any(s["reason"] in ("ARCHIVE_ENCRYPTED", "ARCHIVE_INVALID") for s in skipped)


def test_prepare_rejects_corrupt_7z(tmp_path):
    case_dir, _, service = _service(tmp_path)
    (case_dir / "broken.7z").write_bytes(b"not a valid 7z file")

    case_id = _open(service, case_dir)
    result = service.prepare_case(case_id=case_id)

    assert result.success
    assert len(result.data["extraction"]["skipped"]) == 1
    assert result.data["extraction"]["skipped"][0]["reason"] == "ARCHIVE_INVALID"
