---
name: code-search
description: Verify a log-anchored code hypothesis with OpenGrok and local source. Use for definitions, call paths, and code changes; do not use source alone to establish an incident root cause.
category: supplemental
---

# Code Search (OpenGrok + Local Source)

Use code to test a specific hypothesis already anchored in logs or diagnostics. Code explains what *can* happen; runtime evidence establishes what happened in this incident.

## When to activate

Activate when a log, AEE/tombstone, or user-provided path names a function, module, process, error code, or source file relevant to the incident. Do not activate for exploratory browsing without a log-anchored question.

## Search and scope

1. Start from an exact symbol, process name, path, or distinctive error string. Avoid repeated whole-artifact searches such as `"."`; use one only when establishing the contents of a newly extracted, small artifact.
2. If the project is known from a path or prior result, pass it explicitly. If it is unknown, call `opengrok_list_projects` or omit `projects` and let the MCP discover them for `defs`/`refs` fallback.
3. Inspect `fallbackFrom` and `note` in a successful OpenGrok result. A scoped `full` fallback is useful for discovery but is not equivalent to a definition/reference match.
4. A 400 is a query compatibility or scope problem, not evidence that source is absent. Retry using discovered projects or a scoped `full` query before reporting a code-search gap.
5. Prefer narrow reads around a matching line. If a configured local root cannot read a file, use `opengrok_get_file_content`; record the local-root gap only after the remote read also fails.

## Evidence contract

For every code finding, state its evidence class:

- **Runtime-confirmed:** an incident artifact proves the process, parent/child relationship, command, or code path executed.
- **Source-confirmed:** source proves behavior exists, but the incident did not prove that path ran.
- **Hypothesis:** source behavior plausibly explains the symptom but lacks a required runtime link.

A user-provided source path is source context, not runtime proof. Do not infer a parent PID from `SYSTEMD_EXEC_PID`; require an incident `PPid` snapshot or equivalent process evidence. Do not infer that SIGABRT came from `abort()` solely from its signal number or `si_code`.

## Causal conclusions and patches

Keep the result at **hypothesis** when the incident lacks a stack, core, signal sender, parent-process timing, or another required link in the causal chain. State the shortest validating experiment, such as a controlled reproduction with signal tracing, stderr capture, and a core/backtrace.

Only offer a patch when requested. Label it a proposal unless its exact failing path is runtime-confirmed. Before recommending lifecycle or signal changes, check process-tree behavior (including shell descendants), registered signal handlers, wait/reap semantics, timeout/API impact, and the original design intent.

## Cross-reference output

Return:

1. The log-anchored question and selected project scope.
2. The smallest relevant source excerpt and its evidence class.
3. Runtime evidence that confirms the path, or the specific missing evidence.
4. A conclusion calibrated to that evidence, plus one next validation step when the conclusion remains a hypothesis.

## Language hints

- C/C++: `file_type="cxx"`; kernel C: `file_type="c"`.
- Java/Kotlin: `file_type="java"`.
- Use `opengrok_get_file_history` or local history only when a change/regression hypothesis is in scope.
