"""持久化 RCA 状态与审计事件的数据模型。"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from .contracts import (
    ActionItem,
    CoverageItem,
    EvidenceReference,
    NegativeFinding,
    TimelineEntry,
)

ClaimType = Literal["observed_symptom", "failure_mechanism", "root_cause"]
ClaimStatus = Literal["candidate", "supported", "confirmed", "disputed", "rejected", "superseded"]
ClaimConfidence = Literal["high", "medium_high", "medium", "low"]


def stable_claim_id(claim_type: str, statement: str, component: str | None = None) -> str:
    """根据类型、组件和规范化陈述生成跨运行稳定的 Claim ID。"""
    normalized = re.sub(r"\s+", " ", statement.strip().casefold())
    key = "|".join((claim_type, component or "", normalized))
    return "clm-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class Claim(BaseModel):
    claim_id: str
    type: ClaimType
    statement: str = Field(min_length=1)
    component: str | None = None
    status: ClaimStatus = "candidate"
    confidence: ClaimConfidence = "low"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    falsification: str | None = None
    created_run_id: str
    superseded_by: str | None = None
    replaced_by: str | None = None


class RCAMetadata(BaseModel):
    task_id: str
    steps: int = 0
    skills: list[str] = Field(default_factory=list)


class RCAState(BaseModel):
    revision: int = Field(ge=1)
    updated_at: str
    based_on_runs: list[str] = Field(default_factory=list)
    conclusion_status: Literal["confirmed", "hypothesis_only", "insufficient_evidence"]
    summary: str
    observed_symptom: str | None = None
    failure_mechanism: str | None = None
    root_cause: str | None = None
    trigger_conditions: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    coverage: list[CoverageItem] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    negative_findings: list[NegativeFinding] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    actions: list[ActionItem] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    metadata: RCAMetadata | None = None


class RCAEventDetail(BaseModel):
    claim_id: str | None = None
    from_status: ClaimStatus | None = None
    to_status: ClaimStatus | None = None
    reason: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class RCAEvent(BaseModel):
    revision: int = Field(ge=1)
    event: Literal[
        "claim_created", "claim_updated", "claim_status_changed",
        "root_cause_changed", "evidence_added", "state_initialized",
    ]
    run_id: str
    detail: RCAEventDetail = Field(default_factory=RCAEventDetail)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
