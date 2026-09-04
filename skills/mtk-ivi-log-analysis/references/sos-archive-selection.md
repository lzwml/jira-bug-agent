# SOS/TBox Incident Selection

Read [the shared archive-selection contract](incident-archive-selection.md) first. This reference contains only SOS/TBox-specific round, stream, and reboot rules.

## Boot-round identity

SOS archives commonly contain `Linux_Log/logNN/`. Yocto mobile_log_d creates the next directory by scanning existing `logNN` directories and using `max + 1`, so numbering is a source-verified boot-order counter while those directories are retained.

Use `logNN` for ordering, not incident selection. A crash or reset can place the failure in `log(N-1)` and recovery evidence in `logN`. When available, use `mblog_history` markers as the strongest identity anchor:

- `=====MOBILELOG START=======`
- `log dir: /log/Linux_Log/logNN/`

## Candidate probing

1. Call `inspect_archive` without `time_range` and enumerate `Linux_Log/logNN/` path groups.
2. For each plausible round, select the lowest sequence-numbered `syslog.log.*` member and call `probe_archive_members` with its stable `member_id`.
3. Compare `content_time_ranges`, vlog uptime, `boot_identity`, `anchors`, and `coverage_confidence` with the incident.
4. If `syslog.log.*` is absent, probe the earliest readable `main_log.log.*`, `kernel_log.log.*`, or `mblog_history` member and record the missing business-log stream.

`syslog.log.*` filename time is file modification/rotation time, not content time. Vlog wall time can be wrong after clock changes; its uptime is reliable only within the same boot.

## Incremental stream selection

Extract and index only the selected round and streams needed by the active route:

- `syslog.log.*`: Yocto VLOG Bridge business logs;
- `main_log.log.*` and `events_log.log.*`: Android framework/application evidence carried in the package;
- `kernel_log.log.*`: kernel and driver evidence;
- `bsp_log`, `scp_log`, `nebula_hypervisor_log`, or `atf_log`: add when the active boundary hypothesis requires that domain.

MCU logs live under `Mcu_Log`, outside `Linux_Log/logNN`. CAN, OTA, PKI, and `data` trees also have independent identity and clock rules; correlate them only through observed anchors.

## Reboot cases

For reboot analysis, include the post-reboot round's `reboot-reason`, `pl_lk`, and `bootprof`, while retaining the preceding round that contains the lead-up to failure. Do not infer reset ownership from post-boot symptoms alone.

## Maintainer source

See [Yocto vlog/mobile_log_d design](yocto-vlog-design.md) and [Linux/TBox evidence conventions](linux-vm.md) for source-derived formats and clock semantics.
