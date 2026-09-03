"""把 Jira Issue 导出为 Log MCP 可注册的本地 Case 目录。"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path

from .client import JiraClient
from .config import JiraConfig
from .domain import JiraAttachment, JiraIssue
from .errors import JiraApiError


ISSUE_KEY_PATTERN = re.compile(r"(?<![A-Z0-9_])([A-Z][A-Z0-9_]{1,30}-\d+)(?![A-Z0-9_])", re.IGNORECASE)

# Jira 附件链接格式：https://<host>/secure/attachment/<id>/<filename>
# 或 /secure/attachment/<id>/ 等相对路径
ATTACHMENT_URL_PATTERN = re.compile(
    r"(?:https?://[^/\s]+)?/secure/attachment/(\d+)(?:/[^\s]*)?",
    re.IGNORECASE,
)


def _comment_attachment_ids(issue: JiraIssue) -> list[str]:
    """从评论正文中提取 Jira 附件 ID（不在 issue.attachments 面板中的）。

    测试、运营等角色常把日志附件链接直接贴到评论正文中，
    这些附件不在 Jira 的 "attachment" 字段中，需要单独提取并下载。
    """
    seen = {item.attachment_id for item in issue.attachments}
    found: list[str] = []
    for comment in issue.comments:
        for match in ATTACHMENT_URL_PATTERN.finditer(comment.body):
            aid = match.group(1)
            if aid not in seen:
                seen.add(aid)
                found.append(aid)
    return found


def _safe_filename(value: str) -> str:
    """去掉路径和 Windows 非法字符，附件名永远不能控制导出位置。"""

    name = Path(value.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "unnamed")[:180]


def _atomic_text(path: Path, text: str) -> None:
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    part.replace(path)


def _related_references(issue: JiraIssue) -> list[dict[str, str]]:
    """从结构化关联、描述和评论中提取同 Jira Issue Key。"""

    references: list[dict[str, str]] = []

    def add(key: str | None, source: str) -> None:
        normalized = (key or "").upper()
        if normalized and normalized != issue.key.upper():
            item = {"from_issue": issue.key.upper(), "to_issue": normalized, "source": source}
            if item not in references:
                references.append(item)

    add(issue.parent_key, "parent")
    for key in issue.subtask_keys:
        add(key, "subtask")
    for link in issue.issue_links:
        add(link.target_key, f"issue_link:{link.link_type}:{link.direction}")
    for match in ISSUE_KEY_PATTERN.finditer(issue.description):
        add(match.group(1), "description")
    for comment in issue.comments:
        for match in ISSUE_KEY_PATTERN.finditer(comment.body):
            add(match.group(1), f"comment:{comment.comment_id}")
    return references


def _collection_mode(reference: dict[str, str]) -> str:
    """正式结构关联收集上下文；纯文本引用只当作附件指针。"""

    source = reference["source"]
    return "attachments" if source == "description" or source.startswith("comment:") else "context"


def _markdown(issue: JiraIssue) -> str:
    """生成适合人和模型共同阅读的 Case 首页。"""

    lines = [
        f"# {issue.key}: {issue.summary}", "",
        f"- 类型：{issue.issue_type or '-'}",
        f"- 状态：{issue.status or '-'}",
        f"- 优先级：{issue.priority or '-'}",
        f"- 经办人：{issue.assignee or '-'}",
        f"- 报告人：{issue.reporter or '-'}",
        f"- 解决状态：{issue.resolution or '-'}",
        f"- 影响版本：{', '.join(issue.versions) or '-'}",
        f"- 修复版本：{', '.join(issue.fix_versions) or '-'}",
        f"- 模块：{', '.join(issue.components) or '-'}",
        f"- 标签：{', '.join(issue.labels) or '-'}",
        f"- 父任务：{issue.parent_key or '-'}",
        f"- 创建：{issue.created_at or '-'}",
        f"- 更新：{issue.updated_at or '-'}", "",
        "## 描述", "", issue.description or "（无描述）", "",
        "## 环境", "", issue.environment or "（无环境信息）", "",
        "## 关联信息", "",
    ]
    if issue.subtask_keys:
        lines.append(f"- 子任务：{', '.join(issue.subtask_keys)}")
    if issue.issue_links:
        lines.extend(
            f"- {link.description or link.link_type} `{link.target_key}`"
            f" {link.target_summary or ''} [{link.target_status or '-'}]"
            for link in issue.issue_links
        )
    if not issue.subtask_keys and not issue.issue_links:
        lines.append("（无关联 Issue）")
    if issue.extra_fields:
        lines.extend(["", "## 自定义字段", ""])
        lines.extend(
            f"- `{name}`: {json.dumps(value, ensure_ascii=False, default=str)}"
            for name, value in issue.extra_fields.items()
        )
    lines.extend(["", "## 评论", ""])
    if issue.comments:
        for comment in issue.comments:
            lines.extend([
                f"### {comment.author or '未知用户'} · {comment.created_at or '-'}",
                "", comment.body or "（空评论）", "",
            ])
    else:
        lines.extend(["（无评论）", ""])
    lines.extend(["## 附件", ""])
    if issue.attachments:
        lines.extend(
            f"- `{item.attachment_id}` {item.filename} ({item.size_bytes} bytes)"
            for item in issue.attachments
        )
    else:
        lines.append("（无附件）")
    lines.append("")
    return "\n".join(lines)


class CaseExporter:
    """受预算与根目录约束的 Jira → Case 桥接器。"""

    def __init__(self, config: JiraConfig, client: JiraClient):
        self.config = config
        self.client = client

    def export(
        self,
        issue_key: str,
        max_comments: int,
        include_attachments: bool,
        attachment_ids: list[str],
        max_attachments: int,
        include_related_issues: bool = True,
        include_related_attachments: bool = True,
        related_depth: int = 1,
        max_related_issues: int = 10,
        max_related_attachments: int = 30,
    ) -> dict:
        # 复用聚合收集逻辑，确保 MCP 查询与本地 Case 中的字段语义一致。
        issue, root_comment_collection = self.client.collect_issue_context(
            issue_key, include_comments=True, max_comments=max_comments,
        )
        # 严格导出：根 Issue 评论不完整则拒绝导出，避免 Agent 基于截断数据做出错误判断。
        if root_comment_collection.requested and not root_comment_collection.complete:
            raise JiraApiError(
                "INCOMPLETE_COMMENTS",
                f"根 Issue {issue.key} 评论收集不完整 "
                f"(total={root_comment_collection.total}, collected={root_comment_collection.collected}, "
                f"truncated={root_comment_collection.truncated})；请提高 max_comments 后重试。",
                True,
            )
        root = self.config.export_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        case_dir = (root / issue.key.upper()).resolve()
        if os.path.commonpath([str(root), str(case_dir)]) != str(root):
            raise JiraApiError("UNSAFE_EXPORT_PATH", "Issue Key 生成了不安全的导出路径", False)
        case_dir.mkdir(parents=True, exist_ok=True)

        issue_json = case_dir / "issue.json"
        issue_md = case_dir / "issue.md"
        issue_json_text = json.dumps(issue.model_dump(), ensure_ascii=False, indent=2)
        _atomic_text(issue_json, issue_json_text)
        _atomic_text(issue_md, _markdown(issue))
        exported = [issue_json.name, issue_md.name]
        downloaded: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        # total 是本次 Case 中纳入预算的附件总量；network_downloaded 只统计
        # 本次真正通过 Jira HTTP 传输的字节。
        total = 0
        network_downloaded = 0
        related_attachment_count = 0

        def download_attachments(
            source_issue: JiraIssue,
            attachments_dir: Path,
            candidates: list[JiraAttachment],
            limit: int,
            enabled: bool,
        ) -> None:
            nonlocal total, network_downloaded, related_attachment_count
            if len(candidates) > limit:
                skipped.extend({
                    "source_issue": source_issue.key,
                    "attachment_id": item.attachment_id,
                    "filename": item.filename,
                    "reason": "超过附件数量上限",
                } for item in candidates[limit:])
                candidates = candidates[:limit]
            if not enabled:
                skipped.extend({
                    "source_issue": source_issue.key,
                    "attachment_id": item.attachment_id,
                    "filename": item.filename,
                    "reason": "附件下载已关闭",
                } for item in candidates)
                return
            if not candidates:
                return
            attachments_dir.mkdir(parents=True, exist_ok=True)
            for attachment in candidates:
                if attachment.size_bytes > self.config.max_attachment_bytes:
                    skipped.append({
                        "source_issue": source_issue.key,
                        "attachment_id": attachment.attachment_id,
                        "filename": attachment.filename,
                        "size_bytes": attachment.size_bytes,
                        "limit_bytes": self.config.max_attachment_bytes,
                        "reason": "超过单附件大小限制",
                    })
                    continue
                if total + attachment.size_bytes > self.config.max_export_bytes:
                    skipped.append({
                        "source_issue": source_issue.key,
                        "attachment_id": attachment.attachment_id,
                        "filename": attachment.filename,
                        "size_bytes": attachment.size_bytes,
                        "downloaded_bytes_before": total,
                        "limit_bytes": self.config.max_export_bytes,
                        "reason": "超过单次导出总大小限制",
                    })
                    continue
                filename = f"{_safe_filename(attachment.attachment_id)}_{_safe_filename(attachment.filename)}"
                target = attachments_dir / filename
                if target.exists() and not target.is_symlink():
                    if not target.is_file():
                        raise JiraApiError("EXPORT_TARGET_CONFLICT", f"附件目标不是普通文件: {filename}", False)
                    existing_size = target.stat().st_size
                    if existing_size == attachment.size_bytes:
                        total += existing_size
                        relative = target.relative_to(case_dir).as_posix()
                        exported.append(relative)
                        reused.append({
                            "source_issue": source_issue.key,
                            "attachment_id": attachment.attachment_id,
                            "filename": attachment.filename,
                            "relative_path": relative,
                            "size_bytes": existing_size,
                            "reused": True,
                        })
                        if source_issue.key.upper() != issue.key.upper():
                            related_attachment_count += 1
                        continue
                size = self.client.download_attachment(attachment, target)
                # Jira 元数据中的 size 不能作为安全边界；以实际写入字节复核。
                if total + size > self.config.max_export_bytes:
                    target.unlink(missing_ok=True)
                    skipped.append({
                        "source_issue": source_issue.key,
                        "attachment_id": attachment.attachment_id,
                        "filename": attachment.filename,
                        "size_bytes": size,
                        "downloaded_bytes_before": total,
                        "limit_bytes": self.config.max_export_bytes,
                        "reason": "实际下载大小超过单次导出总限制",
                    })
                    continue
                total += size
                network_downloaded += size
                relative = target.relative_to(case_dir).as_posix()
                exported.append(relative)
                downloaded.append({
                    "source_issue": source_issue.key,
                    "attachment_id": attachment.attachment_id,
                    "filename": attachment.filename,
                    "relative_path": relative,
                    "size_bytes": size,
                    "reused": False,
                })
                if source_issue.key.upper() != issue.key.upper():
                    related_attachment_count += 1

        selected_ids = set(attachment_ids)
        root_candidates = [
            item for item in issue.attachments
            if not selected_ids or item.attachment_id in selected_ids
        ]
        download_attachments(issue, case_dir / "attachments", root_candidates, max_attachments, include_attachments)

        # 从评论正文中提取附件链接（测试/运营常把日志贴到评论里而非附件面板）。
        comment_attachment_ids = _comment_attachment_ids(issue)
        if comment_attachment_ids and include_attachments:
            comment_attachments: list[JiraAttachment] = []
            comment_fetch_skipped: list[dict] = []
            for aid in comment_attachment_ids:
                try:
                    att = self.client.get_attachment_meta(aid)
                    comment_attachments.append(att)
                except JiraApiError as exc:
                    comment_fetch_skipped.append({
                        "attachment_id": aid,
                        "reason": exc.code,
                        "detail": str(exc),
                    })
            if comment_attachments:
                download_attachments(
                    issue, case_dir / "attachments", comment_attachments,
                    max(0, max_attachments - len(root_candidates)),
                    include_attachments,
                )
            if comment_fetch_skipped:
                skipped.extend(comment_fetch_skipped)

        relationships: list[dict[str, str]] = []
        related_issues: list[dict] = []
        related_skipped: list[dict] = []
        seen = {issue.key.upper()}
        queue: list[tuple[str, int, dict[str, str]]] = []
        if include_related_issues and related_depth > 0:
            for reference in _related_references(issue):
                relationships.append(reference)
                queue.append((reference["to_issue"], 1, reference))

        while queue and len(related_issues) < max_related_issues:
            related_key, depth, discovery = queue.pop(0)
            if related_key in seen:
                continue
            seen.add(related_key)
            collection_mode = _collection_mode(discovery)
            try:
                if collection_mode == "attachments":
                    related = self.client.get_issue_attachment_source(related_key)
                    comment_collection = None
                else:
                    related, comment_collection = self.client.collect_issue_context(
                        related_key, include_comments=True, max_comments=max_comments,
                    )
            except JiraApiError as exc:
                related_skipped.append({
                    "issue_key": related_key,
                    "discovered_from": discovery,
                    "reason": exc.code,
                })
                continue

            related_dir = case_dir / "related" / _safe_filename(related.key.upper())
            related_dir.mkdir(parents=True, exist_ok=True)
            if collection_mode == "context":
                related_json = related_dir / "issue.json"
                related_md = related_dir / "issue.md"
                _atomic_text(related_json, json.dumps(related.model_dump(), ensure_ascii=False, indent=2))
                _atomic_text(related_md, _markdown(related))
                exported.extend([
                    related_json.relative_to(case_dir).as_posix(),
                    related_md.relative_to(case_dir).as_posix(),
                ])
            else:
                source_manifest = related_dir / "attachment-source.json"
                _atomic_text(source_manifest, json.dumps({
                    "issue_key": related.key,
                    "summary": related.summary,
                    "collection_mode": collection_mode,
                    "discovered_from": discovery,
                    "attachments": [item.model_dump() for item in related.attachments],
                }, ensure_ascii=False, indent=2))
                exported.append(source_manifest.relative_to(case_dir).as_posix())
            related_issues.append({
                "issue_key": related.key,
                "depth": depth,
                "collection_mode": collection_mode,
                "discovered_from": discovery,
                "comment_collection": comment_collection.model_dump() if comment_collection else None,
                "attachment_count": len(related.attachments),
            })

            remaining_attachment_budget = max(0, max_related_attachments - related_attachment_count)
            download_attachments(
                related,
                related_dir / "attachments",
                list(related.attachments),
                remaining_attachment_budget,
                include_related_attachments,
            )

            # 附件指针永远不递归；只有正式问题关联才可按显式深度展开。
            if collection_mode == "context" and depth < related_depth:
                for reference in _related_references(related):
                    relationships.append(reference)
                    if reference["to_issue"] not in seen:
                        queue.append((reference["to_issue"], depth + 1, reference))

        if queue:
            related_skipped.extend({
                "issue_key": key,
                "discovered_from": discovery,
                "reason": "超过 max_related_issues",
            } for key, _depth, discovery in queue if key not in seen)

        manifest = {
            "schema_version": 2,
            "source": "jira",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "root_issue": issue.key,
            "root_issue_updated_at": issue.updated_at,
            "root_issue_context": {
                "version": 1,
                "comments": root_comment_collection.model_dump(),
                "attachments_listed": len(issue.attachments),
                "extra_fields_collected": list(issue.extra_fields),
            },
            # _atomic_text 在 Windows 上会把 \n 写成 \r\n；对最终落盘字节取哈希，
            # 确保 Worker 跨平台校验时不会误报篡改。
            "issue_json_sha256": hashlib.sha256(issue_json.read_bytes()).hexdigest(),
            "relationships": relationships,
            "related_issues": related_issues,
            "related_skipped": related_skipped,
        }
        manifest_path = case_dir / "collection-manifest.json"
        _atomic_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
        exported.append(manifest_path.name)

        return {
            "issue_key": issue.key,
            "case_path": str(case_dir),
            "exported_files": exported,
            "downloaded_attachments": downloaded,
            "reused_attachments": reused,
            "skipped_attachments": skipped,
            "downloaded_bytes": network_downloaded,
            "attachment_bytes": total,
            "root_issue_context": {
                "version": 1,
                "comments": root_comment_collection.model_dump(),
                "attachments_listed": len(issue.attachments),
                "extra_fields_collected": list(issue.extra_fields),
            },
            "related_issues": related_issues,
            "related_skipped": related_skipped,
            "relationship_count": len(relationships),
            "next_step": "将 case_path 传给 log-analyzer-mcp 的 open_case",
        }
