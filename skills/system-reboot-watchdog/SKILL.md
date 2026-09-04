---
name: system-reboot-watchdog
description: Analyze unexpected reboot, boot loop, watchdog reset, kernel panic, or system-server watchdog cases across reboot identity, previous-boot evidence, kernel, pstore, Android, MCU, and hypervisor domains.
category: symptom
---

# System Reboot and Watchdog

Determine which domain initiated the reset and what stalled or failed immediately beforehand. Boot-completion symptoms after the reset are consequences unless evidence shows an independent boot failure.

## Establish reboot identity

Call `open_case` and `inspect_case`. Separate previous-boot evidence from the new boot using reboot reason, boot IDs, uptime, pstore/ramoops, archive round metadata, or shared reset markers. Never infer previous/current boot solely from directory numbering.

## First evidence pass

Use `parse_diagnostics` for kernel stacks, Fatal, ANR, and watchdog signals. Build a timeline around the last healthy operation, watchdog bite/bark, panic/oops, reset request, shutdown, MCU or hypervisor reset, and the next boot start.

## Branch on reset ownership

- **Kernel panic/oops:** follow the first fault and call trace; later shutdown noise is not the initiating cause.
- **Android/SystemServer watchdog:** identify the blocked checker, held lock, Binder or service dependency and whether the kernel remained responsive.
- **Hardware/MCU/hypervisor reset:** correlate reset reason and cross-domain markers; require a clock anchor before ordering guest and host events.
- **Power loss or brownout:** look for abrupt log termination and power-controller evidence; absence of a software panic does not prove hardware failure.
- **Boot loop without confirmed reset cause:** analyze the repeated failing boot stage while keeping the original reset cause unresolved.

## Evidence and stopping

Report reset owner, last successful transition, first fatal or missing transition, and the evidence tying it to a specific boot. Return `insufficient_evidence` when only post-reboot logs exist or reboot identity cannot be established.

## When to activate code-search

If the kernel panic call trace, watchdog bite, or pstore log names a specific function or module, activate `code-search` to:
- Search for the function in the call trace with `opengrok_search_code` (search_type="defs", projects=["yocto"] for kernel code).
- Read the faulting code path with `locode_read_file` (20-40 lines around the call trace site).
- Check recent commits with `locode_get_history` — a driver or subsystem change near the first occurrence date is a strong regression signal.
- Use `locode_get_blame` on the faulting line to identify the module owner.

Do not activate code-search if the call trace is absent or if the reboot is confirmed as pure power-loss / hardware reset with no software anchoring.
