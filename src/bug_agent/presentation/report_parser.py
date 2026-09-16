"""Turn machine-oriented RCA output into a stable human/client presentation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re

from ..domain.contracts import BugAnalysisResult, RCAReport, ReportValidation
from ..domain.models import ToolEvent
from .renderer import render_markdown
from ..domain.report_validation import validate_report


@dataclass(frozen=True)
class PresentedAnswer:
    """One answer with separate human and machine representations."""

    content: str
    content_format: str = "plain_text"
    report: RCAReport | None = None
    report_validation: ReportValidation | None = None


def parse_report_output(raw: str) -> tuple[RCAReport, bool]:
    """Parse an RCA JSON object while tolerating a short preamble or code fence."""

    candidate = raw.strip()
    fenced = re.fullmatch(
        r"```(?:[A-Za-z0-9_-]+)?\s*(.*?)\s*```",
        candidate,
        re.DOTALL,
    )
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
    try:
        return RCAReport.model_validate(json.loads(candidate)), True
    except (json.JSONDecodeError, ValueError):
        return RCAReport(
            conclusion_status="hypothesis_only",
            summary=raw.strip() or "Agent 未生成分析结论。",
            missing_evidence=["模型未按 RCAReport Schema 返回结构化结果"],
            next_actions=["检查模型的结构化输出能力或调整 Prompt"],
        ), False


def present_conversation_answer(
    raw: str,
    *,
    task_id: str,
    tool_events: list[ToolEvent],
    strict_evidence_validation: bool = True,
) -> PresentedAnswer:
    """Keep ordinary chat as text, but render a structured RCA for humans.

    The typed ``report`` remains available to API clients such as the VS Code
    extension. ``content`` is the corresponding human-readable Markdown.
    """

    report, structured = parse_report_output(raw)
    if not structured:
        return PresentedAnswer(content=raw.strip())
    report, validation = validate_report(
        report,
        tool_events,
        strict=strict_evidence_validation,
    )
    result = BugAnalysisResult(
        task_id=task_id,
        status=(
            "insufficient_evidence"
            if report.conclusion_status == "insufficient_evidence"
            else "completed"
        ),
        report=report,
        steps=max((event.step for event in tool_events), default=0),
        structured_output=True,
        report_validation=validation,
    )
    return PresentedAnswer(
        content=render_markdown(result),
        content_format="markdown",
        report=report,
        report_validation=validation,
    )
