"""常见 Android/Kernel 时间戳解析，不猜测不同 Clock Domain 的映射。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Literal


@dataclass(frozen=True)
class ParsedTimestamp:
    raw: str
    clock_domain: Literal["wall", "android", "kernel_monotonic"]
    normalized: str | None = None
    relative_seconds: float | None = None


def extract_timestamp(line: str, year_hint: int | None = None) -> ParsedTimestamp | None:
    """识别常见时间戳；Kernel monotonic 不会被伪造为墙上时间。"""

    full = re.search(r"\b(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)", line)
    if full:
        raw = full.group(0)
        try:
            normalized = datetime.fromisoformat(raw.replace(" ", "T")).isoformat()
        except ValueError:
            normalized = None
        return ParsedTimestamp(raw=raw, clock_domain="wall", normalized=normalized)

    logcat = re.search(r"(?<!\d)(\d{2})-(\d{2})\s+(\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)", line)
    if logcat:
        raw = logcat.group(0)
        # 没有可信年份时只保留原始 Android 时间，避免历史 Case 被静默补成当前年。
        normalized = None
        if year_hint is not None:
            try:
                normalized = datetime.fromisoformat(
                    f"{year_hint}-{logcat.group(1)}-{logcat.group(2)}T{logcat.group(3)}"
                ).isoformat()
            except ValueError:
                normalized = None
        return ParsedTimestamp(raw=raw, clock_domain="android", normalized=normalized)

    kernel = re.search(r"^\s*\[\s*(\d+(?:\.\d+)?)\]", line)
    if kernel:
        return ParsedTimestamp(
            raw=kernel.group(0).strip(),
            clock_domain="kernel_monotonic",
            relative_seconds=float(kernel.group(1)),
        )
    return None


def infer_component(line: str, anchors: list[str]) -> str | None:
    """优先返回命中的调查锚点，否则尝试提取 Android Log Tag。"""

    lowered = line.casefold()
    for anchor in anchors:
        if anchor.casefold() in lowered:
            return anchor
    tag = re.search(r"\s[VDIWEF]\s+([A-Za-z0-9_.:/-]+)\s*:", line)
    return tag.group(1) if tag else None
