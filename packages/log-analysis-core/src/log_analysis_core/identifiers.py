"""为跨协议层引用生成确定性的短 ID。"""

from __future__ import annotations

import hashlib


def stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    return f"{prefix}_{digest[:16]}"

