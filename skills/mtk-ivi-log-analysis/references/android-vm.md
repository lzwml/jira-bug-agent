# MTK Android VM Evidence Reference

Use this reference when maintaining Android-side parsing rules or investigating an MTK-specific Android artifact layout. Directory names vary by product and MobileLog version; confirm against `file_tree.txt`, `properties` and `mblog_history`.

## MobileLog

Typical evidence under `debuglogger/mobilelog` includes rotating `APLog_*` rounds, current `.curf` buffers, compressed historical rounds and a `boot__normal` subtree. Relevant streams commonly include:

- `main_log`: framework, system service, application and native service messages;
- `kernel_log`: driver, memory, scheduler, panic and watchdog evidence;
- `events_log`: structured Android event tags;
- `radio_log`: modem/connectivity evidence;
- `crash_log`: crash summaries;
- `atf_log`, `apusys_log`, `connsys_picus_log`: platform subsystems;
- `bootprof`, `pl_lk`, `properties`, `mblog_history`: boot and capture identity.

`boot__normal` often preserves early-boot logs copied when boot completion occurs, but retention and overwrite behavior are product configuration. Confirm its boot identity rather than treating the directory name as proof.

## AEE, ANR and Dropbox

- Start AEE discovery from `aee_exp/db_history`; use its event path, subtype, process and time to select the matching database directory.
- Match an ANR using process, timestamp and subject before reading thread stacks. Main-thread state alone is not an ANR cause; inspect the blocking resource and binder relationship.
- Use `binderinfo` to support a concrete transaction-chain hypothesis, not as a global traffic ranking exercise.
- Dropbox entries such as `SYSTEM_BOOT`, `SYSTEM_RESTART`, `system_app_anr` and `system_server_lowmem` are incident indexes or summaries. Correlate them with primary logs.
- Match tombstone process/build identity and crash time before connecting it to the reported symptom.

## Common formats

- Logcat commonly begins with `MM-DD HH:MM:SS.mmm PID TID LEVEL TAG: message` and belongs to a wall-clock domain.
- MTK kernel streams commonly contain priority plus boot-relative seconds. Preserve the raw timestamp and priority.
- ANR traces consist of process metadata and per-thread states/stacks; thread names and TIDs must remain associated with their process.

Avoid universal regex assumptions when a product build changes spacing, prefixes or tag formatting. Deterministic format support belongs in `log-analysis-core` with fixtures from real anonymized logs.
