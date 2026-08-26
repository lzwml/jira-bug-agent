"""V2 领域模型与 MCP 输入契约。

这个模块只定义“数据长什么样”，不读取文件，也不包含业务流程。

把模型集中在这里有三个目的：
1. Agent、MCP Server 和测试共享同一套字段定义；
2. MCP Tool Schema 可以直接由 Pydantic 生成，避免手写 Schema 漂移；
3. 将 Jira、本地目录等外部系统的原始数据统一成稳定的领域对象。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ArtifactKind 是 Agent 能理解的附件分类，不等同于文件扩展名。
# 例如 main_log.curf 会由 CaseRegistry 识别为 logcat。
ArtifactKind = Literal[
    "logcat", "kernel", "anr", "tombstone", "trace",
    "sos", "text", "archive", "binary",
]


class Artifact(BaseModel):
    """Case 中一个可独立引用的附件。

    relative_path 可以提供给模型作为证据来源；真实绝对路径只保存在
    CaseRegistry 内部，避免工具结果泄漏或复用不受信任的路径。
    """

    artifact_id: str
    name: str
    relative_path: str
    kind: ArtifactKind
    size_bytes: int = Field(ge=0)
    modified_at: str | None = None
    readable_text: bool = True


class CaseInfo(BaseModel):
    """一次 Bug 分析任务的附件清单与总体规模。"""

    case_id: str
    name: str
    artifact_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)
    artifacts: list[Artifact] = Field(default_factory=list)


class Evidence(BaseModel):
    """Agent 可以引用和复核的原始证据。

    Evidence 不负责解释根因，只记录“在哪个附件的哪些行看到了什么”。
    根因推理应由 Agent Core 完成。
    """

    evidence_id: str
    artifact_id: str
    artifact_name: str
    relative_path: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content: str
    timestamp_raw: str | None = None
    query: str | None = None


class TimelineEvent(BaseModel):
    """从日志中抽取的关键事件。

    timestamp_normalized 用于墙上时间排序；relative_seconds 用于 Kernel
    monotonic 时间。两个时钟域没有映射证据时不能直接互相换算。
    """

    event_id: str
    artifact_id: str
    relative_path: str
    line_number: int = Field(ge=1)
    clock_domain: Literal["wall", "android", "kernel_monotonic", "unknown"]
    timestamp_raw: str
    timestamp_normalized: str | None = None
    relative_seconds: float | None = None
    component: str | None = None
    event_type: str
    content: str


class DiagnosticFinding(BaseModel):
    """确定性解析器发现的异常结构，不代表最终 RCA 结论。"""

    finding_id: str
    diagnostic_type: Literal["avc", "kernel_stack", "fatal", "anr"]
    severity: Literal["info", "warning", "critical"]
    summary: str
    evidence: Evidence
    attributes: dict = Field(default_factory=dict)


class OpenCaseInput(BaseModel):
    """open_case 的输入；case_path 仍需经过服务端根目录授权。"""

    case_path: str = Field(min_length=1, description="允许根目录内的 Bug 案例目录")


class InspectCaseInput(BaseModel):
    """inspect_case 的输入。sample_limit 防止一次返回全部附件。"""

    case_id: str = Field(min_length=1)
    sample_limit: int = Field(default=50, ge=1, le=200)


class SearchEvidenceInput(BaseModel):
    """search_evidence 的输入契约。

    所有数量字段都有上限，防止模型意外请求无限结果或巨大上下文。
    V2 只支持字面量搜索，刻意不开放任意正则执行。
    """

    case_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=256)
    artifact_ids: list[str] = Field(default_factory=list, max_length=100)
    artifact_kinds: list[ArtifactKind] = Field(default_factory=list, max_length=20)
    case_sensitive: bool = False
    context_before: int = Field(default=3, ge=0, le=20)
    context_after: int = Field(default=3, ge=0, le=20)
    max_results: int = Field(default=50, ge=1, le=200)


class ExtractTimelineInput(BaseModel):
    """extract_timeline 的输入；anchors 是当前调查关注的关键事件。"""

    case_id: str = Field(min_length=1)
    # 领域锚点由 Skill 提供；MCP 不再内置“黑屏/启动”等调查策略。
    anchors: list[str] = Field(min_length=1, max_length=30)
    artifact_ids: list[str] = Field(default_factory=list, max_length=100)
    year_hint: int | None = Field(default=None, ge=2000, le=2100)
    max_events: int = Field(default=200, ge=1, le=1000)


class ParseDiagnosticsInput(BaseModel):
    """parse_diagnostics 的输入；只允许服务端实现的诊断类型。"""

    case_id: str = Field(min_length=1)
    diagnostic_types: list[Literal["avc", "kernel_stack", "fatal", "anr"]] = Field(
        default_factory=lambda: ["avc", "kernel_stack", "fatal", "anr"],
        min_length=1,
        max_length=4,
    )
    artifact_ids: list[str] = Field(default_factory=list, max_length=100)
    max_findings: int = Field(default=100, ge=1, le=500)
