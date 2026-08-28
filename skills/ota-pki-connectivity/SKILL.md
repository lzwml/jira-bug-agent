---
name: ota-pki-connectivity
description: Analyze OTA, PKI, authentication, certificate, download, and connectivity failures by following the operation state machine across HMI, application, transport, trust, storage, and installation stages.
---

# OTA, PKI and Connectivity Failure

Find the first failed state transition for one operation. Do not treat a later generic network, TLS, or UI error as the original failure without correlation.

## Establish operation identity

Call `open_case` and `inspect_case`. Record operation/session/request ID, ECU or target, package/version, endpoint, incident time, boot round, current stage, and available HMI, client, network, PKI and installer artifacts. Keep credentials and sensitive payloads out of evidence excerpts.

## First evidence pass

Build a state-machine timeline covering request initiation, policy/precondition checks, authentication, metadata, download, verification, staging, installation, reboot and result reporting as applicable. Search by operation IDs, endpoint, package/version and explicit transition names rather than a broad error catalog.

## Branch on the first failed transition

- **Precondition/policy:** identify the rejected state, policy source and evaluated value.
- **DNS/route/transport:** separate name resolution, connection, timeout, reset and server response using client and network evidence.
- **TLS/PKI/authentication:** distinguish trust chain, validity time, hostname, credential/token and authorization failures.
- **Download/storage:** correlate requested ranges, response status, retries, free space and persisted bytes.
- **Integrity/install:** compare manifest, hash/signature, package compatibility, installer state and rollback evidence.
- **Backend succeeds but HMI fails:** follow status propagation and stale-cache/session behavior.

## Evidence and stopping

Report the operation identity, last successful state, first failed state, error owner and downstream presentation. Redact secrets. When server-side, certificate-chain, package or installer evidence is absent, state exactly which transition remains unverified.
