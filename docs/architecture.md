# Architecture

## 这一个仓库是什么

本仓库是完整的专用 Agent，而不是某个 MCP Server。它包含四个相互解耦的层：

```text
CLI / HTTP / Queue / Department Workflow
        │
        ▼
BugAnalysisWorker       稳定 Task/Result、依赖生命周期、模式选择
        │
        ▼
BugAnalysisAgent        Agent Loop、步骤预算、内部 Trace、Prompt
        │
        ├── ModelProvider         OpenAI-compatible / DeepSeek / Gateway
        │
        └── McpToolRouter
                ├── jira-bug-mcp       Issue 与附件 Adapter
                ├── log-analyzer-mcp   证据提取 Adapter
                └── future code/rag MCP
```

外部系统只依赖 `BugAnalysisTask → BugAnalysisResult`。`BugAnalysisAgent` 不
知道 Jira REST URL、Log MCP 文件路径或模型厂商 SDK。它只依赖两个小接口：
模型完成一次对话、工具路由执行一次调用。因此 Provider 和 MCP 都可以替换，
而 Agent Loop、领域 Prompt 和运行状态保持稳定。

Worker 契约与内部 Agent Trace 的边界见 [Worker Contract](worker-contract.md)。

## 两条入口流程

### Jira Issue

```text
APP-42
  → get_issue
  → export_issue_case
  → open_case(case_path)
  → inspect/search/timeline/diagnostics
  → RCA
```

### 本地 Case

```text
local directory
  → open_case
  → inspect/search/timeline/diagnostics
  → RCA
```

两条流程最终复用同一套日志证据契约，这就是 Jira Adapter 与 Agent Core 解耦
后的直接收益。

## 和 DSH/Harness 的关系

Harness 负责模型调用、Tool Call 循环、错误观察、状态和终止条件；MCP 提供外部
能力。当前仓库内置一个最小 Harness，适合学习这些机制。未来接入 DSH、Codex
或其他 Runtime 时，可以保留 MCP packages 和领域 Prompt，只替换 Harness。

## 安全边界

- LLM 与 Jira Token 只从进程环境读取；
- Jira 工具默认只读，唯一文件写入限制在导出根目录；
- Log MCP 只读取显式允许的 Case 根目录；
- Jira 描述、评论和日志都视为不可信数据；
- Tool Result 有字符预算，Agent Loop 有步骤预算；
- 当前 Agent 不会回写 Jira，也不会执行附件。
