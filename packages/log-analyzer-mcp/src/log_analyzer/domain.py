"""V2 领域模型与 MCP 输入契约。

【学习要点】这个模块定义了整个 log-analyzer-mcp 的"词汇表"：
- 领域模型(Artifact、Evidence、TimelineEvent 等)描述了业务实体的结构；
- 输入契约(OpenCaseInput、SearchEvidenceInput 等)定义了每个工具接受的参数。

这个模块只定义"数据长什么样"，不读取文件，也不包含业务流程。

把模型集中在这里有三个目的：
1. Agent、MCP Server 和测试共享同一套字段定义；
2. MCP Tool Schema 可以直接由 Pydantic 生成，避免手写 Schema 漂移；
3. 将 Jira、本地目录等外部系统的原始数据统一成稳定的领域对象。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ArtifactKind 是 Agent 能理解的附件分类，不等同于文件扩展名。
# 例如 main_log.curf 会由 CaseRegistry 识别为 logcat。
#
# 【学习要点】为什么不直接用文件扩展名？
# - 同一个扩展名可能是不同类型：.txt 可能是 logcat、kernel log、或普通文本；
# - 不同扩展名可能是同一类型：logcat 可能是 main_log.curf、logcat.txt 等；
# - ArtifactKind 是语义分类，由 CaseRegistry 根据文件内容、路径等综合判断。
ArtifactKind = Literal[
    "logcat", "kernel", "anr", "tombstone", "trace",
    "sos", "aee_db", "text", "archive", "binary",
    "platform",    # platform-specific logs: scp/sspm/adsp/mcupm/atf/gz/bsp/tee/hypervisor/vcp/apusys/connsys/wifi_driver/vm_*/dpmaif
]


class Artifact(BaseModel):
    """Case 中一个可独立引用的附件。

    relative_path 可以提供给模型作为证据来源；真实绝对路径只保存在
    CaseRegistry 内部，避免工具结果泄漏或复用不受信任的路径。

    【学习要点】为什么区分 relative_path 和绝对路径？
    - relative_path: 相对于 Case 根目录的路径，可以安全地返回给模型和上游系统；
    - 绝对路径: 只在 Server 内部使用，防止路径泄露和跨 Case 访问。

    【学习要点】origin 和 source_archive_id 的作用：
    - origin="case": 附件直接位于 Case 目录中；
    - origin="archive": 附件是从归档中解压出来的；
    - source_archive_id: 记录来源归档的 artifact_id，便于追溯和缓存管理。
    """

    artifact_id: str
    name: str
    relative_path: str
    kind: ArtifactKind
    size_bytes: int = Field(ge=0)
    modified_at: str | None = None
    readable_text: bool = True
    origin: Literal["case", "archive"] = "case"
    source_archive_id: str | None = None


class CaseInfo(BaseModel):
    """一次 Bug 分析任务的附件清单与总体规模。

    【学习要点】CaseInfo 是 Agent 了解 Case 的第一步：
    - artifact_count 和 total_size_bytes 帮助 Agent 评估工作量；
    - artifacts 列表让 Agent 知道有哪些可用证据；
    - Agent 可以根据 kind 和 size_bytes 决定优先分析哪些附件。
    """

    case_id: str
    name: str
    artifact_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)
    artifacts: list[Artifact] = Field(default_factory=list)


class Evidence(BaseModel):
    """Agent 可以引用和复核的原始证据。

    Evidence 不负责解释根因，只记录"在哪个附件的哪些行看到了什么"。
    根因推理应由 Agent Core 完成。

    【学习要点】Evidence 是 RCA 报告的基础：
    - line_start/line_end: 精确到行号，可以人工复核；
    - content: 原始日志内容，Agent 应该在结论中引用 evidence_id 而非大段复制；
    - timestamp_raw: 保留原始时间戳格式，便于人工验证；
    - query: 记录是通过什么搜索词找到的，便于追溯分析过程。

    【学习要点】为什么需要 evidence_id？
    Agent 生成的 RCA 报告中会引用证据，例如"根据 ev-123 和 ev-456，
    确认 SurfaceFlinger 在 12:34:56 发生 Fatal"。evidence_id 是稳定标识，
    让人和其他系统可以追溯到原始日志位置。
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

    【学习要点】Android 系统中存在多个时钟域：
    - wall: 墙上时间(用户可见的日期时间)，来自系统时钟；
    - android: Android logcat 时间戳，格式如 "08-28 12:34:56.789"；
    - kernel_monotonic: 内核单调时间，从 boot 开始的秒数，如 "[ 123.456]"；
    - unknown: 无法识别的时间格式。

    【学习要点】为什么不能直接换算？
    - Kernel monotonic 不包含日期信息，无法直接对应到墙上时间；
    - 除非日志中有同步点(如 "kernel time = xxx, wall time = yyy")，
      否则不能直接说 "kernel 时间 123.456 就是 12:34:56"；
    - Skill 中会指导 Agent 如何处理跨时钟域的证据。
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
    """确定性解析器发现的异常结构，不代表最终 RCA 结论。

    【学习要点】DiagnosticFinding vs Evidence 的区别：
    - Evidence: 原始日志行，Agent 需要自己判断是否重要；
    - DiagnosticFinding: 经过确定性解析器(如正则、结构化解析)识别出的异常，
      例如 SELinux AVC 拒绝、Kernel Call Trace、Fatal Exception。

    【学习要点】为什么"不代表最终 RCA 结论"？
    解析器只能识别"这里有一个 Fatal"，但不能判断"这个 Fatal 是否导致了
    用户可见的黑屏"。根因推理需要 Agent 结合症状、时间窗口、多个证据综合判断。
    这就是架构文档中说的"Core 不决定某条 Fatal 是否是当前 Bug 的根因"。
    """

    finding_id: str
    diagnostic_type: Literal["avc", "kernel_stack", "fatal", "anr"]
    severity: Literal["info", "warning", "critical"]
    summary: str
    evidence: Evidence
    attributes: dict = Field(default_factory=dict)


