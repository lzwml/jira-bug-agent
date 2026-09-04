# SOS Archive — Time-Aware Selection

> 源码验证。基于 `mobile_log_d/logging.c` 和 `tcl/cluster/logs` 的实际代码。完整设计文档见 `references/yocto-vlog-design.md`。

SOS/TBox 归档的目录结构（源码验证）：

```
SOS_Archive/
├── Linux_Log/
│   ├── log00/              ← 最早一次 boot（递增计数器，可靠）
│   │   ├── syslog.log.0001.YYYYMMDD_HHMMSS.log.gz   ← vlog bridge 输出
│   │   ├── syslog.log.0002.YYYYMMDD_HHMMSS.log.gz
│   │   ├── main_log.log.0001.YYYYMMDD_HHMMSS.log.gz  ← Android logd
│   │   ├── kernel_log.log.0001.YYYYMMDD_HHMMSS.log.gz
│   │   └── ...
│   ├── log01/              ← 第二次 boot
│   │   └── ...
│   └── file_tree.txt       ← mobile_log_d 创建的目录清单
├── Mcu_Log/
│   └── mculog.log.NNNN.YYYYMMDD_HHMMSS.log.gz
├── can_log/   ota/   pki/   data/
```

---

## 关键事实（源码验证）

### `logNN` 是可靠的递增 boot 计数器

```c
// logging.c: vlog_bridge_scan_boot_index()
// 扫描已有 logNN 目录，取 max_idx + 1
int vlog_bridge_scan_boot_index(const char *parent_dir) {
    // 遍历所有 "logNN" 目录，返回 max_idx + 1
}
```

**结论**：`log00` < `log01` < `log02` 是可靠的递增顺序。Agent 可以直接信任 `logNN` 编号作为 boot 顺序。

### 文件名中的时间戳 ≠ 日志内容时间

```
syslog.log.0001.20260904_103045.log.gz
             ^^^^  ^^^^^^^^^^^^^^^^
             │     └── st.st_mtime（文件最后修改时间，轮转/压缩时刻）
             └── current_file_index（同一 boot 内递增，跨 boot 从 1 重新开始）
```

- 文件名中的时间戳来自 `vlog_bridge_get_file_timestamp()` → `stat().st_mtime`
- 在设备时钟未同步时，这个时间可能完全错误（如 1970 年）
- 日志内容中的时间戳来自 `msg->timestamp_ms`（CLOCK_REALTIME），同样受时钟影响
- 日志内容中的 `uptime` 来自 `msg->uptime_ms`（CLOCK_MONOTONIC），在同一 boot 内始终可靠

### `syslog.log.*` 是 vlog bridge 的格式化输出

`syslog.log.*` 不是 ftrace 或传统 syslog——它是 vlog 客户端库通过 socket 发送到 mobilelogd bridge 的业务日志。格式为：

```
[2026-09-04 10:30:45.123][123.456][I][42][IPCL][Stats][PID:1234][ipcl_stats.c:89 ipcl_report]tx data rate: 1234 Bps
```

### 路由规则：MCU 日志不走 `logNN`

```c
// logging.c: vlog_bridge_write_log()
if (strncmp(msg->module, "MCU", 3) == 0)
    → /log/Mcu_Log/mculog.log.*     // MCU 日志独立目录
else
    → /log/Linux_Log/logNN/syslog.log.*  // 其他所有模块
```

---

## 选择策略

### 第一步：获取归档清单

调用 `inspect_archive`（不带 `time_range`）获取完整成员清单和 `time_groups`。

### 第二步：识别 boot round 目录

使用 `time_groups` 中的 `path_prefix` 识别 `Linux_Log/logNN/` 目录。`logNN` 编号是可靠的递增序列。

### 第三步：选择候选 boot round

**不要默认选择最高编号的 `logNN` 目录。** 事故可能触发 reboot，事故日志在 `log(N-1)` 而非 `logN`（当前启动）。

选择策略：

