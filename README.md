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
├── Video Analysis MCP         录屏关键帧、视觉证据与问题时间点（可选）
├── Model Provider            OpenAI-compatible API
└── Tests / Docs / CI         可回归、可独立 clone
```

MCP 是 Agent 的工具层。完整 Agent 还包含 Prompt、规划循环、上下文预算、错误策略
和最终报告。详细边界见 [架构文档](docs/architecture.md)。

## 当前能力

- `bug-agent analyze-jira APP-42`：读取 Jira、导出附件并分析；
- `bug-agent analyze-local D:\cases\APP-42`：直接分析本地 Case；
- Jira Cloud v3 与 Data Center v2；
- 可组合的团队 Skills：通用日志分诊、Android 黑屏与 MTK IVI 跨域分析；
- Android/logcat、Kernel monotonic、Wall Clock 时间线；
- AVC、Fatal、ANR、Kernel Call Trace 结构化诊断；
- ZIP/TAR/TAR.GZ/TGZ/GZIP 归档清单、选择性安全展开、嵌套深度与解压炸弹预算；
- 持久化分块索引和超大文本日志检索；
- 可追溯 Evidence：Artifact、相对路径和行号；
- RCA Evidence 由 Worker 对照真实工具轨迹校验；无根因引用或事故身份锚点的
  `confirmed` 会自动降级；
- 可替换的 OpenAI-compatible 模型服务；
- 模型轮次、工具调用数、总时长、Tool Result 预算和统一错误观察。
- 稳定的 `BugAnalysisTask → BugAnalysisResult` Worker 契约。
- 异步 HTTP 任务 API：SQLite 状态、幂等提交、受控并发和重启恢复。
- Case 级 RCA 迭代：续分析会继承已有证据状态，并保留完整任务链与变更审计。
- 可选视频证据 MCP：受限于当前 Case 路径，按需抽帧、调用视觉模型并返回可复核时间点。

当前尚未实现 Code Search MCP、RAG 和 Jira 回写；路线见下方 Roadmap。

## 仓库结构

```text
src/bug_agent/                    # Worker / Agent Core / Harness / CLI
packages/jira-bug-mcp/            # Jira Adapter
packages/log-analysis-core/       # 与协议无关的确定性解析引擎
packages/log-analyzer-mcp/        # Core 的 MCP 安全适配层
packages/video-analysis-mcp/      # 录屏/视频证据 MCP（ffmpeg + 可替换视觉模型）
skills/                           # 团队维护的领域分析方法
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

本地开发可以复制 `.env.example` 为 `.env`。Agent CLI 和 Jira MCP 会自动
读取项目当前目录的 `.env`，系统环境变量优先。`.env` 已被 Git 忽略，
但它仍是本地明文文件；生产环境应使用密钥管理系统。

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

如果希望在正式 RCA 之外，额外生成一份面向工程师的调查思路讲解：

```powershell
uv run bug-agent --analysis-guide analyze-local 'D:\bug-cases\APP-42'
```

该讲解不会写入 RCA Markdown；它会保存为
`<case>/.bug-agent/analysis-guides/<task_id>.md`。内容只基于结构化 RCA 与实际工具
轨迹，解释观察、验证问题、排除路径和可复用的排查方法；生成失败不影响 RCA 交付。

输出完整 Worker 结果和内部工具轨迹：

```powershell
uv run bug-agent --json analyze-local 'D:\bug-cases\APP-42'
```

默认情况下，Worker 先加载 `android-log-triage`。Agent 会看到可信 Skill 目录，并在
Issue 或首轮日志证据明确属于某个专项时自动调用 `activate_skill` 加载对应方法。
自动激活的名称和理由会出现在结果及完整 Trace 中。

如果调用方已经知道路线，也可以用 `--skill` 预先激活；多个 `--skill` 可以组合：

```powershell
uv run bug-agent `
  --skill android-log-triage `
  --skill android-black-screen `
  analyze-local 'D:\bug-cases\APP-42'
```

MTK 车机 Case 涉及 Android VM、Linux VM、TBox、SCP、Hypervisor、MCU 或
CAN 等跨域证据时，再叠加平台 Skill：

```powershell
uv run bug-agent `
  --skill android-log-triage `
  --skill mtk-ivi-log-analysis `
  analyze-local 'D:\bug-cases\APP-42'
```

`mtk-ivi-log-analysis` 的入口只保留调查路由、跨时钟约束和证据停止条件；
Android、Linux、时钟域与安全解压细节拆分在其 `references/` 目录，便于团队维护。

症状层目前提供以下独立路线：

| 症状 | Skill |
| --- | --- |
| 黑屏、无显示、显示冻结 | `android-black-screen` |
| ANR、UI 卡死、输入超时 | `android-anr-ui-freeze` |
| Tombstone、Native Crash、Native 服务死亡 | `android-native-crash` |
| 重启、Watchdog、Kernel Panic、Boot Loop | `system-reboot-watchdog` |
| Linux VM、Hypervisor、SCP 或跨 VM 故障 | `linux-virtualization-failure` |
| CAN/MCU 信号缺失、过期或异常 | `can-mcu-signal-analysis` |
| OTA、PKI、认证或连接失败 | `ota-pki-connectivity` |

