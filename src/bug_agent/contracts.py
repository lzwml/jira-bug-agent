"""Worker 对外稳定契约。

这些模型不暴露 MCP Session、LLM message 或 Provider 类型，因此部门 Workflow、
CLI、HTTP API 和任务队列可以共享同一组输入输出。
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .models import ToolEvent


class BugAnalysisTask(BaseModel):
    task_id: str = Field(
        default_factory=lambda: str(uuid4()),
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    source: Literal["jira", "local"]
    issue_key: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
    case_path: str | None = None
    objective: str = Field(default="定位 Bug 根因并给出下一步建议", min_length=1, max_length=2000)
    max_steps: int | None = Field(default=None, ge=1, le=100)
    skills: list[str] = Field(default_factory=lambda: ["android-log-triage"], max_length=5)
    include_trace: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_source_reference(self) -> "BugAnalysisTask":
        if self.source == "jira" and not self.issue_key:
            raise ValueError("source=jira 时必须提供 issue_key")
        if self.source == "local" and not self.case_path:
            raise ValueError("source=local 时必须提供 case_path")
        if self.source == "jira" and self.case_path:
            raise ValueError("source=jira 时不能同时提供 case_path")
        if self.source == "local" and self.issue_key:
            raise ValueError("source=local 时不能同时提供 issue_key")
        return self


class EvidenceReference(BaseModel):
    evidence_id: str
    artifact_id: str | None = None
    relative_path: str
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    excerpt: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_line_range(self) -> "EvidenceReference":
        if self.line_start and self.line_end and self.line_end < self.line_start:
            raise ValueError("line_end 不能小于 line_start")
        return self


class Hypothesis(BaseModel):
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["candidate", "supported", "rejected"] = "candidate"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    falsification: str | None = None


class RCAReport(BaseModel):
    conclusion_status: Literal["confirmed", "hypothesis_only", "insufficient_evidence"]
    summary: str
    confirmed_facts: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class BugAnalysisResult(BaseModel):
    task_id: str
    status: Literal["completed", "insufficient_evidence", "max_steps", "failed"]
    report: RCAReport
    steps: int = Field(ge=0)
    structured_output: bool
    applied_skills: list[str] = Field(default_factory=list)
    trace: list[ToolEvent] = Field(default_factory=list)
    error: str | None = None
