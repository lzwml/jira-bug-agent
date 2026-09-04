---
name: mtk-ivi-log-analysis
description: Analyze MTK automotive IVI evidence across Android VM, Linux VM/TBox, hypervisor, SCP, MCU, CAN, OTA, and PKI domains. Use for MTK platform topology, archives, boot rounds, and clock rules; pair it with one symptom Skill.
category: platform
---

# MTK IVI Log Analysis

Use this platform Skill with one primary symptom Skill. It supplies MTK artifact, archive, boot, and clock semantics; it does not justify a full-platform error sweep.

## Establish the evidence surface

Call `open_case` and `inspect_case`, then classify artifacts by domain. The complete 26-type log catalog and directory trees are in `references/log-directory-reference.md`. Quick summary:

- **Android VM:** `main_log`, `kernel_log`, `events_log`, `radio_log`, `crash_log`, `sys_log`, `stats_log`, `security_log`, `boot__normal`, ANR, AEE, Dropbox, and tombstones. For undecoded AEE `.dbg`, activate `aee-db-extract`.
- **Linux VM/TBox:** `Linux_Log/logNN`, `syslog.log.*` (vlog bridge, SOS-only), all 26 log types suffixed `.log.NNNN.*.gz`, plus `bootprof`, `pl_lk`, `reboot-reason`, `mblog_history`, `file_tree.txt`.
- **Platform subsystems:** `scp_log`, `sspm_log`, `adsp_*_log`, `mcupm_log`, `atf_log`, `gz_log`, `bsp_log`, `nebula_tee_log`, `nebula_hypervisor_log`, `vcp_log`, `apusys_log`, `connsys_picus_log`, `wifi_driver_log`, `vm_*_klog`, `ccci_dpmaif_debug`.
- **Peripheral/application:** `Mcu_Log`, CAN ASC, OTA/HMI, PKI, and application logs.

When evidence is archived, read [the shared archive-selection contract](references/incident-archive-selection.md), then read exactly one format guide:

- APLog or `boot__normal`: [APLog selection](references/aplog-archive-selection.md)
- SOS/TBox or `Linux_Log/logNN`: [SOS selection](references/sos-archive-selection.md)

Do not duplicate those selection rules in a symptom Skill.

The runtime-safe archive sequence is `inspect_archive` → `probe_archive_members` → `extract_archive_members` → `build_index`. Select the smallest incident round and stream set supported by content coverage. Use `prepare_case` only when bounded probing cannot establish usable coverage or the incident genuinely requires broad extraction.

Treat SOS `logNN` and APLog `__NN` as source-verified round counters for boot ordering. Do not equate the highest counter with the incident round; a failure can be in the predecessor round and trigger the next boot.

## Select one symptom route

| Symptom family | Primary Skill |
| --- | --- |
| No usable display, delayed display, or frozen display | `android-black-screen` |
| ANR, frozen UI, input timeout, or app not responding | `android-anr-ui-freeze` |
| Tombstone, native signal, debuggerd/AEE crash, or native service death | `android-native-crash` |
| Unexpected reboot, watchdog, kernel panic, or boot loop | `system-reboot-watchdog` |
| Guest/host startup, IPC, sensor, SCP, or hypervisor boundary failure | `linux-virtualization-failure` |
| Missing, stale, invalid, or mistimed CAN/MCU signal | `can-mcu-signal-analysis` |
| OTA stage, certificate/authentication, or transport failure | `ota-pki-connectivity` |

If the family remains unknown, use `android-log-triage`. The composition contract is in [symptom routing](references/symptom-routing.md).

## Respect clock domains

Keep Android/logcat wall time, kernel/ftrace monotonic time, vlog wall time and uptime, SCP/hypervisor counters, MCU clocks, and CAN capture time separate until an observed anchor permits normalization. Follow [clock-domain rules](references/clock-domains.md).

At every Android/Linux, guest/host, application/kernel, or MCU/CAN boundary, record:

1. the user-visible symptom window;
2. the last successful upstream transition;
3. the first failed or missing downstream transition;
4. supporting and contradictory evidence;
5. the cheapest next observation that could falsify the hypothesis.

Prefer two independent artifacts for cross-domain claims. A nearby warning, repeated error, AVC, temperature sample, or component name does not establish causality.

## Evidence and stopping

Every confirmed fact must cite an Evidence ID or diagnostic finding with relative path and line information. Preserve boot, process, VM/domain, and operation identity.

Return `insufficient_evidence` when the symptom window, required domain, archive contents, clock anchor, or layer boundary is absent. Record which boot rounds and content ranges were checked; do not compensate with broad keyword scanning.

## Maintainer references

- [Complete log directory trees and 26-type catalog](references/log-directory-reference.md)
- [Android artifact conventions](references/android-vm.md)
- [Linux/TBox and peripheral conventions](references/linux-vm.md)
- [Archive extraction boundary](references/archive-safety.md)
- [Android mobile_log_d design](references/android-mobile-log-design.md)
- [Yocto vlog/mobile_log_d design](references/yocto-vlog-design.md)
