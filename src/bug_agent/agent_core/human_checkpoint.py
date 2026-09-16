"""Agent Loop 识别人工检查点所需的纯协议与解析逻辑。"""

from __future__ import annotations

import json

from ..domain.models import HumanCheckpoint


REQUEST_HUMAN_GUIDANCE_TOOL = "request_human_guidance"


def checkpoint_from_result(raw: str) -> HumanCheckpoint | None:
    try:
        payload = json.loads(raw)
        value = payload.get("data", {}).get("human_checkpoint")
        return HumanCheckpoint.model_validate(value) if value else None
    except (json.JSONDecodeError, AttributeError, ValueError):
        return None
