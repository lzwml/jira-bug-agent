from __future__ import annotations

import json

import pytest

from bug_agent.comment_compiler import compile_jira_context
from bug_agent.config import AgentConfig


CONFIG = AgentConfig(
    llm_base_url="https://llm.test/v1",
    llm_api_key="key",
    llm_model="model",
    jira_context_chunk_chars=4000,
    jira_context_summary_max_chars=30000,
)


class EchoCompilerProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, messages, tools):
        assert tools == []
        payload = json.loads(messages[1]["content"])
        self.calls.append(payload)
        return {"content": json.dumps({
            "sources": [{
                "source_id": source["source_id"],
                "current_status": [source["text"][:30]],
                "conclusions": [],
                "attempted_actions": [],
                "next_steps": [],
                "clues": ["component"],
                "open_questions": [],
            } for source in payload["sources"]],
        }, ensure_ascii=False)}


@pytest.mark.anyio
async def test_compiler_covers_description_and_every_comment_across_chunks():
    provider = EchoCompilerProvider()
    issue = {
        "description": "D" * 5000,
        "created_at": "2026-08-28",
        "comments": [
            {"comment_id": "c-1", "author": "A", "body": "one", "created_at": "t1"},
            {"comment_id": "c-2", "author": "B", "body": "two", "created_at": "t2"},
        ],
    }

    result = await compile_jira_context(provider, CONFIG, issue_key="APP-1", issue=issue)

    assert set(result.source_ids) == {"issue-description", "c-1", "c-2"}
    assert result.chunk_count >= 2
    assert len(provider.calls) == result.chunk_count
    compiled = json.loads(result.text)
    assert compiled["source_coverage"]["comment_ids"] == ["c-1", "c-2"]


class MissingSourceProvider:
    async def complete(self, messages, tools):
        return {"content": '{"sources": []}'}


@pytest.mark.anyio
async def test_compiler_fails_when_model_omits_a_source():
    with pytest.raises(ValueError, match="source_id 覆盖不完整"):
        await compile_jira_context(
            MissingSourceProvider(), CONFIG,
            issue_key="APP-1",
            issue={"description": "desc", "comments": []},
        )
