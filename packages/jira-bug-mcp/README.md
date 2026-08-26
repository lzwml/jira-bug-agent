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
| `get_issue` | 读取结构化 Issue 详情 |
| `search_issues` | 用 JQL 分页搜索 |
| `get_comments` | 分页读取评论 |
| `list_attachments` | 只列附件元数据 |
| `export_issue_case` | 导出 Issue、评论和选定附件供 Log MCP 分析 |

所有工具统一返回 `success / data / error_code / error_message / retryable`。
当前版本不提供创建 Issue、修改字段或发表评论，避免分析 Agent 意外改变 Jira。

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
| `JIRA_TIMEOUT_SECONDS` | `30` | HTTP 超时 |
| `JIRA_MAX_ATTACHMENT_BYTES` | 50 MiB | 单附件上限 |
| `JIRA_MAX_EXPORT_BYTES` | 200 MiB | 单次导出总上限 |

## 测试

测试使用 `httpx.MockTransport`，不连接真实 Jira：

```powershell
.\.venv\Scripts\python -m pytest -q
```
