---
name: android-anr-ui-freeze
description: Analyze Android ANR, frozen UI, input timeout, or application-not-responding symptoms using process identity, ANR traces, thread states, Binder dependencies, locks, I/O, and same-window logs.
category: symptom
symptom_family: anr_freeze
required_coverage_contract: anr-freeze-v1
---

# Android ANR and UI Freeze

Determine what prevented the affected process from completing the expected UI or service work. Do not equate a long pause, dropped frames, or an unrelated ANR record with the reported incident.

## Establish identity

Call `open_case` and `inspect_case`. Identify the affected process/package, PID when available, ANR reason, incident time, boot round, foreground component, and matching trace. If these cannot be matched, keep the result at `insufficient_evidence`.

## First evidence pass

Use `parse_diagnostics` for ANR and Fatal signals. Build a focused timeline around `ANR in`, input dispatch timeout, process lifecycle, activity/service transitions, watchdog, and the affected component. Read the matching main-thread trace before expanding searches.

## Branch on the blocked work

- **Binder wait:** identify the remote process/service and whether its Binder pool or dependency is stalled.
- **Monitor contention:** identify the lock owner and the operation holding it; a waiting thread alone does not identify the cause.
- **I/O or kernel wait:** correlate the blocked call with storage, network, device service, or kernel evidence in the same window.
- **Runnable or busy main thread:** find the long-running callback, loop, repeated work, or event storm and its initiating transition.
- **No matching blocked state:** test whether the visible freeze came from rendering, system-wide load, process death/restart, or a different process.

## Evidence and stopping

Connect the user-visible timeout, the matching process/trace, and the dependency or operation that failed to complete. Report contradictory evidence and the cheapest observation that could distinguish remaining hypotheses. Do not assign root cause from ANR reason text alone.

## When to activate code-search

If the ANR trace names a specific function, lock, or Binder interface that appears stalled, activate `code-search` to:
- Search for the blocking function or lock owner path with `opengrok_search_code` (search_type="defs" or "refs").
- Read the relevant code with `locode_read_file` to understand the blocking logic.
- Check recent commits to the affected file with `locode_get_history` — a synchronization or Binder change near the first occurrence date is a strong regression signal.

Do not activate code-search if the ANR is purely a downstream symptom of OOM, system load, or I/O without a specific code-level hypothesis.
