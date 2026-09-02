---
name: code-search
description: Search codebase for symbol definitions, references, file history, and recent changes via OpenGrok — cross-reference code evidence with log findings.
category: supplemental
---

# Code Search (OpenGrok)

Use OpenGrok code search tools (prefixed `opengrok_`) to trace root causes from log evidence into source code.
Do not search code without a specific log-anchored hypothesis. Code search is for verifying/refuting hypotheses, not for browsing.

## When to activate

Activate this skill when:
- A log diagnostic (NE tombstone, kernel panic, ANR, watchdog) names a specific function, module, or file path.
- An extract_timeline gap or parse_diagnostics finding points to a subsystem (e.g. "SurfaceFlinger present fence timeout").
- A hypothesis requires checking whether a recent commit introduced a regression.
- An error code or errno appears in logs and you need to understand its meaning from source.

Do NOT activate when:
- The investigation is still in the log-gathering phase and no code-level hypothesis exists.
- The symptom is purely environmental (thermal, power, signal) with no code anchoring.

## Workflow

### 1. List projects
Call `opengrok_list_projects` to discover available indexed repositories. Note the project names for later use.

### 2. Search for symbols from log evidence
Given a log-anchored symbol (function name, class name, error macro, file path):
- `opengrok_search_code` with `search_type="defs"` to find definitions.
- `opengrok_search_code` with `search_type="refs"` to find all call sites.
- `opengrok_find_file` to locate files by path pattern (e.g. `"SurfaceFlinger.cpp"`).

### 3. Read surrounding context
After finding a definition or reference:
- `opengrok_get_file_content` with `start_line` and `end_line` to read the relevant code block.
- Always request a narrow line range (20-40 lines around the match) — do not request full files.

### 4. Check recent changes
- `opengrok_get_file_history` to see recent commits and authors.
- `opengrok_what_changed` to see recent line-level changes grouped by commit.
- A commit timestamp close to the bug report date is a strong signal for regression.

### 5. Cross-reference with log evidence
- Every code finding must be linked to a log evidence_id.
- "Code looks suspicious" is not evidence — you must find the log event that confirms the code path was taken.
- If code logic explains the symptom but no log confirms execution, put it in hypotheses with missing_evidence.

## Tool reference

| Tool | Use |
|------|-----|
| `opengrok_list_projects` | List all indexed repositories |
| `opengrok_search_code` | Full-text/defs/refs/path search with `file_type` filter |
| `opengrok_find_file` | Find files by path pattern |
| `opengrok_get_file_content` | Read file content with line range |
| `opengrok_get_file_history` | View commit history for a file |
| `opengrok_what_changed` | Recent line changes grouped by commit |
| `opengrok_get_file_annotate` | Line-by-line git blame |
| `opengrok_browse_directory` | Browse directory structure |
| `opengrok_search_suggest` | Query autocomplete suggestions |

## Android-specific tips

- For C++/native code: use `file_type="cxx"` for .cpp/.cc/.h/.hpp files.
- For Java/Kotlin: use `file_type="java"`.
- For kernel: use `file_type="c"` for C sources.
- AOSP project names often match the repo path (e.g. `frameworks/native`, `system/core`).
- When searching for a kernel symbol, limit to the kernel project.
- NE tombstone backtraces give exact library paths and offsets — use the library name (e.g. `libsurfaceflinger.so`) to find the source project.