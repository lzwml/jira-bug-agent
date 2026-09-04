"""
测试能否直接把 .mp4 视频传给 vision 模型，绕过 ffmpeg 抽帧。
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(repo_root / "packages/jira-bug-mcp/src"))
from jira_bug_mcp.config import load_local_env
load_local_env(repo_root / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from video_analysis.config import VideoAnalyzerConfig

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

config = VideoAnalyzerConfig.from_environment()
url = f"{config.provider_url}/chat/completions"
headers = {"Content-Type": "application/json"}
if config.provider_api_key:
    headers["Authorization"] = f"Bearer {config.provider_api_key}"

video_path = repo_root / "exports/BAIC-42133/attachments/566738_20260826-1354.mp4"
print(f"视频: {video_path.name} ({video_path.stat().st_size / 1024:.0f} KB)")

# 方案 A: 直接 base64 视频 data URL
video_b64 = base64.b64encode(video_path.read_bytes()).decode("ascii")
print(f"Base64 长度: {len(video_b64)} 字符")

print("\n" + "=" * 60)
print("[Test A] 直接传 video/mp4 base64 data URL")
print("=" * 60)

try:
    resp = httpx.post(url, headers=headers, json={
        "model": config.provider_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这个视频的内容，用一句话概括"},
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{video_b64}"}},
            ],
        }],
        "max_tokens": 200,
    }, timeout=60)
    print(f"HTTP Status: {resp.status_code}")
    if resp.status_code == 200:
        body = resp.json()
        reply = body["choices"][0]["message"]["content"]
        print(f"[OK] 模型回复: '{reply}'")
    else:
        print(f"[FAIL] {resp.text[:300]}")
except Exception as e:
    print(f"[FAIL] {e}")

print("\n" + "=" * 60)
print("[Test B] 用 image_url 类型传 video data")
print("=" * 60)

try:
    resp = httpx.post(url, headers=headers, json={
        "model": config.provider_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "请描述这个视频的内容，用一句话概括"},
                {"type": "image_url", "image_url": {"url": f"data:video/mp4;base64,{video_b64}"}},
            ],
        }],
        "max_tokens": 200,
    }, timeout=60)
    print(f"HTTP Status: {resp.status_code}")
    if resp.status_code == 200:
        body = resp.json()
        reply = body["choices"][0]["message"]["content"]
        print(f"[OK] 模型回复: '{reply}'")
    else:
        print(f"[FAIL] {resp.text[:300]}")
except Exception as e:
    print(f"[FAIL] {e}")

print("\n完成")