# Worker Contract

## 为什么需要 Worker Facade

`BugAnalysisAgent` 是内部自主循环；`BugAnalysisWorker` 是对部门 Workflow 的
应用边界。调用方不需要启动 MCP、选择 Prompt、管理 Provider 或理解 LLM 的
`tool_calls` 消息。

```text
BugAnalysisTask
      │
      ▼
BugAnalysisWorker
      ├── 配置本次步骤预算
      ├── 按 source 连接 MCP
      ├── 选择 Jira/Local Prompt
      ├── 运行 BugAnalysisAgent
      └── 校验 RCAReport
      │
      ▼
BugAnalysisResult
```

## 输入

`BugAnalysisTask` 的稳定字段：

| 字段 | 说明 |
|---|---|
| `task_id` | 上游幂等、追踪和审计使用；未提供时自动生成 UUID |
| `source` | `jira` 或 `local` |
| `issue_key` | Jira 模式必填 |
| `case_path` | Local 模式必填 |
| `objective` | 本次分析目标，不扩大 Worker 权限 |
| `max_steps` | 可选的单任务步骤预算 |
| `skills` | 本次激活的团队 Skill 名称；默认通用 Android 日志分诊 |
| `include_trace` | 是否在结果中包含内部 Tool Event |
| `metadata` | 上游关联信息，Agent 当前不消费 |

契约拒绝同时传入 `issue_key` 和 `case_path`，避免来源语义不明确。

## 输出

`BugAnalysisResult.status`：

| 状态 | 含义 |
|---|---|
| `completed` | Worker 完成，RCA 至少形成假设或确认结论 |
| `insufficient_evidence` | Worker 正常完成，但证据不足 |
| `max_steps` | 达到 Agent 步骤预算 |
| `failed` | 配置、MCP、Provider 或运行过程失败 |

`report` 始终存在，即使任务失败也能给上游稳定结构。`structured_output=false`
表示模型未遵守 RCA JSON Schema，Worker 使用了兼容降级；调用方可据此禁止自动
进入修改或提交阶段。

`trace` 是内部诊断信息，不是业务契约的推理依据。生产环境应单独控制保存周期
和访问权限，因为其中可能包含 Jira 与日志内容。

Worker 会把成功加载的名称写入 `applied_skills`。不存在、路径不安全、frontmatter
不完整或超过预算的 Skill 会让任务在调用模型和 MCP 前失败。
