# TODO：SOS 归档按时间选择 → 方案二选一

更新时间：2026-09-03

## 背景

当前 `inspect_archive` 的 `time_range` 参数通过 `select_sos_time_range()` 在工具层硬编码了 SOS 归档的目录结构（`Linux_Log/logNN/`、`Mcu_Log/`、`can_log/` 等），与已有的 `select_aplog_time_range()` 处于同一抽象层级。这种模式的问题是每新增一种归档格式就要改代码。

## 两种方案

### 方案 A：工具层做格式识别（当前做法）

`inspect_archive` 的 `time_range` 参数自动识别归档格式，Agent 传入 `time_range` 即可拿到正确子集。

**优点：**
- Agent 侧简单，一次调用就能拿到筛选结果
- 减少 Agent 的 token 消耗（不用传全部成员列表）

**缺点：**
- 每新增一种归档格式就要改代码、发版
- 格式识别逻辑和业务耦合在工具层

### 方案 B：工具只返回清单，Agent/Skill 做选择

`inspect_archive` 不做时间过滤，只返回全部 `member_id` + `member_path`。Agent 按 Skill 文档指导，自己从路径中解析时间戳，选出需要的 `member_id` 传给 `extract_archive_members`。

**优点：**
- 不需要改代码，只更新 Skill 文档就能支持新格式
- 工具层保持纯粹，"时间窗口选择"是分析策略而非工具能力

**缺点：**
- 大归档（几百个成员）全部返回会消耗大量 token
- 需要 Skill 文档足够精确，Agent 才能正确解析时间戳
- 对 Agent 的推理能力要求更高

## 待讨论

1. 是否接受方案 B 的 token 消耗？大归档的成员列表可能几百条，每条包含 `member_id`、`member_path`、`size_bytes` 等字段
2. 是否需要工具层提供一个轻量的"分组摘要"（如按 `Linux_Log/logNN` 分组，每组给出最早/最晚时间戳），既不暴露全部成员，又给 Agent 足够信息做选择？
3. 如果选方案 B，当前已实现的 `select_sos_time_range()` 是保留作为备用还是移除？