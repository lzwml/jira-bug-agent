# APLog Incident-Time Archive Selection

Use this route only when archive member basenames fully match
`APLog_YYYY_MMDD_HHMMSS__NN` (an archive extension such as `.tar.gz` is allowed).
The timestamp is a local, naive Case time and is only a selection signal, not proof that
the incident happened at that time.

1. Take the reported incident date and time from validated Jira context. A time without a
   reliable date, ambiguous cross-midnight context, or unknown Boot identity is a limitation;
   do not guess from the APLog sequence number.
2. Call `inspect_archive` with `time_range` and `neighbor_count=1`. The tool selects starts
   in the requested range, the latest predecessor and earliest successor, ordered by parsed
   start. It never assumes a fixed duration per volume.
3. Use only returned stable `member_id` values with `extract_archive_members`. Never pass a
   bare member path. Reuse an item that already returns `extracted=true` and `artifact_id`.
4. Build a log timeline and validate the actual incident window through ANR, crash, watchdog,
   SurfaceFlinger/HWC, reboot, or other logs. Keep clock domains separate until anchored.
