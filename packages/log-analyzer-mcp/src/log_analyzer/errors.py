"""
结构化错误定义 —— 提供统一的错误码体系和错误处理机制。

所有工具函数在出错时返回 ToolResult，其中的 error_code 使用
本模块定义的常量，确保客户端可以程序化地处理错误。
"""

try:
    from .models import ToolResult
except ImportError:
    from models import ToolResult  # type: ignore


# ========== 错误码常量 ==========

# 通用错误
E_INTERNAL = "INTERNAL_ERROR"          # 内部错误
E_INVALID_PARAMS = "INVALID_PARAMS"     # 参数无效
E_NOT_IMPLEMENTED = "NOT_IMPLEMENTED"   # 功能未实现

# 路径/文件相关
E_PATH_NOT_FOUND = "PATH_NOT_FOUND"     # 路径不存在
E_PATH_TRAVERSAL = "PATH_TRAVERSAL"     # 路径穿越检测
E_PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED" # 路径不在允许范围
E_FILE_TOO_LARGE = "FILE_TOO_LARGE"     # 文件超出大小限制
E_FILE_READ_ERROR = "FILE_READ_ERROR"   # 文件读取失败

# 搜索相关
E_SEARCH_NO_RESULTS = "SEARCH_NO_RESULTS"  # 搜索无结果
E_KEYWORD_TOO_SHORT = "KEYWORD_TOO_SHORT"  # 关键词太短
E_KEYWORD_INVALID = "KEYWORD_INVALID"      # 关键词包含非法字符

# 解析相关
E_PARSE_FAILED = "PARSE_FAILED"  # 解析失败
E_NO_MATCHES = "NO_MATCHES"      # 正则/模式无匹配

# 安全相关
E_COMMAND_NOT_ALLOWED = "COMMAND_NOT_ALLOWED"  # 命令不在白名单
E_SIZE_LIMIT_EXCEEDED = "SIZE_LIMIT_EXCEEDED"  # 超出大小限制


def make_error(
    error_code: str,
    error_message: str,
    retryable: bool = False,
    data: dict | None = None,
) -> ToolResult:
    """快速构造一个错误 ToolResult。

    Args:
        error_code: 错误码
        error_message: 错误描述
        retryable: 是否可重试
        data: 额外的错误上下文数据

    Returns:
        包含错误信息的 ToolResult
    """
    return ToolResult(
        success=False,
        data=data,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )


def make_success(data: dict | None = None) -> ToolResult:
    """快速构造一个成功 ToolResult。

    Args:
        data: 成功返回的业务数据

    Returns:
        表示成功的 ToolResult
    """
    return ToolResult(success=True, data=data or {})