---
name: android-native-crash
description: Analyze Android native crashes and native service deaths from tombstones, debuggerd or AEE records, signals, abort messages, native stacks, process lifecycle, and downstream effects.
category: symptom
---

# Android Native Crash

Identify the crashing process and the first actionable failure in its native execution path. Separate the crash mechanism from the user-visible consequence.

## Establish identity

Call `open_case` and `inspect_case`. Match process name, PID/TID, signal, tombstone or AEE identity, ABI/build, crash time, and boot round. Multiple tombstones from different restarts are separate incidents unless evidence links them.

## First evidence pass

Use `parse_diagnostics` for Fatal signals, then inspect the matching tombstone/AEE record and same-window process lifecycle. Build a focused timeline around debuggerd, crash_dump, `Fatal signal`, abort messages, service death, restart, and the reported symptom.

## Branch on crash mechanism

- **Explicit abort/assert:** trace the abort message and preceding failed invariant; the aborting frame may only enforce an earlier failure.
- **Invalid memory access:** use fault address, signal code, crashing thread and symbolized frames to test ownership or lifetime hypotheses.
- **Watchdog-triggered native death:** identify the monitored operation and dependency rather than treating the kill as the original fault.
- **Corrupt or unsymbolized stack:** rely on build identity and request symbols or a matching binary before naming a source-level cause.
- **Service death without crash identity:** investigate LMK, kill, restart policy, Binder death, or upstream system failure as alternatives.

## Evidence and stopping

Report crash identity, first relevant failing frame or invariant, preceding trigger, and downstream effect with distinct evidence. If symbols, matching build, complete tombstone, or incident identity is missing, state the resulting confidence limit.
