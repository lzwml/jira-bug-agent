"""Worker 对外稳定契约。

这些模型不暴露 MCP Session、LLM message 或 Provider 类型，因此部门 Workflow、
CLI、HTTP API 和任务队列可以共享同一组输入输出。
"""

from __future__ import annotations

from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from .models import HumanCheckpoint, TokenUsage, ToolEvent


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
    # 续分析时指向上一轮任务。它是审计关系，不改变 Case 的来源或权限边界。
    continuation_of: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    max_steps: int | None = Field(default=None, ge=1)
    # goal_mode=True 时不限制工具调用次数，循环直到模型给出最终答案。
    # 此时 max_steps 仅用于对外报告（如果设置的话），不参与循环控制。
    goal_mode: bool = False
    # None 表示使用默认分诊 Skill，并允许 Agent 按证据自动激活专项 Skill；
    # 非空列表表示调用方预先指定的 Skill，自动激活仍可补充兼容的专项/平台 Skill。
    skills: list[str] | None = Field(default=None, min_length=1, max_length=5)
    auto_select_skills: bool = True
    include_trace: bool = False
    # 生成独立的、面向工程师的问题分析讲解；不改变正式 RCA 内容。
    include_analysis_guide: bool = False
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
        if self.continuation_of == self.task_id:
            raise ValueError("continuation_of 不能指向任务自身")
        return self


class EvidenceReference(BaseModel):
    evidence_id: str
    artifact_id: str | None = None
    relative_path: str
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    excerpt: str | None = Field(default=None, max_length=4000)
    # 视频帧不具备行号；timestamp_ms 保留可复核的画面时间锚点。
    timestamp_ms: int | None = Field(default=None, ge=0)
    frame_path: str | None = None

    @model_validator(mode="after")
    def validate_line_range(self) -> "EvidenceReference":
        if self.line_start and self.line_end and self.line_end < self.line_start:
            raise ValueError("line_end 不能小于 line_start")
        return self


class IncidentWindow(BaseModel):
    """一次事故的时间边界；reported 与 verified 必须分别表达。"""

    start: str | None = None
    end: str | None = None
    clock_domain: Literal[
        "wall", "android", "kernel_monotonic", "reported", "unknown"
    ] = "unknown"
    source: Literal["reported", "log", "archive", "video", "unknown"] = "unknown"


# Investigation state is deliberately separate from RCAReport.  The former is the
# auditable control-plane record used while investigating; the latter remains the
# stable engineer-facing report schema.
class IncidentProfile(BaseModel):
    symptom_family: Literal[
        "reboot", "native_crash", "anr_freeze", "display", "audio", "network",
        "ota", "can_mcu", "virtualization", "unknown",
    ] = "unknown"
    reported_window: IncidentWindow | None = None
    target_components: list[str] = Field(default_factory=list)
    target_processes: list[str] = Field(default_factory=list)
    reboot_suspected: bool = False
    user_visible_symptom: str = ""
    trigger_context: str = ""
    source_refs: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class InvestigationHypothesis(BaseModel):
    hypothesis_id: str = Field(pattern=r"^hyp-[A-Za-z0-9._-]+$")
    claim: str = Field(min_length=1, max_length=4000)
    role: Literal["primary", "competing", "downstream"] = "competing"
    status: Literal["open", "supported", "contradicted", "confirmed", "blocked"] = "open"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    required_observations: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    next_falsification: str = ""


class InvestigationCoverage(BaseModel):
    coverage_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    domain: Literal["android", "linux", "hypervisor", "mcu", "can", "video", "code"]
    stream: str = Field(min_length=1, max_length=200)
    boot_identity: str | None = Field(default=None, max_length=500)
    target_window: IncidentWindow | None = None
    status: Literal["planned", "checked", "missing", "unavailable", "not_applicable"] = "planned"
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class InvestigationCandidate(BaseModel):
    candidate_id: str = Field(pattern=r"^cand-[A-Za-z0-9._-]+$")
    artifact_ids: list[str] = Field(default_factory=list)
    query_or_action: str = Field(min_length=1, max_length=4000)
    tests_hypothesis_ids: list[str] = Field(default_factory=list)
    fills_coverage_ids: list[str] = Field(default_factory=list)
    selection_reasons: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class InvestigationState(BaseModel):
    """Structured, evidence-bound planning state persisted in the Run Bundle."""

    mode: Literal["false", "shadow", "enforce"] = "shadow"
    incident_profile: IncidentProfile | None = None
    hypotheses: list[InvestigationHypothesis] = Field(default_factory=list)
    coverage: list[InvestigationCoverage] = Field(default_factory=list)
    candidates: list[InvestigationCandidate] = Field(default_factory=list)
    known_evidence_ids: list[str] = Field(default_factory=list)
    known_artifact_ids: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


