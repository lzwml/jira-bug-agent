"""Jira MCP 的领域模型与工具输入契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    success: bool
    data: dict = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False


class JiraAttachment(BaseModel):
    attachment_id: str
    filename: str
    size_bytes: int = Field(ge=0)
    media_type: str | None = None
    created_at: str | None = None
    author: str | None = None
    # content_url 只在 JiraClient 内部使用，默认不返回给模型。
    content_url: str | None = Field(default=None, exclude=True)


class JiraComment(BaseModel):
    comment_id: str
    author: str | None = None
    body: str
    created_at: str | None = None
    updated_at: str | None = None


class JiraCommentCollection(BaseModel):
    requested: bool
    total: int | None = Field(default=None, ge=0)
    collected: int = Field(ge=0)
    truncated: bool
    complete: bool
    max_comments: int = Field(ge=0)


class JiraIssueLink(BaseModel):
    """归一化后的 Issue 关联。

    direction 从当前 Issue 的视角表达，outward 表示当前 Issue
    指向 target，inward 表示 target 指向当前 Issue。
    """

    link_type: str
    direction: Literal["outward", "inward"]
    description: str | None = None
    target_key: str
    target_summary: str | None = None
    target_status: str | None = None


class JiraIssue(BaseModel):
    issue_id: str
    key: str
    summary: str
    description: str = ""
    issue_type: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee: str | None = None
    reporter: str | None = None
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    environment: str = ""
    resolution: str | None = None
    versions: list[str] = Field(default_factory=list)
    fix_versions: list[str] = Field(default_factory=list)
    parent_key: str | None = None
    subtask_keys: list[str] = Field(default_factory=list)
    issue_links: list[JiraIssueLink] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    attachments: list[JiraAttachment] = Field(default_factory=list)
    comments: list[JiraComment] = Field(default_factory=list)
    # 只收集 JIRA_EXTRA_FIELDS 显式允许的字段，避免无边界暴露企业数据。
    extra_fields: dict[str, Any] = Field(default_factory=dict)


class GetIssueInput(BaseModel):
    issue_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
    include_comments: bool = True


class SearchIssuesInput(BaseModel):
    jql: str = Field(min_length=1, max_length=4000)
    max_results: int = Field(default=20, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1000)


class GetCommentsInput(BaseModel):
    issue_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
    max_results: int = Field(default=50, ge=1, le=100)
    start_at: int = Field(default=0, ge=0)


class ListAttachmentsInput(BaseModel):
    issue_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$")


class CollectIssueContextInput(BaseModel):
    issue_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
    include_comments: bool = True
    max_comments: int = Field(default=1000, ge=0, le=5000)


class ExportIssueCaseInput(BaseModel):
    issue_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
    max_comments: int = Field(default=5000, ge=1, le=5000)
    include_attachments: bool = True
    attachment_ids: list[str] = Field(default_factory=list, max_length=100)
    max_attachments: int = Field(default=30, ge=0, le=100)
    include_related_issues: bool = True
    include_related_attachments: bool = True
    related_depth: int = Field(default=1, ge=0, le=2)
    max_related_issues: int = Field(default=10, ge=0, le=50)
    max_related_attachments: int = Field(default=30, ge=0, le=100)
