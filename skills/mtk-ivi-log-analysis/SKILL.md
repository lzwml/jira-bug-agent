---
name: mtk-ivi-log-analysis
description: Analyze MTK automotive IVI evidence spanning Android VM, Linux VM/TBox, hypervisor, SCP, MCU, CAN, OTA, or PKI logs. Use for MTK platform cases; do not activate for generic Android logs without MTK or cross-domain evidence.
category: platform
---

# MTK IVI Log Analysis

Use this Skill as a platform specialization after establishing the reported symptom. Combine it with a symptom Skill such as `android-black-screen` when appropriate; do not replace symptom-driven investigation with a full-platform error sweep.

## Establish the evidence surface

Call `open_case` and `inspect_case` first. Classify available artifacts by domain:

- **Android VM:** `main_log`, `kernel_log`, `events_log`, `radio_log`, `crash_log`, `boot__normal`, ANR, AEE, Dropbox, tombstones. When AEE evidence is in `.dbg` format, activate `aee-db-extract` to decode it before indexing.
- **Linux VM/TBox:** `Linux_Log/logNN`, `syslog`, `bsp_log`, `scp_log`, `nebula_hypervisor_log`, `atf_log`, `bootprof`, `pl_lk`, `reboot-reason`.
- **Peripheral/application:** MCU log, CAN ASC, OTA/HMI, PKI, Go application logs.

Do not infer the active boot round from `log00`/`log01` numbering alone. Confirm it with `mblog_history`, timestamps, reboot markers, or another boot identity. Treat claims about `boot__normal` retention as platform conventions that require confirmation from the collected file tree.

If required evidence is inside an archive, first call `inspect_archive` (without `time_range`) to inspect the member catalog. Check `time_groups` for `earliest_path_time_reliability`:

- **`unreliable_device_clock`**: the filename timestamps are unreliable due to unsynchronized device clock. Do not use `inspect_archive(time_range=...)`. Call `prepare_case` to extract and index all members. If the archive is too large and `prepare_case` returns skipped archives, fall back to `inspect_archive` without `time_range` to browse members manually, then use `extract_archive_members` with the stable `member_id`.
- **`reliable`**: the filename timestamps are in a plausible range (2024-2030). If the incident time is well-defined from Jira context, use `inspect_archive` with `time_range` + `time_neighbor_count=1` to select only the relevant members, then `extract_archive_members` + `build_index`. If the format is unsupported or a safety budget rejects it, return missing evidence with the reported reason.

For APLog archives: 参见 `references/aplog-archive-selection.md`。先检查 `time_groups` 中的 `path_time_reliability`，根据可靠性决定使用全量解压还是时间筛选。

For SOS/TBox archives containing `Linux_Log/logNN` boot rounds, `Mcu_Log`, `can_log`, `ota`, `pki`, or `data` directories, use the time-based boot round selection strategy in `references/sos-archive-selection.md`. Do not default to the highest-numbered `logNN` directory — the incident often occurred in an earlier round. Use generic `time_groups` to identify candidate directory prefixes, then request their members with `path_prefix`; the Skill, not the tool, decides the incident/predecessor/successor rounds and required reboot evidence. For reboot analysis, always include `reboot-reason`, `pl_lk`, and `bootprof` from the post-reboot round.

## Select the symptom route

This Skill supplies MTK platform context; it is not a substitute for a symptom investigation. Activate one primary symptom Skill, chosen from the reported behavior and the first diagnostic identity:

| Symptom family | Primary Skill |
| --- | --- |
| Boot completes without usable display, delayed display, or frozen display | `android-black-screen` |
| ANR, frozen UI, input timeout, or application not responding | `android-anr-ui-freeze` |
| Tombstone, native signal, debuggerd/AEE crash, or native service death | `android-native-crash` |
| Unexpected reboot, watchdog, kernel panic, or boot loop | `system-reboot-watchdog` |
| Guest/host startup, IPC, sensor, SCP, or hypervisor boundary failure | `linux-virtualization-failure` |
| Missing, stale, invalid, or mistimed CAN/MCU signal | `can-mcu-signal-analysis` |
| OTA stage failure, certificate/authentication failure, or transport loss | `ota-pki-connectivity` |

If the symptom family is still unknown, use `android-log-triage` first. Do not activate every symptom Skill or execute every route. When several symptoms are present, investigate the earliest independently observed failure and treat later symptoms as possible consequences.

The shared route contract and extension rules are documented in `references/symptom-routing.md`.

## Respect clock domains

Keep these clocks separate until a synchronization point is observed:

- Android/logcat wall clock;
- kernel and ftrace boot-relative monotonic time;
- SCP and hypervisor local counters;
- MCU wall clock plus local counter;
- CAN capture time, which may be relative or absolute.

Build cross-domain ordering from shared boot markers, paired request/response IDs, reboot identities, or events visible in both domains. Record the raw timestamp, clock domain and normalization assumption. Do not search a Linux ftrace log using an Android `MM-DD HH:MM` value unless the file actually contains wall-clock timestamps.

## Test hypotheses at layer boundaries

Form only a few competing hypotheses. For each one, identify:

1. the user-visible symptom window;
2. the last successful upstream transition;
3. the first failed or missing downstream transition;
4. supporting and contradictory evidence;
5. the cheapest next observation that could falsify it.

Prefer two independent artifacts when crossing Android/Linux, guest/hypervisor, or application/kernel boundaries. A nearby warning, repeated error, AVC, temperature sample or component name does not establish causality.

## Evidence and stopping standard

Every confirmed fact must cite an Evidence ID or diagnostic finding with relative path and line information. Preserve process, boot and domain identity when the same tag appears in multiple VMs or rounds.

Return `insufficient_evidence` when the symptom window, required domain, archive contents, clock anchor, or layer boundary is absent. Do not compensate by broad keyword scanning.

## Gap-filling rule: never stop at the first empty search

When searching within a specific APLog boot round and the incident time window returns no matching events, do **not** immediately report `insufficient_evidence`. Instead:

1. **Check coverage**: use `extract_timeline` with a broad anchor (e.g. `bootanimation` or `FATAL`) to determine the actual time span of the current round's logs. Compare with the reported incident time.
2. **Expand to adjacent rounds**: if the current round doesn't cover the incident time, inspect neighboring boot rounds. Use `inspect_archive` without `time_range` to see all members, identify the predecessor and successor rounds from `time_groups`, then extract and search those rounds.
3. **Only after exhausting adjacent rounds**: if none of the available boot rounds cover the incident window, report the gap in `missing_evidence` with the specific rounds checked and their observed time spans.

APLog boot round numbering is not a reliable indicator of which round contains the incident — the crash may trigger a reboot and the incident logs are in the prior round, or the device may have booted multiple times.

Platform details for maintainers are separated by concern:

- Android artifact conventions: `references/android-vm.md`
- Linux/TBox and peripheral conventions: `references/linux-vm.md`
- Clock normalization rules: `references/clock-domains.md`
- Archive handling requirements: `references/archive-safety.md`
- APLog incident-time archive selection: `references/aplog-archive-selection.md`
- SOS/TBox incident-time archive selection: `references/sos-archive-selection.md`
