from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from jira_bug_mcp.client import JiraClient
from jira_bug_mcp.config import JiraConfig
from jira_bug_mcp.domain import JiraAttachment
from jira_bug_mcp.errors import JiraApiError
from jira_bug_mcp.exporter import CaseExporter
from jira_bug_mcp.service import JiraService


ISSUE = {
    "id": "10001",
    "key": "APP-42",
    "fields": {
        "summary": "启动后黑屏",
        "description": {
            "type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "开机后停留在黑屏。"}]}
            ],
        },
        "issuetype": {"name": "Bug"},
        "status": {"name": "Open"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Alice"},
        "reporter": {"displayName": "Bob"},
        "labels": ["boot"],
        "components": [{"name": "Framework"}],
        "created": "2026-08-25T10:00:00.000+0800",
        "updated": "2026-08-26T09:00:00.000+0800",
        "attachment": [{
            "id": "20001", "filename": "logcat.txt", "size": 18,
            "mimeType": "text/plain", "content": "https://jira.test/attachment/20001",
        }],
        "comment": {"comments": [{
            "id": "30001", "author": {"displayName": "Carol"},
            "body": {"type": "doc", "content": [{
                "type": "paragraph", "content": [{"type": "text", "text": "可以稳定复现"}],
            }]},
            "created": "2026-08-26T08:00:00.000+0800",
        }]},
    },
}


def jira_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/rest/api/3/issue/APP-42":
        return httpx.Response(200, json=ISSUE)
    if path == "/rest/api/3/issue/APP-42/comment":
        return httpx.Response(200, json={"comments": ISSUE["fields"]["comment"]["comments"], "total": 1})
    if path == "/rest/api/3/search/jql":
        return httpx.Response(200, json={"issues": [ISSUE], "nextPageToken": "page-2"})
    if path == "/attachment/20001":
        return httpx.Response(200, content=b"FATAL boot failed\n", headers={"content-length": "18"})
    return httpx.Response(404)


class JiraServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = JiraConfig(
            base_url="https://jira.test",
            auth_mode="none",
            export_root=Path(self.temp.name),
        )
        self.client = JiraClient(self.config, transport=httpx.MockTransport(jira_transport))
        self.service = JiraService(self.client, CaseExporter(self.config, self.client))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_get_issue_normalizes_adf_and_hides_content_url(self):
        result = self.service.dispatch("get_issue", {"issue_key": "app-42"})
        self.assertTrue(result.success)
        issue = result.data["issue"]
        self.assertEqual(issue["description"], "开机后停留在黑屏。")
        self.assertEqual(issue["comments"][0]["body"], "可以稳定复现")
        self.assertNotIn("content_url", issue["attachments"][0])

    def test_cloud_search_uses_opaque_cursor(self):
        result = self.service.dispatch("search_issues", {"jql": "project = APP"})
        self.assertTrue(result.success)
        self.assertEqual(result.data["item_count"], 1)
        self.assertEqual(result.data["next_cursor"], "page-2")

    def test_export_creates_log_analyzer_case(self):
        result = self.service.dispatch("export_issue_case", {"issue_key": "APP-42"})
        self.assertTrue(result.success)
        case = Path(result.data["case_path"])
        self.assertTrue((case / "issue.json").is_file())
        self.assertTrue((case / "issue.md").is_file())
        self.assertEqual((case / "attachments" / "20001_logcat.txt").read_bytes(), b"FATAL boot failed\n")
        self.assertIn("open_case", result.data["next_step"])

    def test_invalid_issue_key_is_a_contract_error(self):
        result = self.service.dispatch("get_issue", {"issue_key": "../../secret"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_PARAMS")


class JiraHttpErrorTest(unittest.TestCase):
    def test_authentication_error_is_safe_and_non_retryable(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(401, json={"token": "do-not-leak"}))
        config = JiraConfig(base_url="https://jira.test", auth_mode="none")
        client = JiraClient(config, transport=transport)
        service = JiraService(client, CaseExporter(config, client))
        try:
            result = service.dispatch("get_issue", {"issue_key": "APP-1"})
        finally:
            client.close()
        self.assertEqual(result.error_code, "AUTH_FAILED")
        self.assertNotIn("do-not-leak", result.error_message or "")
        self.assertFalse(result.retryable)

    def test_datacenter_search_converts_start_at_to_cursor(self):
        def transport(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/rest/api/2/search")
            return httpx.Response(200, json={"issues": [ISSUE], "total": 3})

        config = JiraConfig(base_url="https://jira.test", deployment="datacenter", auth_mode="none")
        client = JiraClient(config, transport=httpx.MockTransport(transport))
        try:
            page = client.search_issues("project = APP", 1, "1")
        finally:
            client.close()
        self.assertEqual(page["next_cursor"], "2")

    def test_cross_origin_attachment_url_is_rejected(self):
        config = JiraConfig(base_url="https://jira.test", auth_mode="none")
        client = JiraClient(config, transport=httpx.MockTransport(jira_transport))
        attachment = JiraAttachment(
            attachment_id="1", filename="x.txt", size_bytes=1,
            content_url="https://evil.example/x.txt",
        )
        try:
            with tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(JiraApiError) as raised:
                    client.download_attachment(attachment, Path(temp) / "x.txt")
        finally:
            client.close()
        self.assertEqual(raised.exception.code, "UNSAFE_ATTACHMENT_URL")


if __name__ == "__main__":
    unittest.main()
