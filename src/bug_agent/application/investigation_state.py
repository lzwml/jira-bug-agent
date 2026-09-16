"""In-memory, evidence-bound investigation state and its local Agent tool."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import ValidationError

from ..domain.contracts import (
    IncidentProfile,
    InvestigationCandidate,
    InvestigationCoverage,
    InvestigationHypothesis,
    InvestigationState,
)
from ..domain.report_validation import build_evidence_registry
from ..domain.models import ToolEvent


UPDATE_INVESTIGATION_STATE_TOOL = "update_investigation_state"


class DelegateRouter(Protocol):
    def openai_tools(self) -> list[dict[str, Any]]: ...
    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


class InvestigationStateToolRouter:
    """Adds a local state tool without granting filesystem or external access."""

    def __init__(self, delegate: DelegateRouter, *, mode: Literal["false", "shadow", "enforce"]):
        self.delegate = delegate
        self.state = InvestigationState(mode=mode)
        self._events: list[ToolEvent] = []

    def openai_tools(self) -> list[dict[str, Any]]:
        tools = list(self.delegate.openai_tools())
        if self.state.mode == "false":
            return tools
        tools.append({
            "type": "function",
            "function": {
                "name": UPDATE_INVESTIGATION_STATE_TOOL,
                "description": (
                    "更新当前调查的结构化工作状态（事故范围、假设、覆盖面和候选材料）。"
                    "宿主会自动快照并在同一 Case 的后续会话中恢复该状态；此工具不是‘保存最终报告’工具，"
                    "也不能承诺文件或服务端持久化结果。不要询问用户是否调用它来保存报告。"
                    "它不能读取文件或外部系统；Evidence ID 和 Artifact ID 必须来自此前工具返回。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": [
                            "set_incident_profile", "upsert_hypothesis", "upsert_coverage",
                            "record_candidate",
                        ]},
                        "value": {"type": "object"},
                    },
                    "required": ["operation", "value"],
                    "additionalProperties": False,
                },
            },
        })
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == UPDATE_INVESTIGATION_STATE_TOOL:
            return self._update(arguments)
        raw = await self.delegate.call(name, arguments)
        # Rebuild from successful payloads so the state tool can never accept
        # invented evidence/artifact IDs.
        event = ToolEvent(step=len(self._events) + 1, tool_call_id="state-observer", tool_name=name,
                          arguments=arguments, success=self._success(raw), result=raw)
        self._events.append(event)
        registry = build_evidence_registry(self._events)
        self.state.known_evidence_ids = sorted(
            set(self.state.known_evidence_ids) | set(registry)
        )
        self.state.known_artifact_ids = sorted(self._artifact_ids(raw) | set(self.state.known_artifact_ids))
        return raw

    @staticmethod
    def _success(raw: str) -> bool:
        try:
            return bool(json.loads(raw).get("success"))
        except (json.JSONDecodeError, AttributeError):
            return False

    @staticmethod
    def _artifact_ids(raw: str) -> set[str]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return set()
        found: set[str] = set()
        def walk(value: Any) -> None:
            if isinstance(value, dict):
                item = value.get("artifact_id")
                if isinstance(item, str) and item:
                    found.add(item)
                for child in value.values(): walk(child)
            elif isinstance(value, list):
                for child in value: walk(child)
        walk(payload)
        return found

    def _update(self, arguments: dict[str, Any]) -> str:
        operation = arguments.get("operation")
        value = arguments.get("value")
        if not isinstance(value, dict):
            return self._error("INVALID_STATE_VALUE", "value 必须是对象")
        try:
            if operation == "set_incident_profile":
                if self.state.incident_profile is not None:
                    return self._error("INCIDENT_PROFILE_ALREADY_SET", "IncidentProfile 只能首次写入")
                profile = IncidentProfile.model_validate(value)
                self._validate_refs(profile.source_refs, "source_refs")
                self.state.incident_profile = profile
            elif operation == "upsert_hypothesis":
                item = InvestigationHypothesis.model_validate(value)
                self._validate_refs(item.supporting_evidence_ids + item.contradicting_evidence_ids, "evidence_ids")
                old = next((x for x in self.state.hypotheses if x.hypothesis_id == item.hypothesis_id), None)
                if old is None and item.status != "open":
                    return self._error(
                        "INVALID_HYPOTHESIS_TRANSITION",
                        "新 hypothesis 必须以 open 状态写入",
                    )
                if old and not self._valid_transition(old.status, item.status):
                    return self._error("INVALID_HYPOTHESIS_TRANSITION", f"不允许 {old.status} → {item.status}")
                if item.status == "confirmed" and not item.supporting_evidence_ids:
                    return self._error("CONFIRMED_WITHOUT_EVIDENCE", "confirmed hypothesis 必须引用直接证据")
                self._replace(self.state.hypotheses, "hypothesis_id", item)
            elif operation == "upsert_coverage":
                item = InvestigationCoverage.model_validate(value)
                self._validate_refs(item.evidence_ids, "evidence_ids")
                self._validate_artifacts(item.artifact_ids)
                self._replace(self.state.coverage, "coverage_id", item)
            elif operation == "record_candidate":
                item = InvestigationCandidate.model_validate(value)
                self._validate_artifacts(item.artifact_ids)
                known_hypotheses = {x.hypothesis_id for x in self.state.hypotheses}
                known_coverage = {x.coverage_id for x in self.state.coverage}
                if not set(item.tests_hypothesis_ids).issubset(known_hypotheses):
                    return self._error("UNKNOWN_HYPOTHESIS", "candidate 引用了不存在的 hypothesis")
                if not set(item.fills_coverage_ids).issubset(known_coverage):
                    return self._error("UNKNOWN_COVERAGE", "candidate 引用了不存在的 coverage")
                self._replace(self.state.candidates, "candidate_id", item)
            else:
                return self._error("UNKNOWN_STATE_OPERATION", "operation 无效")
        except ValidationError as exc:
            return self._error("INVALID_STATE_MODEL", exc.errors()[0]["msg"])
        except ValueError as exc:
            return self._error("UNKNOWN_REFERENCE", str(exc))
        return json.dumps({"success": True, "data": self.state.model_dump(), "error_code": None,
                           "error_message": None, "retryable": False}, ensure_ascii=False)

    def _validate_refs(self, ids: list[str], field: str) -> None:
        invalid = set(ids) - set(self.state.known_evidence_ids)
        # Jira/comment/user source references are intentionally allowed, but all
        # evidence-like references must be backed by the observed registry.
        invalid = {x for x in invalid if x.startswith(("ev-", "evidence_", "event_"))}
        if invalid: raise ValueError(f"{field} 包含未出现的 Evidence ID: {sorted(invalid)}")

    def _validate_artifacts(self, ids: list[str]) -> None:
        invalid = set(ids) - set(self.state.known_artifact_ids)
        if invalid: raise ValueError(f"artifact_ids 包含未出现的 Artifact ID: {sorted(invalid)}")

    @staticmethod
    def _valid_transition(old: str, new: str) -> bool:
        allowed = {"open": {"open", "supported", "contradicted", "blocked"},
                   "supported": {"supported", "confirmed", "contradicted", "blocked"},
                   "contradicted": {"contradicted", "blocked"}, "confirmed": {"confirmed"},
                   "blocked": {"blocked", "open", "supported", "contradicted"}}
        return new in allowed[old]

    @staticmethod
    def _replace(items: list[Any], key: str, item: Any) -> None:
        for index, old in enumerate(items):
            if getattr(old, key) == getattr(item, key):
                items[index] = item
                return
        items.append(item)

    @staticmethod
    def _error(code: str, message: str) -> str:
        return json.dumps({"success": False, "data": None, "error_code": code,
                           "error_message": message, "retryable": False}, ensure_ascii=False)
