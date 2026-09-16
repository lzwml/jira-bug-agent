"""框架无关的 Agent Loop、端口契约与提示词。"""

from .agent import BugAnalysisAgent
from .contracts import AgentRuntimeConfig, ModelProvider, ProviderFailure, ToolRouter

__all__ = [
    "AgentRuntimeConfig",
    "BugAnalysisAgent",
    "ModelProvider",
    "ProviderFailure",
    "ToolRouter",
]
