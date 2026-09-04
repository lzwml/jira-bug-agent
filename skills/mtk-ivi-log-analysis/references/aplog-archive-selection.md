# APLog Incident-Time Archive Selection

> 源码验证。Android 侧设计文档见 `references/android-mobile-log-design.md`。

## APLog 文件命名

```
APLog_2025_0101_080037__9.tar.gz
      ^^^^ ^^^^ ^^^^^^  ^
      year mmdd HHMMSS  __NN（boot round 序号，双下划线）
```

| 字段 | 来源 | 可信度 |
|------|------|--------|
| `YYYY_MMDD_HHMMSS` | 设备 wall clock | **绝对时间可能不可靠**（时钟未同步时），但**同一设备内的相对顺序可信** |
| `__NN` | boot round 计数器 | **始终可靠**（递增计数器，不依赖时钟） |

### `__NN` 的语义

`__NN` 是 boot round 序号，类似于 SOS 归档中的 `logNN`。`__1` < `__2` < `__3` 是可靠的递增顺序。事故可能触发 reboot，因此事故日志通常在 `__(N-1)` 而非 `__N`（当前启动）。

---

## 选择策略

### 第一步：获取归档清单

调用 `inspect_archive`（不带 `time_range`）获取完整成员清单和 `time_groups`。

### 第二步：按 `__NN` 分组识别 boot round

列出所有 APLog 成员，按 `__NN` 编号分组。`__NN` 是可靠的 boot round 计数器，Agent 可以直接信任。

**关键**：即使 `time_reliability` 为 `"unreliable_device_clock"`，`__NN` 的递增顺序仍然可信。时钟错误只影响文件名中的日期时间部分，不影响 `__NN` 计数器。

### 第三步：选择候选 boot round

**不要默认选择最高编号的 `__NN`。** 事故可能触发 reboot，事故日志在 `__(N-1)` 而非 `__N`。

1. 列出所有 `__NN` 编号，按 NN 递增排序
2. 如果事故时间已知（来自 Jira 描述）：
   - 检查 `time_groups[*].earliest_path_time_reliability`：
     - **`"reliable"`**：使用 `inspect_archive(time_range=..., time_neighbor_count=1)` 直接选择事故前后相关成员
     - **`"unreliable_device_clock"`**：文件名中的绝对时间不可信，但可以：
       - 用 `__NN` 编号确定 boot 顺序
       - 对每个候选 `__NN` round，本次只解压 `main_log` 的最小编号文件，用 `extract_timeline` 确认实际内容时间范围
       - 选择内容时间范围覆盖事故窗口的 round
3. 如果事故时间未知：
   - 解压所有 `__NN` round 的 `main_log` 最小编号文件
   - 用 `build_index` + `extract_timeline` 建立每个 round 的时间画像
   - 根据 Issue 描述的症状匹配对应 round

### 第四步：增量解压与索引

对于选定的 boot round：
1. 用 `extract_archive_members(member_id)` 解压该 round 的成员
2. `build_index` 建立索引
3. 先只解压 `main_log`、`kernel_log`、`events_log`
4. 证据不足时逐步扩展：`crash_log` → `radio_log` → ANR/AEE/Dropbox

### 第五步：`boot__normal` 的特殊处理

`boot__normal` 是早期 boot 日志的保留目录。当事故发生在 boot 早期阶段时：
1. 检查 `boot__normal` 中是否有 `main_log`、`kernel_log` 等
2. `boot__normal` 的保留/覆盖策略是产品配置，Agent 应通过内容时间戳确认其覆盖范围，而非假设其一定包含事故时间

---

## 不可靠时间戳的处理

当 `time_reliability: "unreliable_device_clock"` 时：

- **仍然信任 `__NN`**：`__NN` 是递增计数器，不依赖时钟，始终可靠
- **仍然信任文件名时间的相对顺序**：同一设备上，`APLog_2025_0101_080037` 一定在 `APLog_2025_0101_090000` 之前，即使绝对时间错了
- **不依赖文件名绝对时间做文件选择**：不直接用 `time_range` 筛选
- **不依赖文件名时间与日志内容时间比较**：内容时间戳同样受时钟影响
- **用内容探测建立实际时间范围**：解压 `main_log` 的最小编号文件，用 `extract_timeline` 确定 round 的实际覆盖范围
- **只在内容探测也无法确定时才全量解压**：`prepare_case` 是最后手段

### 为什么 `__NN` 始终可信

APLog 的 `__NN` 由 C 层的 `mobile_log_d` 轮转逻辑生成，类似于 SOS 归档中 `logNN` 的 `vlog_bridge_scan_boot_index()`——都是递增计数器，不依赖设备时钟。时钟错误可能让文件名日期变成 1970 年，但 `__NN` 的递增顺序不会变。

---

## Gap-filling：当前 round 不覆盖事故时间

当在一个 `__NN` boot round 的日志中搜索事故时间窗口无结果时：

1. **不要直接报 `missing_evidence`**。先用 `extract_timeline` 确认当前 round 的实际时间跨度。
2. **扩展检查相邻 round**：检查 `__(N-1)`（前一个，可能是事故触发点）和 `__(N+1)`（后一个，可能是事故后的恢复启动）。
3. 对每个候选 round 重复搜索，直到找到事故时间窗口或所有可用 round 耗尽。
4. 只有在所有可用 boot round 都检查完毕后，才在 `missing_evidence` 中报告：
   - 检查了哪些 `__NN` round
   - 每个 round 的实际日志时间跨度
   - 事故时间与各 round 的关系

---

## 与 SOS 归档的对应关系

| 方面 | APLog | SOS/TBox |
|------|-------|----------|
| Boot round 标识 | `__NN`（双下划线） | `logNN`（log 前缀） |
| 可靠性 | 始终可靠 | 始终可靠 |
| 业务日志 | `main_log`（Android logd） | `syslog.log.*`（vlog bridge） |
| 时间戳来源 | Android logcat wall clock | CLOCK_REALTIME + CLOCK_MONOTONIC |
| VLOG Bridge 日志 | **无**（Android 版无此功能） | **有** |
| MCU 日志 | 无 | `/log/Mcu_Log/`