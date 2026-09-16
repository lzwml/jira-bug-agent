"""把稳定业务结果渲染成人类可读的正式稳定性 RCA；不参与 Agent 推理。"""

from __future__ import annotations

from ..domain.contracts import AnalysisGuide, BugAnalysisResult


STATUS_LABELS = {
    "confirmed": "根因已确认",
    "hypothesis_only": "已形成候选根因",
    "insufficient_evidence": "根因未确认（证据不足）",
}
COVERAGE_LABELS = {
    "covered": "已覆盖",
    "partial": "部分覆盖",
    "not_covered": "未覆盖",
    "not_applicable": "不适用",
}
CLOCK_LABELS = {
    "wall": "Wall Clock",
    "android": "Android",
    "kernel_monotonic": "Kernel Monotonic",
    "reported": "Jira/人工转述",
    "unknown": "未知",
}


def _confidence_label(value: float) -> str:
    if value >= 0.85:
        return "高"
    if value >= 0.70:
        return "中高"
    if value >= 0.45:
        return "中"
    return "低"


def _ids(values: list[str]) -> str:
    return "、".join(f"`{value}`" for value in values) if values else "-"


def _token_usage_label(result: BugAnalysisResult) -> str:
    usage = result.token_usage
    if usage is None:
        return "未知（模型服务未返回 usage）"
    details = [
        f"总计 {usage.total_tokens:,}",
        f"输入 {usage.prompt_tokens:,}",
        f"输出 {usage.completion_tokens:,}",
        f"模型调用 {usage.model_calls} 次",
    ]
    if usage.cached_prompt_tokens is not None:
        details.append(f"缓存输入 {usage.cached_prompt_tokens:,}")
    if usage.reasoning_tokens is not None:
        details.append(f"Reasoning {usage.reasoning_tokens:,}")
    if not usage.complete:
        details.append(
            f"部分统计（{usage.reported_calls}/{usage.model_calls} 次返回 usage）"
        )
    return "；".join(details)


