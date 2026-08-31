"""LogAnalyzerService V2 deterministic service tests."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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
        (self.case_dir / "issue.json").write_text(
            json.dumps(issue, ensure_ascii=False), encoding="utf-8",
        )
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


if __name__ == "__main__":
    unittest.main()
