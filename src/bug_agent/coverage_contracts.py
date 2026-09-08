"""Versioned minimum-coverage contracts for symptom Skills.

These are intentionally small, stable gates.  Skill prose explains *how* to
investigate; this module supplies the identifiers and minimum observable
coverage that later validators can deterministically evaluate.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoverageRequirement:
    coverage_id: str
    domain: str
    streams: tuple[str, ...]
    condition: str


@dataclass(frozen=True)
class CoverageContract:
    name: str
    requirements: tuple[CoverageRequirement, ...]


CONTRACTS: dict[str, CoverageContract] = {
    "reboot-v1": CoverageContract("reboot-v1", (
        CoverageRequirement("android-pre-reset", "android", ("main", "system", "events", "crash"), "reported reboot or watchdog"),
        CoverageRequirement("android-post-reset", "android", ("main", "system", "events"), "reported reboot or watchdog"),
        CoverageRequirement("reset-mechanism", "android", ("kernel", "aee", "anr", "tombstone"), "available artifacts"),
    )),
    "native-crash-v1": CoverageContract("native-crash-v1", (
        CoverageRequirement("crash-identity", "android", ("tombstone", "aee", "crash"), "available artifacts"),
        CoverageRequirement("process-lifecycle", "android", ("main", "system", "events"), "matching incident window"),
    )),
    "anr-freeze-v1": CoverageContract("anr-freeze-v1", (
        CoverageRequirement("anr-trace", "android", ("anr", "main", "system"), "ANR or freeze symptom"),
        CoverageRequirement("dependency", "android", ("events", "kernel"), "blocked operation requires cross-layer check"),
    )),
    "display-v1": CoverageContract("display-v1", (
        CoverageRequirement("app-window", "android", ("main", "system", "events"), "display symptom"),
        CoverageRequirement("composition", "android", ("system", "kernel"), "display symptom"),
        CoverageRequirement("power-panel", "android", ("system", "kernel"), "display symptom"),
    )),
    "can-mcu-v1": CoverageContract("can-mcu-v1", (
        CoverageRequirement("signal-producer", "mcu", ("mcu", "can"), "signal identity known"),
        CoverageRequirement("signal-consumer", "android", ("main", "system"), "consumer is Android-visible"),
    )),
    "virtualization-v1": CoverageContract("virtualization-v1", (
        CoverageRequirement("producer-domain", "linux", ("linux", "hypervisor"), "cross-domain symptom"),
        CoverageRequirement("consumer-domain", "android", ("main", "system"), "cross-domain symptom"),
    )),
    "ota-connectivity-v1": CoverageContract("ota-connectivity-v1", (
        CoverageRequirement("operation-state", "android", ("main", "system", "events"), "operation identity available"),
        CoverageRequirement("network-or-install", "android", ("network", "kernel"), "failed transition needs owner evidence"),
    )),
}


def get_coverage_contract(name: str) -> CoverageContract:
    try:
        return CONTRACTS[name]
    except KeyError as exc:
        raise ValueError(f"未知 required_coverage_contract: {name}") from exc
