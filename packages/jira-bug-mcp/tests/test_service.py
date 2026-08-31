from __future__ import annotations

import json
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
        "environment": "userdebug build on bench A",
        "resolution": None,
        "versions": [{"name": "V1.2"}],
        "fixVersions": [{"name": "V1.3"}],
        "parent": {"key": "APP-10"},
        "subtasks": [{"key": "APP-43"}],
        "issuelinks": [{
            "type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"},
            "outwardIssue": {
                "key": "APP-99",
                "fields": {"summary": "Display service failure", "status": {"name": "Open"}},
            },
        }],
        "customfield_12345": {"value": "IVI"},
        "created": "2026-08-25T10:00:00.000+0800",
        "updated": "2026-08-26T09:00:00.000+0800",
        "attachment": [{
            "id": "20001", "filename": "logcat.txt", "size": 18,
            "mimeType": "text/plain", "content": "https://jira.test/attachment/20001",
        }],
        "comment": {"comments": [{
            "id": "30001", "author": {"displayName": "Carol"},
            "body": {"type": "doc", "content": [{
                "type": "paragraph", "content": [{"type": "text", "text": "可以稳定复现，长日志见 APP-88"}],
            }]},
            "created": "2026-08-26T08:00:00.000+0800",
        }]},
    },
}

RELATED_ISSUE = {
    "id": "10099",
    "key": "APP-99",
    "fields": {
        "summary": "显示服务现场日志",
        "description": "APP-42 的原始日志在附件中",
        "issuetype": {"name": "Bug"},
        "status": {"name": "Open"},
        "priority": {"name": "High"},
        "labels": [],
        "components": [],
        "attachment": [{
            "id": "20099", "filename": "related-logcat.txt", "size": 18,
            "mimeType": "text/plain", "content": "https://jira.test/attachment/20099",
        }],
        "comment": {"comments": []},
    },
}

ATTACHMENT_ONLY_ISSUE = {
    "id": "10088",
    "key": "APP-88",
    "fields": {
        "summary": "APP-42 长日志存放位置",
        "attachment": [{
            "id": "20088", "filename": "long-bugreport.zip", "size": 15,
            "mimeType": "application/zip", "content": "https://jira.test/attachment/20088",
        }],
    },
}


def jira_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/rest/api/3/issue/APP-42":
        return httpx.Response(200, json=ISSUE)
    if path == "/rest/api/3/issue/APP-99":
        return httpx.Response(200, json=RELATED_ISSUE)
    if path == "/rest/api/3/issue/APP-88":
        return httpx.Response(200, json=ATTACHMENT_ONLY_ISSUE)
    if path == "/rest/api/3/issue/APP-42/comment":
        return httpx.Response(200, json={"comments": ISSUE["fields"]["comment"]["comments"], "total": 1})
    if path == "/rest/api/3/issue/APP-99/comment":
        return httpx.Response(200, json={"comments": [], "total": 0})
    if path == "/rest/api/3/serverInfo":
        return httpx.Response(200, json={
            "serverTitle": "Test Jira", "version": "1001", "deploymentType": "Cloud",
        })
    if path == "/rest/api/3/myself":
        return httpx.Response(200, json={"displayName": "Collector", "accountId": "safe-account-id"})
    if path == "/rest/api/3/search/jql":
        return httpx.Response(200, json={"issues": [ISSUE], "nextPageToken": "page-2"})
    if path == "/attachment/20001":
        return httpx.Response(200, content=b"FATAL boot failed\n", headers={"content-length": "18"})
    if path == "/attachment/20099":
        return httpx.Response(200, content=b"RELATED FATAL log\n", headers={"content-length": "18"})
    if path == "/attachment/20088":
        return httpx.Response(200, content=b"LONG BUGREPORT\n", headers={"content-length": "15"})
    return httpx.Response(404)


class JiraServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = JiraConfig(
            base_url="https://jira.test",
            auth_mode="none",
            export_root=Path(self.temp.name),
            extra_fields=("customfield_12345",),
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
        self.assertEqual(issue["comments"][0]["body"], "可以稳定复现，长日志见 APP-88")
        self.assertNotIn("content_url", issue["attachments"][0])

    def test_collect_issue_context_fetches_complete_comments_and_bug_fields(self):
        result = self.service.dispatch("collect_issue_context", {"issue_key": "APP-42"})
        self.assertTrue(result.success)
        issue = result.data["issue"]
        self.assertEqual(issue["environment"], "userdebug build on bench A")
        self.assertEqual(issue["versions"], ["V1.2"])
        self.assertEqual(issue["fix_versions"], ["V1.3"])
        self.assertEqual(issue["parent_key"], "APP-10")
        self.assertEqual(issue["subtask_keys"], ["APP-43"])
        self.assertEqual(issue["issue_links"][0]["target_key"], "APP-99")
        self.assertEqual(issue["extra_fields"], {"customfield_12345": "IVI"})
        self.assertEqual(result.data["collection"]["collected"], 1)
        self.assertFalse(result.data["collection"]["truncated"])
        self.assertTrue(result.data["collection"]["complete"])

    def test_connection_reports_safe_server_metadata(self):
        result = self.service.dispatch("test_connection", {})
        self.assertTrue(result.success)
        self.assertEqual(result.data["server"]["server_title"], "Test Jira")
        self.assertEqual(result.data["server"]["authenticated_user"], "Collector")
        self.assertNotIn("token", result.data["server"])

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
        self.assertEqual(
            (case / "related" / "APP-99" / "attachments" / "20099_related-logcat.txt").read_bytes(),
            b"RELATED FATAL log\n",
        )
        self.assertEqual(
            (case / "related" / "APP-88" / "attachments" / "20088_long-bugreport.zip").read_bytes(),
            b"LONG BUGREPORT\n",
        )
        self.assertTrue((case / "related" / "APP-88" / "attachment-source.json").is_file())
        self.assertFalse((case / "related" / "APP-88" / "issue.json").exists())
        manifest = (case / "collection-manifest.json").read_text(encoding="utf-8")
        self.assertIn('"to_issue": "APP-99"', manifest)
        manifest_data = json.loads(manifest)
        self.assertEqual(manifest_data["schema_version"], 2)
        self.assertIn("root_issue_context", manifest_data)
        self.assertEqual(manifest_data["root_issue_context"]["version"], 1)
        self.assertIn("comments", manifest_data["root_issue_context"])
        self.assertEqual(manifest_data["root_issue_context"]["comments"]["complete"], True)
        self.assertEqual(manifest_data["root_issue_context"]["comments"]["collected"], 1)
        self.assertEqual(manifest_data["root_issue_context"]["attachments_listed"], 1)
        self.assertEqual(manifest_data["root_issue_context"]["extra_fields_collected"], ["customfield_12345"])

        # Also verify root_issue_context in the return data
        self.assertIn("root_issue_context", result.data)
        self.assertEqual(result.data["root_issue_context"]["version"], 1)
        self.assertEqual(result.data["root_issue_context"]["comments"]["complete"], True)
        modes = {item["issue_key"]: item["collection_mode"] for item in result.data["related_issues"]}
        self.assertEqual(modes, {"APP-99": "context", "APP-88": "attachments"})
        self.assertIn("APP-99", {item["source_issue"] for item in result.data["downloaded_attachments"]})
        self.assertIn("open_case", result.data["next_step"])

    def test_repeated_export_reuses_matching_attachments(self):
        attachment_requests: list[str] = []

        def counting_transport(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/attachment/"):
                attachment_requests.append(request.url.path)
            return jira_transport(request)

        client = JiraClient(self.config, transport=httpx.MockTransport(counting_transport))
        service = JiraService(client, CaseExporter(self.config, client))
        try:
            first = service.dispatch("export_issue_case", {"issue_key": "APP-42"})
            requests_after_first = len(attachment_requests)
            second = service.dispatch("export_issue_case", {"issue_key": "APP-42"})
        finally:
            client.close()

        self.assertTrue(first.success and second.success)
        self.assertEqual(requests_after_first, 3)
        self.assertEqual(len(attachment_requests), requests_after_first)
        self.assertEqual(second.data["downloaded_attachments"], [])
        self.assertEqual(len(second.data["reused_attachments"]), 3)
        self.assertEqual(second.data["downloaded_bytes"], 0)
        self.assertEqual(second.data["attachment_bytes"], 51)

    def test_size_mismatch_forces_attachment_redownload(self):
        attachment_requests: list[str] = []

        def counting_transport(request: httpx.Request) -> httpx.Response:
            if request.url.path.startswith("/attachment/"):
                attachment_requests.append(request.url.path)
            return jira_transport(request)

        client = JiraClient(self.config, transport=httpx.MockTransport(counting_transport))
        service = JiraService(client, CaseExporter(self.config, client))
        try:
            first = service.dispatch("export_issue_case", {"issue_key": "APP-42"})
            case = Path(first.data["case_path"])
            (case / "attachments" / "20001_logcat.txt").write_bytes(b"incomplete")
            requests_after_first = len(attachment_requests)
            second = service.dispatch("export_issue_case", {"issue_key": "APP-42"})
        finally:
            client.close()

        self.assertTrue(second.success)
        self.assertEqual(len(attachment_requests), requests_after_first + 1)
        self.assertEqual(len(second.data["downloaded_attachments"]), 1)
        self.assertEqual(len(second.data["reused_attachments"]), 2)

    def test_export_fails_when_root_comments_incomplete(self):
        """严格导出：根 Issue 评论被截断时必须失败。"""
        def truncated_transport(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/rest/api/3/issue/APP-42":
                return httpx.Response(200, json=ISSUE)
            if path == "/rest/api/3/issue/APP-42/comment":
                # 返回 5 条 total 但只给了 2 条，模拟截断
                return httpx.Response(200, json={
                    "comments": [{
                        "id": "30001", "author": {"displayName": "Carol"},
                        "body": {"type": "doc", "content": [{
                            "type": "paragraph", "content": [{"type": "text", "text": "comment 1"}],
                        }]},
                        "created": "2026-08-26T08:00:00.000+0800",
                    }, {
                        "id": "30002", "author": {"displayName": "Dave"},
                        "body": {"type": "doc", "content": [{
                            "type": "paragraph", "content": [{"type": "text", "text": "comment 2"}],
                        }]},
                        "created": "2026-08-26T09:00:00.000+0800",
                    }],
                    "total": 5,
                    "startAt": 0,
                })
            return httpx.Response(404)

        config = JiraConfig(
            base_url="https://jira.test",
            auth_mode="none",
            export_root=Path(self.temp.name),
            extra_fields=("customfield_12345",),
        )
        client = JiraClient(config, transport=httpx.MockTransport(truncated_transport))
        service = JiraService(client, CaseExporter(config, client))
        try:
            result = service.dispatch("export_issue_case", {"issue_key": "APP-42", "max_comments": 2})
        finally:
            client.close()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INCOMPLETE_COMMENTS")

    def test_comment_pagination_fails_when_page_makes_no_progress(self):
        def no_progress_transport(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/3/issue/APP-42":
                return httpx.Response(200, json=ISSUE)
            if request.url.path == "/rest/api/3/issue/APP-42/comment":
                return httpx.Response(200, json={"comments": [], "total": 1, "startAt": 0})
            return httpx.Response(404)

        client = JiraClient(self.config, transport=httpx.MockTransport(no_progress_transport))
        service = JiraService(client, CaseExporter(self.config, client))
        try:
            result = service.dispatch("collect_issue_context", {"issue_key": "APP-42"})
        finally:
            client.close()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "COMMENTS_PAGINATION_NO_PROGRESS")
        self.assertTrue(result.retryable)

    def test_comment_pagination_rejects_duplicate_ids(self):
        comment = {
            "id": "30001", "author": {"displayName": "Carol"},
            "body": "same", "created": "2026-08-26T08:00:00.000+0800",
        }

        def duplicate_transport(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/3/issue/APP-42":
                return httpx.Response(200, json=ISSUE)
            if request.url.path == "/rest/api/3/issue/APP-42/comment":
                start = int(request.url.params.get("startAt", "0"))
                return httpx.Response(200, json={"comments": [comment], "total": 2, "startAt": start})
            return httpx.Response(404)

        client = JiraClient(self.config, transport=httpx.MockTransport(duplicate_transport))
        service = JiraService(client, CaseExporter(self.config, client))
        try:
            result = service.dispatch("collect_issue_context", {"issue_key": "APP-42"})
        finally:
            client.close()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "COMMENTS_PAGINATION_INCONSISTENT")
        self.assertTrue(result.retryable)

    def test_comment_pagination_rejects_total_changes(self):
        def changing_total_transport(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/rest/api/3/issue/APP-42":
                return httpx.Response(200, json=ISSUE)
            if request.url.path == "/rest/api/3/issue/APP-42/comment":
                start = int(request.url.params.get("startAt", "0"))
                comment = {
                    "id": str(30001 + start), "author": {"displayName": "Carol"},
                    "body": "page", "created": "2026-08-26T08:00:00.000+0800",
                }
                return httpx.Response(200, json={
                    "comments": [comment], "total": 2 if start == 0 else 3, "startAt": start,
                })
            return httpx.Response(404)

        client = JiraClient(self.config, transport=httpx.MockTransport(changing_total_transport))
        service = JiraService(client, CaseExporter(self.config, client))
        try:
            result = service.dispatch("collect_issue_context", {"issue_key": "APP-42"})
        finally:
            client.close()
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "COMMENTS_PAGINATION_INCONSISTENT")
        self.assertTrue(result.retryable)

    def test_invalid_issue_key_is_a_contract_error(self):
        result = self.service.dispatch("get_issue", {"issue_key": "../../secret"})
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "INVALID_PARAMS")


class JiraHttpErrorTest(unittest.TestCase):
    def test_client_does_not_inherit_ambient_proxy_by_default(self):
        config = JiraConfig(base_url="https://jira.test", auth_mode="none")
        client = JiraClient(config, transport=httpx.MockTransport(jira_transport))
        try:
            self.assertFalse(client.http.trust_env)
        finally:
            client.close()

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
