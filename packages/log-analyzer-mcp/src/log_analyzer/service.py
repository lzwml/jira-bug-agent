"""面向 Agent 的 V2 日志分析服务。

Service 位于 MCP 协议层和文件解析层之间：

    MCP Server → LogAnalyzerService → CaseRegistry / 文件解析

这里不关心 stdio、JSON-RPC 或 MCP Content 类型，只接收 Python 参数并返回
ToolResult，因此可以脱离 MCP 单独测试，也可以被其他 Runtime 复用。
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Callable

from log_analysis_core import (
    classify_diagnostic_line,
    extract_timestamp,
    infer_component,
    stable_id,
)
from pydantic import ValidationError

from .case_registry import CaseRegistry
from .domain import (
    DiagnosticFinding,
    Evidence,
    ExtractTimelineInput,
    InspectCaseInput,
    OpenCaseInput,
    ParseDiagnosticsInput,
    SearchEvidenceInput,
    TimelineEvent,
)
from .errors import make_error, make_success
from .models import ToolResult


MAX_SCAN_BYTES_PER_FILE = 128 * 1024 * 1024
MAX_SCAN_BYTES_PER_CALL = 512 * 1024 * 1024


class LogAnalyzerService:
    """五个 V2 工具的领域实现与统一分发入口。"""

    def __init__(self, registry: CaseRegistry):
        self.registry = registry
        self.handlers: dict[str, Callable[..., ToolResult]] = {
            "open_case": self.open_case,
            "inspect_case": self.inspect_case,
            "search_evidence": self.search_evidence,
            "extract_timeline": self.extract_timeline,
            "parse_diagnostics": self.parse_diagnostics,
        }

    def dispatch(self, name: str, arguments: dict) -> ToolResult:
        """校验并执行工具，将可预期错误转换为统一 ToolResult。

        MCP Server 只需要调用这一个入口。参数验证失败是调用错误，文件读取
        失败可能可重试；未知编程错误交给最外层协议边界兜底。
        """

        handler = self.handlers.get(name)
        if handler is None:
            return make_error("NOT_IMPLEMENTED", f"未知工具: {name}")
        try:
            return handler(**arguments)
        except ValidationError as exc:
            return make_error("INVALID_PARAMS", exc.json(include_url=False))
        except (OSError, UnicodeError) as exc:
            return make_error("FILE_READ_ERROR", str(exc), retryable=True)

    def open_case(self, **kwargs) -> ToolResult:
        """注册 Case；它是其他四个工具的前置步骤。"""

        params = OpenCaseInput.model_validate(kwargs)
        return self.registry.open_case(params.case_path)

    def inspect_case(self, **kwargs) -> ToolResult:
        """返回 Case 概览，帮助 Agent 在读取大日志前先制定计划。"""

        params = InspectCaseInput.model_validate(kwargs)
        entry = self.registry.get_case(params.case_id)
        if entry is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        _, info = entry
        kind_counts = Counter(artifact.kind for artifact in info.artifacts)
        text_bytes = sum(a.size_bytes for a in info.artifacts if a.readable_text)
        return make_success({
            "case_id": info.case_id,
            "name": info.name,
            "artifact_count": info.artifact_count,
            "total_size_bytes": info.total_size_bytes,
            "text_size_bytes": text_bytes,
            "kinds": dict(sorted(kind_counts.items())),
            "artifacts": [a.model_dump() for a in info.artifacts[: params.sample_limit]],
            "artifacts_truncated": info.artifact_count > params.sample_limit,
        })

    def _evidence_from_pending(self, pending: dict) -> Evidence:
        """把流式搜索过程中暂存的前后文组装为 Evidence。"""

        lines = pending["before"] + [pending["line"]] + pending["after"]
        timestamp = extract_timestamp(pending["line"])
        return Evidence(
            evidence_id=stable_id(
                "evidence",
                f"{pending['artifact'].artifact_id}:{pending['line_number']}:{pending['query']}",
            ),
            artifact_id=pending["artifact"].artifact_id,
            artifact_name=pending["artifact"].name,
            relative_path=pending["artifact"].relative_path,
            line_start=max(1, pending["line_number"] - len(pending["before"])),
            line_end=pending["line_number"] + len(pending["after"]),
            content="\n".join(lines),
            timestamp_raw=timestamp.raw if timestamp else None,
            query=pending["query"],
        )

    def search_evidence(self, **kwargs) -> ToolResult:
        """在 Case 文本附件中进行有预算的流式字面量搜索。

        算法使用一个 before deque 保存前文，并用 pending 列表等待后文到齐，
        因而无需把整个日志读入内存。返回结果始终包含文件与行号，供 Agent
        在最终结论中引用。
        """

        params = SearchEvidenceInput.model_validate(kwargs)
        if self.registry.get_case(params.case_id) is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")

        artifacts = self.registry.select_artifacts(
            params.case_id,
            artifact_ids=params.artifact_ids,
            artifact_kinds=params.artifact_kinds,
        )
        # 只做字面量匹配，避免让不受信任的模型输入触发灾难性正则回溯。
        needle = params.query if params.case_sensitive else params.query.casefold()
        results: list[Evidence] = []
        warnings: list[str] = []
        scanned_bytes = 0
        truncated = False

        for artifact in artifacts:
            path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
            if path is None:
                continue
            if scanned_bytes >= MAX_SCAN_BYTES_PER_CALL:
                warnings.append("达到单次调用总扫描预算，部分文件未扫描")
                truncated = True
                break

            # before 保存最近 N 行；pending 保存已经命中但仍在收集 after 的证据。
            before: deque[str] = deque(maxlen=params.context_before)
            pending: list[dict] = []
            file_bytes = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    file_bytes += len(raw_line.encode("utf-8", errors="replace"))
                    if file_bytes > MAX_SCAN_BYTES_PER_FILE:
                        warnings.append(f"{artifact.relative_path} 超过单文件扫描预算，结果已截断")
                        truncated = True
                        break
                    line = raw_line.rstrip("\r\n")

                    # 当前行先作为之前命中的“后文”，再判断它本身是否命中新证据。
                    completed = []
                    for item in pending:
                        item["after"].append(line)
                        item["remaining"] -= 1
                        if item["remaining"] <= 0:
                            completed.append(item)
                    for item in completed:
                        pending.remove(item)
                        results.append(self._evidence_from_pending(item))

                    haystack = line if params.case_sensitive else line.casefold()
                    if needle in haystack and len(results) + len(pending) < params.max_results:
                        item = {
                            "artifact": artifact,
                            "line_number": line_number,
                            "line": line,
                            "before": list(before),
                            "after": [],
                            "remaining": params.context_after,
                            "query": params.query,
                        }
                        if params.context_after == 0:
                            results.append(self._evidence_from_pending(item))
                        else:
                            pending.append(item)

                    before.append(line)
                    if len(results) >= params.max_results and not pending:
                        truncated = True
                        break

            scanned_bytes += min(file_bytes, MAX_SCAN_BYTES_PER_FILE)
            for item in pending:
                results.append(self._evidence_from_pending(item))
            if len(results) >= params.max_results:
                results = results[: params.max_results]
                truncated = True
                break

        # 零匹配也是成功：它表示“没有发现该证据”，不是工具执行失败。
        return make_success({
            "case_id": params.case_id,
            "query": params.query,
            "items": [item.model_dump() for item in results],
            "match_count": len(results),
            "scanned_bytes": scanned_bytes,
            "truncated": truncated,
            "warnings": warnings,
        })

    def extract_timeline(self, **kwargs) -> ToolResult:
        """从多个附件中提取锚点事件，并在各自时钟域内排序。

        anchors 必须由当前 Skill/Agent 显式提供。不同 clock_domain 的事件被
        保留，但不会被伪造为同一时间轴。
        """

        params = ExtractTimelineInput.model_validate(kwargs)
        if self.registry.get_case(params.case_id) is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        anchors = params.anchors
        anchors_folded = [anchor.casefold() for anchor in anchors]
        artifacts = self.registry.select_artifacts(params.case_id, artifact_ids=params.artifact_ids)
        events: list[TimelineEvent] = []
        scanned_bytes = 0
        truncated = False

        for artifact in artifacts:
            path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
            if path is None:
                continue
            file_bytes = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    file_bytes += len(raw_line.encode("utf-8", errors="replace"))
                    if file_bytes > MAX_SCAN_BYTES_PER_FILE:
                        truncated = True
                        break
                    line = raw_line.rstrip("\r\n")
                    lowered = line.casefold()
                    matched_anchor = next(
                        (anchor for anchor, folded in zip(anchors, anchors_folded) if folded in lowered),
                        None,
                    )
                    if matched_anchor is None:
                        continue
                    timestamp = extract_timestamp(line, params.year_hint)
                    if timestamp is None:
                        continue
                    events.append(TimelineEvent(
                        event_id=stable_id("event", f"{artifact.artifact_id}:{line_number}:{line}"),
                        artifact_id=artifact.artifact_id,
                        relative_path=artifact.relative_path,
                        line_number=line_number,
                        clock_domain=timestamp.clock_domain,
                        timestamp_raw=timestamp.raw,
                        timestamp_normalized=timestamp.normalized,
                        relative_seconds=timestamp.relative_seconds,
                        component=infer_component(line, anchors),
                        event_type=matched_anchor,
                        content=line,
                    ))
                    if len(events) >= params.max_events:
                        truncated = True
                        break
            scanned_bytes += min(file_bytes, MAX_SCAN_BYTES_PER_FILE)
            if len(events) >= params.max_events or scanned_bytes >= MAX_SCAN_BYTES_PER_CALL:
                truncated = True
                break

        # 先排列可归一化的 wall/android 时间，再排列 kernel 相对时间。
        # 这只是稳定展示顺序，不代表两个时钟域已经完成对齐。
        def sort_key(event: TimelineEvent):
            if event.timestamp_normalized:
                return (0, event.timestamp_normalized, 0.0, event.relative_path, event.line_number)
            if event.relative_seconds is not None:
                return (1, "", event.relative_seconds, event.relative_path, event.line_number)
            return (2, "", 0.0, event.relative_path, event.line_number)

        events.sort(key=sort_key)
        return make_success({
            "case_id": params.case_id,
            "anchors": anchors,
            "events": [event.model_dump() for event in events],
            "event_count": len(events),
            "clock_domains": sorted({event.clock_domain for event in events}),
            "truncated": truncated,
        })

    def _make_line_evidence(self, artifact, line_number: int, content: str) -> Evidence:
        """为确定性诊断结果创建可追溯的 Evidence。"""

        timestamp = extract_timestamp(content)
        return Evidence(
            evidence_id=stable_id("evidence", f"{artifact.artifact_id}:{line_number}:{content}"),
            artifact_id=artifact.artifact_id,
            artifact_name=artifact.name,
            relative_path=artifact.relative_path,
            line_start=line_number,
            line_end=line_number,
            content=content,
            timestamp_raw=timestamp.raw if timestamp else None,
        )

    def parse_diagnostics(self, **kwargs) -> ToolResult:
        """使用确定性规则提取常见诊断信号。

        这里输出 Finding 而不是 Root Cause：看到 Fatal/AVC/Call Trace 是事实，
        这些事实是否导致当前 Bug 仍需 Agent 结合时间线、代码和历史案例判断。
        """

        params = ParseDiagnosticsInput.model_validate(kwargs)
        if self.registry.get_case(params.case_id) is None:
            return make_error("CASE_NOT_OPEN", "Case 尚未注册，请先调用 open_case")
        wanted = set(params.diagnostic_types)
        artifacts = self.registry.select_artifacts(params.case_id, artifact_ids=params.artifact_ids)
        findings: list[DiagnosticFinding] = []
        truncated = False
        for artifact in artifacts:
            path = self.registry.get_artifact_path(params.case_id, artifact.artifact_id)
            if path is None:
                continue
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = iter(enumerate(handle, 1))
                for line_number, raw_line in lines:
                    line = raw_line.rstrip("\r\n")
                    diagnostic = classify_diagnostic_line(line, wanted)

                    if diagnostic and diagnostic.diagnostic_type == "kernel_stack":
                        # Call Trace 是多行结构；在同一个流中向后收集，限制 64 行
                        # 防止缺少结束标记的损坏日志吞掉整个文件。
                        stack_lines = [line]
                        end_line = line_number
                        for next_line_number, next_raw in lines:
                            next_line = next_raw.rstrip("\r\n")
                            if not next_line.strip() or len(stack_lines) >= 64:
                                break
                            stack_lines.append(next_line)
                            end_line = next_line_number
                            if "</TASK>" in next_line:
                                break
                        evidence = self._make_line_evidence(artifact, line_number, "\n".join(stack_lines))
                        evidence.line_end = end_line
                        findings.append(DiagnosticFinding(
                            finding_id=stable_id("finding", evidence.evidence_id),
                            diagnostic_type=diagnostic.diagnostic_type,
                            severity=diagnostic.severity,
                            summary=diagnostic.summary,
                            evidence=evidence,
                            attributes={"frame_lines": max(0, len(stack_lines) - 1)},
                        ))
                        if len(findings) >= params.max_findings:
                            truncated = True
                            break
                        continue

                    if diagnostic:
                        evidence = self._make_line_evidence(artifact, line_number, line)
                        findings.append(DiagnosticFinding(
                            finding_id=stable_id("finding", evidence.evidence_id),
                            diagnostic_type=diagnostic.diagnostic_type,
                            severity=diagnostic.severity,
                            summary=diagnostic.summary,
                            evidence=evidence,
                            attributes=diagnostic.attributes,
                        ))
                        if len(findings) >= params.max_findings:
                            truncated = True
                            break
            if len(findings) >= params.max_findings:
                break

        return make_success({
            "case_id": params.case_id,
            "findings": [finding.model_dump() for finding in findings],
            "finding_count": len(findings),
            "truncated": truncated,
        })
