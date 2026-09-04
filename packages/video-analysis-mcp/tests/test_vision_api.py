"""
直接测试 vision 模型 API，不依赖 ffmpeg。
用一小段 base64 编码的测试图片发到 Chat Completions 接口。
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

# 加载 .env（需要从 jira-bug-agent 根目录加载）
repo_root = Path(__file__).resolve().parents[3]  # tests/ -> video-analysis-mcp/ -> packages/ -> jira-bug-agent/
sys.path.insert(0, str(repo_root / "packages/jira-bug-mcp/src"))
from jira_bug_mcp.config import load_local_env
load_local_env(repo_root / ".env")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from video_analysis.config import VideoAnalyzerConfig

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

# 用 Pillow 生成一张 100x100 的纯色图片，然后 base64 编码
from PIL import Image
import io

def make_test_image_b64() -> str:
    """生成一张 100x100 红蓝渐变的测试图片，base64 编码"""
    img = Image.new("RGB", (100, 100), color=(66, 133, 244))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii")

TEST_IMAGE_B64 = make_test_image_b64()


def main():
    config = VideoAnalyzerConfig.from_environment()

    print("=" * 60)
    print("Vision Model API 连通性测试")
    print("=" * 60)
    print(f"  Provider URL: {config.provider_url}")
    print(f"  Model:        {config.provider_model}")
    print(f"  API Key:      {'***' + config.provider_api_key[-8:] if config.provider_api_key else '(未设置)'}")

    if not config.provider_url or not config.provider_model:
        print("\n[FAIL] vision provider 未配置，请检查 .env 中的 VIDEO_ANALYZER_PROVIDER_*")
        return

    url = f"{config.provider_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if config.provider_api_key:
        headers["Authorization"] = f"Bearer {config.provider_api_key}"

    # 测试 1: 纯文本调用（验证基本连通性）
    print("\n" + "-" * 40)
    print("[Test 1] 纯文本 Chat Completions 连通性")
    print("-" * 40)
    try:
        resp = httpx.post(
            url, headers=headers,
            json={
                "model": config.provider_model,
                "messages": [{"role": "user", "content": "请用一句话介绍你自己"}],
                "max_tokens": 100,
            },
            timeout=30,
        )
        print(f"  HTTP Status: {resp.status_code}")
        if resp.status_code == 200:
            body = resp.json()
            reply = body["choices"][0]["message"]["content"]
            print(f"  [OK] 模型回复: {reply}")
            print(f"  model: {body.get('model', 'N/A')}")
            usage = body.get("usage", {})
            if usage:
                print(f"  tokens: prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}")
        else:
            print(f"  [FAIL] 响应: {resp.text[:500]}")
    except Exception as e:
        print(f"  [FAIL] 请求异常: {e}")

    # 测试 2: Vision 多模态调用（图片理解）
    print("\n" + "-" * 40)
    print("[Test 2] Vision 多模态调用（图片理解）")
    print("-" * 40)
    try:
        resp = httpx.post(
            url, headers=headers,
            json={
                "model": config.provider_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "这张图片的尺寸是多少？请不要输出其他内容，只回答尺寸。"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{TEST_IMAGE_B64}"}},
                    ],
                }],
                "max_tokens": 100,
            },
            timeout=30,
        )
        print(f"  HTTP Status: {resp.status_code}")
        if resp.status_code == 200:
            body = resp.json()
            print(f"  [DEBUG] 原始响应: {json.dumps(body, indent=2, ensure_ascii=False)[:800]}")
            reply = body["choices"][0]["message"]["content"]
            print(f"  [OK] 模型回复: '{reply}'")
            print(f"  model: {body.get('model', 'N/A')}")
            finish_reason = body["choices"][0].get("finish_reason", "N/A")
            print(f"  finish_reason: {finish_reason}")
        else:
            print(f"  [FAIL] 响应: {resp.text[:500]}")
    except Exception as e:
        print(f"  [FAIL] 请求异常: {e}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()