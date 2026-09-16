# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

An evidence-driven Android Bug Analysis Agent. It reads Jira issues or local bug case directories, uses MCP tools to collect logs and diagnostics, and produces structured RCA reports distinguishing facts, hypotheses, and missing evidence.

## Commands

```powershell
# Install (requires Python 3.10+ and uv)
uv sync --all-packages --extra dev

# Run all tests
uv run pytest -q tests
uv run pytest -q packages/log-analysis-core/tests
uv run pytest -q packages/jira-bug-mcp/tests
uv run pytest -q packages/log-analyzer-mcp/tests

# Build
uv build

# Analyze a local bug case
uv run bug-agent analyze-local "D:\bug-cases\APP-42"
uv run bug-agent --json analyze-local "D:\bug-cases\APP-42"   # JSON output with trace

# Analyze a Jira issue
uv run bug-agent analyze-jira APP-42

# Activate specific skills (combinable with --skill)
uv run bug-agent --skill android-log-triage --skill android-black-screen analyze-local "D:\bug-cases\APP-42"

# Jira-only operations (no LLM required)
uv run bug-agent collect-jira APP-42
uv run bug-agent collect-jira APP-42 --export-case
uv run bug-agent collect-jira APP-42 --prepare               # download + extract + index
uv run bug-agent jira-check                                   # verify Jira connectivity

# Prepare a local case without LLM
uv run bug-agent prepare-local "D:\bug-cases\APP-42"
```

## Architecture

### Layered design (top to bottom)

```
CLI / HTTP / Queue / Workflow
        │
        ▼
BugAnalysisWorker        Stable Task/Result contract, MCP lifecycle, skill loading
        │
        ├── SkillRegistry        Team-maintained analysis methods (SKILL.md)
        │
        ▼
BugAnalysisAgent         Agent Loop, step budget, tool result truncation, prompt
        │
        ├── ModelProvider        OpenAI-compatible (DeepSeek, Gateway, etc.)
        │
        └── McpToolRouter
                ├── jira-bug-mcp        Jira read-only adapter
                ├── log-analyzer-mcp    File security + MCP adapter
                │       └── log-analysis-core   Deterministic parsers (timestamps, diagnostics)
                └── opengrok-mcp        OpenGrok code search (Python, optional)
```

### Key separation rules

- **Agent Core** must not reference Jira REST fields, log file paths, or model vendor SDKs. It only depends on two protocols: `ModelProvider.complete()` and `ToolRouter`.
- **MCP** is the tool execution layer — it does not contain domain expertise.
- **Skills** define analysis strategy (what to investigate, evidence standards, stop conditions) but never contain file operations, regex parsers, or security rules.
- **log-analysis-core** is a pure deterministic library: timestamp extraction, stable IDs, diagnostic classification. It has no MCP or file I/O dependencies.

### Source layout

```
src/bug_agent/
  agent_core/       # Framework-agnostic loop, ports, prompts, checkpoints
    agent.py         # BugAnalysisAgent tool-call loop
    contracts.py     # ModelProvider, ToolRouter, runtime config, provider failure ports
    human_checkpoint.py # Core checkpoint protocol and result parsing
    prompts.py       # Domain and workflow prompts
  application/      # Analysis workflows and use-case coordination
    worker.py        # Stable Worker facade and runtime lifecycle
    conversation.py  # Multi-turn analysis session
    skill_router.py  # Runtime Skill activation policy
    investigation_state.py # Investigation control-plane tools
    human_guidance.py # Human checkpoint workflow
    rca_reconciliation.py # Cross-run RCA reconciliation
    evaluation.py    # Run review and promotion use cases
    comment_compiler.py # Bounded Jira context compilation
  domain/           # Stable business contracts and deterministic RCA rules
    contracts.py    # BugAnalysisTask, BugAnalysisResult, RCAReport, evidence models
    models.py       # AgentRunResult, ToolEvent, token and intervention models
    rca_state.py    # Persistent RCA state and audit event models
    report_validation.py # Evidence-bound report validation
    coverage_contracts.py # Versioned minimum coverage rules
  infrastructure/   # External services and runtime adapters
    config.py        # AgentConfig and environment loading
    provider.py      # OpenAI-compatible model provider
    mcp_router.py    # MCP process lifecycle and tool routing
    jira_context.py  # Exported Jira Case validation
    skills.py        # Filesystem-backed Skill registry
    tool_catalog.py  # Runtime tool schema catalog and legacy fallback
    persistence/     # Run bundles, chat sessions, run records, and RCA state storage
      chat_store.py  # JSON chat persistence with an injected optimization writer
      optimization_feedback.py # Human intervention optimization artifacts
  interfaces/       # User-facing process entrypoints
    cli.py           # argparse CLI implementation (bug-agent command)
  presentation/     # Report parsing, Markdown rendering, and visualizations
    report_parser.py # Structured RCA parsing and conversation presentation
    renderer.py      # Human-readable Markdown output
    chat_visualization.py # Chat artifact generation
    run_visualization.py  # Run review artifact generation
  __init__.py       # Stable package-level public API; no flat implementation modules

packages/
  jira-bug-mcp/     # Jira Cloud v3 / Data Center v2 adapter, read-only, case exporter
  log-analysis-core/ # Deterministic: timestamps, stable_id, classify_diagnostic_line
  log-analyzer-mcp/  # MCP layer: case registry, archive safety, chunked index, search

skills/             # SKILL.md files with YAML frontmatter (name, description)
tools/              # Bundled third-party tools (aee_extract.exe, etc.)
tests/              # Agent + Worker tests using FakeProvider/FakeRouter
docs/
  architecture.md   # Detailed architecture and data flow
  worker-contract.md # BugAnalysisTask → BugAnalysisResult contract
```