CLI 或上层 Workflow 显式指定的路线优先；未指定时，Agent 可根据已验证的 Issue 上下文和首轮
诊断证据自动激活一个主要症状 Skill，并按需叠加平台 Skill。系统不允许同时激活
两个主要症状 Skill；已验证的级联故障可以额外激活一个 `secondary` 症状 Skill，专门
检查下游影响和覆盖边界。低置信度场景继续使用 `android-log-triage`。如需完全禁止运行时
激活，可添加 `--no-auto-skills`。每条路线遵循相同的触发条件、事件身份、首轮证据、
决策分支、反证/停止条件和输出契约。

Skill 决定分析顺序、时间线锚点和证据标准；解析、路径权限和扫描预算仍由
Core/MCP 代码保证。不要把正则解析器或文件操作写进 Skill。

对于 Jira 模式或 Jira 导出的本地 Case，Worker 会在主分析开始前校验根 Issue 的
全部评论已经完成分页收集。默认在 60,000 字符以内把已验证的描述和全部评论直接
注入初始上下文；更大的上下文才由 Comment Compiler 分块压缩成带 `comment_id`
引用的有损摘要，并受分块数、模型调用次数和总耗时硬预算约束。需要核对精确措辞时
可调用 `get_case_comment` 按 ID 分页读取，该工具会再次验证 Manifest 和文件哈希。
旧版 Case 缺少完整性元数据时会严格拒绝分析，需要重新执行
`collect-jira <KEY> --export-case`。

Agent 默认不会先把所有附件全量解压。它先用 `inspect_case` 判断 Case 规模，
再用 `inspect_archive` 查看候选归档的成员清单；结合 Jira 中的问题症状、发生
时间、日志域、文件名和大小选择成员，通过 `extract_archive_members` 解压，并
只对相关文本调用 `build_index`。如果首轮证据不足，Agent 会逐步扩大时间窗口、
日志域或成员范围，再继续检索。`prepare_case` 只作为用户明确要求或渐进式调查
仍无法确定必要成员时的全量兜底。

## 作为异步 HTTP 服务运行

HTTP 入口适合 Jira 自动化、内部平台和部门 Workflow。请求提交后立即返回
`task_id`，分析在后台受控执行，调用方通过 `GET /tasks/{task_id}` 查询结果：

```powershell
$env:BUG_AGENT_API_ALLOWED_LOCAL_ROOTS = 'D:\bug-cases'
$env:BUG_AGENT_API_KEY = 'replace-with-a-secret'
$env:BUG_AGENT_API_CONCURRENCY = '2'

uv run bug-agent-api
```

服务默认只监听 `127.0.0.1:8000`。本地 Case 必须位于
`BUG_AGENT_API_ALLOWED_LOCAL_ROOTS` 中；未配置允许目录时，API 会拒绝本地路径任务。
任务状态持久化在 SQLite 中，相同 `task_id` 的相同请求具有幂等性。接口、配置和
安全部署说明见 [HTTP API](docs/http-api.md)。

当拿到新日志、复现结果或想验证上一轮假设时，使用
`POST /tasks/{task_id}/continuations` 创建同一 Case 的下一轮分析。它会继承来源和
权限边界、生成新的任务 ID，并将阶段性 `RCA.md` 作为待验证上下文；每一轮的轨迹、
父子关系和 RCA 变更都会保留，避免把 Bug 分析变成彼此孤立的一次性输出。

## 作为 Worker 调用

部门 Workflow、HTTP 服务或任务队列不应调用 CLI，而应依赖稳定 Worker 契约：

```python
from bug_agent import BugAnalysisTask, BugAnalysisWorker
from bug_agent.config import AgentConfig

worker = BugAnalysisWorker(AgentConfig.from_environment())
result = await worker.execute(BugAnalysisTask(
    task_id="workflow-2026-001",
    source="jira",
    issue_key="APP-42",
    objective="定位启动黑屏根因",
))

if result.status == "completed":
    print(result.report.summary)
```

对外结果包含结构化 RCA、Evidence 引用、假设、缺失证据和下一步动作。内部
Tool Event 默认不返回；只有 `include_trace=true` 时才进入结果，避免上游系统
依赖模型消息细节。完整 Schema 见 [Worker Contract](docs/worker-contract.md)。

