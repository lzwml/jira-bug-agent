"""V2 领域服务测试；仅依赖标准库 unittest。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from log_analyzer.case_registry import CaseRegistry
from log_analyzer.service import LogAnalyzerService


class LogAnalyzerServiceV2Test(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.case_dir = self.root / "BUG-40305"
        self.case_dir.mkdir()

        (self.case_dir / "logcat_main.log").write_text(
            "08-26 10:20:31.123  100  200 I SurfaceFlinger: display ready\n"
            "08-26 10:20:32.000  100  200 I BootAnimation: bootanimation exit\n"
            "08-26 10:20:33.000  100  200 E AndroidRuntime: FATAL EXCEPTION: main\n"
            "08-26 10:20:34.000 avc: denied { read write } for "
            "scontext=u:r:system_server:s0 tcontext=u:object_r:vendor_file:s0 "
            "tclass=file permissive=0\n",
            encoding="utf-8",
        )
        (self.case_dir / "kernel.log").write_text(
            "[  123.456789] Call Trace:\n"
            "[  123.456790]  <TASK>\n"
            "[  123.456791]  dump_stack+0x10/0x20\n"
            "[  123.456792]  </TASK>\n",
            encoding="utf-8",
        )

        self.registry = CaseRegistry([str(self.root)])
        self.service = LogAnalyzerService(self.registry)
        opened = self.service.open_case(case_path=str(self.case_dir))
        self.assertTrue(opened.success)
        self.case_id = opened.data["case"]["case_id"]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_open_case_builds_artifact_inventory(self):
        result = self.service.inspect_case(case_id=self.case_id)
        self.assertTrue(result.success)
        self.assertEqual(result.data["artifact_count"], 2)
        self.assertEqual(result.data["kinds"]["kernel"], 1)
        self.assertEqual(result.data["kinds"]["logcat"], 1)

    def test_server_root_cannot_be_expanded_by_caller(self):
        with tempfile.TemporaryDirectory() as outside:
            result = self.service.open_case(case_path=outside)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "PATH_NOT_ALLOWED")

    def test_search_returns_evidence_with_context(self):
        result = self.service.search_evidence(
            case_id=self.case_id,
            query="FATAL EXCEPTION",
            context_before=1,
            context_after=1,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["match_count"], 1)
        evidence = result.data["items"][0]
        self.assertEqual(evidence["line_start"], 2)
        self.assertEqual(evidence["line_end"], 4)
        self.assertIn("bootanimation exit", evidence["content"])
        self.assertIn("avc: denied", evidence["content"])

    def test_search_no_match_is_success(self):
        result = self.service.search_evidence(
            case_id=self.case_id,
            query="definitely-not-present",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["items"], [])
        self.assertEqual(result.data["match_count"], 0)

    def test_timeline_supports_android_and_kernel_clocks(self):
        result = self.service.extract_timeline(
            case_id=self.case_id,
            anchors=["SurfaceFlinger", "bootanimation", "Call Trace:"],
            year_hint=2026,
        )
        self.assertTrue(result.success)
        self.assertEqual(result.data["event_count"], 3)
        self.assertEqual(
            set(result.data["clock_domains"]),
            {"android", "kernel_monotonic"},
        )

    def test_timeline_requires_skill_supplied_anchors(self):
        result = self.service.dispatch("extract_timeline", {"case_id": self.case_id})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_PARAMS")

    def test_parse_diagnostics_returns_structured_findings(self):
        result = self.service.parse_diagnostics(case_id=self.case_id)
        self.assertTrue(result.success)
        finding_types = {item["diagnostic_type"] for item in result.data["findings"]}
        self.assertEqual(finding_types, {"avc", "fatal", "kernel_stack"})
        avc = next(item for item in result.data["findings"] if item["diagnostic_type"] == "avc")
        self.assertEqual(avc["attributes"]["tclass"], "file")
        self.assertEqual(avc["attributes"]["permissions"], ["read", "write"])


if __name__ == "__main__":
    unittest.main()
