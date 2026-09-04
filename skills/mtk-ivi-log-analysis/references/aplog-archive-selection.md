# APLog Incident Selection

Read [the shared archive-selection contract](incident-archive-selection.md) first. This reference contains only APLog-specific identity and stream rules.

## Round identity and confidence

An Android mobile_log_d archive commonly uses:

```text
APLog_2025_0101_080037__9.tar.gz
      YYYY MMDD HHMMSS  __NN
```

| Field | Interpretation | Confidence |
| --- | --- | --- |
| `YYYY_MMDD_HHMMSS` | Device wall clock near archive creation | Not reliable when the device clock changes or is unsynchronized; relative filename order can also be wrong |
| `__NN` | Persistent APLog folder/boot round index | Source-verified counter used to order rounds |

Treat each APLog archive artifact as a round candidate. Use `__NN` as the boot-round order: `__1` precedes `__2`, and so on. The counter orders rounds but does not identify which one contains the incident. If wall-clock content times appear to contradict the counter, keep the counter order and treat the time discrepancy as clock correction or unsynchronized device time.

## Candidate probing

For each plausible APLog artifact:

1. Call `inspect_archive` without `time_range` and identify the earliest readable `main_log` member for that archive.
2. Call `probe_archive_members` with that stable `member_id`.
3. Compare `content_time_ranges`, `boot_identity`, `anchors`, and `coverage_confidence` with the incident.
4. Probe neighboring `__NN` artifacts when the selected archive does not cover the event or when the event may have triggered reboot.

Path timestamps may narrow members only when reported `reliable`; probing still validates content coverage.

## Incremental stream selection

Begin with the streams required by the active symptom route:

- `main_log`: framework, app, and native-service events;
- `events_log`: structured Android lifecycle/events;
- `kernel_log`: driver, panic, watchdog, and boot-relative evidence;
- `system` or equivalent system log when the route needs WindowManager, display, or system-service coverage.

Expand to `crash_log`, `radio_log`, ANR, AEE, Dropbox, or platform streams only when a hypothesis requires them.

Android APLog does not normally contain Yocto VLOG Bridge `syslog.log.*`. Its absence is not an extraction failure; use the available Android system/event streams and report any required missing layer.

## `boot__normal`

`boot__normal` may preserve early-boot logs, but retention is product-dependent. Treat it as another candidate evidence group and confirm its boot identity and content range rather than assuming it belongs to the incident.

## Maintainer source

See [Android mobile_log_d design](android-mobile-log-design.md) for the source-derived index generation and persistence path.
