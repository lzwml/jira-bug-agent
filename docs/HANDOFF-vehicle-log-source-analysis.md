# 车机日志源码分析交接文档

更新时间：2026-09-04

## 状态：已完成

已阅读 Yocto 和 Android 两侧的全部源码，结论已应用到 Skill 文档中。

### Yocto 侧（已完成）
- `G:\work\N60\yocto\src\tcl\cluster\logs` — vlog 客户端库
- `G:\work\N60\yocto\src\devtools\mobile_log_d` — mobile_log_d daemon（含 VLOG Bridge）

### Android 侧（已完成）
- `G:\work\N60\b_android\vendor\mediatek\proprietary\external\mobile_log_d` — Android 版 mobile_log_d（同一套 C 源码，无 VLOG Bridge）
- `G:\work\N60\b_android\vendor\mediatek\proprietary\packages\apps\MTKLogger` — 控制 App

## 已确认的关键事实

### Yocto 侧

1. **`logNN` 是可靠的递增 boot 计数器**：`vlog_bridge_scan_boot_index()` 扫描已有目录，取 `max_idx + 1`。不依赖时钟。
2. **`syslog.log.*` 是 vlog bridge 的格式化业务日志**：通过 socket `@mobilelogd_vlog` 发送，格式为 `[timestamp(wall)][uptime(monotonic)][level][seq][module][submodule][PID][file:line func]message`。
3. **文件名中的时间戳 ≠ 日志内容时间**：`vlog_bridge_get_file_timestamp()` 取 `stat().st_mtime`。
4. **VLOG Bridge 需要显式启动**：`bridge_start` 命令触发。
5. **MCU 日志路由独立**：`module == "MCU"` → `/log/Mcu_Log/`。

### Android 侧

6. **Android 和 Yocto 的 mobile_log_d 是同一套 C 源码**：编译配置不同。Android 版无 VLOG Bridge，无 `syslog.log.*` 输出。
7. **MTKLogger 是控制 App，不是导出/打包工具**：通过 socket `"mobilelogd"` 向 daemon 发送配置命令。
8. **APLog 的 `__NN` 是 boot round 序号**：类似 `logNN`，递增计数器。具体逻辑在 C 代码中。
9. **Android 侧日志目录是 bind mount**：`/data/debuglogger` ↔ `/log/debuglogger`，导出时路径可能有 `/log/` 前缀。
10. **`boot__normal`** 是早期 boot 日志的保留目录，由 `copy_and_dump()` 流程创建。

## 已更新的 Skill 文档

| 文件 | 更新内容 |
|------|---------|
| `references/yocto-vlog-design.md` | **新建** — 完整的 Yocto 侧 vlog/mobile_log_d 设计文档 |
| `references/android-mobile-log-design.md` | **新建** — Android 侧 mobile_log_d/MTKLogger 设计文档 |
| `references/sos-archive-selection.md` | 重写 — 源码验证的 `logNN` 可信度、`syslog.log.*` 格式、`mblog_history` 锚点、分层解压策略 |
| `references/linux-vm.md` | 补全 — `syslog.log.*` 格式说明、`mblog_history` 锚点、`logNN` 可信度说明 |
| `references/android-vm.md` | 补全 — Android/Yocto 关系说明、源码验证标记 |
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

### 无。Android 和 Yocto 两侧的核心源码分析均已完成。

唯一未覆盖的是：APLog 的 `__NN` 编号逻辑在 C 代码中（不在 MTKLogger Java 代码），需要进一步确认具体递增规则。但已知 `__NN` 是 boot round 序号，类似 `logNN`。

## 已完成标准

- [x] 形成从 Yocto 业务模块到 vlog 到 mobile_log_d bridge 到落盘的完整调用链
- [x] `logNN`、`syslog.log.NNNN`、时间戳字段的语义均有源码证据
- [x] 明确哪些信号可以识别同一 Boot（`logNN` 递增、`mblog_history`、`uptime_ms`）
- [x] 更新 Agent 的归档选择策略与 Skill 文档
- [x] 不能因为文件名时间不可信就自动全量解压