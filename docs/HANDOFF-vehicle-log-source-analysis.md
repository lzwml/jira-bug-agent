# 车机日志源码分析交接文档

更新时间：2026-09-04

## 状态：已完成

已阅读 `G:\work\N60\yocto\src\tcl\cluster\logs` 和 `G:\work\N60\yocto\src\devtools\mobile_log_d` 的全部源码，结论已应用到 Skill 文档中。

## 已确认的关键事实

### 1. `logNN` 是可靠的递增 boot 计数器

**源码证据**：`mobile_log_d/logging.c` → `vlog_bridge_scan_boot_index()` 扫描已有 `logNN` 目录，取 `max_idx + 1`。不依赖时钟。

**结论**：`log00` < `log01` < `log02` 是可靠的递增顺序。Agent 可以直接信任。

### 2. `syslog.log.*` 是 vlog bridge 的格式化业务日志

**源码证据**：`tcl/cluster/logs/src/vlog.c` → `vlog_output()` 通过 socket 发送到 `mobilelogd_vlog`，`mobile_log_d/logging.c` → `vlog_bridge_write_log()` 接收并格式化落盘。

**结论**：不是 ftrace 或传统 syslog。格式为 `[timestamp(wall)][uptime(monotonic)][level][seq][module][submodule][PID][file:line func]message`。

### 3. 文件名中的时间戳 ≠ 日志内容时间

**源码证据**：`vlog_bridge_get_file_timestamp()` 取的是 `stat().st_mtime`（文件最后修改/压缩时间），不是日志内容时间。

**结论**：设备时钟未同步时，文件名时间戳可能完全错误（如 1970 年）。但同一 boot 内 `uptime` 字段（CLOCK_MONOTONIC）始终可靠。

### 4. VLOG Bridge 需要显式启动

**源码证据**：`mobilelog.c` → `control_handler("bridge_start")` → `maybe_config_msg()` → `try_start_vlog_bridge_receiver()`。

**结论**：Bridge 不是随 `mobile_log_d` 自动启动的。如果从未触发 `bridge_start`，`syslog.log.*` 将不存在。

### 5. MCU 日志路由独立

**源码证据**：`logging.c` → `vlog_bridge_write_log()` 中 `strncmp(msg->module, "MCU", 3) == 0` → `g_mculog_bridge`。

**结论**：MCU 日志不走 `logNN` 目录，直接落在 `/log/Mcu_Log/`。

## 已更新的 Skill 文档

| 文件 | 更新内容 |
|------|---------|
| `references/yocto-vlog-design.md` | **新建** — 完整的 Yocto 侧 vlog/mobile_log_d 设计文档 |
| `references/sos-archive-selection.md` | 重写 — 源码验证的 `logNN` 可信度、`syslog.log.*` 格式、`mblog_history` 锚点、分层解压策略 |
| `references/linux-vm.md` | 补全 — `syslog.log.*` 格式说明、`mblog_history` 锚点、`logNN` 可信度说明 |
| `references/clock-domains.md` | 补充 — vlog 双时间戳的源码证据（CLOCK_REALTIME vs CLOCK_MONOTONIC） |
| `references/aplog-archive-selection.md` | 修复 — `unreliable_device_clock` 不再立即全量解压 |
| `SKILL.md` (mtk-ivi-log-analysis) | 更新 — 引用新 reference，修正 `logNN` 可信度，修正 `unreliable_device_clock` 策略 |
| `../android-log-triage/SKILL.md` | 更新 — 补充 `mblog_history` 锚点、修正 `unreliable_device_clock` 策略 |

## 核心策略变更

```
之前：
  路径时间不可靠 → prepare_case 全量解压

现在：
  路径时间不可靠
    → 使用 logNN 递增顺序（可靠，不依赖时钟）
    → 对每个候选目录只解压 syslog.log.0001 做内容探测
    → 用 uptime 字段（CLOCK_MONOTONIC）做同一 boot 内排序
    → 只在内容探测无法确定任何 round 时间范围时才全量解压
```

## 待完成

### Android 侧源码（未分析）

`G:\work\N60\b\_android\vendor\mediatek\proprietary\external\mobile_log_d` 和 `G:\work\N60\b\_android\vendor\mediatek\proprietary\packages\apps\MTKLogger` 尚未分析。需要确认：

- APLog 的 `__NN` 编号语义
- APLog 导出时的组包逻辑
- `boot__normal` 的保留策略
- MTKLogger 应用的触发流程

## 已完成标准

- [x] 形成从 Yocto 业务模块到 vlog 到 mobile_log_d bridge 到落盘的完整调用链
- [x] `logNN`、`syslog.log.NNNN`、时间戳字段的语义均有源码证据
- [x] 明确哪些信号可以识别同一 Boot（`logNN` 递增、`mblog_history`、`uptime_ms`）
- [x] 更新 Agent 的归档选择策略与 Skill 文档
- [x] 不能因为文件名时间不可信就自动全量解压