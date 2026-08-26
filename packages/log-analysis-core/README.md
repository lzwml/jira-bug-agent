# log-analysis-core

与 MCP、Agent Runtime 和文件权限无关的确定性日志解析库。

当前提供：

- Android threadtime、Wall Clock、Kernel monotonic 时间戳解析；
- 不跨 Clock Domain 猜测换算的结构化时间结果；
- Android Tag/调查锚点组件识别；
- AVC、Fatal、ANR、Kernel Call Trace 信号分类；
- Evidence、Event、Finding 使用的稳定 ID。

Core 只回答“日志中确定性地出现了什么”，不判断该信号是否是当前 Bug 根因。
文件授权、扫描预算和 Tool Contract 属于 `log-analyzer-mcp`；问题类型的分析顺序
和证据标准属于仓库 `skills/`。