class IncidentIdentity(BaseModel):
    """防止跨 boot、跨进程或跨复现错误拼接证据的事故身份。"""

    incident_id: str | None = Field(default=None, max_length=200)
    reported_window: IncidentWindow | None = None
    verified_window: IncidentWindow | None = None
    boot_identity: str | None = Field(default=None, max_length=500)
    process_name: str | None = Field(default=None, max_length=500)
    pid: int | None = Field(default=None, ge=0)
    build_identity: str | None = Field(default=None, max_length=1000)
    system_domain: str | None = Field(default=None, max_length=200)
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["candidate", "supported", "rejected"] = "candidate"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    falsification: str | None = None


class TimelineEntry(BaseModel):
    timestamp: str
    clock_domain: Literal["wall", "android", "kernel_monotonic", "reported", "unknown"]
    event: str
    interpretation: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class CoverageItem(BaseModel):
    layer: str
    status: Literal["covered", "partial", "not_covered", "not_applicable"]
    finding: str
    evidence_ids: list[str] = Field(default_factory=list)
    gap: str | None = None


class NegativeFinding(BaseModel):
    statement: str
    scope: str
    limitation: str
    evidence_ids: list[str] = Field(default_factory=list)


class ActionItem(BaseModel):
    priority: Literal["P0", "P1", "P2", "P3"]
    action: str
    owner: str | None = None
    expected_artifact: str
    completion_criteria: str


class ReasoningStep(BaseModel):
    """面向工程师的、可核验的问题分析步骤；不是模型内部推理记录。"""

    observation: str = Field(min_length=1)
    question: str = Field(min_length=1)
    reasoning: str = Field(min_length=1)
    verification: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class AnalysisGuide(BaseModel):
    """独立于正式 RCA 的调查思路讲解。"""

    overview: str = Field(min_length=1)
    reasoning_steps: list[ReasoningStep] = Field(default_factory=list)
    reusable_approach: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RCAReport(BaseModel):
    conclusion_status: Literal["confirmed", "hypothesis_only", "insufficient_evidence"]
    summary: str
    incident: IncidentIdentity | None = None
    observed_symptom: str | None = None
    failure_mechanism: str | None = None
    root_cause: str | None = None
    root_cause_evidence_ids: list[str] = Field(default_factory=list)
    trigger_conditions: list[str] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    coverage: list[CoverageItem] = Field(default_factory=list)
    confirmed_facts: list[str] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    negative_findings: list[NegativeFinding] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    actions: list[ActionItem] = Field(default_factory=list)
    # 兼容旧调用方；Renderer 优先使用结构化 actions。
    next_actions: list[str] = Field(default_factory=list)


class SkillActivation(BaseModel):
    name: str
    source: Literal["default", "explicit", "agent"]
    role: Literal["primary", "secondary", "supporting"] = "supporting"
    reason: str = Field(max_length=500)


class ReportValidation(BaseModel):
    grounded: bool
    verified_evidence_count: int = Field(ge=0)
    rejected_evidence_ids: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class BugAnalysisResult(BaseModel):
    task_id: str
    status: Literal[
        "completed", "insufficient_evidence", "max_steps", "failed", "waiting_for_human",
    ]
    report: RCAReport
    steps: int = Field(ge=0)
    structured_output: bool
    report_validation: ReportValidation | None = None
    applied_skills: list[str] = Field(default_factory=list)
    skill_activations: list[SkillActivation] = Field(default_factory=list)
    analysis_guide: AnalysisGuide | None = None
    analysis_guide_error: str | None = None
    trace: list[ToolEvent] = Field(default_factory=list)
    error: str | None = None
    human_checkpoint: HumanCheckpoint | None = None
    token_usage: TokenUsage | None = None
