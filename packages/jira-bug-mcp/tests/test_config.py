from __future__ import annotations

from pathlib import Path

import pytest

from jira_bug_mcp.config import JiraConfig, load_local_env


def test_local_env_loads_supported_values_without_overriding_process_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "JIRA_BASE_URL=https://file.example\n"
        "JIRA_DEPLOYMENT=datacenter\n"
        "JIRA_TOKEN=file-token\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JIRA_TOKEN", "process-token")

    config = JiraConfig.from_environment()

    assert config.base_url == "https://file.example"
    assert config.deployment == "datacenter"
    assert config.token == "process-token"


def test_local_env_rejects_unrelated_environment_names(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("PATH=do-not-change\n", encoding="utf-8")

    with pytest.raises(ValueError, match="不允许的配置名"):
        load_local_env(env_file)
