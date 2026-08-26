"""把 Jira Issue 导出为 Log MCP 可注册的本地 Case 目录。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .client import JiraClient
from .config import JiraConfig
from .domain import JiraAttachment, JiraIssue
from .errors import JiraApiError


def _safe_filename(value: str) -> str:
    """去掉路径和 Windows 非法字符，附件名永远不能控制导出位置。"""

    name = Path(value.replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return (name or "unnamed")[:180]


def _atomic_text(path: Path, text: str) -> None:
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    part.replace(path)


def _markdown(issue: JiraIssue) -> str:
    """生成适合人和模型共同阅读的 Case 首页。"""

    lines = [
        f"# {issue.key}: {issue.summary}", "",
        f"- 类型：{issue.issue_type or '-'}",
        f"- 状态：{issue.status or '-'}",
        f"- 优先级：{issue.priority or '-'}",
        f"- 经办人：{issue.assignee or '-'}",
        f"- 报告人：{issue.reporter or '-'}",
        f"- 创建：{issue.created_at or '-'}",
        f"- 更新：{issue.updated_at or '-'}", "",
        "## 描述", "", issue.description or "（无描述）", "",
        "## 评论", "",
    ]
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
        include_attachments: bool,
        attachment_ids: list[str],
        max_attachments: int,
    ) -> dict:
        issue = self.client.get_issue(issue_key, include_comments=False)
        # Issue fields.comment 常被 Jira 截成第一页。导出 Case 时显式分页，避免
        # Agent 因缺少后续讨论而得到错误结论；同时设上限防止超大 Issue 失控。
        comments = []
        start_at = 0
        while len(comments) < 1000:
            page = self.client.get_comments(issue_key, min(100, 1000 - len(comments)), start_at)
            comments.extend(page["items"])
            if page["next_start_at"] is None:
                break
            start_at = page["next_start_at"]
        issue.comments = comments
        root = self.config.export_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        case_dir = (root / issue.key.upper()).resolve()
        if os.path.commonpath([str(root), str(case_dir)]) != str(root):
            raise JiraApiError("UNSAFE_EXPORT_PATH", "Issue Key 生成了不安全的导出路径", False)
        case_dir.mkdir(parents=True, exist_ok=True)

        issue_json = case_dir / "issue.json"
        issue_md = case_dir / "issue.md"
        _atomic_text(issue_json, json.dumps(issue.model_dump(), ensure_ascii=False, indent=2))
        _atomic_text(issue_md, _markdown(issue))
        exported = [issue_json.name, issue_md.name]
        downloaded: list[dict] = []
        skipped: list[dict] = []

        selected_ids = set(attachment_ids)
        candidates = [a for a in issue.attachments if not selected_ids or a.attachment_id in selected_ids]
        if len(candidates) > max_attachments:
            skipped.extend(
                {"attachment_id": a.attachment_id, "filename": a.filename, "reason": "超过 max_attachments"}
                for a in candidates[max_attachments:]
            )
            candidates = candidates[:max_attachments]

        total = 0
        if include_attachments and candidates:
            attachments_dir = case_dir / "attachments"
            attachments_dir.mkdir(exist_ok=True)
            for attachment in candidates:
                if attachment.size_bytes > self.config.max_attachment_bytes:
                    skipped.append({
                        "attachment_id": attachment.attachment_id,
                        "filename": attachment.filename,
                        "reason": "超过单附件大小限制",
                    })
                    continue
                if total + attachment.size_bytes > self.config.max_export_bytes:
                    skipped.append({
                        "attachment_id": attachment.attachment_id,
                        "filename": attachment.filename,
                        "reason": "超过单次导出总大小限制",
                    })
                    continue
                filename = f"{_safe_filename(attachment.attachment_id)}_{_safe_filename(attachment.filename)}"
                target = attachments_dir / filename
                size = self.client.download_attachment(attachment, target)
                # Jira 元数据中的 size 不能作为安全边界；以实际写入字节复核。
                if total + size > self.config.max_export_bytes:
                    target.unlink(missing_ok=True)
                    skipped.append({
                        "attachment_id": attachment.attachment_id,
                        "filename": attachment.filename,
                        "reason": "实际下载大小超过单次导出总限制",
                    })
                    continue
                total += size
                relative = target.relative_to(case_dir).as_posix()
                exported.append(relative)
                downloaded.append({
                    "attachment_id": attachment.attachment_id,
                    "filename": attachment.filename,
                    "relative_path": relative,
                    "size_bytes": size,
                })
        elif not include_attachments:
            skipped.extend(
                {"attachment_id": a.attachment_id, "filename": a.filename, "reason": "include_attachments=false"}
                for a in candidates
            )

        return {
            "issue_key": issue.key,
            "case_path": str(case_dir),
            "exported_files": exported,
            "downloaded_attachments": downloaded,
            "skipped_attachments": skipped,
            "downloaded_bytes": total,
            "next_step": "将 case_path 传给 log-analyzer-mcp 的 open_case",
        }
