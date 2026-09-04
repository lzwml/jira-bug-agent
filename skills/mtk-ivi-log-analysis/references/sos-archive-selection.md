# SOS Archive — Time-Aware Selection

SOS/TBox archives contain `Linux_Log/logNN/` boot rounds, `Mcu_Log/`, `can_log/`, `ota/`,
`pki/`, and `data/` directories.

## 选择策略

1. 先调用 `inspect_archive`（不带 `time_range`）查看成员清单和 `time_groups`。
2. 检查 `time_groups[*].earliest_path_time_reliability` 和 `latest_path_time_reliability`：
   - `"reliable"`：使用 `inspect_archive` 的 `time_groups` 识别候选目录前缀，然后用 `path_prefix` 选择特定目录的成员。
   - `"unreliable_device_clock"`：直接调用 `prepare_case` 全部解压。
3. 使用 `time_groups` 中的 `path_prefix` 来确定 `log00`/`log01` 等 boot round 目录。不要默认选择最高编号的 `logNN` 目录——事故往往发生在较早的 round 中。
4. 对于 reboot 分析，始终包含 post-reboot round 的 `reboot-reason`、`pl_lk` 和 `bootprof`。