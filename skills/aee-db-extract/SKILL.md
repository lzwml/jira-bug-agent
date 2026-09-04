---
name: aee-db-extract
description: Extract and decode MTK AEE DB (.dbg) files using aee_extract.exe, then index the decoded logs for downstream analysis. Use when bug case attachments contain .dbg files from MTK AEE dumps.
category: supplemental
---

# AEE DB Extract

Decode MTK AEE diagnostic database (`.dbg`) files into analyzable log artifacts. AEE DB files are proprietary binary archives produced by MTK's AEE (Android Exception Engine) — they bundle kernel logs, dumpsys output, traces, and other diagnostic data into a single compressed file.

## When to activate

Activate this skill when:
- A bug case contains `.dbg` files (e.g. `db.03.ANR-fedeaa2ad13f0d63.dbg`, `db.00.NE.dbg`).
- `inspect_case` or `open_case` reports AEE DB artifacts that have not yet been decoded.
- The file naming pattern matches `db.XX.<type>-<hash>.dbg` or `db.XX.<type>.dbg`.

Do NOT activate when:
- The `.dbg` has already been extracted and the `.DEC/` directory is already indexed.
- The case contains only plain-text logs (logcat, kernel log, etc.) with no `.dbg` files.
- The platform is not MTK.

## Workflow

### 1. Locate the dbg files

After `open_case` and `inspect_case`, identify all `.dbg` files in the case directory. Typical naming:

| Pattern | Meaning |
|---------|---------|
| `db.00.NE.dbg` | Native Exception (tombstone) |
| `db.03.ANR-<hash>.dbg` | ANR dump |
| `db.fatal.00.NE.dbg` | Fatal NE (kernel panic / HW reboot) |
| `db.08.ANR.dbg` | ANR with extended diagnostics |

The number (e.g. `03`, `08`, `00`) is the AEE category ID. The type suffix (`NE`, `ANR`, `KE`) indicates the crash family.

### 2. Decode through the controlled tool

Call `extract_aee_db` with the `case_id` and the `.dbg` artifact's `artifact_id`. Do not run `aee_extract.exe` through a shell or pass a raw filesystem path. The MCP tool invokes the configured decoder, enforces the Case boundary and budgets, reuses an existing non-empty `.DEC` result, and registers decoded files.

The tool creates a `<dbg_filename>.DEC/` directory next to the dbg file, containing decoded text files. Example:

```
db.03.ANR-fedeaa2ad13f0d63.dbg
db.03.ANR-fedeaa2ad13f0d63.dbg.DEC/
  ├── __exp_main.txt          # Exception summary — read this first
  ├── SYS_KERNEL_LOG          # Kernel log (dmesg)
  ├── SYS_ANDROID_LOG         # Android logcat (main)
  ├── SYS_ANDROID_EVENT_LOG   # Android event log
  ├── SWT_JBT_TRACES          # ANR/Java thread traces
  ├── SYS_PROCESSES_AND_THREADS  # ps + thread listing
  ├── SYS_BINDER_INFO         # Binder state
  ├── DUMPSYS_*               # Various dumpsys outputs
  ├── PROCESS_*               # Process-specific info (maps, sched, etc.)
  ├── SYS_DISPLAY             # Display/SurfaceFlinger state
  ├── SYS_CPU_INFO            # CPU/Scheduling info
  ├── SYS_MEMORY_INFO         # Memory state
  └── ZZ_INTERNAL / COMMIT_INFO / SYS_CHIP_INFO  # Metadata
```

### 3. Route to the right analysis

After extraction, read `__exp_main.txt` first to identify the crash type and affected process. Then route based on the dbg type:

| dbg type | Primary symptom skill | Key files to inspect |
|----------|----------------------|---------------------|
| `db.XX.ANR` | `android-anr-ui-freeze` | `SWT_JBT_TRACES`, `SYS_ANDROID_LOG`, `SYS_BINDER_INFO`, `__exp_main.txt` |
| `db.XX.NE` | `android-native-crash` | `__exp_main.txt`, `SYS_KERNEL_LOG`, `PROCESS_MAPS`, `SYS_ANDROID_LOG` |
| `db.XX.KE` | `system-reboot-watchdog` | `SYS_KERNEL_LOG`, `__exp_main.txt`, `SYS_CPU_INFO` |
| `db.fatal.XX.NE` | `system-reboot-watchdog` | `SYS_KERNEL_LOG`, `__exp_main.txt`, `SYS_LAST_SPM_OCLA_SRAM_DATA` |

If the dbg type is unknown or ambiguous, read `__exp_main.txt` and `SYS_KERNEL_LOG` first, then decide.

### 4. Build the evidence index

After extraction, index the returned readable `artifacts[*].artifact_id` values with `build_index` so that `parse_diagnostics`, `search_evidence`, and `extract_timeline` can use the decoded files. Read the artifact identified by `recommended_first` (`__exp_main.txt`) before choosing the symptom route.

If the `.dbg` came from an archive, first extract that member with `extract_archive_members`; then call `extract_aee_db` using the registered derived artifact ID.

## Important notes

- `aee_extract.exe` is **Windows-only**. The MCP server uses the bundled tool or `AEE_EXTRACT_BIN` configured by the operator.
- The `.DEC/` output directory is in `.gitignore` and will not be committed.
- `.dbg` files may be renamed — the hash suffix (e.g. `-fedeaa2ad13f0d63`) is not meaningful for analysis; the extraction output depends only on the file contents.
- Some dbg files contain empty logs (`SYS_ANDROID_LOG` = 0 bytes) — this is a platform limitation, not an extraction failure. Cross-reference with other log sources in the same case.
