# SOS Archive Incident-Time Boot Round Selection

Use this route when the archive is an SOS/TBox dump (not `APLog_YYYY_MMDD_HHMMSS__NN` format).
SOS archives contain `Linux_Log/logNN/` boot rounds, `Mcu_Log/`, `can_log/`, `ota/`, `pki/`,
and `data/` directories. Do not extract the entire archive or default to `log08`/the highest-numbered
round — the incident may have occurred in earlier rounds.

## SOS archive structure

### Linux_Log boot rounds

`Linux_Log/logNN` directories are boot rounds. `log00` is the earliest captured round and
`log08` (or the highest number) is the latest. Each round contains per-stream rotation files:

```
Linux_Log/log02/kernel_log.0001.2026_09_01_22_10_32.log.gz   ← early rotation
Linux_Log/log02/kernel_log.0002.2026_09_01_23_59_26.log.gz   ← last rotation before reboot
Linux_Log/log03/bootprof.0001.2026_09_01_23_59_46.log        ← boot after reboot
Linux_Log/log03/kernel_log.0001.2026_09_01_23_59_45.log.gz   ← first rotation of new round
```

The timestamp in the filename is the **rotation (dump) time**, not the log content time.
Within a round, the `.0001` files are the earliest rotation and the highest-numbered files
are the latest. The boot time is approximately the earliest timestamp in the round.

Core streams to include per round:
- `kernel_log`, `main_log`, `syslog` — primary diagnostic streams
- `reboot-reason`, `pl_lk`, `bootprof` — boot identity and reset cause
- `mblog_history` — boot round lineage
- `scp_log`, `nebula_hypervisor_log`, `atf_log`, `bsp_log` — cross-domain evidence

### Mcu_Log

MCU logs follow the pattern `mculog.log.NNNN.YYYY_MM_DD_HH_MM_SS.log.gz` where the
timestamp is the dump time. The sequence number `NNNN` increments across the entire capture
and is independent of the Linux boot round numbering.

### can_log, ota, pki, data

CAN logs are under `can_log/YYYYMMDD/` with start-time timestamps. OTA logs are under
`ota/` with separate master and HMI streams. PKI and application logs are under their
respective directories.

## Time-based boot round selection

1. Take the reported incident date and time from validated Jira context. A time without a
   reliable date, ambiguous cross-midnight context, or unknown boot identity is a limitation;
   do not guess from the `logNN` sequence number.

2. Call `inspect_archive` to get the bounded member catalog and generic `time_groups`.
   Each group reports its path prefix, member counts, and the earliest/latest timestamp that
   is directly parseable from member paths. Request additional members with `path_prefix`
   only after selecting candidate rounds; do not treat the summary as log-content evidence.

3. Identify the incident boot round(s) from the member paths:
   - For each `Linux_Log/logNN/` directory, look at the earliest and latest timestamps in
     the filenames to determine its active time window.
   - The round whose window contains the incident time is the **incident round**.
   - For a reboot that spans two rounds, you need both the **pre-reboot round** (last
     rotation of `logNN`) and the **post-reboot round** (first rotation of `log(N+1)`).

4. Request the selected directory prefixes with `path_prefix` (and any required neighboring
   prefixes), then select their returned `member_id` values:
   - **Incident round(s)**: all core streams from the relevant `logNN` directories.
   - **Predecessor round**: the last few rotations (highest `.NNNN` numbers) of the preceding
     `logNN` — these capture the state leading up to the incident.
   - **Successor round**: the first few rotations (`.0001` files) of the following `logNN` —
     these capture the immediate aftermath.
   - For reboot analysis, always include `reboot-reason`, `pl_lk`, and `bootprof` from the
     post-reboot round.

5. For MCU logs, select the MCU rotation files whose timestamp range covers the incident
   window, plus one predecessor and one successor. The MCU sequence number is independent
   of the Linux boot round — do not assume `mculog.log.0003` corresponds to `log03`.

6. For CAN logs, use generic `time_range` with `time_neighbor_count=1`, then select the
   `.asc` and `.asc.gz` evidence returned by that query.

7. For OTA/PKI/application logs, select the files whose timestamp or rotation covers the
   incident window.

8. Use only returned stable `member_id` values with `extract_archive_members`. Never pass a
   bare member path. Reuse an item that already returns `extracted=true` and `artifact_id`.

9. Call `build_index` for the returned Artifact IDs before beginning analysis.

## Example: reboot analysis

Jira reports a reboot at `2026-09-01 23:59:44`. Inspecting the archive shows:

```
Linux_Log/log02/kernel_log.0002.2026_09_01_23_59_26.log.gz  ← last log before reboot
Linux_Log/log03/bootprof.0001.2026_09_01_23_59_46.log       ← boot after reboot
Linux_Log/log03/reboot-reason.0001.2026_09_01_23_59_47.log  ← reset cause
Linux_Log/log08/kernel_log.log                              ← current, but 5 hours later
```

The incident round is `log02→log03` transition. Extract:
- `log02`: highest-numbered rotations of kernel_log, main_log, syslog, scp_log, nebula_hypervisor_log
- `log03`: all `.0001` rotations plus reboot-reason, pl_lk, bootprof, mblog_history
- `log04`: `.0001` rotations of reboot-reason, pl_lk, bootprof (for comparison if second reboot)
- `Mcu_Log`: `mculog.log.0002` (pre-reboot), `mculog.log.0003` (incident), `mculog.log.0004` (post-reboot)

Do NOT extract `log08` unless a second incident is reported in that window. The highest-numbered
round is the current/latest round and is often irrelevant to earlier incidents.

## Fallback

If the incident time is unknown or ambiguous, extract `mblog_history` from all rounds first
to establish the boot lineage and timestamps, then select the relevant rounds. If that still
does not resolve the incident window, report `insufficient_evidence` with the limitation.