# ========== 工具输入契约 ==========
#
# 【学习要点】每个 Input Model 对应一个 MCP 工具的参数。
# 设计原则：
# 1. 所有数量字段都有上限，防止模型意外请求无限结果；
# 2. ID 字段(如 case_id、artifact_id)不允许为空；
# 3. 可选字段提供合理默认值；
# 4. Field(description=...) 会出现在 MCP Schema 中，指导模型正确使用。
#
# 【学习要点】为什么用 Pydantic 而不是 dict？
# - 类型安全：Pydantic 自动验证类型，如 max_results 必须是 int；
# - 约束验证：ge/le/min_length/max_length 等约束自动检查；
# - 文档生成：Field 的 description 会成为 Schema 的一部分；
# - IDE 支持：自动补全和类型检查。
# ==========


class OpenCaseInput(BaseModel):
    """open_case 的输入；case_path 仍需经过服务端根目录授权。

    【学习要点】case_path 是 Agent 提供的相对或绝对路径，
    但 CaseRegistry 会验证它是否在 LOG_ANALYZER_ALLOWED_ROOTS 下。
    这是防止路径穿越攻击的第一道防线。
    """

    case_path: str = Field(min_length=1, description="允许根目录内的 Bug 案例目录")


class InspectCaseInput(BaseModel):
    """inspect_case 的输入。sample_limit 防止一次返回全部附件。

    【学习要点】sample_limit 的作用：
    一个 Case 可能有几百个附件，全部返回会占用大量 token。
    Agent 可以先获取前 50 个样本，了解 Case 的大致内容，
    然后根据需要深入分析特定附件。
    """

    case_id: str = Field(
        min_length=1,
        description="open_case 返回的 Case 标识；必须引用当前会话已注册的 Case，不能填写文件路径。",
    )
    sample_limit: int = Field(
        default=50, ge=1, le=200,
        description="最多返回多少个附件样本；Case 较大时先用默认值了解材料分布。",
    )


