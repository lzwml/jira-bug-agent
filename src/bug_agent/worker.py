"""部门 Workflow、CLI、HTTP 或队列共同调用的 Worker Facade。"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Callable, Protocol

from .agent import BugAnalysisAgent, ModelProvider, ToolRouter
from .config import AgentConfig, default_export_root
from .contracts import BugAnalysisResult, BugAnalysisTask, RCAReport
from .mcp_router import McpToolRouter
from .prompts import JIRA_WORKFLOW_PROMPT, LOCAL_WORKFLOW_PROMPT, REPORT_FORMAT_PROMPT
from .provider import OpenAICompatibleProvider
from .runstore import write_run_record
from .skills import SkillRegistry


class CloseableProvider(ModelProvider, Protocol):
    async def close(self) -> None: ...


ProviderFactory = Callable[[AgentConfig], CloseableProvider]
RouterFactory = Callable[[], McpToolRouter]


def _default_provider(config: AgentConfig) -> CloseableProvider:
    return OpenAICompatibleProvider(config)


def _extract_report(raw: str) -> tuple[RCAReport, bool]:
    """优先解析严格 JSON；失败时保留模型文本并显式标记为非结构化。"""

    candidate = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, re.DOTALL | re.IGNORECASE)
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


class BugAnalysisWorker:
    """对外稳定 Worker；内部创建并约束一个 BugAnalysisAgent。"""

    def __init__(
        self,
        config: AgentConfig,
        provider_factory: ProviderFactory = _default_provider,
        router_factory: RouterFactory = McpToolRouter,
        skill_registry: SkillRegistry | None = None,
    ):
        self.config = config
        self.provider_factory = provider_factory
        self.router_factory = router_factory
        self.skill_registry = skill_registry or SkillRegistry.default()

    async def execute(self, task: BugAnalysisTask) -> BugAnalysisResult:
        run_config = replace(
            self.config,
            max_steps=task.max_steps if task.max_steps is not None else self.config.max_steps,
        )
        provider = None
        applied_skills: list[str] = []
        # run 初始化为 None：若在 Agent 运行前（Skill 加载/准备阶段）就失败，
        # 落盘时仍能记录 task 与失败结果，只是没有 trace。
        run = None
        try:
            skill_prompt, applied_skills = self.skill_registry.render(task.skills)
            provider = self.provider_factory(run_config)
            try:
                async with self.router_factory() as router:
                    prompt, instruction = await self._prepare(task, router)
                    run = await BugAnalysisAgent(run_config, provider).run(
                        instruction,
                        prompt + "\n\n" + skill_prompt + REPORT_FORMAT_PROMPT,
                        router,
                    )
            finally:
                await provider.close()
        except Exception as exc:
            # Worker 是应用边界：普通准备/基础设施错误转成稳定结果。不要把未知
            # 异常详情直接暴露给上游，以免第三方响应或凭据进入任务系统。
            message = str(exc) if isinstance(exc, (ValueError, OSError)) else type(exc).__name__
            result = BugAnalysisResult(
                task_id=task.task_id,
                status="failed",
                report=RCAReport(
                    conclusion_status="insufficient_evidence",
                    summary="Bug 分析任务执行失败。",
                    missing_evidence=["Worker 未能完成工具环境准备或 Agent 执行"],
                    next_actions=["检查 Worker 配置、MCP 健康状态和输入路径"],
                ),
                steps=0,
                structured_output=False,
                applied_skills=applied_skills,
                error=message,
            )
            # 失败也要落盘（此时 run 为 None，trace 为空），便于排查准备阶段问题。
            write_run_record(task, run, result)
            return result

        report, structured = _extract_report(run.final_answer)
        if run.status == "failed":
            status = "failed"
        elif run.status == "max_steps":
            status = "max_steps"
        elif report.conclusion_status == "insufficient_evidence":
            status = "insufficient_evidence"
        else:
            status = "completed"
        result = BugAnalysisResult(
            task_id=task.task_id,
            status=status,
            report=report,
            steps=run.steps,
            structured_output=structured,
            applied_skills=applied_skills,
            trace=run.tool_events if task.include_trace else [],
            error=run.error,
        )
        # 落盘完整 trace（来自 run.tool_events，与 include_trace 无关），
        # 保证默认运行也能复盘。失败只警告，不影响返回给上游的结果。
        write_run_record(task, run, result)
        return result

    async def _prepare(self, task: BugAnalysisTask, router: ToolRouter) -> tuple[str, str]:
        """只在 Worker 边界处理部署模式和 MCP 生命周期。"""

        # 测试或其他 Runtime 可以提供兼容 Router，并自行记录连接请求。
        connect = getattr(router, "connect_python_server")

        if task.source == "jira":
            export_root = default_export_root()
            export_root.mkdir(parents=True, exist_ok=True)
            await connect("jira", "jira_bug_mcp.server")
            await connect(
                "log", "log_analyzer.server",
                {"LOG_ANALYZER_ALLOWED_ROOTS": str(export_root)},
            )
            instruction = (
                f"任务编号：{task.task_id}\n"
                f"请分析 Jira Bug {task.issue_key.upper()}。目标：{task.objective}"
            )
            return JIRA_WORKFLOW_PROMPT, instruction

        case_path = Path(task.case_path or "").expanduser().resolve()
        if not case_path.is_dir():
            raise ValueError(f"Case 目录不存在: {case_path}")
        await connect(
            "log", "log_analyzer.server",
            {"LOG_ANALYZER_ALLOWED_ROOTS": str(case_path)},
        )
        instruction = (
            f"任务编号：{task.task_id}\n"
            f"请分析本地 Bug Case：{case_path}。目标：{task.objective}"
        )
        return LOCAL_WORKFLOW_PROMPT, instruction
