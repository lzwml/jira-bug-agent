from bug_agent.domain.contracts import (
    ActionItem,
    AnalysisGuide,
    BugAnalysisResult,
    CoverageItem,
    Hypothesis,
    NegativeFinding,
    RCAReport,
    TimelineEntry,
)
from bug_agent.domain.models import TokenUsage
from bug_agent.presentation.renderer import render_analysis_guide, render_markdown


def test_renderer_outputs_formal_stability_rca_sections():
    result = BugAnalysisResult(
        task_id="BAIC-1",
        status="insufficient_evidence",
        structured_output=True,
        steps=8,
        token_usage=TokenUsage(
            prompt_tokens=1200,
            completion_tokens=300,
            total_tokens=1500,
            cached_prompt_tokens=400,
            reasoning_tokens=100,
            model_calls=4,
            reported_calls=4,
            complete=True,
        ),
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
    assert "**模型 Token 消耗**：总计 1,500；输入 1,200；输出 300" in markdown
    assert "模型调用 4 次；缓存输入 400；Reasoning 100" in markdown
    assert "当前没有已验证的技术根因" in markdown


def test_analysis_guide_is_rendered_independently_from_formal_rca():
    guide = AnalysisGuide(
        overview="从症状开始，逐步验证候选故障链。",
        reasoning_steps=[{
            "observation": "发生 ANR",
            "question": "ANR 是否解释黑屏？",
            "reasoning": "先确认直接机制。",
            "verification": "检查 ev-1。",
            "outcome": "进入图形事务路径。",
            "evidence_ids": ["ev-1"],
        }],
        reusable_approach=["先验证直接机制。"],
    )

    markdown = render_analysis_guide(guide, "BAIC-1")

    assert "# 问题分析讲解：BAIC-1" in markdown
    assert "## 调查是怎样推进的" in markdown
    assert "`ev-1`" in markdown
