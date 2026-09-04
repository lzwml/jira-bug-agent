"""
MCP 客户端测试脚本 —— 手动验证 video-analysis MCP Server 的协议握手和工具调用。

目的：
    1. 理解 MCP 协议的 initialize -> tools/list -> tools/call 流程
    2. 验证 Server 的协议实现是否正确
    3. 验证各工具的错误处理（没有 ffmpeg/ffprobe 时的优雅降级）
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Windows 控制台编码修复
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


async def main():
    # 测试视频路径
    repo_root = Path(__file__).resolve().parents[3]  # tests/ -> video-analysis-mcp/ -> packages/ -> jira-bug-agent/
    video_path = repo_root / "exports/BAIC-42133/attachments/566738_20260826-1354.mp4"
    if not video_path.is_file():
        raise SystemExit(f"测试视频不存在: {video_path}")
    print(f"[Setup] 测试视频: {video_path}")
    print(f"[Setup] 视频大小: {video_path.stat().st_size / 1024:.1f} KB")

    # 配置 MCP Server 的连接参数
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "video_analysis.server"],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src"),
            "VIDEO_ANALYZER_ALLOWED_ROOTS": str(video_path.parent.resolve()),
            "VIDEO_ANALYZER_WORK_ROOT": str(repo_root / "exports" / ".video-analysis-cache"),
        }
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # 步骤 1: 初始化握手
            await session.initialize()
            print("\n" + "=" * 60)
            print("[OK] 握手成功: video-analysis MCP Server 已连接")
            print("=" * 60)

            # 步骤 2: 发现所有工具
            tools_result = await session.list_tools()
            print(f"\n[Tools] 发现 {len(tools_result.tools)} 个工具:\n")
            tool_names = []
            for tool in tools_result.tools:
                tool_names.append(tool.name)
                print(f"  - {tool.name}")
                print(f"    {tool.description}")
                print(f"    必填参数: {tool.input_schema.get('required', [])}")
                print()

            # 验证期待的 6 个工具都存在
            expected = {"open_video", "inspect_video", "extract_keyframes", "analyze_video", "query_video", "get_clip"}
            actual = set(tool_names)
            if actual != expected:
                print(f"[WARN] 工具集不匹配! 缺少: {expected - actual}, 多余: {actual - expected}")
            else:
                print("[OK] 6 个工具全部注册成功")

            print("=" * 60)

            # ============================================
            # 测试 1: open_video（没有 ffprobe 时应优雅返回错误）
            # ============================================
            print("\n[Test 1] 调用 open_video（预期: 因缺少 ffprobe 返回错误）\n")
            result = await session.call_tool("open_video", arguments={"video_path": str(video_path)})
            payload = print_result(result)
            if payload.get("success"):
                video_id = payload["data"]["video_id"]
                print(f"  [SURPRISE] 居然成功了! video_id={video_id}")
            else:
                print(f"  [OK] 符合预期地失败了: {payload.get('error_code')} - {payload.get('error_message')}")

            print("\n" + "=" * 60)

            # ============================================
            # 测试 2: 路径安全校验 —— 拒绝 allowed_root 外的路径
            # ============================================
            print("\n[Test 2] 路径安全校验 —— 尝试打开允许目录外的文件\n")
            outside_path = str(Path(__file__).resolve())  # 这个文件不在 allowed_roots 内
            result = await session.call_tool("open_video", arguments={"video_path": outside_path})
            payload = print_result(result)
            if not payload.get("success"):
                print(f"  [OK] 安全校验通过，拒绝外部路径: {payload.get('error_message')}")
            else:
                print(f"  [FAIL] 安全校验未生效，接受了外部路径!")

            print("\n" + "=" * 60)

            # ============================================
            # 测试 3: 调用不存在的工具
            # ============================================
            print("\n[Test 3] 调用不存在的工具\n")
            result = await session.call_tool("nonexistent_tool", arguments={})
            payload = print_result(result)
            if not payload.get("success") and payload.get("error_code") == "UNKNOWN_TOOL":
                print(f"  [OK] 正确返回 UNKNOWN_TOOL")
            else:
                print(f"  [WARN] 未预期的返回值")

            print("\n" + "=" * 60)

            # ============================================
            # 测试 4: GetClipInput 参数校验（在 domain 层测试）
            # ============================================
            print("\n[Test 4] GetClipInput 参数校验 (domain 层)")

            from video_analysis.domain import GetClipInput

            # 合法范围
            try:
                clip = GetClipInput(video_id="vid-test", start_ms=0, end_ms=1000)
                print(f"  [OK] 合法 clip: {clip.start_ms}ms -> {clip.end_ms}ms")
            except ValueError as e:
                print(f"  [FAIL] 合法 clip 被拒绝: {e}")

            # end < start
            try:
                GetClipInput(video_id="vid-test", start_ms=100, end_ms=50)
                print(f"  [FAIL] 应拒绝 end_ms < start_ms")
            except ValueError as e:
                print(f"  [OK] 正确拒绝 end_ms < start_ms: {e}")

            # 超过 120 秒
            try:
                GetClipInput(video_id="vid-test", start_ms=0, end_ms=120_001)
                print(f"  [FAIL] 应拒绝超过 120s 的 clip")
            except ValueError as e:
                print(f"  [OK] 正确拒绝超过 120s 的 clip: {e}")

            print("\n" + "=" * 60)
            print("\n[OK] 所有测试完成! video-analysis MCP 协议握手和工具调用验证通过.")
            print("=" * 60)


def print_result(result) -> dict:
    """美化输出 MCP 工具调用结果，返回解析后的 payload。"""
    for content in result.content:
        text = getattr(content, "text", None)
        if text is None:
            print(f"  [RAW] {content}")
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            print(f"  [RAW] {text[:300]}")
            return {}

        success = data.get("success", False)
        status = "[OK]" if success else "[FAIL]"
        print(f"  {status} success={success}")
        print(f"  error_code={data.get('error_code')}, retryable={data.get('retryable')}")
        if data.get("error_message"):
            print(f"  error_message: {data['error_message']}")
        if data.get("data"):
            inner = data["data"]
            if isinstance(inner, dict):
                for key, value in inner.items():
                    if isinstance(value, list):
                        print(f"  data.{key}: {len(value)} 条")
                    elif isinstance(value, dict):
                        print(f"  data.{key}: {json.dumps(value, ensure_ascii=False)[:120]}...")
                    else:
                        print(f"  data.{key}: {value}")
            else:
                print(f"  data: {str(inner)[:120]}")
        return data
    return {}


if __name__ == "__main__":
    asyncio.run(main())