def render_markdown(result: BugAnalysisResult) -> str:
    report = result.report
    lines = [
        f"# 阶段性 RCA：{result.task_id}",
        "",
        "## 1. 执行摘要",
        "",
        f"- **结论状态**：{STATUS_LABELS[report.conclusion_status]}",
        f"- **用户可见现象**：{report.observed_symptom or '未结构化记录'}",
        f"- **直接故障机制**：{report.failure_mechanism or '尚未确认'}",
        f"- **技术根因**：{report.root_cause or '尚未确认'}",
        f"- **根因证据**：{_ids(report.root_cause_evidence_ids)}",
        f"- **模型 Token 消耗**：{_token_usage_label(result)}",
        "",
    ]
    if report.conclusion_status != "confirmed":
        lines.extend([
            "> **阅读提示**：当前没有已验证的技术根因。"
            "下方涉及因果的内容均为候选解释，不能作为定案结论。",
            "",
        ])
    summary = report.summary
    if report.conclusion_status != "confirmed" and "根因尚未确认" not in summary:
        summary = f"根因尚未确认。当前证据下：{summary}"
    lines.extend([summary, ""])

    if result.human_checkpoint is not None:
        checkpoint = result.human_checkpoint
        lines[1:1] = [
            "",
            "## 等待人工提示",
            "",
            f"- **问题**：{checkpoint.question}",
            f"- **阻塞原因**：{checkpoint.blocking_reason}",
            f"- **需要输入**：{checkpoint.requested_input}",
            f"- **已尝试工具调用**：{checkpoint.tool_attempts_before}",
        ]

    if result.report_validation is not None:
        validation = result.report_validation
        label = "已通过" if validation.grounded else "未通过"
        lines.insert(
            9,
            f"- **证据校验**：{label}（已验证 {validation.verified_evidence_count} 条）",
        )

    if report.incident is not None:
        incident = report.incident
        lines.extend([
            "### 事故身份", "",
            f"- Incident：{incident.incident_id or '未确定'}",
            f"- Boot：{incident.boot_identity or '未确定'}",
            f"- 进程：{incident.process_name or '未确定'}"
            + (f" (PID {incident.pid})" if incident.pid is not None else ""),
            f"- Build：{incident.build_identity or '未确定'}",
            f"- 系统域：{incident.system_domain or '未确定'}",
            "",
        ])

    if report.trigger_conditions:
        lines.extend(["### 触发或促成条件", ""])
        lines.extend(f"- {item}" for item in report.trigger_conditions)
        lines.append("")

    if report.timeline:
        lines.extend([
            "## 2. 关键时间线", "",
            "| 时间 | 时钟域 | 事件 | 解释 | 证据 |",
            "|---|---|---|---|---|",
        ])
        for item in report.timeline:
            lines.append(
                f"| {item.timestamp} | {CLOCK_LABELS[item.clock_domain]} | "
                f"{item.event} | {item.interpretation or '-'} | {_ids(item.evidence_ids)} |"
            )
        lines.append("")

    if report.coverage:
        lines.extend([
            "## 3. 分层调查覆盖", "",
            "| 层级 | 覆盖状态 | 当前结论 | 剩余缺口 | 证据 |",
            "|---|---|---|---|---|",
        ])
        for item in report.coverage:
            lines.append(
                f"| {item.layer} | {COVERAGE_LABELS[item.status]} | {item.finding} | "
                f"{item.gap or '-'} | {_ids(item.evidence_ids)} |"
            )
        lines.append("")

    if report.confirmed_facts:
        lines.extend(["## 4. 已确认事实", ""])
        lines.extend(f"- {item}" for item in report.confirmed_facts)
        lines.append("")

    if report.hypotheses:
        lines.extend(["## 5. 假设与证伪", ""])
        for index, item in enumerate(report.hypotheses, 1):
            lines.extend([
                f"### 假设 {index}：{item.statement}", "",
                f"- **状态**：{item.status}",
                f"- **置信度**：{_confidence_label(item.confidence)}",
                f"- **支持证据**：{_ids(item.supporting_evidence_ids)}",
                f"- **反对证据**：{_ids(item.contradicting_evidence_ids)}",
                f"- **证伪方法**：{item.falsification or '-'}",
            ])
            if item.missing_evidence:
                lines.append("- **尚缺证据**：" + "；".join(item.missing_evidence))
            lines.append("")

    if report.negative_findings:
        lines.extend(["## 6. 反证与负向结果", ""])
        for item in report.negative_findings:
            lines.extend([
                f"- **{item.statement}**",
                f"  - 范围：{item.scope}",
                f"  - 限制：{item.limitation}",
                f"  - 证据：{_ids(item.evidence_ids)}",
            ])
        lines.extend(["", "> 零匹配只表示当前证据范围内未命中，不等于事件未发生。", ""])

    if report.missing_evidence:
        lines.extend(["## 7. 缺失证据", ""])
        lines.extend(f"- {item}" for item in report.missing_evidence)
        lines.append("")

    if report.actions or report.next_actions:
        lines.extend(["## 8. 下一步动作", ""])
        if report.actions:
            lines.extend([
                "| 优先级 | 动作 | 模块/负责人 | 预期产物 | 完成标准 |",
                "|---|---|---|---|---|",
            ])
            for item in report.actions:
                lines.append(
                    f"| {item.priority} | {item.action} | {item.owner or '-'} | "
                    f"{item.expected_artifact} | {item.completion_criteria} |"
                )
        else:
            lines.extend(f"- {item}" for item in report.next_actions)
        lines.append("")

    if report.evidence:
        lines.extend(["## 9. 证据附录", ""])
        for item in report.evidence:
            location = item.relative_path
            if item.line_start:
                location += f":{item.line_start}"
                if item.line_end and item.line_end != item.line_start:
                    location += f"-{item.line_end}"
            excerpt = f" — {item.excerpt}" if item.excerpt else ""
            lines.append(f"- `{item.evidence_id}` {location}{excerpt}")
        lines.append("")

    if result.skill_activations:
        source_labels = {"default": "默认", "explicit": "显式", "agent": "自动"}
        lines.extend(["## 10. 分析元数据", ""])
        lines.extend([
            f"- **Task ID**：`{result.task_id}`",
            f"- **执行步数**：{result.steps}",
            f"- **结构化输出**：{'是' if result.structured_output else '否'}",
            "- **使用的 Skills**：",
        ])
        for item in result.skill_activations:
            lines.append(
                f"  - `{item.name}`（{source_labels[item.source]}）：{item.reason}"
            )
        lines.append("")

    if not result.structured_output:
        lines.extend(["> 警告：模型未返回标准 RCA JSON，本结果使用了兼容降级。", ""])
    return "\n".join(lines).rstrip()


def render_analysis_guide(guide: AnalysisGuide, task_id: str) -> str:
    """渲染独立学习产物，刻意不修改正式 RCA 的 render_markdown。"""

    lines = [f"# 问题分析讲解：{task_id}", "", guide.overview, ""]
    if guide.reasoning_steps:
        lines.extend(["## 调查是怎样推进的", ""])
        for index, step in enumerate(guide.reasoning_steps, 1):
            lines.extend([
                f"### {index}. {step.question}", "",
                f"- **观察**：{step.observation}",
                f"- **为什么先查这里**：{step.reasoning}",
                f"- **如何验证**：{step.verification}",
                f"- **结果与下一步**：{step.outcome}",
                f"- **证据**：{_ids(step.evidence_ids)}", "",
            ])
    if guide.reusable_approach:
        lines.extend(["## 可复用的排查方法", ""])
        lines.extend(f"- {item}" for item in guide.reusable_approach)
        lines.append("")
    if guide.limitations:
        lines.extend(["## 当前边界", ""])
        lines.extend(f"- {item}" for item in guide.limitations)
        lines.append("")
    return "\n".join(lines).rstrip()
