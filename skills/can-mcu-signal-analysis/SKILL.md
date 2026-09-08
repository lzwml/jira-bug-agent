---
name: can-mcu-signal-analysis
description: Analyze missing, stale, invalid, duplicated, or mistimed CAN/MCU signals and their application-visible effects using signal identity, capture timing, MCU state, transport, decoding, and consumer evidence.
category: symptom
symptom_family: can_mcu
required_coverage_contract: can-mcu-v1
---

# CAN and MCU Signal Analysis

Determine whether the expected signal was produced, transported, decoded, accepted, and consumed. Do not infer signal correctness from bus activity volume alone.

## Establish signal identity

Call `open_case` and `inspect_case`. Record bus/channel, CAN ID and signal when known, expected value or transition, capture clock semantics, incident window, MCU state, and consuming service/application. Request the mapping or DBC when raw frames cannot be interpreted reliably.

## First evidence pass

Build the shortest end-to-end timeline: source state change, MCU transmit, bus observation, gateway/transport, decoder update, consumer decision, and user-visible result. Search only for the relevant ID, signal, request/correlation ID, state transition, timeout, or reset.

## Branch on the signal path

- **Not produced:** inspect MCU/source preconditions, power state and state-machine transition.
- **Produced but absent on capture:** verify channel, filters, gateway routing, bus errors and capture coverage.
- **Present but invalid/stale:** compare counter, checksum, timeout, value range, period and decoding version.
- **Decoded but not consumed:** inspect subscription, service lifecycle, cache/state gating and consumer logs.
- **Consumed but UI/action is wrong:** continue at application or HMI logic rather than blaming transport.

## Evidence and stopping

Report the last successful stage and first failed or missing stage with raw timestamp and clock domain. Without signal definitions, capture semantics, or consumer evidence, narrow the boundary but do not claim a decoded business meaning.
