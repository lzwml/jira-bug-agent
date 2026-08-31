from bug_agent.contracts import (
    ActionItem,
    BugAnalysisResult,
    CoverageItem,
    Hypothesis,
    NegativeFinding,
    RCAReport,
    TimelineEntry,
)
from bug_agent.renderer import render_markdown


def test_renderer_outputs_formal_stability_rca_sections():
    result = BugAnalysisResult(
        task_id="BAIC-1",
        status="insufficient_evidence",
        structured_output=True,
        steps=8,
        report=RCAReport(
            conclusion_status="insufficient_evidence",
            summary="直接机制已确认，技术根因尚未确认。",
            observed_symptom="STR 唤醒后 PHUD 黑屏",
            failure_mechanism="PHUD Input ANR，无法恢复显示",
            root_cause=None,
            trigger_conditions=["STR PowerOff 切换"],
            timeline=[TimelineEntry(
                timestamp="07:27:30", clock_domain="android",
                event="PHUD Input ANR", interpretation="应用进程失去响应",
                evidence_ids=["ev-1"],
            )],
            coverage=[CoverageItem(
                layer="SurfaceFlinger", status="partial",
                finding="未发现 fatal", evidence_ids=[],
                gap="缺少目标窗口 sys_log",
            )],
            confirmed_facts=["PHUD 发生 ANR。"],
            hypotheses=[Hypothesis(
                statement="PHUD 图形事务阻塞", confidence=0.78,
                status="supported", supporting_evidence_ids=["ev-1"],
                missing_evidence=["完整 native 栈"], falsification="补抓 debuggerd",
            )],
            negative_findings=[NegativeFinding(
                statement="未命中 SurfaceFlinger fatal", scope="目标 ANR 文件",
                limitation="ANR 文件不是完整 sys_log",
            )],
            missing_evidence=["目标时间窗 sys_log"],
            actions=[ActionItem(
                priority="P0", action="补抓 Perfetto", owner="系统性能",
                expected_artifact="Perfetto trace",
                completion_criteria="可观察 PHUD 到 HWC 的完整链路",
            )],
        ),
    )

    markdown = render_markdown(result)

    assert "# 阶段性 RCA：BAIC-1" in markdown
    assert "**直接故障机制**：PHUD Input ANR" in markdown
    assert "**技术根因**：尚未确认" in markdown
    assert "## 2. 关键时间线" in markdown
    assert "## 3. 分层调查覆盖" in markdown
    assert "## 6. 反证与负向结果" in markdown
    assert "| P0 | 补抓 Perfetto | 系统性能 |" in markdown
    assert "置信度**：中高" in markdown
