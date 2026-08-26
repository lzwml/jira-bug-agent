"""单行诊断信号分类；多行窗口和文件读取由调用方管理。"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Literal


DiagnosticType = Literal["avc", "kernel_stack", "fatal", "anr"]
Severity = Literal["warning", "critical"]


@dataclass(frozen=True)
class LineDiagnostic:
    diagnostic_type: DiagnosticType
    severity: Severity
    summary: str
    attributes: dict = field(default_factory=dict)


def classify_diagnostic_line(line: str, wanted: set[str]) -> LineDiagnostic | None:
    """返回确定性 Finding 候选，不判断它是不是当前 Bug 的根因。"""

    lowered = line.casefold()
    if "avc" in wanted and "avc: denied" in lowered:
        permissions = re.search(r"avc:\s*denied\s*\{([^}]*)\}", line, re.IGNORECASE)
        attributes = {}
        for name in ("scontext", "tcontext", "tclass", "permissive"):
            match = re.search(rf"\b{name}=([^\s]+)", line, re.IGNORECASE)
            if match:
                attributes[name] = match.group(1)
        attributes["permissions"] = permissions.group(1).split() if permissions else []
        return LineDiagnostic("avc", "warning", "SELinux AVC 拒绝", attributes)
    if "fatal" in wanted and ("fatal exception" in lowered or "fatal signal" in lowered):
        return LineDiagnostic("fatal", "critical", "检测到致命异常")
    if "anr" in wanted and "anr in" in lowered:
        return LineDiagnostic("anr", "critical", "检测到 ANR")
    if "kernel_stack" in wanted and "call trace:" in lowered:
        return LineDiagnostic("kernel_stack", "critical", "检测到 Kernel Call Trace")
    return None

