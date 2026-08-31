---
name: android-log-triage
description: Triage an Android bug from logcat, kernel, ANR, tombstone, trace, or mixed diagnostic artifacts when the failure family is not yet known.
category: base
---

# Android Log Triage

Establish what evidence exists before choosing a root-cause theory.

1. Call `open_case`, then `inspect_case`. Record available artifact kinds, time coverage, and obvious gaps.
2. Call `parse_diagnostics` for `fatal`, `anr`, `kernel_stack`, and `avc`. Treat findings as signals, not causes.
3. Build an initial timeline with anchors relevant to the observed artifacts. A reasonable broad set is `FATAL`, `ANR in`, `Watchdog`, `Call Trace:`, `Kernel panic`, and `avc: denied`; remove irrelevant anchors and add component-specific ones as evidence emerges.
4. Use `search_evidence` for concrete components, errors, process names, or event transitions found in the Issue and diagnostics. Do not search an exhaustive keyword catalog without a hypothesis.
5. Form at most a few competing hypotheses. For each, state supporting evidence, contradictory evidence, and the cheapest next check that could falsify it.

Keep Android/wall time separate from kernel monotonic time unless a synchronization point is present. A repeated error, nearby timestamp, AVC, Fatal, or stack trace does not by itself establish causality.

If the Issue or first evidence pass clearly identifies a specialized family such as black screen, ANR, native crash, or kernel panic, call `activate_skill` for the matching symptom Skill before continuing this analysis run. Do not activate a specialist from an isolated keyword without matching incident identity.

Return `insufficient_evidence` when required artifacts or time ranges are missing. Every confirmed fact must cite an Evidence ID or a diagnostic finding with file and line information.
