"""把稳定业务结果渲染成人类可读文本；不参与 Agent 推理。"""

from __future__ import annotations

from .contracts import BugAnalysisResult


def render_markdown(result: BugAnalysisResult) -> str:
    report = result.report
    lines = [f"# Bug 分析结果：{result.status}", "", report.summary, ""]
    if result.skill_activations:
        source_labels = {"default": "默认", "explicit": "显式", "agent": "自动"}
        lines.extend(["## 使用的 Skills", ""])
        for item in result.skill_activations:
            lines.append(
                f"- `{item.name}`（{source_labels[item.source]}）：{item.reason}"
            )
        lines.append("")
    sections = [
        ("已确认事实", report.confirmed_facts),
        ("缺失证据", report.missing_evidence),
        ("下一步动作", report.next_actions),
    ]
    for title, items in sections:
        if items:
            lines.extend([f"## {title}", ""])
            lines.extend(f"- {item}" for item in items)
            lines.append("")
    if report.hypotheses:
        lines.extend(["## 假设", ""])
        for item in report.hypotheses:
            lines.append(f"- [{item.status} / {item.confidence:.0%}] {item.statement}")
        lines.append("")
    if report.evidence:
        lines.extend(["## 证据引用", ""])
        for item in report.evidence:
            location = item.relative_path
            if item.line_start:
                location += f":{item.line_start}"
                if item.line_end and item.line_end != item.line_start:
                    location += f"-{item.line_end}"
            lines.append(f"- `{item.evidence_id}` {location}")
        lines.append("")
    if not result.structured_output:
        lines.extend(["> 警告：模型未返回标准 RCA JSON，本结果使用了兼容降级。", ""])
    return "\n".join(lines).rstrip()
