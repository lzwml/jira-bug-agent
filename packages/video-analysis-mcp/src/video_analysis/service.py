"""Video file security, deterministic frame extraction, and pluggable vision analysis."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

import httpx

from .config import VideoAnalyzerConfig
from .domain import VideoEvidence, VideoInfo


class VideoAnalyzerError(ValueError):
    pass


class VideoAnalyzerService:
    def __init__(self, config: VideoAnalyzerConfig):
        self.config = config
        self._videos: dict[str, tuple[Path, VideoInfo]] = {}

    def _safe_path(self, raw_path: str) -> tuple[Path, Path]:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise VideoAnalyzerError("video file does not exist")
        for root in self.config.allowed_roots:
            try:
                return path, path.relative_to(root)
            except ValueError:
                continue
        raise VideoAnalyzerError("video path is outside VIDEO_ANALYZER_ALLOWED_ROOTS")

    def _probe(self, path: Path, relative_path: Path) -> VideoInfo:
        if path.stat().st_size > self.config.max_video_bytes:
            raise VideoAnalyzerError("video exceeds VIDEO_ANALYZER_MAX_VIDEO_BYTES")
        try:
            completed = subprocess.run(
                [self.config.ffprobe_bin, "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
                check=True, capture_output=True, text=True, timeout=30,
            )
            data = json.loads(completed.stdout)
            duration_ms = round(float(data.get("format", {}).get("duration", 0)) * 1000)
            streams = data.get("streams", [])
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
            raise VideoAnalyzerError("unable to inspect video; ensure ffprobe is installed") from exc
        if duration_ms > self.config.max_duration_seconds * 1000:
            raise VideoAnalyzerError("video exceeds VIDEO_ANALYZER_MAX_DURATION_SECONDS")
        visual = next((item for item in streams if item.get("codec_type") == "video"), {})
        if not visual:
            raise VideoAnalyzerError("input has no video stream")
        digest = hashlib.sha256(f"{path}:{path.stat().st_mtime_ns}:{path.stat().st_size}".encode()).hexdigest()[:20]
        return VideoInfo(video_id=f"vid-{digest}", relative_path=relative_path.as_posix(), size_bytes=path.stat().st_size,
                         duration_ms=max(duration_ms, 0), width=int(visual.get("width") or 0), height=int(visual.get("height") or 0),
                         has_audio=any(item.get("codec_type") == "audio" for item in streams))

    def open_video(self, video_path: str) -> dict[str, Any]:
        path, relative = self._safe_path(video_path)
        info = self._probe(path, relative)
        self._videos[info.video_id] = (path, info)
        return info.model_dump()

    def inspect_video(self, video_id: str) -> dict[str, Any]:
        return self._get(video_id)[1].model_dump()

    def _get(self, video_id: str) -> tuple[Path, VideoInfo]:
        try:
            return self._videos[video_id]
        except KeyError as exc:
            raise VideoAnalyzerError("unknown video_id; call open_video first") from exc

    def extract_keyframes(self, video_id: str, interval_seconds: float, max_frames: int) -> list[VideoEvidence]:
        path, info = self._get(video_id)
        timestamps = list(range(0, max(info.duration_ms, 1), max(1, round(interval_seconds * 1000))))[:max_frames]
        target = self.config.work_root / info.video_id / "frames"
        target.mkdir(parents=True, exist_ok=True)
        frames: list[VideoEvidence] = []
        for timestamp_ms in timestamps:
            frame_path = target / f"{timestamp_ms:010d}.jpg"
            if not frame_path.is_file():
                try:
                    subprocess.run([self.config.ffmpeg_bin, "-y", "-ss", f"{timestamp_ms / 1000:.3f}", "-i", str(path), "-frames:v", "1", "-q:v", "3", str(frame_path)], check=True, capture_output=True, timeout=30)
                except (OSError, subprocess.SubprocessError) as exc:
                    raise VideoAnalyzerError("unable to extract frames; ensure ffmpeg is installed") from exc
            frames.append(VideoEvidence(evidence_id=f"{info.video_id}-t{timestamp_ms}", video_id=info.video_id,
                relative_path=info.relative_path, timestamp_ms=timestamp_ms,
                frame_path=str(frame_path.relative_to(self.config.work_root)).replace("\\", "/")))
        return frames

    def analyze_video(self, video_id: str, objective: str, interval_seconds: float, max_frames: int) -> dict[str, Any]:
        frames = self.extract_keyframes(video_id, interval_seconds, max_frames)
        if not self.config.provider_url or not self.config.provider_model:
            raise VideoAnalyzerError("vision provider is not configured")
        content: list[dict[str, Any]] = [{"type": "text", "text": (
            "Analyze these ordered screen-recording frames for a software bug. "
            "Return strict JSON with summary, events [{start_ms,end_ms,description,confidence}], and missing_evidence. "
            f"Objective: {objective}") }]
        for frame in frames:
            image = self.config.work_root / (frame.frame_path or "")
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
        headers = {"Content-Type": "application/json"}
        if self.config.provider_api_key:
            headers["Authorization"] = f"Bearer {self.config.provider_api_key}"
        response = httpx.post(f"{self.config.provider_url}/chat/completions", headers=headers,
            json={"model": self.config.provider_model, "messages": [{"role": "user", "content": content}], "temperature": 0}, timeout=120)
        response.raise_for_status()
        raw = response.json()["choices"][0]["message"]["content"]
        try:
            analysis = json.loads(raw.removeprefix("```json").removesuffix("```").strip())
        except (AttributeError, json.JSONDecodeError) as exc:
            raise VideoAnalyzerError("vision provider returned non-JSON analysis") from exc
        analysis["frames"] = [frame.model_dump() for frame in frames]
        analysis["video"] = self.inspect_video(video_id)
        return analysis

    def get_clip(self, video_id: str, start_ms: int, end_ms: int) -> dict[str, Any]:
        path, info = self._get(video_id)
        if end_ms > info.duration_ms:
            raise VideoAnalyzerError("clip end exceeds video duration")
        target = self.config.work_root / info.video_id / "clips" / f"{start_ms}-{end_ms}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([self.config.ffmpeg_bin, "-y", "-ss", f"{start_ms / 1000:.3f}", "-to", f"{end_ms / 1000:.3f}", "-i", str(path), "-c", "copy", str(target)], check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            raise VideoAnalyzerError("unable to create clip; ensure ffmpeg is installed") from exc
        return {"video_id": video_id, "relative_path": info.relative_path, "start_ms": start_ms, "end_ms": end_ms,
                "clip_path": str(target.relative_to(self.config.work_root)).replace("\\", "/")}
