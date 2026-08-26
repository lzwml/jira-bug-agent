"""
MCP 客户端测试脚本 —— 手动验证 log-analyzer MCP Server 的协议握手和工具调用。

目的：
    1. 理解 MCP 协议的 initialize -> tools/list -> tools/call 流程
    2. 验证 Server 的协议实现是否正确
    3. 这是"手写 Client"以建立 MCP 心智模型的练习
"""

import asyncio
import json
import os
from pathlib import Path
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Windows 控制台编码修复
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


async def main():
    if len(sys.argv) < 2:
        raise SystemExit("用法: python test_mcp_client.py <Bug案例目录>")
    case_path = Path(sys.argv[1]).resolve()
    if not case_path.is_dir():
        raise SystemExit(f"案例目录不存在: {case_path}")

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "log_analyzer.server"],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).resolve().parent / "src"),
            "LOG_ANALYZER_ALLOWED_ROOTS": str(case_path),
        }
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # 步骤 1: 初始化握手
            await session.initialize()
            print("=" * 60)
            print("[OK] 握手成功: MCP Server 已连接")
            print("=" * 60)

            # 步骤 2: 发现所有工具
            tools_result = await session.list_tools()
            print(f"\n[Tools] 发现 {len(tools_result.tools)} 个工具:\n")
            for tool in tools_result.tools:
                print(f"  - {tool.name}")
                print(f"    {tool.description}")
                print(f"    必填参数: {tool.input_schema.get('required', [])}")
                print()

            print("=" * 60)

            # 步骤 3: 注册 Case
            print("\n[Test 1] 调用 open_case\n")
            result = await session.call_tool("open_case", arguments={"case_path": str(case_path)})
            print_result(result)
            payload = parse_result(result)
            if not payload.get("success"):
                raise RuntimeError("open_case 失败")
            case_id = payload["data"]["case"]["case_id"]

            print("\n" + "=" * 60)

            # 步骤 4: 查看 Case
            print("\n[Test 2] 调用 inspect_case\n")
            result = await session.call_tool("inspect_case", arguments={"case_id": case_id})
            print_result(result)

            print("\n" + "=" * 60)
            print("\n[Test 3] 验证搜索零匹配仍返回成功\n")
            result = await session.call_tool(
                "search_evidence",
                arguments={"case_id": case_id, "query": "__MCP_V2_NO_MATCH__"},
            )
            print_result(result)
            payload = parse_result(result)
            if not payload.get("success") or payload["data"]["match_count"] != 0:
                raise RuntimeError("search_evidence 零匹配语义错误")

            print("\n" + "=" * 60)
            print("\n[OK] 所有测试完成! MCP 协议握手和工具调用验证通过.")
            print("=" * 60)


def parse_result(result) -> dict:
    """读取第一个文本结果。"""
    for content in result.content:
        text = getattr(content, "text", None)
        if text:
            return json.loads(text)
    return {}


def print_result(result):
    """美化输出 MCP 工具调用结果。"""
    for content in result.content:
        text = getattr(content, "text", None)
        if text is None:
            print(f"  [RAW] {content}")
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            print(f"  [RAW] {text[:300]}")
            continue

        success = data.get("success", False)
        if success:
            print("  [OK] 成功")
            inner = data.get("data", {})
            for key, value in inner.items():
                if key == "report":
                    print(f"     report: {json.dumps(value, ensure_ascii=False)[:200]}...")
                elif isinstance(value, list):
                    print(f"     {key}: {len(value)} 条")
                else:
                    print(f"     {key}: {value}")
        else:
            print(f"  [FAIL] {data.get('error_code')}: {data.get('error_message')}")
            print(f"     retryable: {data.get('retryable')}")


if __name__ == "__main__":
    asyncio.run(main())
