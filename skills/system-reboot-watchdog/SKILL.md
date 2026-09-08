---
name: system-reboot-watchdog
description: Analyze unexpected reboot, boot loop, watchdog reset, kernel panic, or system-server watchdog cases across reboot identity, previous-boot evidence, kernel, pstore, Android, MCU, and hypervisor domains.
category: symptom
symptom_family: reboot
required_coverage_contract: reboot-v1
---

# System Reboot and Watchdog

Determine which domain initiated the reset and what stalled or failed immediately beforehand. Boot-completion symptoms after the reset are consequences unless evidence shows an independent boot failure.

## Establish reboot identity

Call `open_case` and `inspect_case`. Separate previous-boot evidence from the new boot using reboot reason, boot IDs, uptime, pstore/ramoops, archive round metadata, or shared reset markers. Never infer previous/current boot solely from directory numbering.

## First evidence pass

Use `parse_diagnostics` for kernel stacks, Fatal, ANR, and watchdog signals. Build a timeline around the last healthy operation, watchdog bite/bark, panic/oops, reset request, shutdown, MCU or hypervisor reset, and the next boot start.

Before proposing a cause, write an evidence-coverage ledger for the reported incident window. For an Android or `system_server` failure, it must cover the Android round immediately before the reset and the next observed boot, and explicitly account for the relevant `main`, `system`, `events`, `crash`, kernel, AEE/ANR/tombstone streams. Mark a stream `checked`, `not present`, or `not yet inspected`; Linux/SOS evidence cannot substitute for missing Android coverage. When probing archived members and the reported local time is known, pass it as `incident_time_range` and compare the echoed target with each `content_time_ranges` result before extraction.

## Branch on reset ownership

- **Kernel panic/oops:** follow the first fault and call trace; later shutdown noise is not the initiating cause.
- **Android/SystemServer watchdog:** identify the blocked checker, held lock, Binder or service dependency and whether the kernel remained responsive.
- **Hardware/MCU/hypervisor reset:** correlate reset reason and cross-domain markers; require a clock anchor before ordering guest and host events.
- **Power loss or brownout:** look for abrupt log termination and power-controller evidence; absence of a software panic does not prove hardware failure.
- **Boot loop without confirmed reset cause:** analyze the repeated failing boot stage while keeping the original reset cause unresolved.

## Verify identity before causality

Keep the initiating failure, diagnostic collection, abort/dump failure, process death, reset, and subsequent boot as separate transitions until evidence connects them. A nearby event, a shared numeric PID/TID, a thread display name, or a zero-match keyword search is not that connection.

For `crash_dump`/ART abort paths, verify the target process, target TID, thread-group ownership and clock/boot identity from process-scoped evidence before deciding whether an Android thread, userspace process, or kernel task was involved. Do not infer ownership from `comm` alone. If two sources appear to assign different identities to the same number, preserve the conflict and seek `/proc`, process list, tombstone header, debuggerd target metadata, or equivalent identity evidence.

Treat every proposed cause as a falsifiable hypothesis until the triggering condition, direct failure mechanism, and reboot consequence are each anchored. Normal GC, lock contention, AEE collection, an application crash, dma-buf warnings, or hypervisor activity may be concurrent or downstream; do not promote any of them from temporal proximity alone.

## Evidence and stopping

Report reset owner, last successful transition, first fatal or missing transition, and the evidence tying it to a specific boot. Return `insufficient_evidence` when only post-reboot logs exist or reboot identity cannot be established.

## When to activate code-search

If the kernel panic call trace, watchdog bite, or pstore log names a specific function or module, activate `code-search` to:
- Search for the function in the call trace with `opengrok_search_code` (search_type="defs", projects=["yocto"] for kernel code).
- Read the faulting code path with `locode_read_file` (20-40 lines around the call trace site).
- Check recent commits with `locode_get_history` — a driver or subsystem change near the first occurrence date is a strong regression signal.
- Use `locode_get_blame` on the faulting line to identify the module owner.

Do not activate code-search if the call trace is absent or if the reboot is confirmed as pure power-loss / hardware reset with no software anchoring.
