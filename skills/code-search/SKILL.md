---
name: code-search
description: Search codebase for symbol definitions, references, and recent changes via OpenGrok + local source — cross-reference code evidence with log findings.
category: supplemental
---

# Code Search (OpenGrok + Local Source)

Two-step workflow: OpenGrok finds where, local source provides what.
Do not search code without a specific log-anchored hypothesis. Code search is for verifying/refuting hypotheses, not for browsing.

## When to activate

Activate this skill when:
- A log diagnostic (NE tombstone, kernel panic, ANR, watchdog) names a specific function, module, or file path.
- An `extract_timeline` gap or `parse_diagnostics` finding points to a subsystem (e.g. "SurfaceFlinger present fence timeout").
- A hypothesis requires checking whether a recent commit introduced a regression.
- An error code or errno appears in logs and you need to understand its meaning from source.

Do NOT activate when:
- The investigation is still in the log-gathering phase and no code-level hypothesis exists.
- The symptom is purely environmental (thermal, power, signal) with no code anchoring.

## Workflow

### Step 1: Search (OpenGrok)
Given a log-anchored symbol:
- `opengrok_search_code` with `search_type="defs"` to find definitions.
- `opengrok_search_code` with `search_type="refs"` to find all call sites.
- `opengrok_find_file` to locate files by path pattern (e.g. `"SurfaceFlinger.cpp"`).
- `opengrok_list_projects` to discover available indexed repositories.

### Step 2: Read (local source)
From search results, use `project` and `path` to read the actual file from local disk:
- `locode_read_file` — full file content, supports line ranges. Always request a narrow range (20-40 lines around the match).
- `locode_get_history` — recent git commits, authors, and messages. A commit close to the bug report date is a strong regression signal.
- `locode_get_blame` — who changed which lines and when. Useful for identifying module owners.
- `locode_list_roots` — see configured local source root mappings.

### Step 3: Cross-reference
- Every code finding must be linked to a log evidence_id.
- "Code looks suspicious" is not evidence — you must find the log event that confirms the code path was taken.
- If code logic explains the symptom but no log confirms execution, put it in hypotheses with missing_evidence.

## Tool reference

| Phase | Tool | Use |
|-------|------|-----|
| Search | `opengrok_list_projects` | List all indexed repositories |
| Search | `opengrok_search_code` | Full-text/defs/refs/path search with `file_type` filter |
| Search | `opengrok_find_file` | Find files by path pattern |
| Read | `locode_read_file` | Read full local source file with line ranges |
| Read | `locode_get_history` | Git log — recent commits, authors, messages |
| Read | `locode_get_blame` | Git blame — who changed which lines and when |
| Read | `locode_list_roots` | List configured local source root mappings |

## Android-specific tips

- For C++/native code: use `file_type="cxx"` for .cpp/.cc/.h/.hpp files.
- For Java/Kotlin: use `file_type="java"`.
- For kernel: use `file_type="c"` for C sources.
- NE tombstone backtraces give exact library paths and offsets — use the library name (e.g. `libsurfaceflinger.so`) to find the source project.
- When searching for a kernel symbol, limit to the kernel project (`projects=["yocto"]`).