"""LogAnalyzerService V2 deterministic service tests."""
from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from log_analyzer.case_registry import CaseRegistry
from log_analyzer.service import LogAnalyzerService


class LogAnalyzerServiceV2Test(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.case_dir = self.root / "CASE-1"
        self.case_dir.mkdir()
        (self.case_dir / "logcat.txt").write_text(
            "08-26 10:20:31.123  100  200 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "08-26 10:20:32.000  100  200 W audit: avc: denied { read write } "
            "for scontext=u:r:app:s0 tcontext=u:object_r:vendor_file:s0 tclass=file\n",
            encoding="utf-8",
        )
        self.registry = CaseRegistry([str(self.root)])
        self.service = LogAnalyzerService(self.registry)
        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        self.case_id = opened.data["case"]["case_id"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_search_timeline_and_diagnostics(self):
        search = self.service.search_evidence(case_id=self.case_id, query="FATAL")
        self.assertTrue(search.success)
        self.assertEqual(search.data["match_count"], 1)
        timeline = self.service.extract_timeline(
            case_id=self.case_id, anchors=["FATAL", "avc: denied"],
        )
        self.assertTrue(timeline.success)
        self.assertEqual(timeline.data["event_count"], 2)
        diagnostics = self.service.parse_diagnostics(
            case_id=self.case_id, diagnostic_types=["fatal", "avc"],
        )
        self.assertTrue(diagnostics.success)
        self.assertEqual(diagnostics.data["finding_count"], 2)
        avc = next(item for item in diagnostics.data["findings"] if item["diagnostic_type"] == "avc")
        self.assertEqual(avc["attributes"]["permissions"], ["read", "write"])

    def test_aee_db_is_highlighted_decoded_registered_and_indexed(self):
        # The .dbg suffix must win over the embedded ANR token; binary AEE DBs
        # must never be misclassified as directly readable ANR text.
        dbg_path = self.case_dir / "db.03.ANR-sample.dbg"
        dbg_path.write_bytes(b"aee-binary")
        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        self.case_id = opened.data["case"]["case_id"]

        inspected = self.service.inspect_case(case_id=self.case_id)
        self.assertTrue(inspected.success)
        aee_items = inspected.data["summary"]["aee_databases"]
        self.assertEqual(inspected.data["kinds"]["aee_db"], 1)
        self.assertEqual(aee_items[0]["name"], "db.03.ANR-sample.dbg")
        self.assertEqual(aee_items[0]["activation_skill"], "aee-db-extract")
        self.assertFalse(aee_items[0]["decoded"])
        self.assertEqual(
            inspected.data["summary"]["required_skill_activations"][0]["name"],
            "aee-db-extract",
        )

        def fake_run(command, **kwargs):
            source = Path(command[1])
            output = Path(f"{source}.DEC")
            output.mkdir()
            (output / "__exp_main.txt").write_text("Process: demo\nFatal signal 11\n", encoding="utf-8")
            (output / "SYS_KERNEL_LOG").write_text("kernel marker\n", encoding="utf-8")
            return SimpleNamespace(returncode=0)

        with patch("log_analyzer.service.subprocess.run", side_effect=fake_run) as decoder:
            extracted = self.service.extract_aee_db(
                case_id=self.case_id,
                artifact_id=aee_items[0]["artifact_id"],
            )

        self.assertTrue(extracted.success)
        self.assertFalse(extracted.data["reused"])
        self.assertEqual(extracted.data["decoded_file_count"], 2)
        self.assertIsNotNone(extracted.data["recommended_first"])
        decoder.assert_called_once()

        text_ids = [
            item["artifact_id"] for item in extracted.data["artifacts"]
            if item["readable_text"]
        ]
        indexed = self.service.build_index(case_id=self.case_id, artifact_ids=text_ids)
        self.assertTrue(indexed.success)
        searched = self.service.search_evidence(case_id=self.case_id, query="Fatal signal 11")
        self.assertTrue(searched.success)
        self.assertEqual(searched.data["match_count"], 1)

    def test_inspect_case_bounds_archives_and_supports_targeted_artifact_discovery(self):
        import zipfile

        for index in range(60):
            with zipfile.ZipFile(self.case_dir / f"archive-{index:03d}.zip", "w") as archive:
                archive.writestr("marker.txt", "data")
        (self.case_dir / "db.fatal.00.KE.dbg").write_bytes(b"aee")
        (self.case_dir / "SYSTEM_LAST_KMSG.txt").write_text("panic", encoding="utf-8")

        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        inspected = self.service.inspect_case(case_id=self.case_id)

        self.assertTrue(inspected.success)
        summary = inspected.data["summary"]
        self.assertEqual(len(summary["archives"]), 20)
        self.assertTrue(summary["archives_truncated"])
        self.assertNotIn("priority_artifacts", summary)

        filtered = self.service.inspect_case(
            case_id=self.case_id,
            artifact_kinds=["aee_db"],
            path_contains="db.fatal.00.KE",
        )
        self.assertTrue(filtered.success)
        self.assertEqual(filtered.data["selection"]["matched_artifact_count"], 1)
        self.assertEqual(filtered.data["artifacts"][0]["kind"], "aee_db")


class GetCaseCommentTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.case_dir = self.root / "BUG-40305"
        self.case_dir.mkdir()
        issue = {
            "key": "BUG-40305",
            "comments": [
                {
                    "comment_id": "10001",
                    "author": "Alice",
                    "created_at": "2026-08-01T10:00:00+00:00",
                    "updated_at": "2026-08-01T10:00:00+00:00",
                    "body": "First comment: initial report.",
                },
                {
                    "comment_id": "10002",
                    "author": "Bob",
                    "created_at": "2026-08-02T12:00:00+00:00",
                    "updated_at": "2026-08-02T13:00:00+00:00",
                    "body": "0123456789",
                },
            ],
        }
        issue_bytes = json.dumps(issue, ensure_ascii=False).encode("utf-8")
        (self.case_dir / "issue.json").write_bytes(issue_bytes)
        (self.case_dir / "collection-manifest.json").write_text(json.dumps({
            "schema_version": 2,
            "source": "jira",
            "root_issue": "BUG-40305",
            "root_issue_context": {
                "version": 1,
                "comments": {
                    "total": 2,
                    "collected": 2,
                    "complete": True,
                    "truncated": False,
                },
            },
            "issue_json_sha256": hashlib.sha256(issue_bytes).hexdigest(),
        }), encoding="utf-8")
        (self.case_dir / "logcat.txt").write_text("ready\n", encoding="utf-8")
        self.registry = CaseRegistry([str(self.root)])
        self.service = LogAnalyzerService(self.registry)
        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        self.case_id = opened.data["case"]["case_id"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reads_one_comment_by_id(self):
        result = self.service.get_case_comment(
            case_id=self.case_id, comment_id="10001",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["author"], "Alice")
        self.assertEqual(result.data["body"], "First comment: initial report.")
        self.assertFalse(result.data["has_more"])

    def test_paginates_long_comment_by_characters(self):
        first = self.service.get_case_comment(
            case_id=self.case_id, comment_id="10002", offset=0, limit=4,
        )
        self.assertTrue(first.success)
        self.assertEqual(first.data["body"], "0123")
        self.assertEqual(first.data["next_offset"], 4)
        second = self.service.get_case_comment(
            case_id=self.case_id, comment_id="10002", offset=4, limit=20,
        )
        self.assertEqual(second.data["body"], "456789")
        self.assertFalse(second.data["has_more"])

    def test_unknown_comment_is_structured_error(self):
        result = self.service.get_case_comment(
            case_id=self.case_id, comment_id="missing",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "COMMENT_NOT_FOUND")

    def test_revalidates_issue_hash_on_every_read(self):
        issue_path = self.case_dir / "issue.json"
        issue_path.write_text(issue_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
        result = self.service.get_case_comment(
            case_id=self.case_id, comment_id="10001",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "JIRA_CASE_CONTEXT_TAMPERED")

    def test_legacy_case_without_manifest_requires_reexport(self):
        (self.case_dir / "collection-manifest.json").unlink()
        result = self.service.get_case_comment(
            case_id=self.case_id, comment_id="10001",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "JIRA_CASE_REEXPORT_REQUIRED")

    def test_case_not_open(self):
        result = self.service.get_case_comment(
            case_id="case_missing", comment_id="10001",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "CASE_NOT_OPEN")

    def test_issue_json_required(self):
        other = self.root / "NO-JIRA"
        other.mkdir()
        (other / "log.txt").write_text("ready\n", encoding="utf-8")
        opened = self.service.open_case(case_path=str(other))
        result = self.service.get_case_comment(
            case_id=opened.data["case"]["case_id"], comment_id="10001",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "ISSUE_JSON_NOT_FOUND")


class InspectArchiveTimeRangeTest(unittest.TestCase):
    """inspect_archive time_range behavior with mixed timestamped/untimed members."""

    def setUp(self):
        import zipfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.case_dir = self.root / "CASE-ARCHIVE"
        self.case_dir.mkdir()

        # Create an archive with a mix of APLogs and non-timestamped members
        archive_path = self.case_dir / "android.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("APLog_2026_0826_120000__1/main.log", "line1")
            archive.writestr("APLog_2026_0826_131229__8/main.log", "line2")
            archive.writestr("APLog_2026_0826_140000__9/main.log", "line3")
            archive.writestr("anr/traces.txt", "anr content")
            archive.writestr("aee_exp/db.00.NE/exp_detail.txt", "ne detail")

        from log_analyzer.archive_manager import ArchiveLimits
        self.registry = CaseRegistry(
            [str(self.root)],
            archive_limits=ArchiveLimits(
                max_archive_bytes=20 * 1024 * 1024,
                max_members=100,
                max_member_bytes=20 * 1024 * 1024,
                max_expanded_bytes=40 * 1024 * 1024,
                max_compression_ratio=1000,
            ),
        )
        self.service = LogAnalyzerService(self.registry)
        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        self.case_id = opened.data["case"]["case_id"]
        inspect = self.service.inspect_case(case_id=self.case_id)
        self.archive_id = inspect.data["artifacts"][0]["artifact_id"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_time_range_preserves_untimed_members(self):
        """When time_range is specified, untimed members (anr/, aee_exp/) are preserved."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
            time_range={"start": "2026-08-26T13:00:00", "end": "2026-08-26T14:00:00"},
        )
        self.assertTrue(result.success)
        paths = {item["member_path"] for item in result.data["members"]}
        # Timestamped members in range
        self.assertIn("APLog_2026_0826_131229__8/main.log", paths)
        # Untimed members must be present
        self.assertIn("anr/traces.txt", paths, "untimed members should be preserved")
        self.assertIn("aee_exp/db.00.NE/exp_detail.txt", paths, "untimed members should be preserved")

    def test_time_range_excludes_out_of_range_timestamped(self):
        """Timestamped members outside the time range (but within neighbors) are excluded."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
            time_range={"start": "2026-08-26T13:00:00", "end": "2026-08-26T14:00:00"},
            time_neighbor_count=0,
        )
        self.assertTrue(result.success)
        paths = {item["member_path"] for item in result.data["members"]}
        # 12:00 is outside range, no neighbor
        self.assertNotIn("APLog_2026_0826_120000__1/main.log", paths)

    def test_time_range_with_neighbor_includes_predecessor(self):
        """time_neighbor_count=1 includes the predecessor."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
            time_range={"start": "2026-08-26T13:00:00", "end": "2026-08-26T14:00:00"},
            time_neighbor_count=1,
        )
        self.assertTrue(result.success)
        paths = {item["member_path"] for item in result.data["members"]}
        self.assertIn("APLog_2026_0826_120000__1/main.log", paths)

    def test_time_range_selection_payload_has_untimed_count(self):
        """selection payload reports untimed_member_count."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
            time_range={"start": "2026-08-26T13:00:00", "end": "2026-08-26T14:00:00"},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["selection"]["untimed_member_count"], 2)

    def test_time_relation_untimed(self):
        """Untimed members get time_relation='untimed'."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
            time_range={"start": "2026-08-26T13:00:00", "end": "2026-08-26T14:00:00"},
        )
        self.assertTrue(result.success)
        anr = next(item for item in result.data["members"] if item["member_path"] == "anr/traces.txt")
        self.assertEqual(anr["path_time_relation"], "untimed")

    def test_no_time_range_returns_all_members(self):
        """Without time_range, all members are returned."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["member_count"], 5)

    def test_time_range_reliability_is_reliable_for_normal_dates(self):
        """Archive with only reliably-dated members has time_reliability='reliable'."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
            time_range={"start": "2026-08-26T12:00:00", "end": "2026-08-26T14:00:00"},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["selection"]["time_reliability"], "reliable")
        self.assertIsNone(result.data["selection"]["time_reliability_detail"])

    def test_time_range_reliability_unreliable_for_jan1(self):
        """Archive with Jan-1 dates has time_reliability='unreliable_device_clock'."""
        import zipfile
        path = self.case_dir / "unreliable.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("APLog_2025_0101_080037__9/main.log", "data")
            archive.writestr("APLog_2025_0101_090140__2/main.log", "data")
        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        case_id = opened.data["case"]["case_id"]
        inspect = self.service.inspect_case(case_id=case_id)
        archive_id = next(
            item["artifact_id"] for item in inspect.data["artifacts"]
            if item["name"] == "unreliable.zip"
        )
        result = self.service.inspect_archive(
            case_id=case_id,
            artifact_id=archive_id,
            time_range={"start": "2026-08-26T00:00:00", "end": "2026-08-26T23:59:59"},
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["selection"]["time_reliability"], "unreliable_device_clock")
        self.assertIn("设备时钟未同步", result.data["selection"]["time_reliability_detail"])

    def test_member_path_timestamp_reliability_field(self):
        """Each member with a timestamp has path_timestamp_reliability."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
        )
        self.assertTrue(result.success)
        for item in result.data["members"]:
            if item["path_timestamp"] is not None:
                self.assertIsNotNone(item["path_timestamp_reliability"])
                self.assertEqual(item["path_timestamp_clock_domain"], "path_basename")
            else:
                self.assertIsNone(item["path_timestamp_reliability"])

    def test_time_groups_have_reliability(self):
        """time_groups include earliest/latest_path_time_reliability."""
        result = self.service.inspect_archive(
            case_id=self.case_id,
            artifact_id=self.archive_id,
        )
        self.assertTrue(result.success)
        for group in result.data["time_groups"]:
            if group["earliest_path_time"] is not None:
                self.assertIsNotNone(group["earliest_path_time_reliability"])
            if group["latest_path_time"] is not None:
                self.assertIsNotNone(group["latest_path_time_reliability"])
            self.assertIsNotNone(group["unreliable_timestamped_member_count"])

    def test_stream_groups_pair_active_curf_with_latest_rotated_predecessor(self):
        import zipfile

        path = self.case_dir / "aplog-stream.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("APLog__9/main_log_145__2026_0908_001300", "old")
            archive.writestr("APLog__9/main_log_146__2026_0908_001649", "previous")
            archive.writestr("APLog__9/main_log_2026_0908_001649.curf", "active")
            archive.writestr("APLog__9/events_log_2026_0907_233729.curf", "events")

        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        inspect = self.service.inspect_case(
            case_id=self.case_id,
            path_contains="aplog-stream.zip",
        )
        archive_id = inspect.data["artifacts"][0]["artifact_id"]
        result = self.service.inspect_archive(case_id=self.case_id, artifact_id=archive_id)

        self.assertTrue(result.success)
        main = next(item for item in result.data["stream_groups"] if item["stream"] == "main_log")
        self.assertEqual(
            main["predecessor_member"],
            "APLog__9/main_log_146__2026_0908_001649",
        )
        self.assertEqual(len(main["recommended_member_ids"]), 2)
        self.assertIn("active .curf", main["coverage_rule"])


if __name__ == "__main__":
    unittest.main()
