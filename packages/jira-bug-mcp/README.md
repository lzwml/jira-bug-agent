# jira-bug-mcp

一个面向 Bug 分析 Agent 的只读 Jira MCP Server。它隐藏 Jira Cloud / Data
Center 的 REST 差异，并能把 Issue 与附件导出为 `log-analyzer-mcp` 可读取的
本地 Case。

## 为什么单独做一个 MCP

```text
Agent / Codex
    │ MCP：稳定工具契约
    ▼
JiraService
    ├── JiraClient：认证、HTTP、Cloud v3 / Data Center v2、ADF
    └── CaseExporter：受控本地写入、文件名清洗、下载预算
             │
             ▼
        log-analyzer-mcp / open_case
```

Agent 依赖的是 `get_issue` 等能力，而不是 Jira URL、认证方式或 REST 字段。
所以 MCP 与 Jira API 绑定，但 Agent 本身不必与 Jira 绑定；以后可以实现同样
工具契约的本地 Markdown、GitHub Issues 或禅道 Adapter。

## 工具

| 工具 | 用途 |
|---|---|
| `test_connection` | 校验 Jira 网络、认证和 REST 版本 |
| `get_issue` | 读取结构化 Issue 详情 |
| `collect_issue_context` | 聚合收集 Bug 字段、完整评论、关联 Issue 和附件元数据 |
| `search_issues` | 用 JQL 分页搜索 |
| `get_comments` | 分页读取评论 |
| `list_attachments` | 只列附件元数据 |
| `export_issue_case` | 导出 Issue、评论和选定附件供 Log MCP 分析 |

所有工具统一返回 `success / data / error_code / error_message / retryable`。
当前版本不提供创建 Issue、修改字段或发表评论，避免分析 Agent 意外改变 Jira。

## 信息收集边界

`collect_issue_context` 默认收集：

- 摘要、描述、类型、状态、优先级、经办人和报告人；
- 环境、标签、模块、影响版本、修复版本和解决状态；
- 父任务、子任务和关联 Issue；
- 分页后的评论（默认最多 1000 条）；
- 附件名称、类型、大小、作者和时间，但不在该工具中下载。

企业 Jira 中的车型、分支、复现概率等往往是自定义字段。用
`JIRA_EXTRA_FIELDS` 按 Jira 字段 ID 显式加入白名单：

```powershell
$env:JIRA_EXTRA_FIELDS = 'customfield_12345,customfield_12346'
```

未在白名单中的自定义字段不会返回给 Agent。

## 关联 Issue 与日志附件

`export_issue_case` 默认展开一层关联 Issue，但根据来源区分两种模式：

- `context`：Jira 正式 Issue Link、父任务和子任务，收集必要上下文；
- `attachments`：描述或评论中的 Issue Key，只读摘要和附件元数据并下载附件。

`attachments` 模式不读取对方评论、不保存完整 Issue、不继续递归，
适用于“日志太长，见 BAIC-xxxxx 附件”的场景。

关联 Issue 的附件会保存在 `related/<ISSUE-KEY>/attachments/`，导出根目录的
`collection-manifest.json` 记录 `from_issue / to_issue / source`，使日志
证据可回溯到引用它的评论或 Issue Link。

重复导出同一 Issue 时，Exporter 用 Jira 附件 ID 生成稳定目标名，并校验本地
普通文件的实际大小是否与 Jira 元数据一致。一致则加入 `reused_attachments`
并跳过网络下载；大小不一致或文件缺失时只重新下载对应附件。复用文件仍计入
`attachment_bytes` 和单次导出预算，`downloaded_bytes` 只统计本次网络传输。

默认安全边界是：展开 1 层、最多 10 个关联 Issue、最多 30 个关联
附件，并与根 Issue 共享单附件和单次导出字节预算。访问不到的
关联 Issue 只会记录在 `related_skipped`，不会让整个 Case 导出失败。

## 安装与运行

```powershell
cd 'packages/jira-bug-mcp'
python -m venv .venv
.\.venv\Scripts\pip install -e '.[dev]'
```

Jira Cloud（邮箱 + API Token）：

```powershell
$env:JIRA_BASE_URL = 'https://your-domain.atlassian.net'
$env:JIRA_DEPLOYMENT = 'cloud'
$env:JIRA_AUTH_MODE = 'basic'
$env:JIRA_USER = 'you@example.com'
$env:JIRA_TOKEN = 'your-api-token'
$env:JIRA_EXPORT_ROOT = 'D:\jira-cases'
.\.venv\Scripts\jira-bug-mcp.exe
```

Data Center 常使用 Personal Access Token：

```powershell
$env:JIRA_BASE_URL = 'https://jira.company.example'
$env:JIRA_DEPLOYMENT = 'datacenter'
$env:JIRA_AUTH_MODE = 'bearer'
$env:JIRA_TOKEN = 'your-personal-access-token'
```

不要把 Token 写进仓库或 MCP 工具参数。企业 Jira 的代理、SSO 和字段配置
可能不同，先用有最小只读权限的专用账号验证。

## 与 Log MCP 串联

1. 调 `export_issue_case({"issue_key":"APP-42"})`；
2. 读取结果中的 `case_path`；
3. 将其传给 Log MCP：`open_case({"case_path":"..."})`；
4. 再调用 `inspect_case`、`search_evidence`、`extract_timeline`。

Log MCP 的 `LOG_ANALYZER_ALLOWED_ROOTS` 必须包含 `JIRA_EXPORT_ROOT`，否则它会
按设计拒绝读取。附件只下载，不解压、不执行；单文件和单次导出均有大小限制。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `JIRA_BASE_URL` | 必填 | Jira 根地址 |
| `JIRA_DEPLOYMENT` | `cloud` | `cloud` / `datacenter` |
| `JIRA_AUTH_MODE` | `basic` | `basic` / `bearer` / `none` |
| `JIRA_USER` | - | basic 用户名/邮箱 |
| `JIRA_TOKEN` | - | API Token / PAT |
| `JIRA_EXPORT_ROOT` | `exports` | Case 导出根目录 |
| `JIRA_VERIFY_SSL` | `true` | 是否校验证书；生产环境不要关闭 |
| `JIRA_TRUST_ENV` | `false` | 是否继承 `HTTP_PROXY/HTTPS_PROXY`；内网直连默认关闭 |
| `JIRA_TIMEOUT_SECONDS` | `30` | HTTP 超时 |
| `JIRA_MAX_ATTACHMENT_BYTES` | 50 MiB | 单附件上限 |
| `JIRA_MAX_EXPORT_BYTES` | 200 MiB | 单次导出总上限 |
| `JIRA_EXTRA_FIELDS` | - | 逗号分隔的自定义字段白名单 |

Android bugreport、SoS 和 CAN 压缩包通常超过默认预算。在确认本地磁盘
容量后，可以在 `.env` 中使用：

```text
JIRA_MAX_ATTACHMENT_BYTES=2147483648
JIRA_MAX_EXPORT_BYTES=8589934592
```

分别表示单附件 2 GiB 和单次 Case 8 GiB。附件仍使用流式下载，
并会根据实际写入字节再次校验总预算。

## 测试

测试使用 `httpx.MockTransport`，不连接真实 Jira：

```powershell
.\.venv\Scripts\python -m pytest -q
```
