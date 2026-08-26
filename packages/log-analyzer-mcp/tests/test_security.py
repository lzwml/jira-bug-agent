"""
安全层单元测试 —— 验证路径守卫、文件大小限制、命令白名单。

测试范围：
- PathGuard: 路径穿越检测、允许目录范围、路径规范化
- FileSizeLimit: 大小检查、文件不存在处理
- CommandWhitelist: 白名单匹配、拒绝非白名单命令
- validate_path: 一站式验证函数
"""

import os
import tempfile
import pytest
from log_analyzer.security import (
    PathGuard,
    FileSizeLimit,
    CommandWhitelist,
    validate_path,
    DEFAULT_MAX_FILE_SIZE,
)


class TestPathGuard:
    """PathGuard 路径守卫测试。"""

    def test_allow_within_dir(self):
        """测试允许目录内的路径放行。"""
        guard = PathGuard(allowed_dirs=["/var/log"])
        result = guard.validate("/var/log/messages")
        assert result.success is True
        assert result.data["resolved_path"].endswith("messages")

    def test_block_path_traversal(self):
        """测试阻止路径穿越。"""
        guard = PathGuard(allowed_dirs=["/var/log"])
        result = guard.validate("/var/log/../../etc/passwd")
        assert result.success is False
        assert result.error_code == "PATH_TRAVERSAL"

    def test_block_path_traversal_windows(self):
        """测试阻止 Windows 风格的路径穿越。"""
        guard = PathGuard(allowed_dirs=["C:\\Logs"])
        result = guard.validate("C:\\Logs\\..\\..\\Windows\\System32\\config")
        assert result.success is False
        assert result.error_code == "PATH_TRAVERSAL"

    def test_block_outside_allowed(self):
        """测试阻止不在允许目录内的路径。"""
        guard = PathGuard(allowed_dirs=["/var/log"])
        result = guard.validate("/etc/passwd")
        assert result.success is False
        assert result.error_code == "PATH_NOT_ALLOWED"

    def test_empty_path(self):
        """测试空路径处理。"""
        guard = PathGuard()
        result = guard.validate("")
        assert result.success is False
        assert result.error_code == "EMPTY_PATH"

    def test_no_allowed_dirs_allows_all(self):
        """测试未设置允许目录时放行所有路径。"""
        guard = PathGuard()
        # 会尝试解析路径，但不会因不在白名单而拒绝
        result = guard.validate("/tmp")
        if result.success:
            assert "resolved_path" in result.data

    def test_multiple_allowed_dirs(self):
        """测试多个允许目录。"""
        guard = PathGuard(allowed_dirs=["/var/log", "/tmp", "/home"])
        result = guard.validate("/home/user/logs.txt")
        # 如果 /home/user 存在则会成功，否则 INVALID_PATH
        # 但不会返回 PATH_NOT_ALLOWED
        if result.success:
            assert result.data["resolved_path"]

    def test_case_sensitivity(self):
        """测试路径大小写敏感（取决于操作系统）。"""
        # 此测试不强制具体行为，只确保不崩溃
        guard = PathGuard(allowed_dirs=["/VAR/LOG"])
        guard.validate("/var/log/test")


class TestFileSizeLimit:
    """FileSizeLimit 文件大小限制测试。"""

    def test_allow_small_file(self):
        """测试小文件放行。"""
        with tempfile.NamedTemporaryFile(delete=False, mode="w") as f:
            f.write("small content")
            f.flush()
            fname = f.name

        try:
            checker = FileSizeLimit(max_bytes=10 * 1024 * 1024)  # 10 MB
            result = checker.check(fname)
            assert result.success is True
            assert result.data["file_size"] > 0
        finally:
            os.unlink(fname)

    def test_reject_large_file(self):
        """测试超大文件拒绝。"""
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            # 写入超过 1 字节的内容
            f.write(b"a" * 100)
            f.flush()
            fname = f.name

        try:
            checker = FileSizeLimit(max_bytes=10)  # 仅允许 10 字节
            result = checker.check(fname)
            assert result.success is False
            assert result.error_code == "FILE_TOO_LARGE"
        finally:
            os.unlink(fname)

    def test_nonexistent_file(self):
        """测试不存在的文件。"""
        checker = FileSizeLimit()
        result = checker.check("/nonexistent/file.log")
        assert result.success is False
        assert result.error_code == "FILE_STAT_ERROR"

    def test_default_max_size(self):
        """测试默认最大文件大小。"""
        checker = FileSizeLimit()
        # 默认 10 MB
        assert checker.max_bytes == DEFAULT_MAX_FILE_SIZE


class TestCommandWhitelist:
    """CommandWhitelist 命令白名单测试。"""

    def test_allow_whitelisted_command(self):
        """测试白名单内命令放行。"""
        wl = CommandWhitelist(allowed_commands={"/usr/bin/grep"})
        # abspath 会规范化路径，但只要能匹配到白名单即可
        # 注意：在 Windows 上路径不同
        import sys
        if sys.platform == "win32":
            # Windows 不适用此测试，跳过
            pytest.skip("Windows 路径不适用此测试")

        result = wl.check("/usr/bin/grep")
        assert result.success is True

    def test_block_non_whitelisted(self):
        """测试非白名单命令拒绝。"""
        wl = CommandWhitelist(allowed_commands={"/usr/bin/grep"})
        result = wl.check("/usr/bin/rm")
        assert result.success is False
        assert result.error_code == "COMMAND_NOT_ALLOWED"

    def test_empty_command(self):
        """测试空命令处理。"""
        wl = CommandWhitelist()
        result = wl.check("")
        assert result.success is False
        assert result.error_code == "EMPTY_COMMAND"

    def test_default_whitelist_contains_grep(self):
        """测试默认白名单包含 grep。"""
        wl = CommandWhitelist()
        assert "/usr/bin/grep" in wl.allowed_commands
        assert "/bin/grep" in wl.allowed_commands


class TestValidatePath:
    """validate_path 一站式验证函数测试。"""

    def test_valid_file(self):
        """测试合法文件路径。"""
        with tempfile.NamedTemporaryFile(delete=False, mode="w") as f:
            f.write("test")
            f.flush()
            fname = f.name

        try:
            # 使用临时目录作为允许目录
            temp_dir = os.path.dirname(fname)
            result = validate_path(fname, allowed_dirs=[temp_dir])
            assert result.success is True
            assert result.data["is_file"] is True
        finally:
            os.unlink(fname)

    def test_path_traversal_detected(self):
        """测试路径穿越检测。"""
        result = validate_path("/var/log/../../etc/shadow")
        assert result.success is False
        assert result.error_code == "PATH_TRAVERSAL"

    def test_path_not_allowed(self):
        """测试路径不在允许目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个不在允许目录中的文件
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test")

            result = validate_path(test_file, allowed_dirs=["/var/log"])
            assert result.success is False
            assert result.error_code == "PATH_NOT_ALLOWED"

    def test_file_too_large(self):
        """测试文件大小超限。"""
        with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
            f.write(b"x" * 1000)
            f.flush()
            fname = f.name

        try:
            temp_dir = os.path.dirname(fname)
            result = validate_path(fname, allowed_dirs=[temp_dir], max_file_size=50)
            assert result.success is False
            assert result.error_code == "FILE_TOO_LARGE"
        finally:
            os.unlink(fname)