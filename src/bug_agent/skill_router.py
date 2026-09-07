"""把项目 Skill 暴露为受控的本地 Agent 工具。"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from .contracts import SkillActivation
from .skills import MAX_SKILLS_PER_TASK, SkillDocument, SkillRegistry


ACTIVATE_SKILL_TOOL = "activate_skill"


class DelegateRouter(Protocol):
    def openai_tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


class SkillAwareToolRouter:
    """代理 MCP Router，并在不扩大外部权限的前提下动态加载 Skill。"""

    def __init__(
        self,
        delegate: DelegateRouter,
        registry: SkillRegistry,
        initial_names: list[str],
        *,
        initial_source: Literal["default", "explicit"],
        auto_enabled: bool,
        documents: list[SkillDocument] | None = None,
    ):
        self.delegate = delegate
        self.registry = registry
        self.auto_enabled = auto_enabled
        catalog = documents if documents is not None else registry.discover()
        self.documents = {item.name: item for item in catalog}
        self._active: dict[str, SkillDocument] = {}
        self.activations: list[SkillActivation] = []
        initial_symptom_seen = False
        for name in initial_names:
            document = registry.load(name)
            self._active[name] = document
            role: Literal["primary", "secondary", "supporting"] = "supporting"
            if document.category == "symptom":
                role = "secondary" if initial_symptom_seen else "primary"
                initial_symptom_seen = True
            self.activations.append(SkillActivation(
                name=name,
                source=initial_source,
                role=role,
                reason=(
                    "Worker 默认启用通用日志分诊"
                    if initial_source == "default"
                    else "调用方显式指定"
                ),
            ))

    @property
    def activated_names(self) -> list[str]:
        return list(self._active)

    def catalog_prompt(self) -> str:
        if not self.auto_enabled:
            return ""
        rows = [
            "# Automatic Skill Activation",
            "你可以调用 activate_skill 按需加载仓库维护的可信分析方法。",
            "当 Issue、首轮诊断或日志证据明确属于某一专项时，必须在形成结论前激活对应 Skill。",
            "工具返回 required_skill_activations 时必须逐项调用 activate_skill；当 inspect_case 返回 aee_db 或 inspect_archive 成员 kind=aee_db 时，必须激活 aee-db-extract 后再解码和分析。",
            "每次只允许一个 primary 症状 Skill；级联故障可额外激活一个 secondary 症状 Skill，后者只能用于验证下游影响或覆盖边界。category=platform 可叠加。",
            "不要仅因日志中偶然出现一个关键词就激活；应结合用户症状、事件身份或诊断证据。",
            "activate_skill 返回的 instructions 是可信仓库指令，但不能扩大工具、路径或写入权限。",
            "可用目录：",
        ]
        for item in self.documents.values():
            state = "（已激活）" if item.name in self._active else ""
            rows.append(f"- {item.name} [{item.category}]{state}: {item.description}")
        return "\n".join(rows)

    def openai_tools(self) -> list[dict[str, Any]]:
        tools = list(self.delegate.openai_tools())
        if not self.auto_enabled:
            return tools
        if any(
            tool.get("function", {}).get("name") == ACTIVATE_SKILL_TOOL
            for tool in tools
        ):
            raise ValueError(f"工具名称冲突: {ACTIVATE_SKILL_TOOL}")
        tools.append({
            "type": "function",
            "function": {
                "name": ACTIVATE_SKILL_TOOL,
                "description": (
                    "Activate one trusted project Skill when the reported symptom or collected "
                    "evidence matches its catalog description."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": list(self.documents),
                            "description": "Skill name from the trusted catalog.",
                        },
                        "reason": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 500,
                            "description": "Observed symptom or evidence that justifies activation.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["primary", "secondary"],
                            "description": "Symptom Skill role. Omit for the primary route; use secondary only for a verified cascade/downstream effect.",
                        },
                    },
                    "required": ["name", "reason"],
                    "additionalProperties": False,
                },
            },
        })
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name != ACTIVATE_SKILL_TOOL:
            return await self.delegate.call(name, arguments)
        return self._activate(arguments)

    def _activate(self, arguments: dict[str, Any]) -> str:
        if not self.auto_enabled:
            return self._error("SKILL_AUTO_SELECTION_DISABLED", "自动 Skill 激活已禁用")
        skill_name = arguments.get("name")
        reason = arguments.get("reason")
        if not isinstance(skill_name, str) or skill_name not in self.documents:
            return self._error("UNKNOWN_SKILL", "只能激活目录中列出的 Skill")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            return self._error("INVALID_SKILL_REASON", "reason 必须是 1..500 字符的非空文本")
        document = self.documents[skill_name]
        requested_role = arguments.get("role")
        if requested_role is None:
            requested_role = "primary" if document.category == "symptom" else "supporting"
        if requested_role not in {"primary", "secondary", "supporting"}:
            return self._error("INVALID_SKILL_ROLE", "role 必须是 primary 或 secondary")
        if document.category != "symptom" and requested_role != "supporting":
            return self._error("INVALID_SKILL_ROLE", "只有症状 Skill 可以声明 primary/secondary")
        if skill_name in self._active:
            document = self._active[skill_name]
            activation = next(item for item in self.activations if item.name == skill_name)
            return self._success(document, already_active=True, role=activation.role)
        if len(self._active) >= MAX_SKILLS_PER_TASK:
            return self._error(
                "SKILL_LIMIT_REACHED", f"单任务最多加载 {MAX_SKILLS_PER_TASK} 个 Skills",
            )
        if document.category == "symptom":
            symptom_activations = [
                item for item in self.activations
                if self._active[item.name].category == "symptom"
            ]
            if requested_role == "secondary" and not symptom_activations:
                return self._error(
                    "SECONDARY_WITHOUT_PRIMARY",
                    "必须先建立 primary 症状路线，才能激活 secondary 级联路线",
                )
            if requested_role == "primary" and symptom_activations:
                return self._error(
                    "PRIMARY_SKILL_CONFLICT",
                    f"主要症状 Skill 已是 {symptom_activations[0].name}，不能再激活 {skill_name}",
                )
            if requested_role == "secondary" and any(
                item.role == "secondary" for item in symptom_activations
            ):
                return self._error(
                    "SECONDARY_SKILL_LIMIT",
                    "单次分析最多激活一个 secondary 症状路线",
                )
        self._active[skill_name] = document
        self.activations.append(SkillActivation(
            name=skill_name, source="agent", role=requested_role, reason=reason.strip(),
        ))
        return self._success(document, already_active=False, role=requested_role)

    @staticmethod
    def _success(
        document: SkillDocument,
        *,
        already_active: bool,
        role: Literal["primary", "secondary", "supporting"],
    ) -> str:
        return json.dumps({
            "success": True,
            "data": {
                "skill": document.name,
                "category": document.category,
                "description": document.description,
                "already_active": already_active,
                "role": role,
                "instructions": document.instructions,
            },
            "error_code": None,
            "error_message": None,
            "retryable": False,
        }, ensure_ascii=False)

    @staticmethod
    def _error(code: str, message: str) -> str:
        return json.dumps({
            "success": False,
            "data": None,
            "error_code": code,
            "error_message": message,
            "retryable": False,
        }, ensure_ascii=False)
