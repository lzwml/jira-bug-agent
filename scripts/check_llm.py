"""LLM Provider 连通性检查脚本。

用途：在接入企业网关或更换模型服务后，快速验证配置是否正确、
网关是否支持 Agent 所需的 tool_calls 能力，而不需要运行完整的
Bug 分析流程。

使用方法：
    uv run python scripts/check_llm.py

检查分三步，逐步深入：
1. 配置加载：验证 .env 中的 BUG_AGENT_LLM_* 变量能被正确读取；
2. 基本对话：发送一条简单消息，验证网关能正常返回；
3. 工具调用：发送带 tools 的请求，验证网关支持 tool_calls（Agent 必需）。

任何一步失败都会给出明确的错误提示和排查建议。
"""

from __future__ import annotations

import asyncio
import sys

import httpx


def print_step(num: int, title: str) -> None:
    print(f"\n[{num}/3] {title}")
    print("-" * 50)


async def check_config() -> "object | None":
    """步骤 1：验证配置能从 .env 正确加载。"""
    print_step(1, "检查配置加载")
    try:
        from bug_agent.config import AgentConfig
        config = AgentConfig.from_environment()
        print(f"  base_url : {config.llm_base_url}")
        print(f"  model    : {config.llm_model}")
        masked = config.llm_api_key
        masked = ("*" * max(0, len(masked) - 4)) + masked[-4:] if len(masked) >= 4 else "(too short!)"
        print(f"  api_key  : {masked}")
        print(f"  timeout  : {config.llm_timeout_seconds}s")
        if "your-llm-gateway" in config.llm_base_url or "replace-with" in config.llm_api_key:
            print("\n  [警告] 检测到 .env 仍是占位符，请先填入真实网关地址和密钥！")
            return None
        return config
    except ValueError as e:
        print(f"  [失败] {e}")
        print("\n  排查建议：检查 .env 文件是否存在，BUG_AGENT_LLM_BASE_URL /")
        print("  BUG_AGENT_LLM_API_KEY / BUG_AGENT_LLM_MODEL 三个变量是否都已填写。")
        return None


async def check_basic_chat(config) -> bool:
    """步骤 2：验证基本对话补全能正常工作。"""
    print_step(2, "检查基本对话（POST /chat/completions）")
    url = f"{config.llm_base_url}/chat/completions"
    print(f"  请求 -> {url}")
    try:
        async with httpx.AsyncClient(
            timeout=config.llm_timeout_seconds,
            headers={"Authorization": f"Bearer {config.llm_api_key}"},
        ) as client:
            response = await client.post(url, json={
                "model": config.llm_model,
                "messages": [{"role": "user", "content": "你好，请回复 OK"}],
                "temperature": 0.1,
            })
    except httpx.TransportError as e:
        print(f"  [失败] 网络连接错误: {type(e).__name__}: {e}")
        print("\n  排查建议：")
        print("  - 确认 base_url 是否正确（是否能 ping 通网关主机）")
        print("  - 确认是否需要代理（当前 Provider 不读取 HTTP_PROXY）")
        print("  - 确认网关是否要求内网/VPN 连接")
        return False

    print(f"  HTTP {response.status_code}")
    if response.status_code in {401, 403}:
        print("  [失败] 认证失败：密钥无效或无权限")
        print("\n  排查建议：确认 BUG_AGENT_LLM_API_KEY 是否正确，网关是否接受 Bearer 认证。")
        return False
    if not response.is_success:
        print(f"  [失败] 网关拒绝请求: {response.text[:300]}")
        return False

    try:
        payload = response.json()
        message = payload["choices"][0]["message"]
        content = message.get("content") or ""
        print(f"  模型回复: {content[:100]!r}")
        usage = payload.get("usage")
        if usage:
            print(f"  token 用量: prompt={usage.get('prompt_tokens')}, "
                  f"completion={usage.get('completion_tokens')}")
        print("  [OK] 基本对话正常")
        return True
    except (ValueError, KeyError, IndexError) as e:
        print(f"  [失败] 响应格式不符合 OpenAI 规范: {e}")
        print(f"  原始响应: {response.text[:300]}")
        print("\n  排查建议：网关可能不是 OpenAI-compatible，需要检查")
        print("  响应是否包含 choices[0].message 结构。")
        return False


async def check_tool_calls(config) -> bool:
    """步骤 3：验证网关支持 tool_calls（Agent 工具调用的前提）。"""
    print_step(3, "检查工具调用（tool_calls 支持）")
    url = f"{config.llm_base_url}/chat/completions"

    # 定义一个极简工具，看模型是否会调用它
    tools = [{
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前时间",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }]

    try:
        async with httpx.AsyncClient(
            timeout=config.llm_timeout_seconds,
            headers={"Authorization": f"Bearer {config.llm_api_key}"},
        ) as client:
            response = await client.post(url, json={
                "model": config.llm_model,
                "messages": [{"role": "user", "content": "现在几点了？请使用工具查询。"}],
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.1,
            })
    except httpx.TransportError as e:
        print(f"  [失败] 网络错误: {type(e).__name__}")
        return False

    if not response.is_success:
        print(f"  [失败] HTTP {response.status_code}: {response.text[:300]}")
        print("\n  排查建议：网关可能不支持 tools 参数。")
        print("  Agent 的工具调用依赖此能力，若不支持则无法使用。")
        return False

    try:
        payload = response.json()
        message = payload["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            call = tool_calls[0]
            func = call.get("function", {})
            print(f"  模型请求调用工具: {func.get('name')}")
            print(f"  工具参数: {func.get('arguments')}")
            print("  [OK] 网关支持 tool_calls，Agent 可以正常工作")
            return True
        else:
            content = message.get("content") or ""
            print(f"  模型未调用工具，直接回复: {content[:100]!r}")
            print("  [警告] 模型没有发起工具调用。可能原因：")
            print("  - 模型能力较弱，不理解何时该调用工具")
            print("  - 网关忽略了 tools 参数")
            print("  建议：换一个支持 function calling 的模型重试。")
            return False
    except (ValueError, KeyError, IndexError) as e:
        print(f"  [失败] 响应解析失败: {e}")
        return False


async def main() -> int:
    print("=" * 50)
    print("  LLM Provider 连通性检查")
    print("=" * 50)

    config = await check_config()
    if config is None:
        return 1

    if not await check_basic_chat(config):
        return 1

    if not await check_tool_calls(config):
        return 1

    print("\n" + "=" * 50)
    print("  全部通过！可以运行 Agent 了：")
    print("  uv run bug-agent analyze-local <case_path>")
    print("=" * 50)
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
