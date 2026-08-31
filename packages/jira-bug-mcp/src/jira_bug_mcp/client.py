"""Jira REST API 适配器。

它把 Cloud v3 与 Data Center v2 的响应归一化为领域模型。Service 不需要
知道 HTTP、认证或 Atlassian Document Format（ADF）的细节。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from .config import JiraConfig
from .domain import JiraAttachment, JiraComment, JiraCommentCollection, JiraIssue, JiraIssueLink
from .errors import JiraApiError


def flatten_adf(value: Any) -> str:
    """将 Cloud 常见的 ADF 文档降级成适合 Agent 阅读的纯文本。"""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(flatten_adf(item) for item in value)
    if not isinstance(value, dict):
        return str(value)
    node_type = value.get("type")
    if node_type == "text":
        return str(value.get("text", ""))
    content = value.get("content", [])
    text = "".join(flatten_adf(item) for item in content)
    # 块级节点换行，避免标题、段落、列表被粘成一行。
    if node_type in {"paragraph", "heading", "listItem", "blockquote", "codeBlock"}:
        return text.rstrip() + "\n"
    if node_type == "hardBreak":
        return "\n"
    return text


def _display_name(user: Any) -> str | None:
    if not isinstance(user, dict):
        return None
    return user.get("displayName") or user.get("name") or user.get("emailAddress")


def _named_values(value: Any) -> list[str]:
    """提取 Jira 常见 name 列表字段，对异常数据保持容错。"""

    if not isinstance(value, list):
        return []
    return [str(item.get("name")) for item in value if isinstance(item, dict) and item.get("name")]


def _extra_value(value: Any) -> Any:
    """将白名单自定义字段转成可序列化、可阅读的值。"""

    if isinstance(value, dict) and value.get("type") == "doc":
        return flatten_adf(value).strip()
    if isinstance(value, dict):
        # select/user/version 等字段优先保留人类可读值。
        for key in ("value", "displayName", "name", "key"):
            if value.get(key) is not None:
                return value[key]
        return {str(key): _extra_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_extra_value(item) for item in value]
    return value


class JiraClient:
    """同步 Jira Client；允许注入 MockTransport，便于无网络单元测试。"""

    def __init__(self, config: JiraConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        headers = {"Accept": "application/json", "User-Agent": "jira-bug-mcp/0.1"}
        auth = None
        if config.auth_mode == "basic":
            if not config.user or not config.token:
                raise ValueError("basic 认证需要 JIRA_USER 和 JIRA_TOKEN")
            auth = httpx.BasicAuth(config.user, config.token)
        elif config.auth_mode == "bearer":
            if not config.token:
                raise ValueError("bearer 认证需要 JIRA_TOKEN")
            headers["Authorization"] = f"Bearer {config.token}"
        self.http = httpx.Client(
            base_url=config.base_url,
            headers=headers,
            auth=auth,
            verify=config.verify_ssl,
            trust_env=config.trust_env,
            timeout=config.timeout_seconds,
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self.http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            detail = str(exc).strip()
            lowered = detail.lower()
            if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
                message = "Jira TLS 证书校验失败；请配置公司 CA，不要在生产环境关闭 SSL 校验"
                code = "TLS_VERIFY_FAILED"
            else:
                safe_detail = f": {detail}" if detail else ""
                message = f"无法连接 Jira: {type(exc).__name__}{safe_detail}"
                code = "NETWORK_ERROR"
            raise JiraApiError(code, message, True) from exc
        if response.is_success:
            return response
        mapping = {
            400: ("INVALID_REQUEST", "Jira 拒绝了请求", False),
            401: ("AUTH_FAILED", "Jira 认证失败，请检查账号或 Token", False),
            403: ("FORBIDDEN", "当前 Jira 账号无权访问该资源", False),
            404: ("NOT_FOUND", "Jira 资源不存在或当前账号不可见", False),
            429: ("RATE_LIMITED", "Jira 请求频率受限", True),
        }
        code, message, retryable = mapping.get(
            response.status_code,
            ("JIRA_UNAVAILABLE", f"Jira 返回 HTTP {response.status_code}", response.status_code >= 500),
        )
        raise JiraApiError(code, message, retryable)

    def _json(self, method: str, path: str, **kwargs) -> dict:
        response = self._request(method, path, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise JiraApiError("INVALID_RESPONSE", "Jira 返回了无效 JSON", True) from exc
        if not isinstance(data, dict):
            raise JiraApiError("INVALID_RESPONSE", "Jira JSON 顶层不是对象", True)
        return data

    @staticmethod
    def _attachment(raw: dict) -> JiraAttachment:
        return JiraAttachment(
            attachment_id=str(raw.get("id", "")),
            filename=str(raw.get("filename", "unnamed")),
            size_bytes=max(0, int(raw.get("size") or 0)),
            media_type=raw.get("mimeType"),
            created_at=raw.get("created"),
            author=_display_name(raw.get("author")),
            content_url=raw.get("content"),
        )

    @staticmethod
    def _comment(raw: dict) -> JiraComment:
        return JiraComment(
            comment_id=str(raw.get("id", "")),
            author=_display_name(raw.get("author")),
            body=flatten_adf(raw.get("body")).strip(),
            created_at=raw.get("created"),
            updated_at=raw.get("updated"),
        )

    def _issue(self, raw: dict, include_comments: bool = True) -> JiraIssue:
        fields = raw.get("fields") or {}
        comments_raw = (fields.get("comment") or {}).get("comments", []) if include_comments else []
        links: list[JiraIssueLink] = []
        for raw_link in fields.get("issuelinks") or []:
            link_type = raw_link.get("type") or {}
            for direction, issue_name, description_name in (
                ("outward", "outwardIssue", "outward"),
                ("inward", "inwardIssue", "inward"),
            ):
                target = raw_link.get(issue_name)
                if not isinstance(target, dict):
                    continue
                target_fields = target.get("fields") or {}
                links.append(JiraIssueLink(
                    link_type=str(link_type.get("name") or "link"),
                    direction=direction,
                    description=link_type.get(description_name),
                    target_key=str(target.get("key") or ""),
                    target_summary=target_fields.get("summary"),
                    target_status=(target_fields.get("status") or {}).get("name"),
                ))
        parent = fields.get("parent") or {}
        return JiraIssue(
            issue_id=str(raw.get("id", "")),
            key=str(raw.get("key", "")),
            summary=str(fields.get("summary") or ""),
            description=flatten_adf(fields.get("description")).strip(),
            issue_type=(fields.get("issuetype") or {}).get("name"),
            status=(fields.get("status") or {}).get("name"),
            priority=(fields.get("priority") or {}).get("name"),
            assignee=_display_name(fields.get("assignee")),
            reporter=_display_name(fields.get("reporter")),
            labels=list(fields.get("labels") or []),
            components=[item.get("name", "") for item in fields.get("components") or []],
            environment=flatten_adf(fields.get("environment")).strip(),
            resolution=(fields.get("resolution") or {}).get("name"),
            versions=_named_values(fields.get("versions")),
            fix_versions=_named_values(fields.get("fixVersions")),
            parent_key=parent.get("key"),
            subtask_keys=[
                str(item.get("key")) for item in fields.get("subtasks") or []
                if isinstance(item, dict) and item.get("key")
            ],
            issue_links=links,
            created_at=fields.get("created"),
            updated_at=fields.get("updated"),
            attachments=[self._attachment(item) for item in fields.get("attachment") or []],
            comments=[self._comment(item) for item in comments_raw],
            extra_fields={
                field: _extra_value(fields.get(field))
                for field in self.config.extra_fields
                if field in fields
            },
        )

    def get_issue(self, issue_key: str, include_comments: bool = True) -> JiraIssue:
        fields = [
            "summary", "description", "issuetype", "status", "priority", "assignee",
            "reporter", "labels", "components", "environment", "resolution", "versions",
            "fixVersions", "parent", "subtasks", "issuelinks", "created", "updated", "attachment",
        ]
        fields.extend(self.config.extra_fields)
        if include_comments:
            fields.append("comment")
        data = self._json(
            "GET",
            f"/rest/api/{self.config.api_version}/issue/{quote(issue_key, safe='')}",
            params={"fields": ",".join(fields)},
        )
        return self._issue(data, include_comments)

    def get_issue_attachment_source(self, issue_key: str) -> JiraIssue:
        """只读取关联 Issue 的摘要与附件元数据。

        评论中的 Jira Key 常只是大日志存放位置，不应因此收集
        对方描述、评论和整个关联图。
        """

        data = self._json(
            "GET",
            f"/rest/api/{self.config.api_version}/issue/{quote(issue_key, safe='')}",
            params={"fields": "summary,attachment"},
        )
        return self._issue(data, include_comments=False)

    def collect_issue_context(
        self,
        issue_key: str,
        include_comments: bool = True,
        max_comments: int = 1000,
    ) -> tuple[JiraIssue, JiraCommentCollection]:
        """收集一个 Issue 的完整静态上下文。

        get_issue 中内嵌的 comment 可能被 Jira 截断，因此聚合工具
        单独分页读取，并返回可供导出和 Worker 验证的完整性元数据。
        """

        issue = self.get_issue(issue_key, include_comments=False)
        if not include_comments or max_comments == 0:
            return issue, JiraCommentCollection(
                requested=False,
                total=None,
                collected=0,
                truncated=False,
                complete=False,
                max_comments=max_comments,
            )
        comments: list[JiraComment] = []
        start_at = 0
        truncated = False
        total: int | None = None
        while len(comments) < max_comments:
            page = self.get_comments(issue_key, min(100, max_comments - len(comments)), start_at)
            comments.extend(page["items"])
            total = page["total"]
            next_start = page["next_start_at"]
            if next_start is None:
                break
            if len(comments) >= max_comments:
                truncated = True
                break
            start_at = next_start
        issue.comments = comments
        complete = total is not None and not truncated and len(comments) == total
        return issue, JiraCommentCollection(
            requested=True,
            total=total,
            collected=len(comments),
            truncated=truncated or not complete,
            complete=complete,
            max_comments=max_comments,
        )

    def get_server_info(self) -> dict:
        """验证 Jira 网络、认证与 REST 版本，不读取业务 Issue。"""

        data = self._json("GET", f"/rest/api/{self.config.api_version}/serverInfo")
        # serverInfo 在部分部署中允许匿名访问；myself 才能真正校验 Token。
        myself = self._json("GET", f"/rest/api/{self.config.api_version}/myself")
        return {
            "base_url": self.config.base_url,
            "deployment": self.config.deployment,
            "server_title": data.get("serverTitle"),
            "version": data.get("version"),
            "deployment_type": data.get("deploymentType"),
            "authenticated_user": _display_name(myself),
            "account_id": myself.get("accountId") or myself.get("key") or myself.get("name"),
        }

    def search_issues(self, jql: str, max_results: int, cursor: str | None = None) -> dict:
        fields = ["summary", "issuetype", "status", "priority", "assignee", "updated"]
        if self.config.deployment == "cloud":
            body: dict[str, Any] = {"jql": jql, "maxResults": max_results, "fields": fields}
            if cursor:
                body["nextPageToken"] = cursor
            data = self._json("POST", "/rest/api/3/search/jql", json=body)
            next_cursor = data.get("nextPageToken")
        else:
            try:
                start_at = int(cursor or "0")
            except ValueError as exc:
                raise JiraApiError("INVALID_CURSOR", "Data Center cursor 必须是整数", False) from exc
            data = self._json(
                "POST", "/rest/api/2/search",
                json={"jql": jql, "maxResults": max_results, "startAt": start_at, "fields": fields},
            )
            consumed = start_at + len(data.get("issues") or [])
            next_cursor = str(consumed) if consumed < int(data.get("total") or 0) else None
        issues = [self._issue(item, include_comments=False) for item in data.get("issues") or []]
        return {"items": issues, "next_cursor": next_cursor, "is_last": not bool(next_cursor)}

    def get_comments(self, issue_key: str, max_results: int, start_at: int = 0) -> dict:
        data = self._json(
            "GET",
            f"/rest/api/{self.config.api_version}/issue/{quote(issue_key, safe='')}/comment",
            params={"maxResults": max_results, "startAt": start_at},
        )
        comments = [self._comment(item) for item in data.get("comments") or []]
        consumed = start_at + len(comments)
        total = int(data.get("total") or consumed)
        return {"items": comments, "next_start_at": consumed if consumed < total else None, "total": total}

    def download_attachment(self, attachment: JiraAttachment, destination: Path) -> int:
        """流式下载单个附件，并在超过预算时立即中止。"""

        configured = urlparse(self.config.base_url)
        content_url = attachment.content_url or (
            f"/rest/api/{self.config.api_version}/attachment/content/{quote(attachment.attachment_id, safe='')}"
        )
        resolved = urlparse(urljoin(self.config.base_url + "/", content_url))
        if (resolved.scheme, resolved.netloc) != (configured.scheme, configured.netloc):
            raise JiraApiError("UNSAFE_ATTACHMENT_URL", "附件下载地址不属于已配置的 Jira", False)
        part = destination.with_name(destination.name + ".part")
        written = 0
        try:
            try:
                with self.http.stream("GET", resolved.geturl(), headers={"Accept": "*/*"}) as response:
                    if not response.is_success:
                        # 复用统一状态语义，同时避免为了映射错误而再次发起请求。
                        if response.status_code == 401:
                            raise JiraApiError("AUTH_FAILED", "Jira 附件认证失败", False)
                        raise JiraApiError("ATTACHMENT_DOWNLOAD_FAILED", f"附件下载返回 HTTP {response.status_code}", response.status_code >= 500)
                    length = int(response.headers.get("content-length", "0") or 0)
                    if length > self.config.max_attachment_bytes:
                        raise JiraApiError("ATTACHMENT_TOO_LARGE", "附件超过单文件大小限制", False)
                    with part.open("wb") as handle:
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if written > self.config.max_attachment_bytes:
                                raise JiraApiError("ATTACHMENT_TOO_LARGE", "附件超过单文件大小限制", False)
                            handle.write(chunk)
            except httpx.TransportError as exc:
                raise JiraApiError("NETWORK_ERROR", f"Jira 附件传输中断: {type(exc).__name__}", True) from exc
            part.replace(destination)
            return written
        finally:
            if part.exists():
                part.unlink()