### Worker contract (stable public API)

`BugAnalysisTask` → `BugAnalysisWorker.execute()` → `BugAnalysisResult`

- `source`: `"jira"` or `"local"` — mutually exclusive fields (`issue_key` vs `case_path`)
- `skills`: optional pre-activated list; omission loads `android-log-triage`, max 5
- `auto_select_skills`: lets the Agent load one symptom Skill plus compatible platform Skills
- `include_trace`: controls whether internal `ToolEvent` list is returned
- Result `status`: `completed`, `insufficient_evidence`, `max_steps`, `failed`
- `structured_output=false` means the model didn't return valid RCA JSON — upstream should treat this as degraded

### Skill system

Skills are stored as `skills/<name>/SKILL.md` with required YAML frontmatter:

```yaml
---
name: skill-name
description: One-line purpose
---
# Markdown instructions for the model
```

- Skill names must match `[a-z0-9]+(?:-[a-z0-9]+)*`
- Max 64 KiB per skill, max 5 skills per task
- Override skill root via `BUG_AGENT_SKILLS_ROOT` env var
- Skills are injected into the system prompt — they don't expand tool permissions

### Tool result safety

- Tool results are truncated to `max_tool_result_chars` (default 40,000) — truncation preserves valid JSON structure, never leaks half-parseable JSON
- `retryable=true` errors allow limited retries; `retryable=false` errors should not be retried
- Zero-match search results are valid observations, not errors

## Environment configuration

Copy `.env.example` to `.env` and configure:

- `BUG_AGENT_LLM_BASE_URL`, `BUG_AGENT_LLM_API_KEY`, `BUG_AGENT_LLM_MODEL` — required for analysis
- `BUG_AGENT_MAX_STEPS` (12), `BUG_AGENT_LLM_TIMEOUT_SECONDS` (120), `BUG_AGENT_MAX_TOOL_RESULT_CHARS` (40000)
- `JIRA_BASE_URL`, `JIRA_DEPLOYMENT` (cloud/datacenter), `JIRA_AUTH_MODE` (basic/bearer), `JIRA_USER`, `JIRA_TOKEN` — required for Jira mode
- `JIRA_EXPORT_ROOT` — where exported cases are stored
- `LOG_ANALYZER_ALLOWED_ROOTS` — semicolon-separated paths on Windows
- `OPENGROK_ENABLE_CODE_SEARCH` (false) — enable OpenGrok code search as a third MCP server
- `OPENGROK_BASE_URL`, `OPENGROK_USERNAME`, `OPENGROK_PASSWORD`, `OPENGROK_VERIFY_SSL` — OpenGrok connection

OpenGrok MCP Server is a Python package in `packages/opengrok-mcp/` — no Node.js required.

## Testing conventions

- Tests use `FakeProvider` (returns canned responses) and `FakeRouter` (records connections) — no real LLM or Jira calls
- `tmp_path` fixture for case directories
- `monkeypatch.setenv` for Jira config in tests
- Test files: `test_agent.py`, `test_worker.py`, `test_contracts.py`, `test_cli.py`, `test_provider.py`, `test_skills.py`, `test_mcp_router_integration.py`
- Package-level tests mirror the same pattern in `packages/*/tests/`

## Development rules (from CONTRIBUTING.md)

1. Agent Core must not reference Jira REST fields, local config, or fixed model addresses
2. External system differences go in MCP/Provider adapters
3. Tool behavior changes must include tests that don't depend on real enterprise services
4. No real tokens, issue keys, or company addresses in logs, fixtures, or exceptions
5. Jira write operations require prior design of minimal permissions, audit, and human-in-the-loop
6. Deterministic parsing goes in `log-analysis-core`; MCP handles protocol and security; analysis strategy goes in Skills
