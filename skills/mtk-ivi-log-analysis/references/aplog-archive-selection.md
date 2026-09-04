# APLog Incident-Time Archive Selection

APLog 归档的文件名时间戳（如 `APLog_2025_0101_080037__9`）受设备时钟错误影响，可能不可靠。

## 选择策略

1. 先调用 `inspect_archive`（不带 `time_range`）查看成员清单和 `time_groups`。
2. 检查 `time_groups[*].earliest_path_time_reliability` 和 `latest_path_time_reliability`：
   - `"unreliable_device_clock"`：文件名时间戳不可信，年份 < 2024。**不要立即调用 `prepare_case`**。改用 `inspect_archive` 查看成员清单，利用 `__NN` 或 `boot__normal` 等结构化标记识别候选 boot round，然后手动选择 `member_id` 提取关键成员。只有在所有候选 round 的成员都无法确定内容时间范围时才作为最后手段调用 `prepare_case`。
   - `"reliable"`：文件名时间戳在 2024-2030 范围。如果事故时间明确，使用 `inspect_archive(time_range=..., time_neighbor_count=1)` 选择事故前后相关成员，然后用 `extract_archive_members` 解压、`build_index` 建立索引。
3. 每个成员的 `path_timestamp_reliability` 字段单独标记了该成员时间戳的可信度。
4. 如果 `prepare_case` 因预算限制跳过了某些归档，使用 `inspect_archive` 手动浏览成员并用 `extract_archive_members(member_id)` 选择性解压。

## 不可靠时间戳的处理

当 `time_reliability: "unreliable_device_clock"` 时：
- 不依赖 APLog 文件名时间戳选择文件
- 不依赖 APLog 文件名时间戳推断 boot round
- 不依赖 APLog 文件名时间戳与日志内容时间戳比较
- 事故时间只作为搜索线索，在全部日志中搜索验证
- **优先使用结构化标记**（如 `boot__normal`、`__NN` 编号、logcat 内容时间戳）来建立 boot 身份和时间范围

## Gap-filling: 当前 boot round 日志不覆盖事故时间时

当在某个 boot round 的日志中搜索事故时间窗口无结果时：

1. **不要直接报 missing_evidence**。先用 `extract_timeline` 确认当前 round 日志的实际时间跨度。
2. **检查相邻的 boot round**：APLog 归档通常包含多个 boot round（`__1`～`__9`），事故可能发生在前一个 round（崩溃导致重启）或后一个 round（设备多次启动）。
3. 对每个候选 round 重复搜索，直到找到事故时间窗口或所有可用 round 耗尽。
4. 只有在所有可用 boot round 都检查完毕后仍然找不到时，才在 `missing_evidence` 中报告：
   - 检查了哪些 boot round
   - 每个 round 的实际日志时间跨度
   - 事故时间与各 round 的关系