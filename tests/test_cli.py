from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from bug_agent import cli


@pytest.mark.anyio
async def test_collect_jira_does_not_require_llm_configuration(monkeypatch):
    args = cli._parser().parse_args(["collect-jira", "app-42"])
    called = []

    def fake_collect(namespace):
        called.append(namespace.issue_key)
        return 0

    monkeypatch.setattr(cli, "_run_jira_collection", fake_collect)
    monkeypatch.setattr(
        cli.AgentConfig,
        "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("collect-jira 不应读取 LLM 配置")),
    )

    assert await cli._run(args) == 0
    assert called == ["app-42"]


@pytest.mark.anyio
async def test_prepare_local_does_not_require_llm_and_extracts_archive(monkeypatch, tmp_path, capsys):
    case_dir = tmp_path / "CASE-1"
    case_dir.mkdir()
    with zipfile.ZipFile(case_dir / "logs.zip", "w") as archive:
        archive.writestr("main_log.txt", "FATAL EXCEPTION: main\n")
    monkeypatch.setenv("LOG_ANALYZER_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(
        cli.AgentConfig,
        "from_environment",
        lambda: (_ for _ in ()).throw(AssertionError("prepare-local 不应读取 LLM 配置")),
    )
    args = cli._parser().parse_args(["prepare-local", str(case_dir)])

    assert await cli._run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["prepare"]["success"] is True
    assert payload["prepare"]["data"]["extraction"]["expanded_file_count"] == 1
    assert payload["prepare"]["data"]["index"]["artifact_count"] == 1
    assert Path(payload["work_dir"]) == (case_dir / ".bug-agent").resolve()
    assert payload["extraction_layout"] == "archive_sibling"
    assert Path(payload["extracted_root"]) == case_dir.resolve()
    assert (case_dir / "logs" / "main_log.txt").is_file()
    assert (case_dir / ".bug-agent" / "index" / "logs.sqlite3").is_file()
    assert not (tmp_path / "CASE-1_prepared").exists()


def test_collect_prepare_implies_export_case():
    args = cli._parser().parse_args(["collect-jira", "APP-42", "--prepare", "--no-index"])

    assert args.prepare is True
    assert args.export_case is False
    assert args.no_index is True


def test_analysis_cli_can_disable_automatic_skill_activation():
    args = cli._parser().parse_args([
        "--no-auto-skills", "analyze-local", "D:/cases/APP-42",
    ])

    assert args.no_auto_skills is True


@pytest.mark.anyio
async def test_prepare_local_accepts_explicit_sibling_work_dir(tmp_path, capsys):
    case_dir = tmp_path / "CASE-2"
    case_dir.mkdir()
    (case_dir / "main.log").write_text("ready\n", encoding="utf-8")
    work_dir = tmp_path / "visible-output"
    args = cli._parser().parse_args([
        "prepare-local", str(case_dir), "--work-dir", str(work_dir), "--no-index",
    ])

    assert await cli._run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["work_dir"]) == work_dir.resolve()


@pytest.mark.anyio
async def test_prepare_local_keeps_extraction_beside_archive_with_explicit_work_dir(tmp_path, capsys):
    case_dir = tmp_path / "CASE-3"
    case_dir.mkdir()
    archive_path = case_dir / "android.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("logs/main.log", "ready\n")
    work_dir = tmp_path / "index-output"
    args = cli._parser().parse_args([
        "prepare-local", str(case_dir), "--work-dir", str(work_dir),
    ])

    assert await cli._run(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (case_dir / "android" / "logs" / "main.log").is_file()
    assert (work_dir / "index" / "logs.sqlite3").is_file()
