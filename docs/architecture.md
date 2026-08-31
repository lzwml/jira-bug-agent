# Architecture

## 这一个仓库是什么

本仓库是完整的专用 Agent，而不是某个 MCP Server。它包含四个相互解耦的层：

```text
CLI / HTTP / Queue / Department Workflow
        │
        ▼
BugAnalysisWorker       稳定 Task/Result、依赖生命周期、模式选择
        │
        ├── SkillRegistry          团队分析方法与证据标准
        │
        ▼
BugAnalysisAgent        Agent Loop、步骤预算、内部 Trace、Prompt
        │
        ├── ModelProvider         OpenAI-compatible / DeepSeek / Gateway
        │
        └── McpToolRouter
                ├── jira-bug-mcp       Issue 与附件 Adapter
                ├── log-analyzer-mcp   文件安全与 MCP Adapter
                │       └── log-analysis-core  确定性解析
                └── future code/rag MCP
```

外部系统只依赖 `BugAnalysisTask → BugAnalysisResult`。`BugAnalysisAgent` 不
知道 Jira REST URL、Log MCP 文件路径或模型厂商 SDK。它只依赖两个小接口：
模型完成一次对话、工具路由执行一次调用。因此 Provider 和 MCP 都可以替换，
而 Agent Loop、领域 Prompt 和运行状态保持稳定。

Worker 契约与内部 Agent Trace 的边界见 [Worker Contract](worker-contract.md)。

当前 HTTP 入口在 Worker 之上增加 SQLite 状态存储和有界并发调度器。它只负责
任务生命周期、幂等和恢复，不介入 Prompt、工具选择或 RCA 生成：

```text
POST /tasks → queued → running → BugAnalysisWorker → completed / failed
     │                                             │
     └──────────── SQLite Task Store ──────────────┘
```

详细接口和部署边界见 [HTTP API](http-api.md)。当前实现面向单服务进程；未来接入
分布式队列时，应替换 Dispatcher，而不是改变 Worker 契约。

## Core、MCP 与 Skill

```text
Skill                  怎么调查、关注什么、证据何时充分
  ↓ anchors / queries / diagnostic types
Log MCP                安全注册 Case、执行有预算的工具调用
  ↓
Log Analysis Core      时间戳、稳定 ID、诊断信号等确定性解析
```

Skill 不执行文件操作，也不承担安全控制。Core 不决定某条 Fatal 或 AVC 是否是
当前 Bug 的根因。MCP 是两者之间的受控执行边界，而不是领域专家本身。

Skill 按职责组合，而不是把整个平台写进一个文件：

```text
初始分诊（故障类型未知）       android-log-triage
              │
              ▼
主要症状路线                  ANR / Native Crash / Reboot / CAN / OTA / ...
              +
平台约束（按需叠加）           mtk-ivi-log-analysis
```

一个分析 Pass 应选择一个主要症状路线。平台 Skill 补充日志拓扑、平台术语和跨时钟
规则，但不重复症状路线的调查决策。CLI 或上层 Workflow 可以预先指定 Skill；否则
Worker 默认加载通用分诊，并向 Agent 暴露只读的 `activate_skill` 本地工具和可信目录。
Agent 可根据 Issue 或首轮诊断证据加载一个主要症状 Skill，并按需叠加平台 Skill。
激活名称、来源和理由进入稳定结果，工具调用进入完整 Trace，便于审计和 Eval。

`activate_skill` 只返回仓库中已验证的调查指令，不创建外部工具、不扩大 Case 路径，
也不能改变 Jira 只读策略。运行时约束拒绝第二个主要症状 Skill，避免一个 Pass 同时
沿多条互相竞争的路线发散。

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
- Log MCP 只读取显式允许的 Case 根目录，归档展开和索引仅写入服务端隔离工作区；
- Jira 描述、评论和日志都视为不可信数据；
- Tool Result 有字符预算，Agent Loop 有步骤预算；
- 当前 Agent 不会回写 Jira，也不会执行附件。
