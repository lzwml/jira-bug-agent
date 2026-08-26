# Jira / Local Android Bug Agent

一个面向 Android 系统工程师的证据驱动 Bug 分析 Agent。它既能从 Jira Issue
开始，也能直接分析本地 Bug Case；Agent 通过 MCP 组合 Jira 上下文和日志证据，
最终输出区分事实、假设和缺失证据的 RCA 报告。

> 当前状态：`0.1.0` Alpha，适合学习 Agent Harness、MCP 与工具解耦；使用真实
> 企业 Bug 前请先完成权限、脱敏和安全评审。

## 它不是“一个 Jira MCP”

```text
jira-bug-agent
├── Agent Harness             模型调用、Tool Loop、状态与终止条件
├── Jira MCP                  Issue、评论、附件
├── Log MCP                   Evidence、Timeline、Diagnostics
├── Model Provider            OpenAI-compatible API
└── Tests / Docs / CI         可回归、可独立 clone
```

MCP 是 Agent 的工具层。完整 Agent 还包含 Prompt、规划循环、上下文预算、错误策略
和最终报告。详细边界见 [架构文档](docs/architecture.md)。

## 当前能力

- `bug-agent analyze-jira APP-42`：读取 Jira、导出附件并分析；
- `bug-agent analyze-local D:\cases\APP-42`：直接分析本地 Case；
- Jira Cloud v3 与 Data Center v2；
- Android/logcat、Kernel monotonic、Wall Clock 时间线；
- AVC、Fatal、ANR、Kernel Call Trace 结构化诊断；
- 可追溯 Evidence：Artifact、相对路径和行号；
- 可替换的 OpenAI-compatible 模型服务；
- 步骤预算、Tool Result 预算和统一错误观察。

当前尚未实现 Code Search MCP、RAG 和 Jira 回写；路线见下方 Roadmap。

## 仓库结构

```text
src/bug_agent/                    # Agent Core / Harness / CLI
packages/jira-bug-mcp/            # Jira Adapter
packages/log-analyzer-mcp/        # 日志证据工具
tests/                            # Agent 与 Provider 测试
docs/architecture.md              # 分层与数据流
```

这是一个 uv workspace。两个 MCP 仍是独立 Python package，但与 Agent 一起安装
和测试。

## 安装

需要 Python 3.10+ 和 [uv](https://docs.astral.sh/uv/)：

```powershell
git clone <your-repository-url> jira-bug-agent
cd jira-bug-agent
uv sync --all-packages --extra dev
```

复制 `.env.example` 中需要的值到你自己的安全配置系统或终端环境。项目不会
自动读取 `.env`，避免使用者误以为明文文件是密钥保险箱。

模型服务必须兼容 Chat Completions 的 `tools/tool_calls`：

```powershell
$env:BUG_AGENT_LLM_BASE_URL = 'https://your-llm-gateway.example/v1'
$env:BUG_AGENT_LLM_API_KEY = 'your-key'
$env:BUG_AGENT_LLM_MODEL = 'your-model'
```

## 分析本地 Case

```powershell
uv run bug-agent analyze-local 'D:\bug-cases\APP-42'
```

输出完整运行状态和工具轨迹：

```powershell
uv run bug-agent --json analyze-local 'D:\bug-cases\APP-42'
```

## 分析 Jira Issue

Jira Cloud 示例：

```powershell
$env:JIRA_BASE_URL = 'https://your-domain.atlassian.net'
$env:JIRA_DEPLOYMENT = 'cloud'
$env:JIRA_AUTH_MODE = 'basic'
$env:JIRA_USER = 'you@example.com'
$env:JIRA_TOKEN = 'your-api-token'
$env:JIRA_EXPORT_ROOT = 'D:\bug-cases'

uv run bug-agent analyze-jira APP-42
```

Data Center PAT 通常配置为 `JIRA_DEPLOYMENT=datacenter`、
`JIRA_AUTH_MODE=bearer`。企业 SSO、代理和自定义字段可能需要扩展 Jira Client。

## 测试

默认测试不连接真实 Jira 或模型服务：

```powershell
uv run pytest -q tests
uv run pytest -q packages/jira-bug-mcp/tests
uv run pytest -q packages/log-analyzer-mcp/tests
```

## Roadmap

- [x] 可移植 Agent Loop
- [x] Jira MCP
- [x] Log Analyzer MCP V2
- [x] Jira / Local 双入口 CLI
- [ ] Code Search / Git MCP
- [ ] 历史 Bug RAG
- [ ] RCAReport 严格结构化输出与 Renderer
- [ ] Eval 数据集与 LLM-as-Judge
- [ ] Human-in-the-loop Jira 回写
- [ ] Web UI / Task history

## 开源许可

完整仓库暂未选择许可证。公开发布前需要由仓库所有者明确选择 MIT、Apache-2.0
或其他许可；在此之前不要假定获得了复制、修改或分发授权。
