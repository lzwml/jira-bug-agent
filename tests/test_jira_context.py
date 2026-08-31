from __future__ import annotations

import hashlib
import json

import pytest

from bug_agent.jira_context import JiraCaseValidationError, load_jira_initial_context


def write_case(path, *, comments, complete=True, collected=None, total=None):
    path.mkdir()
    issue = {
        "key": "APP-42",
        "summary": "display issue",
        "description": "screen is incomplete",
        "comments": comments,
    }
    raw = json.dumps(issue, ensure_ascii=False, indent=2).encode("utf-8")
    (path / "issue.json").write_bytes(raw)
    (path / "issue.md").write_text("# APP-42", encoding="utf-8")
    count = len(comments)
    (path / "collection-manifest.json").write_text(json.dumps({
        "schema_version": 2,
        "source": "jira",
        "root_issue": "APP-42",
        "root_issue_context": {
            "version": 1,
            "comments": {
                "total": count if total is None else total,
                "collected": count if collected is None else collected,
                "complete": complete,
                "truncated": not complete,
            },
        },
        "issue_json_sha256": hashlib.sha256(raw).hexdigest(),
    }), encoding="utf-8")


def test_verified_jira_case_returns_structured_issue(tmp_path):
    case = tmp_path / "APP-42"
    write_case(case, comments=[{"comment_id": "c1", "body": "check display"}])

    context = load_jira_initial_context(case, require_jira=False, max_chars=10000)

    assert context is not None
    assert context.issue_key == "APP-42"
    assert context.comments_total == 1
    assert context.issue["comments"][0]["comment_id"] == "c1"


def test_incomplete_comments_fail_strictly(tmp_path):
    case = tmp_path / "APP-42"
    write_case(case, comments=[{"comment_id": "c1", "body": "one"}], complete=False, total=2)

    with pytest.raises(JiraCaseValidationError, match="JIRA_COMMENTS_INCOMPLETE"):
        load_jira_initial_context(case, require_jira=False, max_chars=10000)


def test_legacy_jira_case_requires_reexport(tmp_path):
    case = tmp_path / "APP-42"
    case.mkdir()
    (case / "issue.json").write_text('{"key":"APP-42","comments":[]}', encoding="utf-8")

    with pytest.raises(JiraCaseValidationError, match="JIRA_CASE_REEXPORT_REQUIRED"):
        load_jira_initial_context(case, require_jira=False, max_chars=10000)


def test_generic_local_case_has_no_jira_requirement(tmp_path):
    case = tmp_path / "logs"
    case.mkdir()
    (case / "main.log").write_text("ready", encoding="utf-8")

    assert load_jira_initial_context(case, require_jira=False, max_chars=10000) is None


def test_modified_issue_json_is_rejected(tmp_path):
    case = tmp_path / "APP-42"
    write_case(case, comments=[])
    (case / "issue.json").write_text('{"key":"APP-42","comments":[{}]}', encoding="utf-8")

    with pytest.raises(JiraCaseValidationError, match="JIRA_CASE_CONTEXT_TAMPERED"):
        load_jira_initial_context(case, require_jira=False, max_chars=10000)
