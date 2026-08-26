"""Jira MCP 的领域模型与工具输入契约。"""

from __future__ import annotations

from typing import Literal

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
    created_at: str | None = None
    updated_at: str | None = None
    attachments: list[JiraAttachment] = Field(default_factory=list)
    comments: list[JiraComment] = Field(default_factory=list)


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


class ExportIssueCaseInput(BaseModel):
    issue_key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
    include_attachments: bool = True
    attachment_ids: list[str] = Field(default_factory=list, max_length=100)
    max_attachments: int = Field(default=30, ge=0, le=100)
