"""
数据模型定义 —— 使用 Pydantic v2 提供结构化数据验证。

所有工具统一使用 ToolResult 作为返回值包装，确保客户端可以
根据 success 字段判断操作结果，通过 error_code 进行程序化错误处理。
"""

from pydantic import BaseModel, Field
from typing import Optional


class AvcRecord(BaseModel):
    """SELinux AVC 拒绝日志解析后的结构化记录。

    Attributes:
        source_type: 访问发起方（如 httpd_t）
        target_type: 访问目标（如 etc_t）
        target_class: 访问类别（如 file, dir, socket）
        permissions: 被拒绝的权限列表（如 ["read", "write"]）
        permissive: 是否在宽容模式下（true 表示仅记录但不阻止）
        raw_log: 原始日志行
    """
    source_type: str
    target_type: str
    target_class: str
    permissions: list[str]
    permissive: bool
    raw_log: str


class KernelStackFrame(BaseModel):
    """内核调用栈的单个栈帧。"""
    address: str = ""
    symbol: str = ""
    offset: str = ""


class KernelStack(BaseModel):
    """内核调用栈解析结果。"""
    timestamp: str = ""
    process: str = ""
    pid: int = 0
    frames: list[KernelStackFrame] = Field(default_factory=list)
    raw_log: str = ""


class SearchResult(BaseModel):
    """搜索结果中的单个匹配项。

    Attributes:
        file_path: 匹配文件路径
        line_number: 匹配行号
        content: 匹配行内容
        timestamp: 可选的日志时间戳，用于日志类文件
    """
    file_path: str
    line_number: int
    content: str
    timestamp: Optional[str] = None


class ToolResult(BaseModel):
    """所有工具的通用返回值包装。

    Attributes:
        success: 操作是否成功
        data: 成功时的业务数据
        error_code: 错误码，用于程序化判断错误类型
        error_message: 人类可读的错误描述
        retryable: 该错误是否可重试
    """
    success: bool
    data: Optional[dict] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    retryable: bool = False


class ReportSection(BaseModel):
    """报告中的单个章节。"""
    title: str
    content: str
    severity: str = "info"  # info / warning / critical


class AnalysisReport(BaseModel):
    """完整的结构化分析报告。"""
    title: str
    summary: str
    sections: list[ReportSection] = Field(default_factory=list)
    source_logs: list[str] = Field(default_factory=list)