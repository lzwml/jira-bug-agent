"""
工具函数定义 —— 所有 MCP 工具的纯 Python 实现。

每个工具都返回统一的 ToolResult 结构，遵循以下约定：
- success=True: data 中包含业务数据
- success=False: error_code + error_message 描述错误原因
- retryable=True: 客户端可以稍后重试该操作

安全说明：
- 所有文件和路径操作都经过 security.validate_path() 校验
- 不执行任何外部命令（纯 Python 实现）
- 不修改任何文件（只读操作）
"""

import os
import re
import fnmatch
from datetime import datetime
from typing import Optional

try:
    from .models import AvcRecord, KernelStack, KernelStackFrame, SearchResult, ToolResult, AnalysisReport, ReportSection
    from .errors import make_error, make_success
    from .security import validate_path
except ImportError:
    from models import AvcRecord, KernelStack, KernelStackFrame, SearchResult, ToolResult, AnalysisReport, ReportSection  # type: ignore
    from errors import make_error, make_success  # type: ignore
    from security import validate_path  # type: ignore


# ========== 辅助函数 ==========

def _read_file_lines(file_path: str, encoding: str = "utf-8", errors: str = "replace") -> Optional[list[str]]:
    """安全地读取文件所有行，失败时返回 None。"""
    try:
        with open(file_path, "r", encoding=encoding, errors=errors) as f:
            return f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        return None


