"""Stable domain and tool-input contracts for video evidence."""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class OpenVideoInput(BaseModel):
    video_path: str = Field(min_length=1, description="Path to a video inside an allowed Bug Case root")


class InspectVideoInput(BaseModel):
    video_id: str = Field(min_length=1)


class ExtractKeyframesInput(BaseModel):
    video_id: str = Field(min_length=1)
    interval_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    max_frames: int = Field(default=24, ge=1, le=120)


class AnalyzeVideoInput(BaseModel):
    video_id: str = Field(min_length=1)
    objective: str = Field(min_length=1, max_length=2000)
    interval_seconds: float = Field(default=2.0, ge=0.2, le=60.0)
    max_frames: int = Field(default=24, ge=1, le=120)


class QueryVideoInput(AnalyzeVideoInput):
    """Alias with an explicit question for timeline-grounded follow-up analysis."""


class GetClipInput(BaseModel):
    video_id: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> "GetClipInput":
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be later than start_ms")
        if self.end_ms - self.start_ms > 120_000:
            raise ValueError("clip duration must not exceed 120 seconds")
        return self


class VideoInfo(BaseModel):
    video_id: str
    relative_path: str
    size_bytes: int = Field(ge=0)
    duration_ms: int = Field(ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    has_audio: bool


class VideoEvidence(BaseModel):
    evidence_id: str
    video_id: str
    relative_path: str
    timestamp_ms: int = Field(ge=0)
    frame_path: str | None = None
    description: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
