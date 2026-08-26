"""与 MCP、Agent Runtime 和文件权限无关的确定性日志分析原语。"""

from .diagnostics import LineDiagnostic, classify_diagnostic_line
from .identifiers import stable_id
from .timestamps import ParsedTimestamp, extract_timestamp, infer_component

__all__ = [
    "LineDiagnostic",
    "ParsedTimestamp",
    "classify_diagnostic_line",
    "extract_timestamp",
    "infer_component",
    "stable_id",
]

__version__ = "0.1.0"

