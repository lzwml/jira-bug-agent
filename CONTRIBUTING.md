# Contributing

## 开发原则

1. Agent Core 不得引用 Jira REST 字段、本机 OpenClaw 配置或固定模型地址；
2. 外部系统差异放在 MCP/Provider Adapter；
3. 工具行为变化必须补充不依赖真实企业服务的测试；
4. 日志、Fixture 和异常中禁止放真实 Token、Issue 或公司地址；
5. Jira 写操作必须先设计最小权限、审计和 Human-in-the-loop。
6. 确定性解析放入 `log-analysis-core`；MCP 只做协议与安全适配；排障策略放入 Skill。

## 验证

```powershell
uv sync --all-packages --extra dev
uv run pytest -q tests
uv run pytest -q packages/log-analysis-core/tests
uv run pytest -q packages/jira-bug-mcp/tests
uv run pytest -q packages/log-analyzer-mcp/tests
uv build
```

Pull Request 请说明受影响的层：Agent Core、Provider、MCP Tool Contract 或具体
Adapter，并指出兼容性和安全边界是否改变。