无论 `include_trace` 是否开启，Worker 都会在 Case 的 `.bug-agent/runs/` 保存
Run Bundle v2：执行轨迹、证据与主张快照、输入指纹覆盖、模型/Prompt/Skill/工具
版本哈希、预算使用和完整性摘要放在同一份记录中。它既是执行可视化的数据源，
也是后续真实稳定性黄金 Case Eval 的可审计输入；字段说明见
[Worker Contract](docs/worker-contract.md#run-bundle-v2黄金-case-的可回放输入)。

人工复核以旁路 Review 保存，不会回写或污染原始运行记录；确认或纠正后的运行可以
晋升为版本化黄金 Case，评分维度、反馈 JSON 和命令见
[真实稳定性黄金 Case Eval](docs/golden-case-eval.md)。

交互分析中，Agent 在完成必要工具调查后仍遇到真实阻塞时，会主动生成一个结构化
人工检查点。工程师直接回复即可在同一会话上下文继续；检查点、回复以及回复后发生的
Tool/Skill 变化都会本地留存。优化目标是让同类黄金 Case 后续不再需要相同提示，
而不是把人工回复当成模型结论。

每次人工提示被消费后，系统还会自动在
`<case>/.bug-agent/optimization/candidates/` 生成一条待审核优化候选，将影响归因到
工具选择、Skill 路由、Skill 内容或 Case 输入契约。它只提出改动方向，不会自动修改
Skill；候选必须经人工确认，并通过固定模型下的黄金 Case 回归后才能进入 Agent。

```powershell
bug-agent visualize-run <case>/.bug-agent/runs/<run>.json
bug-agent visualize-chat <case>/.bug-agent/chat-sessions/<session>.json
bug-agent eval-review <run-bundle.json> <review.json> --promote
bug-agent eval-run <candidate-run-bundle.json> <golden-case.json>
```

`visualize-chat` 默认生成一个完全本地的四页复盘站点：概览、会话、工具和优化。
原来的 `<session>.html` 保留为跳转入口，因此旧书签刷新后仍可使用；工具用途、参数契约、
返回文件/Evidence 位置及人工判断集中在工具页，不再打断会话阅读。

## 分析 Jira Issue

先可以只验证 Jira 接入并收集信息。该命令不需要配置大模型：

```powershell
uv run bug-agent collect-jira APP-42
```

如果还需要生成本地 Case 并下载附件：

```powershell
uv run bug-agent collect-jira APP-42 --export-case
```

只做 Jira 收集、附件下载、全量安全解压和索引，不配置也不调用大模型：

```powershell
uv run bug-agent collect-jira APP-42 --prepare
```

如果当前只需要下载和全量解压，不建立索引：

```powershell
uv run bug-agent collect-jira APP-42 --prepare --no-index
```

`--prepare` 是显式的全量准备入口，并隐含 `--export-case`；它不会执行 Agent 的
按症状和问题时间选择成员流程。输出中的 `export` 区分
`downloaded_attachments` 与 `reused_attachments`，`preparation` 包含展开、跳过和
索引统计。归档会展开到它旁边的 `<归档文件名>.unpacked/`，方便人工查看；索引和
内部状态默认写入 Case 下的 `.bug-agent/`。原始附件不会被修改。`--work-dir`
只改变索引和内部状态的位置，不改变解压位置。

对于已经存在的本地 Case，也可以显式执行全量解压和索引：

```powershell
uv run bug-agent prepare-local "D:\bug-cases\APP-42"
```

`prepare-local` 与 `collect-jira --prepare` 一样，是供人工批处理或最终兜底使用的
全量准备命令，不是 Agent 的默认调查路径。需要忽略缓存时添加
`--force-rebuild`；只解压不索引使用 `--no-index`，只索引现有文本使用
`--no-extract`。

导出时会默认展开一层 Jira 关联，包括评论中引用的 Issue，并下载
关联 Issue 的日志附件。本地已存在且附件 ID、文件名与大小均匹配时会直接复用，
不会再次下载。如只需当前 Issue：

```powershell
uv run bug-agent collect-jira APP-42 --export-case --no-related
```

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
uv run pytest -q packages/log-analysis-core/tests
uv run pytest -q packages/jira-bug-mcp/tests
uv run pytest -q packages/log-analyzer-mcp/tests
```

## Roadmap

- [x] 可移植 Agent Loop
- [x] Jira MCP
- [x] Log Analyzer MCP V2
- [x] Log Analysis Core 与 MCP 协议层分离
- [x] 安全归档展开、持久化分块索引与超大日志检索
- [x] 通用日志分诊与黑屏分析 Skills
- [x] Jira / Local 双入口 CLI
- [x] 可嵌入部门 Workflow 的 Worker Facade 与稳定输入输出契约
- [x] 异步 HTTP API、SQLite 任务状态与有界并发调度
- [ ] Code Search / Git MCP
- [x] 根据 Issue 与首轮证据自动激活专项 Skill，并记录可审计理由
- [ ] 历史 Bug RAG
- [x] RCAReport Schema、兼容解析与 Markdown Renderer
- [x] 可选 Video Analysis MCP：安全视频注册、关键帧、视觉分析和问题片段导出
- [ ] 使用模型原生 constrained output 强制 RCAReport
- [ ] Eval 数据集与 LLM-as-Judge
- [ ] Human-in-the-loop Jira 回写
- [ ] Web UI / Task history

## 开源许可

完整仓库暂未选择许可证。公开发布前需要由仓库所有者明确选择 MIT、Apache-2.0
或其他许可；在此之前不要假定获得了复制、修改或分发授权。
