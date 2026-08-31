"""面向 Agent 的 Jira 工具语义层。

调用关系是 MCP Server → JiraService → JiraClient/CaseExporter。Service 可被
单元测试或其他 Runtime 直接复用，因此不依赖 MCP SDK。
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import ValidationError

from .client import JiraClient
from .domain import (
    CollectIssueContextInput,
    ExportIssueCaseInput,
    GetCommentsInput,
    GetIssueInput,
    ListAttachmentsInput,
    SearchIssuesInput,
    ToolResult,
)
from .errors import JiraApiError, failure, success
from .exporter import CaseExporter


class JiraService:
    def __init__(self, client: JiraClient, exporter: CaseExporter):
        self.client = client
        self.exporter = exporter
        self.handlers: dict[str, Callable[..., ToolResult]] = {
            "test_connection": self.test_connection,
            "get_issue": self.get_issue,
            "collect_issue_context": self.collect_issue_context,
            "search_issues": self.search_issues,
            "get_comments": self.get_comments,
            "list_attachments": self.list_attachments,
            "export_issue_case": self.export_issue_case,
        }

    def test_connection(self, **kwargs) -> ToolResult:
        if kwargs:
            return failure("INVALID_PARAMS", "test_connection 不接受参数")
        return success({"server": self.client.get_server_info()})

    def dispatch(self, name: str, arguments: dict) -> ToolResult:
        handler = self.handlers.get(name)
        if handler is None:
            return failure("NOT_IMPLEMENTED", f"未知工具: {name}")
        try:
            return handler(**arguments)
        except ValidationError as exc:
            return failure("INVALID_PARAMS", exc.json(include_url=False))
        except JiraApiError as exc:
            return failure(exc.code, exc.message, exc.retryable)
        except OSError as exc:
            return failure("FILE_WRITE_ERROR", str(exc), True)

    def get_issue(self, **kwargs) -> ToolResult:
        params = GetIssueInput.model_validate(kwargs)
        issue = self.client.get_issue(params.issue_key.upper(), params.include_comments)
        return success({"issue": issue.model_dump()})

    def collect_issue_context(self, **kwargs) -> ToolResult:
        params = CollectIssueContextInput.model_validate(kwargs)
        issue, comment_collection = self.client.collect_issue_context(
            params.issue_key.upper(), params.include_comments, params.max_comments,
        )
        return success({
            "issue": issue.model_dump(),
            "collection": {
                "total": comment_collection.total,
                "collected": comment_collection.collected,
                "truncated": comment_collection.truncated,
                "complete": comment_collection.complete,
                "attachments_listed": len(issue.attachments),
                "extra_fields_collected": list(issue.extra_fields),
            },
        })

    def search_issues(self, **kwargs) -> ToolResult:
        params = SearchIssuesInput.model_validate(kwargs)
        page = self.client.search_issues(params.jql, params.max_results, params.cursor)
        return success({
            "items": [issue.model_dump(exclude={"attachments", "comments", "description"}) for issue in page["items"]],
            "item_count": len(page["items"]),
            "next_cursor": page["next_cursor"],
            "is_last": page["is_last"],
        })

    def get_comments(self, **kwargs) -> ToolResult:
        params = GetCommentsInput.model_validate(kwargs)
        page = self.client.get_comments(params.issue_key.upper(), params.max_results, params.start_at)
        return success({
            "items": [comment.model_dump() for comment in page["items"]],
            "item_count": len(page["items"]),
            "next_start_at": page["next_start_at"],
            "total": page["total"],
        })

    def list_attachments(self, **kwargs) -> ToolResult:
        params = ListAttachmentsInput.model_validate(kwargs)
        issue = self.client.get_issue(params.issue_key.upper(), include_comments=False)
        return success({
            "issue_key": issue.key,
            "items": [item.model_dump() for item in issue.attachments],
            "item_count": len(issue.attachments),
        })

    def export_issue_case(self, **kwargs) -> ToolResult:
        params = ExportIssueCaseInput.model_validate(kwargs)
        return success(self.exporter.export(
            params.issue_key.upper(),
            params.max_comments,
            params.include_attachments,
            params.attachment_ids,
            params.max_attachments,
            params.include_related_issues,
            params.include_related_attachments,
            params.related_depth,
            params.max_related_issues,
            params.max_related_attachments,
        ))
