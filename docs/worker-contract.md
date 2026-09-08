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
      ├── 配置步骤、工具调用数与总时长硬预算
      ├── 导出并验证 Jira Case
      ├── 按 source 连接 Log MCP
      ├── 选择 Jira/Local Prompt
      ├── 运行 BugAnalysisAgent
      └── 用工具轨迹校验 RCAReport Evidence
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
| `continuation_of` | 可选的父任务 ID；声明本次是同一 Case 的续分析，Worker 会把已有 RCA 状态作为待验证上下文 |
| `max_steps` | 可选的单任务模型轮次预算；工具调用数和总时长仍受服务端硬预算限制 |
| `skills` | 可选的预激活 Skill；省略时 Worker 默认加载通用 Android 日志分诊 |
| `auto_select_skills` | 是否允许 Agent 根据 Issue 和证据调用 `activate_skill`，默认 `true` |
| `include_trace` | 是否在结果中包含内部 Tool Event |
| `include_analysis_guide` | 是否在 RCA 完成后生成独立的问题分析讲解；默认 `false`，不影响正式 RCA |
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
| `waiting_for_human` | Agent 已完成必要的自动调查，但存在工具无法解除的明确阻塞，返回结构化人工检查点 |

`report` 始终存在，即使任务失败也能给上游稳定结构。`structured_output=false`
表示模型未遵守 RCA JSON Schema，Worker 使用了兼容降级；调用方可据此禁止自动
进入修改或提交阶段。

`report_validation` 记录 RCA 的证据落地结果。Worker 从成功工具结果重建 Evidence
Registry，逐项校验报告中的 Evidence ID、Artifact、相对路径、行号、视频时间点和摘录。
无法验证的引用会被移除；`confirmed` 缺少已验证根因或工具证据时自动降级为
`hypothesis_only`。`grounded=false` 的报告不能进入自动关闭 Bug 或代码修改流程。

`report.incident` 显式保存事故身份，包括 reported/verified 两套时间窗、boot、进程、
build、系统域和身份依据。无法建立的字段必须进入 limitations，避免跨 boot、跨进程或
跨复现拼接证据。

`trace` 是内部诊断信息，不是业务契约的推理依据。生产环境应单独控制保存周期
和访问权限，因为其中可能包含 Jira 与日志内容。

## 人工检查点属于 Agent Runtime

工具列表中的 `request_human_guidance` 是 Harness 提供的受约束能力。Agent 至少完成
两次真实工具调查后才可以调用；请求必须声明问题类别、单一问题、阻塞原因和需要的
具体输入。过早请求会返回 `HUMAN_GUIDANCE_PREMATURE`，Agent 必须继续自动调查。

成功请求后状态变为 `waiting_for_human`，`human_checkpoint` 随结果和 Run Bundle 一起
保存。在 `chat-local` / `chat-jira` 会话中，工程师下一条消息自动绑定该 checkpoint，
Agent 沿用同一消息、工具和 Skill 上下文继续运行。人工回复只作为待验证线索注入，
不能直接成为 confirmed 事实。会话记录同时保存 checkpoint、回复、回复后调用的工具
以及激活的 Skill，因而可以判断提示究竟补偿了工具选择、Skill 路由还是数据缺口。

Chat Session v3 还会按版本保存当时实际发送给模型的工具描述和参数 Schema，并让每轮
引用对应快照。会话复盘页因此可以逐次对照“工具用途—本次参数—契约要求—返回摘要”，
区分选错工具、参数不满足契约和工具返回能力不足。旧版会话没有快照时使用当前代码中的
契约后补，页面会明确提示它不一定等同于当时版本。

复盘中的人工参与不限于 Agent 主动请求的 checkpoint。初始任务之后的每次用户输入都会
改变后续调查，应按“本轮人工输入—后续工具路径—Agent 响应”留在反馈链中；显式纠错、
补充上下文、范围调整、证据指向和要求深挖只是不同类别。只有经人工确认需要进入优化的
轮次才成为训练或规则候选，普通追问不会被自动当成正确答案。

## Run Bundle v2：黄金 Case 的可回放输入

每次执行都会在 `<case>/.bug-agent/runs/` 保留一个 `schema_version=2` 的 JSON
Run Bundle。原有 `task`、`result`、`trace`、`phase` 等字段继续保留；新增字段用于
把一次真实运行转换成可以审核和进入 Eval 的事实记录：

