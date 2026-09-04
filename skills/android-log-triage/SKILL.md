---
name: android-log-triage
description: Triage Android logcat, kernel, ANR, tombstone, trace, or mixed diagnostic evidence when the failure family is not yet known. Route confirmed MTK platform cases to mtk-ivi-log-analysis.
category: base
---

# Android Log Triage

Establish the incident identity and evidence coverage before choosing a root-cause theory. This Skill owns generic Android triage; platform archive selection belongs to the matching platform Skill.

## First pass

1. Call `open_case`, then `inspect_case`. Record the reported symptom, incident window, process/component, boot identity, artifact kinds, time coverage, and gaps.
2. If `inspect_case` reports `aee_db`, or an inspected archive contains a member with `kind=aee_db`, activate `aee-db-extract` before decoding it. Also activate `mtk-ivi-log-analysis` for MTK, APLog, SOS/TBox, `Linux_Log/logNN`, or other MTK cross-domain evidence.
3. Call `parse_diagnostics` for the signal families supported by the available artifacts, such as `fatal`, `anr`, `kernel_stack`, and `avc`. Treat findings as leads, not causes.
4. Build a focused timeline. Start with only anchors suggested by the Issue or diagnostics; common examples include `FATAL`, `ANR in`, `Watchdog`, `Call Trace:`, `Kernel panic`, and `avc: denied`.
5. Use `search_evidence` for concrete processes, components, errors, or transitions already observed. Do not run an exhaustive keyword sweep.

## Route by observed identity

Activate one primary symptom Skill when the Issue and evidence agree on the failure family:

| Observed family | Primary Skill |
| --- | --- |
| Black, delayed, or frozen display | `android-black-screen` |
| ANR, input timeout, or frozen UI | `android-anr-ui-freeze` |
| Tombstone, native signal, or native service death | `android-native-crash` |
| Reboot, watchdog, panic, or boot loop | `system-reboot-watchdog` |

Do not activate a specialist from an isolated keyword. If multiple symptoms exist, investigate the earliest independently observed failure and treat later symptoms as possible consequences.

## Evidence standard

- Keep Android wall time and boot-relative monotonic time separate until an observed synchronization point exists.
- Preserve process, boot, and artifact identity; do not merge similar messages from different incidents.
- For each viable hypothesis, state support, contradiction, and the cheapest falsification check.
- Every confirmed fact must cite an Evidence ID or diagnostic finding with file and line information.
- Return `insufficient_evidence` when the incident identity, required artifact, relevant time coverage, or necessary layer boundary is absent.

Jira descriptions and comments are reported context, not log proof. Engineer conclusions in comments remain hypotheses until independently supported by evidence.
