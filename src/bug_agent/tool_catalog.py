"""工具契约快照与旧会话的当前版本后补目录。"""

from __future__ import annotations

from typing import Any

from log_analyzer.domain import (
    BuildIndexInput,
    ExtractAeeDbInput,
    ExtractArchiveMembersInput,
    ExtractTimelineInput,
    GetCaseCommentInput,
    InspectArchiveInput,
    InspectCaseInput,
    OpenCaseInput,
    ParseDiagnosticsInput,
    PrepareCaseInput,
    ProbeArchiveMembersInput,
    SearchEvidenceInput,
)


_LOG_TOOLS = {
    "open_case": ("注册 Case 目录并返回 case_id 与附件清单；后续调查使用 ID，不再传裸路径。", OpenCaseInput),
    "inspect_case": ("查看 Case 的附件类型、大小和样本，决定下一步调查哪些材料。", InspectCaseInput),
    "prepare_case": ("全量展开归档并建立索引的最终兜底；不应作为默认首选。", PrepareCaseInput),
    "inspect_archive": ("只读查看归档成员、时间和日志域，不解压内容。", InspectArchiveInput),
    "probe_archive_members": ("读取指定归档成员的少量样本，判断时间窗、boot 和诊断锚点。", ProbeArchiveMembersInput),
    "extract_archive_members": ("按稳定 member_id 选择性解压所需归档成员。", ExtractArchiveMembersInput),
    "extract_aee_db": ("解码 AEE DB，并注册可继续索引的文本产物。", ExtractAeeDbInput),
    "build_index": ("把指定附件增量加入 Case 文本索引，供后续检索。", BuildIndexInput),
    "search_evidence": ("在已注册 Case 文本中做字面量搜索，返回可引用 Evidence。", SearchEvidenceInput),
    "extract_timeline": ("从多种时钟域日志中提取锚点事件并形成时间线。", ExtractTimelineInput),
    "parse_diagnostics": ("提取 AVC、Kernel Call Trace、Fatal 和 ANR 等结构化诊断。", ParseDiagnosticsInput),
    "get_case_comment": ("按 comment_id 分页读取已验证 Jira 评论原文。", GetCaseCommentInput),
}


def normalize_tool_catalog(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把实际发送给模型的 OpenAI Tool Schema 固化为稳定目录。"""

    catalog: list[dict[str, Any]] = []
    for item in tools:
        function = item.get("function", {})
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        catalog.append({
            "name": name,
            "description": str(function.get("description") or ""),
            "parameters": function.get("parameters") or {"type": "object"},
        })
    return catalog


def current_fallback_catalog(tool_names: set[str]) -> list[dict[str, Any]]:
    """为未保存 Tool Schema 的旧会话提供当前版本契约，并明确标记为后补。"""

    catalog: list[dict[str, Any]] = []
    for name in sorted(tool_names):
        if name in _LOG_TOOLS:
            description, model = _LOG_TOOLS[name]
            catalog.append({
                "name": name,
                "description": description,
                "parameters": model.model_json_schema(),
            })
        elif name == "activate_skill":
            catalog.append({
                "name": name,
                "description": "根据已观察到的症状或证据激活一份可信专项分析方法。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "要激活的 Skill 名称。"},
                        "reason": {"type": "string", "description": "支持激活的已观察症状或证据。"},
                        "role": {"type": "string", "description": "primary 或 secondary 症状路线。"},
                    },
                    "required": ["name", "reason"],
                    "additionalProperties": False,
                },
            })
        else:
            catalog.append({
                "name": name,
                "description": "旧会话未保存该工具的描述，当前版本也无法补齐。",
                "parameters": {"type": "object", "properties": {}},
            })
    return catalog
