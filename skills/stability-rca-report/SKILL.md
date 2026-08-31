---
name: stability-rca-report
description: Produce a formal, evidence-driven stability RCA that separates symptom, failure mechanism, root cause, coverage gaps, and actionable follow-up.
category: supplemental
---

# Stability RCA Report

Use this reporting method for every final stability analysis. This Skill governs how findings are expressed; it does not change the investigation tools or evidence threshold.

## Separate the conclusion layers

Never collapse these concepts into one statement:

1. **Observed symptom** — what the user or test observed.
2. **Direct failure mechanism** — the immediately verified mechanism that explains the symptom, such as an ANR, process death, missing buffer submission, HWC present failure, or panel remaining blank.
3. **Technical root cause** — the verified underlying defect that produced the failure mechanism.
4. **Trigger or contributing condition** — STR transition, race, resource pressure, bad input, version change, or other condition that exposed the defect.

A verified failure mechanism is not automatically a verified root cause. If the underlying defect is not proven, set the conclusion to `hypothesis_only` or `insufficient_evidence` and state what is still missing.

## Build a critical timeline

Include only events that establish the causal sequence. Each entry must carry its clock domain, source/evidence IDs, and interpretation. Keep Android/wall time and kernel monotonic time separate unless a synchronization point exists. Jira descriptions and comments are valid investigation inputs but must be labeled as reported context rather than raw-log proof.

## Report layer coverage

List every layer relevant to the activated symptom Skill. Mark each as `covered`, `partial`, `not_covered`, or `not_applicable`, and state the result, supporting evidence, and remaining gap.

Examples:

- Black screen: app/window, SurfaceFlinger, HWC, kernel/DRM, panel/backlight, power/MCU.
- ANR: main thread, locks, Binder/remote service, scheduler/CPU, I/O, memory/GC.
- Reboot: framework watchdog, native service, kernel panic, hardware watchdog, power/MCU, hypervisor/VM.

Finding strong evidence at one layer does not remove the obligation to record whether other relevant layers were covered.

## Treat negative results separately

Place zero-match searches and "no error found" observations in `negative_findings`, not `confirmed_facts`. Every negative finding must include its scope and limitation. Zero matches mean only that the searched evidence did not contain the query; they do not prove the event did not occur.

## State hypotheses with refutation paths

For each hypothesis, include support, contradiction, missing evidence, and the cheapest falsification check. Use qualitative confidence in the rendered report (`high`, `medium_high`, `medium`, `low`) rather than presenting model-generated probabilities as calibrated statistics.

## Make actions executable

Each next action must include priority, action, owner/module when known, expected artifact, and completion criterion. "Collect more logs" is insufficient; name the time window, trace source, process/component, and what observation would confirm or reject the hypothesis.

## Evidence requirements

- Confirmed facts and timeline entries must cite stable Evidence IDs.
- Do not invent files, line numbers, timestamps, owners, or evidence IDs.
- Keep analysis metadata (Task ID, Skills, steps, trace location) separate from the causal narrative.