class PrepareCaseInput(BaseModel):
    """安全展开归档并为文本附件建立持久化分块索引。

    资源上限全部来自服务端环境配置，调用者只能缩小选择范围，不能自行扩大预算。

    【学习要点】这是"全量准备"的兜底工具：
    - extract_archives=True: 展开所有 ZIP/TAR/GZIP；
    - build_index=True: 为所有文本附件建立 SQLite 索引；
    - force_rebuild=True: 忽略缓存，强制重新处理。

    【学习要点】为什么 artifact_ids 可以缩小范围？
    默认情况下 prepare_case 处理所有附件，但 Agent 可以指定
    只处理特定的 artifact_ids，避免不必要的计算。
    但无论如何都不能超过服务端配置的预算上限(如最大解压字节数)。
    """

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    artifact_ids: list[str] = Field(
        default_factory=list, max_length=100,
        description="只处理这些附件 ID；留空表示处理 Case 内所有符合条件的附件。",
    )
    extract_archives: bool = Field(default=True, description="是否递归展开受支持的归档。")
    build_index: bool = Field(default=True, description="是否为可读文本附件建立搜索索引。")
    force_rebuild: bool = Field(default=False, description="是否忽略已有缓存并重新处理。")


class ArchiveTimeRange(BaseModel):
    """归档成员路径可见时间的本地查询范围，不隐式换算时区。"""

    start: datetime = Field(description="本地时间，格式 YYYY-MM-DDTHH:MM:SS")
    end: datetime = Field(description="本地时间，格式 YYYY-MM-DDTHH:MM:SS，必须不早于 start")

    def model_post_init(self, __context) -> None:
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise ValueError("time_range 不接受时区；请提供本地 naive 时间")
        if self.end < self.start:
            raise ValueError("time_range.end 必须不早于 time_range.start")


class InspectArchiveInput(BaseModel):
    """只读归档成员清单，不将成员内容解压到磁盘。

    【学习要点】这是渐进式调查的关键工具：
    Agent 先查看归档里有什么(成员名、大小、类型)，
    然后根据症状和时间窗口选择需要解压的成员，
    而不是一上来就全量解压几个 GB 的 ZIP。

    【学习要点】member_offset 用于分页：
    一个 ZIP 可能有几万个成员，一次返回会超时。
    Agent 可以通过多次调用(调整 offset)逐步浏览成员清单。

    【学习要点】source_sha256 的作用：
    用于缓存验证。如果 Agent 之前已经检查过这个归档，
    可以提供 SHA256 哈希值，Server 会快速判断归档是否变化。
    """

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    artifact_id: str = Field(
        min_length=1,
        description="open_case 或 inspect_case 返回的归档附件 ID，不能填写归档路径。",
    )
    member_offset: int = Field(
        default=0, ge=0, le=1_000_000,
        description="成员分页起点；继续读取时使用上次返回的下一偏移量。",
    )
    source_sha256: str | None = Field(
        default=None, min_length=64, max_length=64,
        description="可选的归档 SHA-256，用于确认本次查看的仍是同一份归档。",
    )
    max_members: int = Field(
        default=1000, ge=1, le=5000,
        description="本页最多返回的归档成员数；不是解压数量。",
    )
    path_prefix: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="只返回此前缀下的成员；前缀必须来自此前返回的 member_path 或 time_groups",
    )
    time_range: ArchiveTimeRange | None = Field(
        default=None,
        description="按成员路径/文件名中可解析的本地时间过滤；不会推断日志内容时间或领域相邻成员",
    )
    time_neighbor_count: int = Field(
        default=0,
        ge=0,
        le=3,
        description="路径时间查询时，额外返回窗口前后各 N 个可解析时间成员；不包含任何领域语义",
    )


