"""
模型单元测试 —— 验证 Pydantic 模型的创建、验证和序列化。

测试范围：
- AvcRecord 模型的基本创建和字段验证
- ToolResult 成功/失败状态的正确构造
- SearchResult 的时间戳字段
- KernelStack 和 KernelStackFrame 的默认值
- ReportSection 和 AnalysisReport 的结构完整性
"""

import pytest
from log_analyzer.models import (
    AvcRecord,
    ToolResult,
    SearchResult,
    KernelStackFrame,
    KernelStack,
    ReportSection,
    AnalysisReport,
)


class TestAvcRecord:
    """AvcRecord 模型测试。"""

    def test_basic_creation(self):
        """测试基本的 AvcRecord 创建。"""
        record = AvcRecord(
            source_type="httpd_t",
            target_type="etc_t",
            target_class="file",
            permissions=["read", "write"],
            permissive=False,
            raw_log="avc: denied { read write } for ... scontext=httpd_t tcontext=etc_t tclass=file",
        )
        assert record.source_type == "httpd_t"
        assert record.target_type == "etc_t"
        assert record.target_class == "file"
        assert record.permissions == ["read", "write"]
        assert record.permissive is False
        assert "avc: denied" in record.raw_log

    def test_permissive_mode(self):
        """测试宽容模式下的记录。"""
        record = AvcRecord(
            source_type="init_t",
            target_type="sysfs_t",
            target_class="dir",
            permissions=["read"],
            permissive=True,
            raw_log="avc: denied { read } ... permissive=1",
        )
        assert record.permissive is True

    def test_empty_permissions(self):
        """测试空权限列表。"""
        record = AvcRecord(
            source_type="test_t",
            target_type="test_t",
            target_class="file",
            permissions=[],
            permissive=False,
            raw_log="",
        )
        assert record.permissions == []

    def test_serialization(self):
        """测试模型序列化（model_dump）。"""
        record = AvcRecord(
            source_type="a",
            target_type="b",
            target_class="c",
            permissions=["x"],
            permissive=False,
            raw_log="test",
        )
        dumped = record.model_dump()
        assert dumped["source_type"] == "a"
        assert dumped["permissions"] == ["x"]
        assert dumped["permissive"] is False


class TestToolResult:
    """ToolResult 模型测试。"""

    def test_success_result(self):
        """测试成功结果的构造。"""
        result = ToolResult(
            success=True,
            data={"key": "value"},
        )
        assert result.success is True
        assert result.data == {"key": "value"}
        assert result.error_code is None
        assert result.error_message is None
        assert result.retryable is False

    def test_error_result(self):
        """测试错误结果的构造。"""
        result = ToolResult(
            success=False,
            error_code="FILE_NOT_FOUND",
            error_message="日志文件不存在",
            retryable=True,
        )
        assert result.success is False
        assert result.error_code == "FILE_NOT_FOUND"
        assert result.error_message == "日志文件不存在"
        assert result.retryable is True

    def test_data_none_default(self):
        """测试 data 字段默认为 None。"""
        result = ToolResult(success=True)
        assert result.data is None

    def test_retryable_default(self):
        """测试 retryable 默认值为 False。"""
        result = ToolResult(success=False, error_code="E", error_message="err")
        assert result.retryable is False


class TestSearchResult:
    """SearchResult 模型测试。"""

    def test_basic_creation(self):
        """测试基本的 SearchResult 创建。"""
        result = SearchResult(
            file_path="/var/log/messages",
            line_number=42,
            content="error: connection refused",
            timestamp="2024-01-15 10:30:45",
        )
        assert result.file_path == "/var/log/messages"
        assert result.line_number == 42
        assert "connection refused" in result.content
        assert result.timestamp == "2024-01-15 10:30:45"

    def test_timestamp_none(self):
        """测试时间戳为 None 的场景。"""
        result = SearchResult(
            file_path="/src/main.c",
            line_number=10,
            content="int main() {",
        )
        assert result.timestamp is None

    def test_serialization(self):
        """测试序列化后字段完整性。"""
        result = SearchResult(
            file_path="/path/to/file",
            line_number=1,
            content="hello",
        )
        dumped = result.model_dump()
        assert dumped["file_path"] == "/path/to/file"
        assert dumped["timestamp"] is None


class TestKernelStack:
    """KernelStack 和 KernelStackFrame 模型测试。"""

    def test_empty_stack(self):
        """测试空调用栈的默认值。"""
        stack = KernelStack()
        assert stack.frames == []
        assert stack.pid == 0
        assert stack.process == ""

    def test_with_frames(self):
        """测试包含栈帧的栈。"""
        stack = KernelStack(
            timestamp="[12345.678901]",
            process="kworker/0:1",
            pid=42,
            frames=[
                KernelStackFrame(address="ffffffff", symbol="dump_stack", offset="0x12/0x20"),
                KernelStackFrame(symbol="some_function", offset="0x34/0x50"),
            ],
        )
        assert stack.pid == 42
        assert len(stack.frames) == 2
        assert stack.frames[0].symbol == "dump_stack"
        assert stack.frames[1].offset == "0x34/0x50"


class TestReportModels:
    """分析报告相关模型测试。"""

    def test_report_section(self):
        """测试报告章节模型。"""
        section = ReportSection(
            title="AVC 分析",
            content="发现 3 条拒绝记录",
            severity="critical",
        )
        assert section.title == "AVC 分析"
        assert section.severity == "critical"

    def test_report_section_default_severity(self):
        """测试 severity 默认值。"""
        section = ReportSection(title="信息", content="普通信息")
        assert section.severity == "info"

    def test_analysis_report(self):
        """测试完整分析报告模型。"""
        report = AnalysisReport(
            title="系统安全分析报告",
            summary="共发现 5 个问题",
            sections=[
                ReportSection(title="AVC", content="3 条拒绝", severity="critical"),
            ],
            source_logs=["/var/log/audit/audit.log"],
        )
        assert len(report.sections) == 1
        assert report.source_logs == ["/var/log/audit/audit.log"]


class TestGetCaseCommentInput:
    """get_case_comment 按 ID 和字符片段读取。"""

    def test_defaults(self):
        from log_analyzer.domain import GetCaseCommentInput

        params = GetCaseCommentInput(case_id="case_abc", comment_id="10001")
        assert params.offset == 0
        assert params.limit == 4000

    def test_bounds_and_required_comment_id(self):
        import pytest
        from log_analyzer.domain import GetCaseCommentInput

        with pytest.raises(Exception):
            GetCaseCommentInput(case_id="case_abc", comment_id="", offset=0)
        with pytest.raises(Exception):
            GetCaseCommentInput(case_id="case_abc", comment_id="10001", offset=-1)
        with pytest.raises(Exception):
            GetCaseCommentInput(case_id="case_abc", comment_id="10001", limit=20001)