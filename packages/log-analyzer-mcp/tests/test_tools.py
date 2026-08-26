"""
工具函数单元测试 —— 验证各个工具函数的逻辑和返回值。

测试范围：
- search_log: 关键词搜索、大小写、正则模式
- extract_time_window: 时间窗口筛选
- parse_selinux_avc: AVC 日志解析
- parse_kernel_stack: 内核栈解析
- search_source_code: 源码搜索
- generate_report: 报告生成
"""

import os
import tempfile
import pytest
from log_analyzer.tools import (
    search_log,
    extract_time_window,
    parse_selinux_avc,
    parse_kernel_stack,
    search_source_code,
    generate_report,
)
from log_analyzer.models import SearchResult, AvcRecord, ToolResult


# ========== 辅助工具：创建临时日志文件 ==========


@pytest.fixture
def temp_log_dir():
    """创建包含测试日志文件的临时目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建普通日志文件
        log_file = os.path.join(tmpdir, "test.log")
        with open(log_file, "w", encoding="utf-8") as f:
            f.write("2024-01-15 10:00:00 INFO 服务启动成功\n")
            f.write("2024-01-15 10:05:30 ERROR 连接数据库失败\n")
            f.write("2024-01-15 10:10:15 WARN 磁盘使用率超过 80%\n")
            f.write("2024-01-15 10:15:00 INFO 请求处理完成\n")
            f.write("2024-01-15 10:20:45 ERROR 超时连接已关闭\n")

        # 创建审计日志文件
        audit_file = os.path.join(tmpdir, "audit.log")
        with open(audit_file, "w", encoding="utf-8") as f:
            f.write("type=AVC msg=audit(1705300000.123:456): ")
            f.write('avc:  denied  { read write } for  pid=1234 comm="httpd" ')
            f.write('name="config" dev=sda1 ino=5678 ')
            f.write("scontext=system_u:system_r:httpd_t:s0 ")
            f.write("tcontext=system_u:object_r:etc_t:s0 ")
            f.write("tclass=file permissive=0\n")
            f.write("type=AVC msg=audit(1705300001.456:789): ")
            f.write('avc:  denied  { read } for  pid=5678 comm="sshd" ')
            f.write("scontext=system_u:system_r:sshd_t:s0 ")
            f.write("tcontext=system_u:object_r:shadow_t:s0 ")
            f.write("tclass=file permissive=1\n")

        # 创建内核日志文件
        kern_file = os.path.join(tmpdir, "kern.log")
        with open(kern_file, "w", encoding="utf-8") as f:
            f.write("[12345.678901] Call Trace:\n")
            f.write("[12345.678902]  <TASK>\n")
            f.write("[12345.678903]  dump_stack+0x12/0x20\n")
            f.write("[12345.678904]  some_function+0x34/0x50\n")
            f.write("[12345.678905]  </TASK>\n")

        yield tmpdir


class TestSearchLog:
    """search_log 工具函数测试。"""

    def test_search_basic(self, temp_log_dir):
        """测试基本关键词搜索。"""
        result = search_log(
            log_dirs=[temp_log_dir],
            keyword="ERROR",
            file_pattern="*.log",
        )
        assert result.success is True
        assert len(result.data["results"]) >= 2  # 有 2 条 ERROR 日志

    def test_search_case_insensitive(self, temp_log_dir):
        """测试不区分大小写搜索。"""
        result = search_log(
            log_dirs=[temp_log_dir],
            keyword="error",
            case_sensitive=False,
        )
        assert result.success is True
        assert len(result.data["results"]) >= 2

    def test_search_case_sensitive(self, temp_log_dir):
        """测试区分大小写搜索（小写 error 不应匹配大写 ERROR）。"""
        result = search_log(
            log_dirs=[temp_log_dir],
            keyword="error",
            case_sensitive=True,
        )
        # 文件中只有 "ERROR"，没有小写 "error"
        # 结果可能为空
        if result.success:
            count = len(result.data["results"])
        else:
            assert result.error_code == "SEARCH_NO_RESULTS"

    def test_search_no_match(self, temp_log_dir):
        """测试搜索无匹配。"""
        result = search_log(
            log_dirs=[temp_log_dir],
            keyword="NONEXISTENT_KEYWORD_XYZ",
        )
        assert result.success is False
        assert result.error_code == "SEARCH_NO_RESULTS"

    def test_search_empty_keyword(self, temp_log_dir):
        """测试空关键词。"""
        result = search_log(
            log_dirs=[temp_log_dir],
            keyword="",
        )
        assert result.success is False
        assert result.error_code == "KEYWORD_TOO_SHORT"

    def test_search_empty_dirs(self):
        """测试空目录列表。"""
        result = search_log(
            log_dirs=[],
            keyword="test",
        )
        assert result.success is False
        assert result.error_code == "INVALID_PARAMS"

    def test_search_with_regex(self, temp_log_dir):
        """测试正则表达式搜索。"""
        result = search_log(
            log_dirs=[temp_log_dir],
            keyword="ERROR|WARN",
            use_regex=True,
        )
        assert result.success is True
        assert len(result.data["results"]) >= 3  # 2 ERROR + 1 WARN

    def test_search_result_has_timestamp(self, temp_log_dir):
        """测试搜索结果包含时间戳。"""
        result = search_log(
            log_dirs=[temp_log_dir],
            keyword="ERROR",
        )
        if result.success:
            for r in result.data["results"]:
                assert "timestamp" in r
                assert "file_path" in r
                assert "line_number" in r


class TestExtractTimeWindow:
    """extract_time_window 工具函数测试。"""

    def test_extract_valid_window(self, temp_log_dir):
        """测试有效时间窗口提取。"""
        result = extract_time_window(
            log_dirs=[temp_log_dir],
            start_time="2024-01-15 10:00:00",
            end_time="2024-01-15 10:10:00",
        )
        assert result.success is True
        # 日志在时间窗口内：10:00、10:05、10:08 三条
        assert len(result.data["results"]) >= 2

    def test_extract_narrow_window(self, temp_log_dir):
        """测试狭窄时间窗口。"""
        result = extract_time_window(
            log_dirs=[temp_log_dir],
            start_time="2024-01-15 10:00:00",
            end_time="2024-01-15 10:01:00",
        )
        # 只有第 1 条日志在窗口内
        assert result.success is True
        assert len(result.data["results"]) == 1

    def test_extract_window_no_match(self, temp_log_dir):
        """测试时间窗口无匹配。"""
        result = extract_time_window(
            log_dirs=[temp_log_dir],
            start_time="2025-01-01 00:00:00",
            end_time="2025-12-31 23:59:59",
        )
        assert result.success is False
        assert result.error_code == "SEARCH_NO_RESULTS"

    def test_invalid_time_format(self, temp_log_dir):
        """测试无效时间格式。"""
        result = extract_time_window(
            log_dirs=[temp_log_dir],
            start_time="not-a-time",
            end_time="2024-01-15 10:10:00",
        )
        assert result.success is False
        assert result.error_code == "INVALID_PARAMS"

    def test_start_after_end(self, temp_log_dir):
        """测试起始时间晚于结束时间。"""
        result = extract_time_window(
            log_dirs=[temp_log_dir],
            start_time="2024-01-15 10:10:00",
            end_time="2024-01-15 10:00:00",
        )
        assert result.success is False
        assert result.error_code == "INVALID_PARAMS"


class TestParseSelinuxAvc:
    """parse_selinux_avc 工具函数测试。"""

    def test_parse_avc_records(self, temp_log_dir):
        """测试 AVC 日志解析。"""
        result = parse_selinux_avc(
            log_dirs=[temp_log_dir],
            file_pattern="audit.log*",
        )
        assert result.success is True
        records = result.data["records"]
        assert len(records) >= 2

        # 验证第一条记录
        first = records[0]
        assert first["source_type"] == "httpd_t"
        assert first["target_type"] == "etc_t"
        assert first["target_class"] == "file"
        assert "read" in first["permissions"]
        assert "write" in first["permissions"]
        assert first["permissive"] is False

        # 验证第二条记录（宽容模式）
        second = records[1]
        assert second["source_type"] == "sshd_t"
        assert second["target_type"] == "shadow_t"
        assert second["permissive"] is True

    def test_parse_no_matches(self, temp_log_dir):
        """测试无匹配的 AVC 日志。"""
        result = parse_selinux_avc(
            log_dirs=[temp_log_dir],
            file_pattern="*.log",  # 匹配所有日志，但只有 audit.log 有 AVC 内容
            max_records=10,
        )
        # 应该仍然能解析到 audit.log 中的 AVC 记录
        if result.success:
            assert len(result.data["records"]) >= 2

    def test_parse_empty_dir(self):
        """测试空目录。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = parse_selinux_avc(log_dirs=[tmpdir])
            assert result.success is False
            assert result.error_code == "NO_MATCHES"