class ExtractArchiveMembersInput(BaseModel):
    """通过 inspect_archive 返回的稳定 member_id 选择性解压。

    【学习要点】member_ids 来自 inspect_archive 的返回结果，
    是稳定标识符，不是文件路径。Server 内部会映射到实际的归档成员，
    防止 Agent 构造恶意路径。
    """

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    artifact_id: str = Field(min_length=1, description="inspect_archive 检查过的归档附件 ID。")
    member_ids: list[str] = Field(
        min_length=1, max_length=200,
        description="inspect_archive 或 probe_archive_members 返回的稳定成员 ID；不能填写成员路径。",
    )
    force_rebuild: bool = Field(default=False, description="是否覆盖该成员已有的安全解压缓存。")


class ExtractAeeDbInput(BaseModel):
    """Decode one registered MTK AEE DB artifact with the server-controlled extractor."""

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    artifact_id: str = Field(min_length=1, description="inspect_case 返回的 aee_db artifact_id")


class ProbeArchiveMembersInput(BaseModel):
    """在不落盘、不展开整个归档的前提下，读取成员的有界内容样本。"""

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    artifact_id: str = Field(min_length=1, description="inspect_archive 检查过的归档附件 ID。")
    member_ids: list[str] = Field(
        min_length=1, max_length=50,
        description="需要读取前缀样本的稳定成员 ID，来自 inspect_archive。",
    )
    incident_time_range: ArchiveTimeRange | None = Field(
        default=None,
        description=(
            "本次探测要验证的已上报事故时间窗；工具会在返回中原样保留，供模型和人工"
            "核对所选成员是否覆盖目标时段。它不会替代 content_time_ranges 的实际日志覆盖。"
        ),
    )
    max_bytes_per_member: int = Field(
        default=64 * 1024, ge=1024, le=256 * 1024,
        description="每个成员最多读取的未压缩字节数；服务端仍会施加总预算。",
    )


class BuildIndexInput(BaseModel):
    """把指定 Artifact 增量加入该 Case 的持久化日志索引集合。

    【学习要点】索引的作用：
    - 加速搜索：没有索引时，search_evidence 需要扫描所有文件；
    - 持久化：索引保存在 SQLite 中，同一 Case 的多次分析可以复用；
    - 增量构建：Agent 可以先索引一部分文件，后续逐步扩大范围。

    【学习要点】为什么 artifact_ids 上限是 500 而不是 100？
    索引构建是批量操作，一次处理多个文件可以分摊 I/O 开销。
    但也不能无限大，防止单次操作超时。
    """

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    artifact_ids: list[str] = Field(
        min_length=1, max_length=500,
        description="本轮要加入搜索索引的文本附件 ID；通常来自解压或解码工具返回。",
    )
    force_rebuild: bool = Field(default=False, description="是否忽略这些附件已有的索引缓存。")


class SearchEvidenceInput(BaseModel):
    """search_evidence 的输入契约。

    所有数量字段都有上限，防止模型意外请求无限结果或巨大上下文。
    V2 只支持字面量搜索，刻意不开放任意正则执行。

    【学习要点】为什么不支持正则？
    1. 安全：正则可能有 ReDoS(正则拒绝服务)风险；
    2. 可控：字面量搜索的性能可预测，正则可能因为复杂度爆炸而超时；
    3. 简化：字面量搜索足够覆盖大多数 Bug 分析场景。

    【学习要点】context_before/after 的作用：
    单行日志往往不足以理解问题，需要前后几行的上下文。
    但也不能返回太多，避免结果过大。

    【学习要点】artifact_kinds 用于按类型过滤：
    例如 Agent 可以只在 logcat 中搜索 "Fatal"，而不搜索 kernel log。
    """

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    query: str = Field(
        min_length=1, max_length=256,
        description="要在日志中查找的字面量文本，不支持正则表达式。",
    )
    artifact_ids: list[str] = Field(
        default_factory=list, max_length=100,
        description="只在这些附件 ID 中搜索；留空表示搜索当前已索引范围。",
    )
    artifact_kinds: list[ArtifactKind] = Field(
        default_factory=list, max_length=20,
        description="按日志类型进一步限制搜索范围；留空表示不按类型过滤。",
    )
    case_sensitive: bool = Field(default=False, description="是否区分查询文本的大小写。")
    context_before: int = Field(default=3, ge=0, le=20, description="每个命中前附带的上下文行数。")
    context_after: int = Field(default=3, ge=0, le=20, description="每个命中后附带的上下文行数。")
    max_results: int = Field(default=50, ge=1, le=200, description="本次最多返回的 Evidence 数量。")