1. 列出所有 `logNN` 目录，按 NN 递增排序
2. 如果事故时间已知（来自 Jira 描述）：
   - 对每个候选 `logNN` 目录，先用 `path_prefix` 选择该目录的 `syslog.log.*` 成员
   - 从最早的 `logNN` 开始，每个目录只解压 `syslog.log` 的最小索引文件（通常 `0001`）
   - 用 `extract_timeline` 确认该 round 的实际内容时间范围
   - 选择内容时间范围覆盖事故窗口的 round
3. 如果事故时间未知：
   - 解压 **所有** `logNN` 目录的 `syslog.log.*` 最小编号文件
   - 用 `build_index` + `extract_timeline` 建立每个 round 的时间画像
   - 根据 Issue 描述的症状（如"今天早上启动黑屏"）匹配对应 round
4. 对于 reboot 分析，始终包含 post-reboot round 的 `reboot-reason`、`pl_lk` 和 `bootprof`

### 第四步：`time_reliability` 的处理

检查 `time_groups[*].earliest_path_time_reliability`：

- **`"reliable"`**：文件名时间戳在 2024-2030 范围。可以使用 `time_range` 参数辅助筛选，但仍需用内容时间戳验证。
- **`"unreliable_device_clock"`**：文件名时间戳不可信（年份异常或为 0）。

**重要**：即使 `time_reliability` 为 `"unreliable_device_clock"`，仍应**优先使用 `logNN` 递增顺序 + 内容探测**来选择候选目录，而不是直接调用 `prepare_case` 全量解压。理由：

- `logNN` 编号不依赖时钟，是可靠的递增计数器
- 可以只解压每个候选目录的 `syslog.log.0001.*` 来确认内容时间范围
- 用 `uptime_ms` 字段进行同一 boot 内的相对时间排序（不受时钟错误影响）

**只有在以下情况才使用 `prepare_case` 全量解压：**
- 候选目录的内容探测无法确定任何 round 的实际时间范围
- 日志内容时间戳也全部不可信（如全部为 0）
- 事故涉及跨 boot 的复杂时序分析

### 第五步：增量解压与索引

对于选定的 boot round：
1. 用 `path_prefix` 选择该目录的所需成员
2. `extract_archive_members` → `build_index`
3. 先只解压 `syslog.log.*`（业务日志）+ `main_log.log`（Android logd）+ `kernel_log.log`
4. 证据不足时逐步扩展：`events_log` → `bsp_log` → `scp_log` → `nebula_hypervisor_log`

### 第六步：使用 `mblog_history` 锚点

`mblog_history` 是 mobile_log_d 自身的运行日志，包含：
- `=====MOBILELOG START=======` — 每次启动标记
- `log dir: /log/Linux_Log/logNN/` — 目录创建记录

搜索 `mblog_history` 中的 `log dir:` 可以精确匹配每个 `logNN` 与 boot 顺序，是比文件名时间戳更可靠的 boot 身份锚点。

---

## Gap-filling：当前 round 不覆盖事故时间

当在一个 boot round 的日志中搜索事故时间窗口无结果时：

1. **不要直接报 `insufficient_evidence`**。先用 `extract_timeline` 确认当前 round 的实际时间跨度。
2. **扩展检查相邻 round**：事故可能发生在前一个 round（崩溃导致重启）或后一个 round（设备多次启动）。
3. 对每个候选 round 重复搜索，直到找到事故时间窗口或所有可用 round 耗尽。
4. 只有在所有可用 boot round 都检查完毕后，才在 `missing_evidence` 中报告：
   - 检查了哪些 boot round（`logNN` 编号）
   - 每个 round 的实际日志时间跨度
   - 事故时间与各 round 的关系

---

## 与其他日志域的关系

- **APLog**：Android 侧日志，来自 `mobilelog/APLog_*`，使用 `references/aplog-archive-selection.md` 的策略
- **Mcu_Log**：MCU 日志，独立于 `logNN` 目录，直接在 `/log/Mcu_Log/` 下
- **can_log/ota/pki/data**：独立目录，不在 `logNN` 下