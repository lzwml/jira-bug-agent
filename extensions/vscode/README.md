# BugAgent for VS Code

BugAgent 的内部 VS Code 客户端。它支持本地 Case 与 Jira 会话、工具进度、
Evidence 路径跳转，以及超大日志的只读分段查看。

开发时打开 jira-bug-agent 工作区，执行“BugAgent: 分析本地 Case”即可。
插件会自动运行 uv run bug-agent-api。连接已有服务时，请配置
bugAgent.serverUrl，并通过“BugAgent: 设置 API Key”保存密钥。

远程服务地址必须使用 HTTPS；远程文件路径暂不支持本机跳转。
