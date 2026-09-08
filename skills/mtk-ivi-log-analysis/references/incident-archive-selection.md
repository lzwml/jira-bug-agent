# Incident Archive Selection Contract

Use this shared workflow for MTK APLog and SOS/TBox archives. Read the matching format guide afterward; format-specific evidence requirements override only the stream choices, not this safety or coverage contract.

## Controlled workflow

1. Use `inspect_case` to identify candidate archive artifacts. For each plausible artifact, call `inspect_archive` without `time_range` to obtain its member catalog, stable `member_id` values, path groups, and path-time reliability.
2. Identify boot-round candidates from archive names and member paths. Do not assume the newest or highest-numbered round contains the incident; a failure can trigger the following boot.
3. Select one representative member per candidate round and call `probe_archive_members`. If the reported local incident window is known, pass it as `incident_time_range` so the intended target remains visible in the tool record and result. Probing reads a bounded prefix without writing member content. Compare the echoed target with `content_time_ranges`, `boot_identity`, `anchors`, and `coverage_confidence`; the target itself is not proof of content coverage.
4. Select the smallest set of rounds and streams that can cover the symptom and required layer boundaries. Extract them with `extract_archive_members`, then pass the returned readable artifact IDs to `build_index`.
5. Expand incrementally only when the first set cannot test the active hypotheses.

Never bypass a rejected or unsupported archive with ad-hoc shell extraction. Follow [the archive safety boundary](archive-safety.md).

## Time and boot rules

- Treat Issue/Jira time as a lead until log content confirms coverage.
- If path time is `reliable`, `inspect_archive(time_range=..., time_neighbor_count=1)` may narrow candidates, but content probing still validates the selected round.
- If path time is `unreliable_device_clock`, do not use it for selection or ordering. Use round identity plus content probing.
- Use monotonic or uptime values only within the same boot unless an explicit cross-domain anchor exists.
- Treat source-verified round counters as the boot-order authority. If their order conflicts with wall-clock content times, preserve the discrepancy as evidence of clock correction or unsynchronized time; use content coverage and boot anchors to locate the incident without reversing the counter order.

## Empty-search recovery

A zero-match search does not prove that an event did not occur.

1. Confirm the indexed member's actual content range with its probe profile or a broad timeline anchor.
2. If it does not cover the incident, inspect and probe adjacent available rounds.
3. If it covers the incident, test query spelling, process/domain identity, and whether the required stream was indexed before expanding scope.
4. Report `insufficient_evidence` only after the relevant available rounds or required streams have been exhausted.

Record the rounds checked, representative members, observed content ranges, clock domains, and remaining gaps.

## Full-extraction fallback

Use `prepare_case` only when bounded inspection and probing cannot establish usable coverage, or when the incident genuinely requires broad cross-archive evidence. If extraction or indexing hits a safety budget, keep already indexed evidence, report the error code and skipped scope, and request the smallest missing artifacts needed to continue.
