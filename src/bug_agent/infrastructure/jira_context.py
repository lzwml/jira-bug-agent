"""Validate exported Jira Cases and build bounded, untrusted Agent context."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


MANIFEST_NAME = "collection-manifest.json"


class JiraCaseValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class JiraInitialContext:
    """经过导出完整性与哈希校验的根 Issue 上下文。"""

    issue_key: str
    issue: dict
    comments_total: int
    raw_text: str


def _reexport(issue_key: str | None) -> JiraCaseValidationError:
    key = issue_key or "<ISSUE_KEY>"
    return JiraCaseValidationError(
        "JIRA_CASE_REEXPORT_REQUIRED",
        f"Case 缺少评论完整性证明，请重新导出: "
        f"uv run bug-agent collect-jira {key} --export-case",
    )


def load_jira_initial_context(
    case_path: Path,
    *,
    require_jira: bool,
    max_chars: int,
) -> JiraInitialContext | None:
    """Return verified Jira description/comments, or None for a generic local Case."""

    manifest_path = case_path / MANIFEST_NAME
    issue_path = case_path / "issue.json"
    issue_markdown = case_path / "issue.md"
    # 任一 Jira 导出标志存在，都按 Jira Case 严格处理，避免损坏/残缺导出
    # 被错误降级为普通本地日志 Case 而绕过评论完整性要求。
    looks_like_jira = issue_path.is_file() or issue_markdown.is_file()
    if not manifest_path.is_file():
        if require_jira or looks_like_jira:
            raise _reexport(None)
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if require_jira or looks_like_jira:
            raise JiraCaseValidationError(
                "JIRA_CASE_MANIFEST_INVALID", "collection-manifest.json 无法读取",
            ) from exc
        return None
    if not isinstance(manifest, dict):
        raise JiraCaseValidationError("JIRA_CASE_MANIFEST_INVALID", "Manifest 必须是 JSON 对象")
    issue_key = str(manifest.get("root_issue") or "").upper() or None
    if manifest.get("source") != "jira" or manifest.get("schema_version") != 2:
        if require_jira or looks_like_jira or issue_key:
            raise _reexport(issue_key)
        return None
    if not issue_path.is_file():
        raise JiraCaseValidationError("JIRA_CASE_CONTEXT_MISSING", "导出 Case 缺少 issue.json")

    root_context = manifest.get("root_issue_context")
    if not isinstance(root_context, dict) or root_context.get("version") != 1:
        raise _reexport(issue_key)
    collection = root_context.get("comments") if isinstance(root_context, dict) else None
    if not isinstance(collection, dict):
        raise _reexport(issue_key)
    total = collection.get("total")
    collected = collection.get("collected")
    complete = collection.get("complete")
    truncated = collection.get("truncated")
    if (
        not isinstance(total, int)
        or isinstance(total, bool)
        or not isinstance(collected, int)
        or isinstance(collected, bool)
        or total < 0
        or collected < 0
        or complete is not True
        or truncated is not False
        or collected != total
    ):
        raise JiraCaseValidationError(
            "JIRA_COMMENTS_INCOMPLETE",
            f"Jira 评论未完整收集 (total={total}, collected={collected}, truncated={truncated})",
        )

    try:
        issue_bytes = issue_path.read_bytes()
        expected_hash = manifest["issue_json_sha256"]
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(char not in "0123456789abcdef" for char in expected_hash.lower())
        ):
            raise KeyError("issue_json_sha256")
        if hashlib.sha256(issue_bytes).hexdigest() != expected_hash:
            raise JiraCaseValidationError("JIRA_CASE_CONTEXT_TAMPERED", "issue.json 与 Manifest 哈希不一致")
        issue = json.loads(issue_bytes.decode("utf-8"))
    except JiraCaseValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as exc:
        raise JiraCaseValidationError("JIRA_CASE_CONTEXT_INVALID", "issue.json 无法验证") from exc
    if not isinstance(issue, dict):
        raise JiraCaseValidationError("JIRA_CASE_CONTEXT_INVALID", "issue.json 必须是 JSON 对象")
    actual_key = str(issue.get("key") or "").upper()
    comments = issue.get("comments")
    comment_ids = [
        str(item.get("comment_id") or "") if isinstance(item, dict) else ""
        for item in comments
    ] if isinstance(comments, list) else []
    if (
        actual_key != issue_key
        or not isinstance(comments, list)
        or len(comments) != collected
        or any(not comment_id for comment_id in comment_ids)
        or len(set(comment_ids)) != len(comment_ids)
    ):
        raise JiraCaseValidationError(
            "JIRA_CASE_CONTEXT_MISMATCH",
            "Issue Key、评论数或 comment_id 与 Manifest 不一致",
        )

    raw_text = json.dumps({
        "issue_key": actual_key,
        "summary": issue.get("summary"),
        "description": issue.get("description"),
        "environment": issue.get("environment"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "comments": comments,
    }, ensure_ascii=False, indent=2)
    context_size = len(raw_text)
    if context_size > max_chars:
        raise JiraCaseValidationError(
            "JIRA_CONTEXT_TOO_LARGE",
            f"完整 Jira 描述与评论共 {context_size} 字符，超过读取上限 {max_chars}",
        )
    return JiraInitialContext(actual_key, issue, total, raw_text)