def _extract_timestamp(line: str) -> Optional[str]:
    """从日志行中提取时间戳。

    支持常见格式：
    - "2024-01-15 10:30:45"
    - "Jan 15 10:30:45"
    - "2024-01-15T10:30:45"
    """
    patterns = [
        (r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", None),
        (r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}", None),
        (r"\d{4}-\d{2}-\d{2}", None),
    ]
    for pattern, _ in patterns:
        match = re.search(pattern, line)
        if match:
            return match.group(0)
    return None


# ========== 工具函数 ==========


def search_log(
    log_dirs: list[str],
    keyword: str,
    file_pattern: str = "*.log",
    max_results: int = 100,
    case_sensitive: bool = False,
    use_regex: bool = False,
) -> ToolResult:
    """在日志目录中搜索关键词，返回匹配行列表。

    支持递归搜索目录下的所有日志文件，可指定文件通配符模式。
    返回的每个结果包含文件路径、行号、内容和时间戳（如能提取）。

    Args:
        log_dirs: 待搜索的日志目录列表
        keyword: 搜索关键词（支持正则或普通字符串）
        file_pattern: 文件通配符模式，如 *.log, *.txt, syslog*
        max_results: 最大返回结果数（默认 100）
        case_sensitive: 是否区分大小写（默认不区分）
        use_regex: keyword 是否为正则表达式（默认按普通字符串搜索）

    Returns:
        ToolResult: 成功时 data["results"] 为 SearchResult 列表
    """
    # 参数校验
    if not keyword or len(keyword.strip()) < 1:
        return make_error("KEYWORD_TOO_SHORT", "搜索关键词不能为空")

    if not log_dirs:
        return make_error("INVALID_PARAMS", "日志目录列表不能为空")

    # 编译搜索模式
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        if use_regex:
            pattern = re.compile(keyword, flags)
        else:
            pattern = re.compile(re.escape(keyword), flags)
    except re.error as e:
        return make_error("INVALID_PARAMS", f"正则表达式无效: {e}")

    results: list[SearchResult] = []

    for log_dir in log_dirs:
        # 路径安全校验
        path_result = validate_path(log_dir, allowed_dirs=log_dirs)
        if not path_result.success:
            continue

        resolved_dir = path_result.data["resolved_path"]
        if not path_result.data["is_dir"]:
            continue

        # 递归遍历目录
        for root, dirs, files in os.walk(resolved_dir):
            for filename in files:
                if not fnmatch.fnmatch(filename, file_pattern):
                    continue

                file_path = os.path.join(root, filename)

                # 安全检查
                vr = validate_path(file_path, allowed_dirs=log_dirs)
                if not vr.success:
                    continue

                lines = _read_file_lines(file_path)
                if lines is None:
                    continue

                for line_no, line in enumerate(lines, 1):
                    line_stripped = line.rstrip("\n\r")
                    if pattern.search(line_stripped):
                        ts = _extract_timestamp(line_stripped)
                        results.append(SearchResult(
                            file_path=file_path,
                            line_number=line_no,
                            content=line_stripped,
                            timestamp=ts,
                        ))
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

    if not results:
        return make_error("SEARCH_NO_RESULTS", f"在日志目录中未找到匹配 '{keyword}' 的结果")

    return make_success({
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "keyword": keyword,
    })


def extract_time_window(
    log_dirs: list[str],
    start_time: str,
    end_time: str,
    file_pattern: str = "*.log",
    time_format: str = "%Y-%m-%d %H:%M:%S",
    max_results: int = 1000,
) -> ToolResult:
    """提取指定时间窗口内的日志行。

    遍历日志文件，解析每行的时间戳，筛选出在 [start_time, end_time]
    范围内的行。

    Args:
        log_dirs: 日志目录列表
        start_time: 起始时间，格式由 time_format 参数指定
        end_time: 结束时间
        file_pattern: 文件通配符模式
        time_format: 时间格式字符串（默认 "%Y-%m-%d %H:%M:%S"）
        max_results: 最大返回结果数

    Returns:
        ToolResult: 成功时 data["results"] 为时间窗口内的日志行
    """
    # 参数校验
    try:
        start_dt = datetime.strptime(start_time, time_format)
        end_dt = datetime.strptime(end_time, time_format)
    except ValueError as e:
        return make_error("INVALID_PARAMS", f"时间格式错误: {e}")

    if start_dt >= end_dt:
        return make_error("INVALID_PARAMS", "起始时间必须早于结束时间")

    if not log_dirs:
        return make_error("INVALID_PARAMS", "日志目录列表不能为空")

    results: list[SearchResult] = []

    for log_dir in log_dirs:
        path_result = validate_path(log_dir, allowed_dirs=log_dirs)
        if not path_result.success:
            continue

        resolved_dir = path_result.data["resolved_path"]
        if not path_result.data["is_dir"]:
            continue

        for root, dirs, files in os.walk(resolved_dir):
            for filename in files:
                if not fnmatch.fnmatch(filename, file_pattern):
                    continue

                file_path = os.path.join(root, filename)
                vr = validate_path(file_path, allowed_dirs=log_dirs)
                if not vr.success:
                    continue

                lines = _read_file_lines(file_path)
                if lines is None:
                    continue

                for line_no, line in enumerate(lines, 1):
                    ts = _extract_timestamp(line)
                    if ts is None:
                        continue

                    # 尝试将提取的时间戳转换为 datetime
                    try:
                        # 尝试完整格式
                        line_dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        try:
                            # 尝试 ISO 格式
                            line_dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            # 尝试 syslog 格式
                            try:
                                line_dt = datetime.strptime(ts, "%b %d %H:%M:%S")
                                line_dt = line_dt.replace(year=start_dt.year)
                            except ValueError:
                                continue

                    if start_dt <= line_dt <= end_dt:
                        line_stripped = line.rstrip("\n\r")
                        results.append(SearchResult(
                            file_path=file_path,
                            line_number=line_no,
                            content=line_stripped,
                            timestamp=ts,
                        ))
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

    if not results:
        return make_error("SEARCH_NO_RESULTS", f"在时间窗口 [{start_time}, {end_time}] 内未找到日志")

    return make_success({
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "start_time": start_time,
        "end_time": end_time,
    })


def parse_selinux_avc(
    log_dirs: list[str],
    file_pattern: str = "audit.log*",
    max_records: int = 500,
) -> ToolResult:
    """解析 SELinux AVC 拒绝日志。

    从审计日志中提取 AVC 拒绝记录，解析为结构化的 AvcRecord。
    支持标准 avc: denied 格式，包含 scontext、tcontext、tclass、
    permissive 等字段。

    AVC 日志示例：
        type=AVC msg=audit(1705300000.123:456): avc: denied { read } for ...
        scontext=system_u:system_r:httpd_t:s0 tcontext=system_u:object_r:etc_t:s0
        tclass=file permissive=0

    Args:
        log_dirs: 审计日志目录列表
        file_pattern: 文件通配符模式（默认 audit.log*）
        max_records: 最大解析记录数

    Returns:
        ToolResult: 成功时 data["records"] 为 AvcRecord 列表
    """
    if not log_dirs:
        return make_error("INVALID_PARAMS", "日志目录列表不能为空")

    # 单条 AVC 日志的正则匹配
    avc_pattern = re.compile(
        r'avc:\s*(denied|granted)\s*\{([^}]+)\}\s*'
        r'for\s+(?:pid=\d+\s+)?'
        r'comm="[^"]*"\s+'
        r'(?:name="[^"]*"\s+)?'
        r'(?:dev=[^\s]+\s+)?'
        r'(?:ino=\d+\s+)?'
        r'scontext=([^\s]+)\s+'
        r'tcontext=([^\s]+)\s+'
        r'tclass=([^\s]+)'
        r'(?:\s+permissive=(\d))?',
        re.IGNORECASE,
    )

    records: list[AvcRecord] = []
    seen_lines: set[str] = set()

    for log_dir in log_dirs:
        path_result = validate_path(log_dir, allowed_dirs=log_dirs)
        if not path_result.success:
            continue

        resolved_dir = path_result.data["resolved_path"]
        if not path_result.data["is_dir"]:
            continue

        for root, dirs, files in os.walk(resolved_dir):
            for filename in files:
                if not fnmatch.fnmatch(filename, file_pattern):
                    continue

                file_path = os.path.join(root, filename)
                vr = validate_path(file_path, allowed_dirs=log_dirs)
                if not vr.success:
                    continue

                lines = _read_file_lines(file_path)
                if lines is None:
                    continue

                for line in lines:
                    line_stripped = line.strip()
                    match = avc_pattern.search(line_stripped)
                    if not match:
                        continue

                    # 去重
                    if line_stripped in seen_lines:
                        continue
                    seen_lines.add(line_stripped)

                    action = match.group(1).lower()  # denied / granted
                    perms_str = match.group(2).strip()
                    scontext = match.group(3)
                    tcontext = match.group(4)
                    tclass = match.group(5)
                    permissive_str = match.group(6)  # 可能为 None

                    # 提取源类型和目标类型（取最后一个冒号后的部分）
                    source_type = scontext.split(":")[-2] if ":" in scontext else scontext
                    target_type = tcontext.split(":")[-2] if ":" in tcontext else tcontext

                    # 权限列表
                    permissions = [p.strip() for p in perms_str.split() if p.strip()]

                    # permissive 判断
                    permissive = permissive_str == "1" if permissive_str else False

                    # 只有 denied 才记录为 AVC 拒绝
                    if action == "denied":
                        records.append(AvcRecord(
                            source_type=source_type,
                            target_type=target_type,
                            target_class=tclass,
                            permissions=permissions,
                            permissive=permissive,
                            raw_log=line_stripped,
                        ))

                    if len(records) >= max_records:
                        break
                if len(records) >= max_records:
                    break
            if len(records) >= max_records:
                break

    if not records:
        return make_error("NO_MATCHES", "未找到 SELinux AVC 拒绝日志")

    return make_success({
        "records": [r.model_dump() for r in records],
        "total": len(records),
    })


def parse_kernel_stack(
    log_dirs: list[str],
    file_pattern: str = "kern.log*",
    max_stacks: int = 50,
) -> ToolResult:
    """解析内核调用栈。

    从内核日志中提取调用栈（Call Trace）信息，解析为结构化的
 KernelStack 对象，包含进程名、PID、栈帧地址和符号。

    内核栈日志示例：
        [12345.678901] Call Trace:
        [12345.678902]  <TASK>
        [12345.678903]  dump_stack+0x12/0x20
        [12345.678904]  some_function+0x34/0x50
        [12345.678905]  </TASK>

    Args:
        log_dirs: 内核日志目录列表
        file_pattern: 文件通配符模式（默认 kern.log*）
        max_stacks: 最大解析栈数量

    Returns:
        ToolResult: 成功时 data["stacks"] 为 KernelStack 列表
    """
    if not log_dirs:
        return make_error("INVALID_PARAMS", "日志目录列表不能为空")

    # 栈帧正则：匹配 "[timestamp]  symbol+offset/size" 或 "[timestamp]  address symbol+offset"
    frame_pattern = re.compile(
        r'\[\s*\d+\.\d+\]\s+(?:<TASK>|</TASK>)?\s*'
        r'(?:([0-9a-fA-F]+)\s+)?'  # 可选地址
        r'([a-zA-Z_][a-zA-Z0-9_.]*)\+?(0x[0-9a-fA-F]+)?',  # 符号+偏移
        re.IGNORECASE,
    )

    # 匹配 Call Trace 开始行
    call_trace_start = re.compile(r'\[\s*\d+\.\d+\]\s+Call Trace:')

    stacks: list[KernelStack] = []
    current_stack: Optional[KernelStack] = None
    in_trace: bool = False
    seen_stacks: set[str] = set()

    for log_dir in log_dirs:
        path_result = validate_path(log_dir, allowed_dirs=log_dirs)
        if not path_result.success:
            continue

        resolved_dir = path_result.data["resolved_path"]
        if not resolved_dir:
            continue

        for root, dirs, files in os.walk(resolved_dir):
            for filename in files:
                if not fnmatch.fnmatch(filename, file_pattern):
                    continue

                file_path = os.path.join(root, filename)
                vr = validate_path(file_path, allowed_dirs=log_dirs)
                if not vr.success:
                    continue

                lines = _read_file_lines(file_path)
                if lines is None:
                    continue

                for line in lines:
                    line_stripped = line.strip()

                    # 检测 Call Trace 开始
                    if call_trace_start.search(line_stripped):
                        if current_stack and current_stack.frames:
                            sig = str(current_stack.model_dump())
                            if sig not in seen_stacks:
                                seen_stacks.add(sig)
                                stacks.append(current_stack)
                        current_stack = KernelStack()
                        current_stack.raw_log = line_stripped
                        in_trace = True
                        continue

                    if not in_trace or current_stack is None:
                        continue

                    # 检测 </TASK> 结束
                    if "</TASK>" in line_stripped:
                        in_trace = False
                        if current_stack.frames:
                            sig = str(current_stack.model_dump())
                            if sig not in seen_stacks:
                                seen_stacks.add(sig)
                                stacks.append(current_stack)
                        current_stack = None
                        if len(stacks) >= max_stacks:
                            break
                        continue

                    # 尝试匹配栈帧
                    match = frame_pattern.search(line_stripped)
                    if match:
                        addr = match.group(1) or ""
                        symbol = match.group(2) or ""
                        offset = match.group(3) or ""
                        # 过滤掉非栈帧的符号
                        if symbol and symbol not in ("TASK",):
                            frame = KernelStackFrame(
                                address=addr,
                                symbol=symbol,
                                offset=offset,
                            )
                            current_stack.frames.append(frame)

                    # 累积原始日志
                    current_stack.raw_log += "\n" + line_stripped

                if len(stacks) >= max_stacks:
                    break
            if len(stacks) >= max_stacks:
                break
        if len(stacks) >= max_stacks:
            break

    if not stacks:
        return make_error("NO_MATCHES", "未找到内核调用栈信息")

    return make_success({
        "stacks": [s.model_dump() for s in stacks],
        "total": len(stacks),
    })


def search_source_code(
    source_dirs: list[str],
    keyword: str,
    file_pattern: str = "*.c",
    max_results: int = 100,
    case_sensitive: bool = True,
    use_regex: bool = False,
) -> ToolResult:
    """在源码目录中搜索关键词。

    专门用于源代码搜索，默认区分大小写、默认搜索 .c 文件。
    支持常见的源码文件扩展名（.c, .h, .py, .go, .rs, .java 等）。

    Args:
        source_dirs: 源码目录列表
        keyword: 搜索关键词
        file_pattern: 文件通配符模式（默认 *.c）
        max_results: 最大返回结果数
        case_sensitive: 是否区分大小写（默认 True）
        use_regex: keyword 是否为正则表达式

    Returns:
        ToolResult: 成功时 data["results"] 为 SearchResult 列表
    """
    if not keyword or len(keyword.strip()) < 1:
        return make_error("KEYWORD_TOO_SHORT", "搜索关键词不能为空")

    if not source_dirs:
        return make_error("INVALID_PARAMS", "源码目录列表不能为空")

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        if use_regex:
            pattern = re.compile(keyword, flags)
        else:
            pattern = re.compile(re.escape(keyword), flags)
    except re.error as e:
        return make_error("INVALID_PARAMS", f"正则表达式无效: {e}")

    results: list[SearchResult] = []

    for src_dir in source_dirs:
        path_result = validate_path(src_dir, allowed_dirs=source_dirs)
        if not path_result.success:
            continue

        resolved_dir = path_result.data["resolved_path"]
        if not path_result.data["is_dir"]:
            continue

        for root, dirs, files in os.walk(resolved_dir):
            for filename in files:
                if not fnmatch.fnmatch(filename, file_pattern):
                    continue

                file_path = os.path.join(root, filename)
                vr = validate_path(file_path, allowed_dirs=source_dirs)
                if not vr.success:
                    continue

                lines = _read_file_lines(file_path)
                if lines is None:
                    continue

                for line_no, line in enumerate(lines, 1):
                    line_stripped = line.rstrip("\n\r")
                    if pattern.search(line_stripped):
                        results.append(SearchResult(
                            file_path=file_path,
                            line_number=line_no,
                            content=line_stripped,
                        ))
                        if len(results) >= max_results:
                            break
                if len(results) >= max_results:
                    break
            if len(results) >= max_results:
                break

    if not results:
        return make_error("SEARCH_NO_RESULTS", f"在源码目录中未找到匹配 '{keyword}' 的结果")

    return make_success({
        "results": [r.model_dump() for r in results],
        "total": len(results),
        "keyword": keyword,
    })


def generate_report(
    title: str,
    summary: str,
    avc_records: Optional[list[dict]] = None,
    kernel_stacks: Optional[list[dict]] = None,
    log_results: Optional[list[dict]] = None,
    source_results: Optional[list[dict]] = None,
) -> ToolResult:
    """生成结构化分析报告。

    将多个分析工具的输出整合为一份结构化的分析报告，
    包含摘要、各章节内容和严重级别。

    Args:
        title: 报告标题
        summary: 报告摘要
        avc_records: SELinux AVC 记录列表（来自 parse_selinux_avc）
        kernel_stacks: 内核栈列表（来自 parse_kernel_stack）
        log_results: 日志搜索结果（来自 search_log 或 extract_time_window）
        source_results: 源码搜索结果（来自 search_source_code）

    Returns:
        ToolResult: 成功时 data["report"] 包含完整报告
    """
    if not title:
        return make_error("INVALID_PARAMS", "报告标题不能为空")

    sections: list[ReportSection] = []

    # -- AVC 分析章节 --
    if avc_records:
        denied_count = sum(1 for r in avc_records if not r.get("permissive", False))
        permissive_count = sum(1 for r in avc_records if r.get("permissive", False))

        avc_content = [
            f"共发现 {len(avc_records)} 条 AVC 拒绝记录：",
            f"  - 强制模式拒绝: {denied_count} 条",
            f"  - 宽容模式记录: {permissive_count} 条",
            "",
        ]

        # 按源类型分组统计
        type_stats: dict[str, int] = {}
        for r in avc_records:
            st = r.get("source_type", "unknown")
            type_stats[st] = type_stats.get(st, 0) + 1

        if type_stats:
            avc_content.append("按源类型分布：")
            for st, count in sorted(type_stats.items(), key=lambda x: -x[1]):
                avc_content.append(f"  - {st}: {count} 次")

        sections.append(ReportSection(
            title="SELinux AVC 拒绝分析",
            content="\n".join(avc_content),
            severity="critical" if denied_count > 0 else "warning",
        ))

    # -- 内核栈分析章节 --
    if kernel_stacks:
        ks_content = [
            f"共捕获 {len(kernel_stacks)} 个内核调用栈",
            "",
        ]
        for i, stack in enumerate(kernel_stacks[:10], 1):
            frames = stack.get("frames", [])
            top_frame = frames[0]["symbol"] if frames else "unknown"
            ks_content.append(f"  #{i}: 入口 {top_frame} ({len(frames)} 帧)")

        if len(kernel_stacks) > 10:
            ks_content.append(f"  ... 还有 {len(kernel_stacks) - 10} 个栈")

        sections.append(ReportSection(
            title="内核调用栈分析",
            content="\n".join(ks_content),
            severity="warning" if len(kernel_stacks) > 5 else "info",
        ))

    # -- 日志分析章节 --
    if log_results:
        log_content = [
            f"日志搜索结果共 {len(log_results)} 条匹配行",
        ]
        # 提取前 20 条作为示例
        for r in log_results[:20]:
            fp = r.get("file_path", "")
            ln = r.get("line_number", 0)
            ct = r.get("content", "")[:120]
            log_content.append(f"  {fp}:{ln}  {ct}")

        if len(log_results) > 20:
            log_content.append(f"  ... 还有 {len(log_results) - 20} 条")

        sections.append(ReportSection(
            title="日志搜索分析",
            content="\n".join(log_content),
            severity="info",
        ))

    # -- 源码分析章节 --
    if source_results:
        src_content = [
            f"源码搜索结果共 {len(source_results)} 条匹配行",
        ]
        for r in source_results[:15]:
            fp = r.get("file_path", "")
            ln = r.get("line_number", 0)
            ct = r.get("content", "")[:120]
            src_content.append(f"  {fp}:{ln}  {ct}")

        if len(source_results) > 15:
            src_content.append(f"  ... 还有 {len(source_results) - 15} 条")

        sections.append(ReportSection(
            title="源码关联分析",
            content="\n".join(src_content),
            severity="info",
        ))

    report = AnalysisReport(
        title=title,
        summary=summary,
        sections=sections,
        source_logs=list(set(
            (r.get("file_path", "") for r in (log_results or [])),
        )),
    )

    return make_success({
        "report": report.model_dump(),
        "section_count": len(sections),
    })