---
name: linux-virtualization-failure
description: Analyze Linux guest, host, hypervisor, SCP, sensor, or cross-VM service failures where startup, IPC, resource, interrupt, or virtualization boundaries affect an Android/IVI symptom.
category: symptom
symptom_family: virtualization
required_coverage_contract: virtualization-v1
---

# Linux and Virtualization Failure

Locate the guest/host or producer/consumer boundary where the expected transition stops. Do not attribute an Android symptom to Linux merely because Linux logs contain nearby warnings.

## Establish topology and identity

Call `open_case` and `inspect_case`. Identify VM/domain, boot round, service or device, producer and consumer, IPC/transport boundary, and available Linux, hypervisor, SCP and Android evidence. Keep local counters and wall clocks separate until anchored.

## First evidence pass

Build a timeline from the expected service startup or request through transport, host/hypervisor handling, device/SCP activity, response, and Android-side consumption. Search for named services, endpoints, request IDs, device nodes, interrupts, resets, and explicit state transitions derived from the Issue or observed logs.

## Branch on the failed boundary

- **Producer never ready:** inspect service startup, dependency ordering, device probe and resource acquisition.
- **Request sent, no boundary crossing:** inspect IPC channel, shared memory, virtio, routing, permissions, and endpoint lifecycle.
- **Host/device completes, guest misses response:** inspect interrupt delivery, queue state, timeout and consumer lifecycle.
- **Data arrives but is rejected:** compare version, schema, state, freshness and validation evidence.
- **Domain restart or reset:** correlate reboot identities and determine whether it precedes or follows the symptom.

## Evidence and stopping

Prefer paired request/response IDs or an event visible on both sides of a boundary. Report the last confirmed domain transition and first missing or failed one. Without evidence from both sides, preserve the unobserved boundary as missing evidence.
