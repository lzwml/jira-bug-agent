# HTTP API

HTTP API 是 CLI 之外的第二个应用入口。它不复制分析逻辑，而是异步调度现有的
`BugAnalysisWorker`：提交请求立即获得 `task_id`，调用方随后查询状态和结果。

## 启动

除模型和 Jira 的既有配置外，建议至少设置以下 API 配置：

```powershell
$env:BUG_AGENT_API_ALLOWED_LOCAL_ROOTS = 'D:\bug-cases'
$env:BUG_AGENT_API_KEY = 'replace-with-a-secret'
$env:BUG_AGENT_API_CONCURRENCY = '2'
$env:BUG_AGENT_API_DB_PATH = 'D:\bug-agent-state\tasks.sqlite3'

uv run bug-agent-api
```

服务默认只监听 `127.0.0.1:8000`。`BUG_AGENT_API_ALLOWED_LOCAL_ROOTS` 在 Windows
上使用分号分隔多个目录；未配置时，API 会拒绝所有 `source=local` 请求，防止远程
调用方借 Case 路径读取服务器任意目录。Jira 任务不受该配置影响，它继续使用
`JIRA_EXPORT_ROOT`。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BUG_AGENT_API_HOST` | `127.0.0.1` | 监听地址 |
| `BUG_AGENT_API_PORT` | `8000` | 监听端口 |
| `BUG_AGENT_API_CONCURRENCY` | `2` | 同时执行的任务数，范围 1..32 |
| `BUG_AGENT_API_DB_PATH` | `.bug-agent/api/tasks.sqlite3` | SQLite 状态库 |
| `BUG_AGENT_API_KEY` | 未设置 | 设置后要求 `X-API-Key` 请求头 |

如果服务需要监听非本机地址，应设置 API Key，并由反向代理提供 TLS、访问控制和
请求大小限制。健康检查 `/health` 刻意不要求 API Key。

## 提交和查询

```powershell
$headers = @{ 'X-API-Key' = $env:BUG_AGENT_API_KEY }
$body = @{
  task_id = 'workflow-2026-001'
  source = 'local'
  case_path = 'D:\bug-cases\APP-42'
  objective = '定位启动黑屏根因'
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri 'http://127.0.0.1:8000/tasks' `
  -Headers $headers `
  -ContentType 'application/json' `
  -Body $body
```

提交成功返回 HTTP `202`：

```json
{"task_id":"workflow-2026-001","status":"queued","created":true}
```

查询任务：

```powershell
Invoke-RestMethod `
  -Uri 'http://127.0.0.1:8000/tasks/workflow-2026-001' `
  -Headers $headers
```

外层状态为 `queued`、`running`、`completed` 或 `failed`。分析正常结束时外层状态为
`completed`；具体是成功定位、证据不足还是达到步骤上限，继续查看内层
`result.status`。

## 幂等、恢复和当前边界

- 相同 `task_id` 和完全相同的任务内容重复提交时，不会重复执行，返回
  `created=false`；
- 相同 `task_id` 配合不同任务内容时返回 HTTP `409`；
- 服务重启后，数据库中原先的 `running` 任务会回到 `queued` 并重新执行；
- SQLite 中包含任务输入和分析结果，应按 Bug 数据的敏感级别保护和备份；
- 当前调度器是单服务进程方案，不是分布式队列。不要用多个 API 进程共同承担提交；
  后续可以保持 HTTP 和 Worker 契约不变，仅替换调度器。
