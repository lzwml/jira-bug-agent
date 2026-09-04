# MTK Linux VM and Peripheral Evidence Reference

> 源码验证。基于 `mobile_log_d/logging.c`、`tcl/cluster/logs/vlog.c` 的实际代码。完整设计文档见 `references/yocto-vlog-design.md`。

## Boot rounds and core streams

### `Linux_Log/logNN` 目录

`logNN` 是可靠的递增 boot 计数器（源码验证：`vlog_bridge_scan_boot_index()` 扫描已有目录取 `max_idx + 1`）。`log00` < `log01` < `log02` 的递增顺序始终可靠，不依赖设备时钟。

**事故定位**：事故可能触发 reboot，因此事故日志通常在 `log(N-1)` 而非 `logN`（当前启动）。搜索时优先匹配内容时间范围，而非默认选择最新或最早目录。

### 核心日志流

| 文件 | 来源 | 说明 |
|------|------|------|
| `syslog.log.NNNN.*.log.gz` | **vlog bridge** | Yocto 业务模块日志（IPCL/Media/Network/Screen/PowerManager/Update），格式为 `[timestamp][uptime][level][seq][module][submodule][PID][file:line func]message`。详见 `references/yocto-vlog-design.md` |
| `main_log.log.*` | Android logd | Framework、SystemService、app 日志 |
| `kernel_log.log.*` | kernel kmsg | 驱动、内存、调度、panic、watchdog |
| `events_log.log.*` | Android logd | 结构化事件标签 |
| `bsp_log.log.*` | ftrace bsp | BSP tracepoint 流（boot-relative 秒数，非 wall clock） |
| `scp_log.log.*` | `/dev/scp` | Sensor Control Processor 固件日志 |
| `nebula_hypervisor_log.log.*` | `/dev/thyp-log-dev0` | VM/vCPU 调度和生命周期 |
| `atf_log.log.*` | `/proc/atf_log/atf_log` | 可信固件和 SMC 访问 |
| `bootprof` | — | 启动性能分析 |
| `pl_lk` | — | preloader/LK 日志 |
| `reboot-reason` | — | 重启原因（panic/watchdog/正常关机） |
| `properties` | — | 系统属性快照 |
| `mblog_history` | `mobile_log_d` 自身 | 运行日志，包含 `=====MOBILELOG START=======` 和 `log dir:` 记录 |

### `mblog_history` — 最重要的 boot 身份锚点

路径：`/data/misc/mblog/mblog_history`（在 SOS 归档中可能位于 `data/` 目录下）

`mobile_log_d` 自身的运行日志，格式为 `YYYY:MM:DD HH:MM:SS message(PID)`。关键标记：

- `=====MOBILELOG START=======` — 每次进程启动后的第一条日志
- `log dir: /log/Linux_Log/logNN/` — 目录创建记录，精确匹配 `logNN` 与真实 boot 顺序
- 配置变更、错误等

**Agent 用法**：搜索 `mblog_history` 中的 `log dir:` 可以精确确定每个 `logNN` 对应的 boot 身份，比文件名时间戳或 `logNN` 编号推断更可靠。

### `syslog.log.*` 格式

`syslog.log.*` 不是 ftrace 或传统 syslog——它是 vlog bridge 从业务模块收到的格式化日志：

```
[2026-09-04 10:30:45.123][123.456][I][42][IPCL][Stats][PID:1234][ipcl_stats.c:89 ipcl_report]tx data rate: 1234 Bps
 ^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^ ^^ ^^^^ ^^^^^^ ^^^^^^ ^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^
 wall clock timestamp      uptime   lv seq  module submod  PID       file:line func                 message
```

- `timestamp` 来自 `CLOCK_REALTIME`（受校时影响）
- `uptime` 来自 `CLOCK_MONOTONIC`（同一 boot 内始终可靠）
- `level` 缩写：E=ERROR, W=WARNING, I=INFO, D=DEBUG, V=VERBOSE
- `module` 为业务模块名：IPCL、Media、Network、Screen、PowerManager、Update、MCU 等

---

## SCP, hypervisor, ATF and MCU

- 将 SCP 温度和传感器采样视为测量值，需要有阈值和症状关联才能作为故障证据。
- 使用 hypervisor 日志建立 VM/vCPU 生命周期、饥饿或重置证据，仅在确认受影响的 VM 和事故窗口后。
- ATF `deny access` 消息表示被拒绝的安全监控调用；需要确认调用者、VM 和功能影响后，才能将其视为原因。
- MCU 日志可能同时携带 wall time 和本地计数器。保留模块、级别和电源/IPC 状态转换。

---

## CAN, OTA, PKI and application logs

- 对于 CAN ASC，从头部确认时间戳是相对还是绝对。分析请求的 ID/信号和事故窗口；高频本身不是错误。
- 对于 OTA/HMI，重构操作状态机：请求、下载、验证、安装、重启和结果报告。找到第一个失败的转换。
- 对于 PKI，脱敏令牌、证书、VIN 和密钥材料。只报告操作和错误类别，不暴露完整敏感值。
- 对于应用日志，将应用故障连接到 IPC/设备/服务转换，而不是对通用 `ERROR` 字符串做排名。

---

## SOS archive boot round selection

SOS/TBox 归档的完整选择策略见 `references/sos-archive-selection.md`（源码验证版本）。Yocto 侧日志系统的完整设计见 `references/yocto-vlog-design.md`。

关键原则：
- `logNN` 是可靠的递增序列，**不要**选择最高编号作为默认
- 事故可能触发 reboot，事故日志在 `log(N-1)` 而非 `logN`
- `mblog_history` 是验证 boot 身份的最可靠锚点
- `syslog.log.*` 是 vlog bridge 的格式化业务日志，不是 ftrace
- 文件名中的时间戳是 `st.st_mtime`（轮转/压缩时刻），不是日志内容时间