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
| `YYYY_MMDD_HHMMSS` | 设备 wall clock | **绝对时间不可靠**（时钟未同步时）。相对顺序也不可靠——时钟回拨、NTP 跳跃、手动校时都可能破坏文件名时间戳的顺序 |
| `__NN` | boot round 计数器 | **推测为递增**（未找到精确生成代码行，但 Android/Yocto 共享同一套 mobile_log_d C 源码，Yocto 侧 `logNN` 已源码验证为 `max_idx+1`）。边界条件（清理日志、恢复出厂、存储满时是否复位）尚未确认 |

### `__NN` 的语义

`__NN` 是 boot round 序号，推测为递增计数器：
- Android 和 Yocto 的 `mobile_log_d` 是同一套 C 源码
- Yocto 侧 `logNN` 已源码验证：`vlog_bridge_scan_boot_index()` 扫描已有目录取 `max_idx + 1`
- `__NN` 的精确生成代码行尚未在 Android C 代码中定位，边界条件（清理日志、恢复出厂、存储满时是否复位）待确认
- 正常使用场景下，`__1` < `__2` < `__3` 可视为递增顺序

事故可能触发 reboot，因此事故日志通常在 `__(N-1)` 而非 `__N`（当前启动）。

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
       - 对每个候选 `__NN` round，用 `probe_archive_members` 读取 `main_log` 成员前缀（不落盘）
       - 从返回的 `content_time_ranges` 获取实际内容时间范围
       - 选择覆盖事故窗口的 round
3. 如果事故时间未知：
   - 用 `probe_archive_members` 读取所有 `__NN` round 的 `main_log` 成员
   - 从返回的 `content_time_ranges` 和 `anchors` 建立每个 round 的时间画像
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

- **不依赖文件名时间戳**：绝对时间不可靠，相对顺序也不可靠（时钟回拨、NTP 跳跃、手动校时都可能改变后续生成的文件名时序）
- **不依赖文件名时间与日志内容时间比较**：两者都受设备时钟影响
- **用 `__NN` 编号作为 boot 排序依据**：推测为递增计数器，正常场景下可用。但需注意 `__NN` 的精确生成逻辑和边界条件（复位、清理等）尚未源码确认
- **用 `probe_archive_members` 建立实际时间范围**：不落盘读取 `main_log` 成员前缀，从返回的 `content_time_ranges` 直接获取时间覆盖范围，无需先解压再 `extract_timeline`
- **只在 probe 也无法确定时才全量解压**：`prepare_case` 是最后手段

### `__NN` 可信度说明

APLog 的 `__NN` 推测为递增计数器，依据：
- Android 和 Yocto 的 `mobile_log_d` 是同一套 C 源码
- Yocto 侧 `logNN` 已源码验证为 `max_idx + 1`（`vlog_bridge_scan_boot_index()`）
- `__NN` 的精确生成代码行尚未在 Android C 代码中定位，边界条件待确认

Agent 在正常场景下可以将 `__NN` 作为 boot 排序依据，但不应将 "始终可靠" 当作毋庸置疑的事实。如果 `__NN` 编号与实际内容时间范围矛盾，优先信任内容探测结果。

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
| 可靠性 | 推测为递增 | 源码验证可靠 |
| 业务日志 | `main_log`（Android logd） | `syslog.log.*`（vlog bridge） |
| 时间戳来源 | Android logcat wall clock | CLOCK_REALTIME + CLOCK_MONOTONIC |
| VLOG Bridge 日志 | **无**（Android 版无此功能） | **有** |
| MCU 日志 | 无 | `/log/Mcu_Log/`