| 字段 | 用途 |
|---|---|
| `provenance` | 记录 Agent 包版本、构建 revision、模型名，以及完整 System Prompt、用户指令、工具 Schema 和每个 Skill 的 SHA-256；不保存 Provider URL、API Key 或 Prompt 原文；仅 CI/发布系统注入的 revision 标记为 authoritative |
| `budget` | 同时记录配置的步骤/工具调用/总时长/单次结果预算与实际步骤、工具调用数、结果字符数、耗时和结束原因；`token_usage` 累加 Agent Loop 每次成功响应的 prompt/completion/total，可用时同时记录 cached/reasoning token 与 usage 覆盖率；Provider 完全未返回 usage 时为 `null` |
| `derived.evidence_registry` | 仅从成功且可完整解析的 Tool Event 重建本轮真实 Evidence Registry |
| `derived.claim_snapshot` | 固化症状、失效机制、根因、假设、缺失证据和报告校验结果，供后续规则化评分 |
| `derived.input_fingerprint` | 记录本轮工具实际看见的 Case、Artifact 元数据清单和归档内容哈希覆盖率；同时保存执行 Trace 哈希 |
| `derived.agent_metrics` | 记录成功/失败/重复工具调用、工具分布、动态 Skill 激活及人工检查点次数；这是优化 Agent 控制面的主要指标 |
| `lifecycle` | 记录 running/最终状态、阶段以及开始结束时间，保留中途失败现场 |
| `integrity` | 对 Bundle 中除自身以外的全部内容计算 SHA-256，用于发现落盘后的意外修改 |

`input_fingerprint.complete_content_fingerprint=false` 不是错误，而是明确的覆盖声明：
普通 Artifact 当前只提供路径、大小、修改时间等元数据；只有 MCP 已经计算并返回
`source_fingerprint.sha256` 的输入才算内容级指纹。Eval 导入时应把该字段作为可复现
等级的一部分，不能把元数据相同误认为文件内容必然相同。

CI 或发布系统应通过 `BUG_AGENT_BUILD_REVISION` 注入实际 commit SHA；本地未设置时
Worker 会尝试读取当前 Git HEAD。Bundle 是事实源，执行可视化和黄金 Case 标注都应
从它生成，不应另建一套不可追溯的数据。

当 `include_analysis_guide=true` 时，结果还会包含可选的 `analysis_guide`。它使用已
结构化的 RCA 与实际工具轨迹解释调查如何推进，且只保留报告中已存在的 `evidence_id`。
它不是 RCA 的字段，也不会被正式 RCA Markdown 渲染器写入；若附加生成失败，RCA 仍按
原状态返回，失败原因记录在 `analysis_guide_error`。

Worker 会把成功加载的名称写入 `applied_skills`。不存在、路径不安全、frontmatter
不完整或超过预算的 Skill 会让任务在调用模型和 MCP 前失败。

`skill_activations` 记录每个 Skill 的来源（`default`、`explicit` 或 `agent`）及原因。
自动模式只允许一个 `primary` 症状 Skill；当证据已经显示级联故障时，可额外激活一个
`secondary` 症状 Skill，用于验证下游影响和覆盖边界，但不能与 primary 竞争根因所有权。
同时允许叠加 `category=platform`；显式预激活的症状路线具有优先权。动态加载由 Worker 本地完成，
不会扩大 MCP 工具或文件路径权限。

## Jira 评论硬前置条件

Jira 模式和 Jira 导出的 Local Case 都必须在主 Agent 启动前证明根 Issue 的评论已
完整分页收集。`collection-manifest.json` 的 `root_issue_context.comments` 记录 Jira
报告总数、实际收集数量和截断状态；缺少该元数据、数量不一致、评论被截断或
`issue.json` 哈希不匹配时，Worker 返回 `failed`，不会让模型基于残缺上下文分析。

不超过 `BUG_AGENT_JIRA_DIRECT_CONTEXT_MAX_CHARS`（默认 60,000 字符）的完整描述与
评论会直接放入 `DIRECT_JIRA_CONTEXT`。更大的上下文才通过 Comment Compiler 分块
生成带 `comment_id` 来源的 `COMPILED_JIRA_CONTEXT`；这是有损摘要，受最大分块数、
模型调用次数和总耗时硬预算约束。需要核对某条评论原文时，可通过 Log MCP 的
`get_case_comment(case_id, comment_id, offset, limit)` 分页读取；每次读取都会重新
验证 Manifest、评论数量与 `issue.json` 哈希。纯本地日志 Case 不受 Jira 评论完整性
规则约束。

Jira 路径严格按“连接 Jira MCP → 确定性导出 → 验证导出路径和上下文 → 连接 Log MCP
→ 创建 Provider”的顺序执行。运行记录的 `jira_context` 保存 direct/compiled 模式、
原始字符数、分块数、模型尝试数和重试数；失败记录的 `failure.phase` 标明失败阶段，
但不保存第三方响应正文或凭据。

旧版导出的 Jira Case 没有完整性元数据，需要重新导出：

```powershell
uv run bug-agent collect-jira APP-42 --export-case
```