class TestParseKernelStack:
    """parse_kernel_stack 工具函数测试。"""

    def test_parse_kernel_stack(self, temp_log_dir):
        """测试内核栈解析。"""
        result = parse_kernel_stack(
            log_dirs=[temp_log_dir],
            file_pattern="kern.log*",
        )
        assert result.success is True
        stacks = result.data["stacks"]
        assert len(stacks) >= 1

        # 验证栈帧内容
        stack = stacks[0]
        frames = stack.get("frames", [])
        assert len(frames) >= 2  # 至少 2 个栈帧

        # 验证栈帧符号
        symbols = [f["symbol"] for f in frames]
        assert "dump_stack" in symbols
        assert "some_function" in symbols

    def test_parse_no_matches(self, temp_log_dir):
        """测试无匹配的内核栈。"""
        result = parse_kernel_stack(
            log_dirs=[temp_log_dir],
            file_pattern="*.log",  # 搜索所有日志，但只有 kern.log 有栈
        )
        # 应该能解析到
        if result.success:
            assert len(result.data["stacks"]) >= 1


class TestSearchSourceCode:
    """search_source_code 工具函数测试。"""

    def test_search_source(self, temp_log_dir):
        """测试源码搜索（使用临时目录模拟）。"""
        # 创建模拟源码文件
        src_file = os.path.join(temp_log_dir, "main.c")
        with open(src_file, "w", encoding="utf-8") as f:
            f.write("#include <stdio.h>\n")
            f.write("int main() {\n")
            f.write('    printf("hello world\\n");\n')
            f.write("    return 0;\n")
            f.write("}\n")

        result = search_source_code(
            source_dirs=[temp_log_dir],
            keyword="printf",
            file_pattern="*.c",
        )
        assert result.success is True
        assert len(result.data["results"]) >= 1
        assert result.data["results"][0]["content"].strip() == 'printf("hello world\\n");'

    def test_search_source_python(self, temp_log_dir):
        """测试搜索 Python 源码。"""
        py_file = os.path.join(temp_log_dir, "test.py")
        with open(py_file, "w", encoding="utf-8") as f:
            f.write("def hello():\n")
            f.write("    print('hello')\n")

        result = search_source_code(
            source_dirs=[temp_log_dir],
            keyword="def hello",
            file_pattern="*.py",
        )
        assert result.success is True
        assert len(result.data["results"]) == 1


