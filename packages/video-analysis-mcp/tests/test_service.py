from __future__ import annotations

import pytest

from video_analysis.config import VideoAnalyzerConfig
from video_analysis.domain import GetClipInput, VideoEvidence, VideoInfo
from video_analysis.service import VideoAnalyzerError, VideoAnalyzerService


def test_open_video_keeps_only_relative_path_and_rejects_outside_root(tmp_path, monkeypatch):
    allowed = tmp_path / "case"
    allowed.mkdir()
    video = allowed / "screen.mp4"
    video.write_bytes(b"placeholder")
    service = VideoAnalyzerService(VideoAnalyzerConfig((allowed,), tmp_path / "work"))
    info = VideoInfo(video_id="vid-test", relative_path="screen.mp4", size_bytes=11,
                     duration_ms=1_000, width=1080, height=1920, has_audio=True)
    monkeypatch.setattr(service, "_probe", lambda path, relative: info)

    result = service.open_video(str(video))

    assert result["relative_path"] == "screen.mp4"
    assert str(allowed) not in str(result)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"placeholder")
    with pytest.raises(VideoAnalyzerError, match="outside"):
        service.open_video(str(outside))


def test_visual_analysis_requires_explicit_provider_configuration(tmp_path, monkeypatch):
    video = tmp_path / "screen.mp4"
    video.write_bytes(b"placeholder")
    service = VideoAnalyzerService(VideoAnalyzerConfig((tmp_path,), tmp_path / "work"))
    info = VideoInfo(video_id="vid-test", relative_path="screen.mp4", size_bytes=11,
                     duration_ms=1_000, width=1080, height=1920, has_audio=False)
    service._videos[info.video_id] = (video, info)
    monkeypatch.setattr(service, "extract_keyframes", lambda *args: [VideoEvidence(
        evidence_id="vid-test-t0", video_id="vid-test", relative_path="screen.mp4", timestamp_ms=0,
    )])

    with pytest.raises(VideoAnalyzerError, match="not configured"):
        service.analyze_video("vid-test", "定位登录失败", 1, 1)


def test_clip_contract_rejects_invalid_or_unbounded_ranges():
    with pytest.raises(ValueError, match="later"):
        GetClipInput(video_id="vid", start_ms=100, end_ms=100)
    with pytest.raises(ValueError, match="120"):
        GetClipInput(video_id="vid", start_ms=0, end_ms=120_001)