class ExtractTimelineInput(BaseModel):
    """extract_timeline 的输入；anchors 是当前调查关注的关键事件。

    【学习要点】anchors 由 Skill 提供：
    例如黑屏分析的 Skill 会建议 anchors 包含 ["bootanimation", "SurfaceFlinger",
    "HWC", "present"] 等关键词。MCP 不再内置"黑屏/启动"等调查策略，
    这是架构文档中"Skill 决定分析顺序、时间线锚点"的体现。

    【学习要点】year_hint 用于解析不完整时间戳：
    某些日志格式只有 "08-28 12:34:56" 没有年份，year_hint 帮助解析器补全。
    """

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    # 领域锚点由 Skill 提供；MCP 不再内置"黑屏/启动"等调查策略。
    anchors: list[str] = Field(
        min_length=1, max_length=30,
        description="由当前 Skill 或假设给出的关键事件字面量，例如 watchdog、bootanimation。",
    )
    artifact_ids: list[str] = Field(
        default_factory=list, max_length=100,
        description="只从这些附件中提取事件；留空表示使用当前已索引范围。",
    )
    year_hint: int | None = Field(
        default=None, ge=2000, le=2100,
        description="日志时间戳缺少年份时使用的年份提示；不能用于跨时钟域推断。",
    )
    max_events: int = Field(default=200, ge=1, le=1000, description="本次最多返回的时间线事件数。")


class ParseDiagnosticsInput(BaseModel):
    """parse_diagnostics 的输入；只允许服务端实现的诊断类型。

    【学习要点】diagnostic_types 是白名单：
    只允许服务端已实现的 Android 稳定性诊断类型，
    因为这是 log_analysis_core 实现的确定性解析器。
    未来新增诊断类型需要先在 Core 中实现解析逻辑。
    """

    case_id: str = Field(min_length=1, description="open_case 返回的当前 Case 标识。")
    diagnostic_types: list[Literal[
        "avc", "kernel_stack", "fatal", "anr", "watchdog", "kernel_panic",
        "hung_task", "lmk_oom", "binder_stall",
    ]] = Field(
        default_factory=lambda: [
            "avc", "kernel_stack", "fatal", "anr", "watchdog", "kernel_panic",
            "hung_task", "lmk_oom", "binder_stall",
        ],
        min_length=1,
        max_length=9,
        description="要运行的确定性诊断类型白名单；应结合症状选择，避免无目的全扫。",
    )
    artifact_ids: list[str] = Field(
        default_factory=list, max_length=100,
        description="只诊断这些附件 ID；留空表示检查当前适用的已索引附件。",
    )
    max_findings: int = Field(default=100, ge=1, le=500, description="本次最多返回的结构化诊断发现数。")


# ========== Jira Case Comments 领域模型 ==========


class GetCaseCommentInput(BaseModel):
    """按稳定 comment_id 读取原文片段，不接受任何裸文件路径。

    offset/limit 是字符分页，不是评论列表分页。这样即使单条评论很长，模型也能
    按需精读而不会把整条原文一次塞进上下文。
    """

    case_id: str = Field(min_length=1, description="已注册的 Case ID")
    comment_id: str = Field(min_length=1, max_length=200, description="issue.json 中的 comment_id")
    offset: int = Field(default=0, ge=0, le=10_000_000, description="评论正文字符偏移")
    limit: int = Field(default=4000, ge=1, le=20_000, description="本页最多返回的字符数")