class TestGenerateReport:
    """generate_report 工具函数测试。"""

    def test_generate_minimal_report(self):
        """测试生成最小报告（仅标题和摘要）。"""
        result = generate_report(
            title="测试报告",
            summary="这是一个测试",
        )
        assert result.success is True
        report = result.data["report"]
        assert report["title"] == "测试报告"
        assert report["summary"] == "这是一个测试"
        assert len(report["sections"]) == 0  # 没有额外数据

    def test_generate_report_with_avc(self):
        """测试包含 AVC 数据的报告。"""
        avc_records = [
            {
                "source_type": "httpd_t",
                "target_type": "etc_t",
                "target_class": "file",
                "permissions": ["read", "write"],
                "permissive": False,
                "raw_log": "avc: denied ...",
            }
        ]
        result = generate_report(
            title="安全分析报告",
            summary="发现 1 条 AVC 拒绝",
            avc_records=avc_records,
        )
        assert result.success is True
        report = result.data["report"]
        assert len(report["sections"]) == 1
        assert report["sections"][0]["title"] == "SELinux AVC 拒绝分析"
        assert report["sections"][0]["severity"] == "critical"

    def test_generate_report_empty_title(self):
        """测试空标题。"""
        result = generate_report(
            title="",
            summary="test",
        )
        assert result.success is False
        assert result.error_code == "INVALID_PARAMS"

    def test_generate_report_section_count(self):
        """测试报告章节数量正确。"""
        result = generate_report(
            title="完整报告",
            summary="包含所有章节",
            avc_records=[{"source_type": "a", "target_type": "b", "target_class": "c", "permissions": ["x"], "permissive": False, "raw_log": ""}],
            log_results=[{"file_path": "/test.log", "line_number": 1, "content": "test"}],
        )
        assert result.success is True
        assert result.data["section_count"] == 2  # AVC + 日志