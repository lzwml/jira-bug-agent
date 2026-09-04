"""Server-only settings. Model calls cannot expand file or frame budgets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VideoAnalyzerConfig:
    allowed_roots: tuple[Path, ...]
    work_root: Path
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    provider_url: str = ""
    provider_api_key: str = ""
    provider_model: str = ""
    max_video_bytes: int = 2 * 1024 * 1024 * 1024
    max_duration_seconds: int = 1800

    @classmethod
    def from_environment(cls) -> "VideoAnalyzerConfig":
        raw_roots = os.getenv("VIDEO_ANALYZER_ALLOWED_ROOTS", os.getcwd())
        roots = tuple(Path(item).expanduser().resolve() for item in raw_roots.split(os.pathsep) if item.strip())
        if not roots:
            raise ValueError("VIDEO_ANALYZER_ALLOWED_ROOTS must contain at least one path")
        work_root = Path(os.getenv("VIDEO_ANALYZER_WORK_ROOT", ".video-analysis")).expanduser().resolve()
        max_video_bytes = int(os.getenv("VIDEO_ANALYZER_MAX_VIDEO_BYTES", str(2 * 1024 * 1024 * 1024)))
        max_duration_seconds = int(os.getenv("VIDEO_ANALYZER_MAX_DURATION_SECONDS", "1800"))
        if max_video_bytes < 1:
            raise ValueError("VIDEO_ANALYZER_MAX_VIDEO_BYTES must be positive")
        if not 1 <= max_duration_seconds <= 14_400:
            raise ValueError("VIDEO_ANALYZER_MAX_DURATION_SECONDS must be in 1..14400")
        return cls(
            allowed_roots=roots,
            work_root=work_root,
            ffmpeg_bin=os.getenv("VIDEO_ANALYZER_FFMPEG_BIN", "ffmpeg"),
            ffprobe_bin=os.getenv("VIDEO_ANALYZER_FFPROBE_BIN", "ffprobe"),
            provider_url=os.getenv("VIDEO_ANALYZER_PROVIDER_URL", "").strip().rstrip("/"),
            provider_api_key=os.getenv("VIDEO_ANALYZER_PROVIDER_API_KEY", "").strip(),
            provider_model=os.getenv("VIDEO_ANALYZER_PROVIDER_MODEL", "").strip(),
            max_video_bytes=max_video_bytes,
            max_duration_seconds=max_duration_seconds,
        )
