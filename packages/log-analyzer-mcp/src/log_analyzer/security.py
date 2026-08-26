"""
安全层 —— 路径范围限制、路径穿越防护、命令白名单、文件大小限制。

信任边界说明：
- 本工具只读访问日志目录和源码目录，不会修改任何文件
- 所有路径必须经过 validate_path() 校验，阻止 ../ 穿越
- 文件读取前检查大小，超限文件直接拒绝
- 外部命令执行仅允许白名单内的命令
"""

import os
import re
from pathlib import Path
from typing import Optional

try:
    from .models import ToolResult
except ImportError:
    from models import ToolResult  # type: ignore


# 默认最大文件大小：10 MB
DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024


class PathGuard:
    """路径守卫 —— 限制只允许读取指定目录范围内的文件。

    将用户提供的路径规范化后，验证其是否在允许的根目录下，
    防止路径穿越攻击（如 ../../etc/passwd）。

    Attributes:
        allowed_dirs: 允许访问的根目录列表（绝对路径）
    """

    def __init__(self, allowed_dirs: Optional[list[str]] = None):
        """初始化路径守卫。

        Args:
            allowed_dirs: 允许访问的根目录列表。如果为 None，则允许所有路径。
        """
        self.allowed_dirs: list[str] = []
        if allowed_dirs:
            # 规范化所有允许目录为绝对路径
            for d in allowed_dirs:
                resolved = self._resolve(d)
                if resolved:
                    self.allowed_dirs.append(resolved)

    def _resolve(self, path: str) -> Optional[str]:
        """将路径解析为绝对路径，失败时返回 None。"""
        try:
            return str(Path(path).resolve(strict=False))
        except (OSError, ValueError, RuntimeError):
            return None

    def validate(self, path: str) -> ToolResult:
        """验证路径是否在允许的目录范围内。

        Args:
            path: 用户提供的路径

        Returns:
            ToolResult: success=True 表示路径合法，data 包含规范化后的路径
        """
        if not path or not path.strip():
            return ToolResult(
                success=False,
                error_code="EMPTY_PATH",
                error_message="路径不能为空",
                retryable=False,
            )

        # 保留明确的路径穿越错误语义；最终授权仍由 commonpath 判断。
        if ".." in re.split(r"[\\/]+", path):
            return ToolResult(
                success=False,
                error_code="PATH_TRAVERSAL",
                error_message="检测到路径穿越攻击，拒绝访问",
                retryable=False,
            )

        resolved = self._resolve(path)
        if not resolved:
            return ToolResult(
                success=False,
                error_code="INVALID_PATH",
                error_message=f"无法解析路径: {path}",
                retryable=False,
            )

        # 如果没有设置允许目录，则放行所有路径
        if not self.allowed_dirs:
            return ToolResult(success=True, data={"resolved_path": resolved})

        # 检查路径是否在允许的目录范围内
        for allowed in self.allowed_dirs:
            try:
                within_root = os.path.commonpath([resolved, allowed]) == allowed
            except ValueError:
                within_root = False
            if within_root:
                return ToolResult(success=True, data={"resolved_path": resolved})

        return ToolResult(
            success=False,
            error_code="PATH_NOT_ALLOWED",
            error_message=f"路径不在允许的目录范围内: {resolved}",
            retryable=False,
        )


class FileSizeLimit:
    """文件大小限制器 —— 防止读取超大文件导致内存溢出。

    Attributes:
        max_bytes: 允许的最大文件大小（字节）
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_FILE_SIZE):
        self.max_bytes = max_bytes

    def check(self, file_path: str) -> ToolResult:
        """检查文件是否超出大小限制。

        Args:
            file_path: 文件路径

        Returns:
            ToolResult: success=True 表示文件大小合规
        """
        try:
            actual_size = os.path.getsize(file_path)
        except OSError as e:
            return ToolResult(
                success=False,
                error_code="FILE_STAT_ERROR",
                error_message=f"无法获取文件大小: {e}",
                retryable=True,
            )

        if actual_size > self.max_bytes:
            mb = self.max_bytes / (1024 * 1024)
            return ToolResult(
                success=False,
                error_code="FILE_TOO_LARGE",
                error_message=f"文件大小 ({actual_size} 字节) 超过限制 ({mb:.1f} MB)",
                retryable=False,
            )

        return ToolResult(
            success=True,
            data={"file_size": actual_size, "max_bytes": self.max_bytes},
        )


class CommandWhitelist:
    """命令白名单 —— 限制可执行的外部命令。

    只允许执行白名单内指定的命令，且仅运行命令本身（不传参）。
    参数通过工具的 params 字段独立传递，避免 shell 注入。

    Attributes:
        allowed_commands: 允许的命令路径集合（绝对路径）
    """

    # 默认白名单：只允许安全的只读系统命令
    DEFAULT_COMMANDS: set[str] = {
        "/usr/bin/grep",
        "/usr/bin/find",
        "/usr/bin/cat",
        "/usr/bin/head",
        "/usr/bin/tail",
        "/usr/bin/wc",
        "/usr/bin/dmesg",
        "/usr/bin/ausearch",
        "/usr/bin/audit2why",
        "/bin/grep",
        "/bin/cat",
        "/bin/head",
        "/bin/tail",
        "/bin/dmesg",
    }

    def __init__(self, allowed_commands: Optional[set[str]] = None):
        self.allowed_commands = allowed_commands or self.DEFAULT_COMMANDS

    def check(self, command: str) -> ToolResult:
        """检查命令是否在白名单中。

        Args:
            command: 命令路径（应为绝对路径）

        Returns:
            ToolResult: success=True 表示命令允许执行
        """
        if not command:
            return ToolResult(
                success=False,
                error_code="EMPTY_COMMAND",
                error_message="命令不能为空",
                retryable=False,
            )

        # 规范化命令路径
        resolved = os.path.abspath(os.path.normpath(command))

        if resolved not in self.allowed_commands:
            return ToolResult(
                success=False,
                error_code="COMMAND_NOT_ALLOWED",
                error_message=f"命令不在白名单中: {resolved}",
                retryable=False,
            )

        return ToolResult(success=True, data={"command": resolved})


def validate_path(
    path: str,
    allowed_dirs: Optional[list[str]] = None,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> ToolResult:
    """一站式路径验证 —— 路径合法性 + 路径穿越 + 文件大小限制。

    这是安全层对外的主要入口函数，组合了 PathGuard 和 FileSizeLimit
    的检查逻辑。

    Args:
        path: 待验证的路径
        allowed_dirs: 允许的根目录列表
        max_file_size: 文件大小上限

    Returns:
        ToolResult: success=True 表示路径完全合法
    """
    guard = PathGuard(allowed_dirs)
    result = guard.validate(path)
    if not result.success:
        return result

    resolved_path = result.data["resolved_path"]

    # 如果是文件，检查文件大小
    if os.path.isfile(resolved_path):
        size_checker = FileSizeLimit(max_file_size)
        size_result = size_checker.check(resolved_path)
        if not size_result.success:
            return size_result

    return ToolResult(
        success=True,
        data={
            "resolved_path": resolved_path,
            "is_file": os.path.isfile(resolved_path),
            "is_dir": os.path.isdir(resolved_path),
        },
    )
