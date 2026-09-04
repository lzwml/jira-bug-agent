# Android 侧日志系统设计（源码验证）

基于 `G:\work\N60\b_android\vendor\mediatek\proprietary\external\mobile_log_d` 和 `MTKLogger` 的源码分析。最后更新：2026-09-04。

---

## 总体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│  Android 侧                                                          │
│                                                                      │
│  ┌──────────────────────┐    ┌──────────────────────────────────┐   │
│  │ MTKLogger App         │    │ mobile_log_d (Android 版)         │   │
│  │ (com.debug.loggerui)  │    │ /system_ext/bin/mobile_log_d     │   │
│  │                       │    │                                  │   │
│  │ 控制 mobile_log_d     │    │ 收集 Android logd (main/radio/   │   │
│  │ 的启停、配置、路径    │    │ events/system/...) + kernel +   │   │
│  └──────────┬───────────┘    │ SCP/SSPM/ADSP/ATF/BSP/...        │   │
│             │                │                                  │   │
│             │ socket         │ 落盘到 /log/debuglogger/         │   │
│             │ "mobilelogd"   │ mobilelog/ 目录下                 │   │
│             │                │                                  │   │
│             └────────────────┤ ← 同一套 C 代码，不带 VLOG Bridge │
│                              └──────────────────────────────────┘   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Android 版 mobile_log_d 与 Yocto 版的关系                     │   │
│  │                                                              │   │
│  │ 同一套 C 源码，编译配置不同：                                   │   │
│  │ - Android: Android.bp, 无 VLOG Bridge, 无 vlog_bridge_*.h    │   │
│  │ - Yocto: Makefile, 带 VLOG Bridge, -I./tcl/cluster/logs/     │   │
│  │                                                              │   │
│  │ 两个版本通过同一个控制 socket 通信：                            │   │
│  │ MTKLogger → socket("mobilelogd") → mobile_log_d (Android)     │   │
│  │ vlog_init → socket("mobilelogd") → mobile_log_d (Yocto)       │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Android mobile_log_d 启动流程

### init.rc

```init
# 目录创建
on post-fs-data
    mkdir /data/log_temp 0755 system system
    mkdir /data/misc/mblog 0755 system system
    mkdir /data/debuglogger 0770 shell log

# 服务定义
service mobile_log_d /system_ext/bin/mobile_log_d
    class main
    user root

# Boot 时挂载
on boot
    mount none /log/debuglogger /data/debuglogger bind    ← bind mount!
    copy /sys/fs/pstore/console-ramoops-0 /data/debuglogger/
    mkdir /log/anr 0775 system system
    mkdir /log/tombstones 0775 system system
    mkdir /log/aee_exp 0777 root root
    ...
    mount none /data/anr /log/anr bind
    mount none /data/tombstones /log/tombstones bind
    mount none /data/aee_exp /log/aee_exp bind
```

### 关键目录结构

```
/data/debuglogger/          ← 实际存储位置（Android 侧）
  └── mobilelog/            ← APLog 在此
        ├── APLog_YYYY_MMDD_HHMMSS__NN.tar.gz  ← APLog 归档
        ├── APLog_YYYY_MMDD_HHMMSS__NN.curf     ← 当前正在写入的
        └── boot__normal/                        ← 早期 boot 日志

/log/debuglogger/           ← bind mount → /data/debuglogger/
/log/anr/                   ← bind mount → /data/anr/
/log/tombstones/            ← bind mount → /data/tombstones/
/log/aee_exp/               ← bind mount → /data/aee_exp/
```

---

## MTKLogger App

### 定位

MTKLogger 是一个**控制 App**，不是一个导出/打包工具。它通过 socket 向 `mobile_log_d` 发送配置命令。实际的日志收集、轮转、压缩由 `mobile_log_d` daemon 完成。

### 连接方式

```java
// LogFactory.java
private static MobileLog sMobileLog = new MobileLog(
    new LogSocketConnection("mobilelogd"),  // ← Unix Domain Socket
    LogType.MOBILE_LOG
);
```

Socket 名称 `"mobilelogd"` 对应 `mobile_log_d` daemon 的 `/run/mobilelogd` 控制 socket（Android 的 `LocalSocket` 自动处理 abstract namespace）。

### 控制命令

| 命令 | 说明 |
|------|------|
| `deep_start` | 启动日志收集（含 boot config 写入） |
| `deep_stop` | 停止日志收集（含 boot config 写入） |
| `set_storage_path,<path>` | 设置日志存储路径 |
| `logsize=<N>` | 设置日志总大小（MB） |
| `autostart=<0\|1>` | 设置开机自启动 |
| `sublog_<Name>=<0\|1>` | 启用/禁用子日志类型 |
| `get_sublog_list` | 获取子日志列表 |

### 子日志类型

从 `get_sublog_list` 返回的格式 `AndroidLog_1;KernelLog_1;SCPLog_1;...`：

| 子日志 | 名称 | 说明 |
|--------|------|------|
| AndroidLog | Android 主日志 | logcat main/buffer |
| KernelLog | Kernel 日志 | kmsg |
| SCPLog | SCP 固件 | Sensor Control Processor |
| ATFLog | ATF 日志 | Arm Trusted Firmware |
| BSPLog | BSP ftrace | BSP tracepoint |
| SSPMLog | SSPM 日志 | 协处理器 |
| ADSPLog | ADSP 日志 | Audio DSP |
| MCUPMLog | MCUPM | MCU Power Manager |
| WifiDriverLog | Wi-Fi 驱动 | WiFi driver debug |

### MTKLogger 不做什么

MTKLogger **不负责**：
- APLog 文件的命名（`APLog_YYYY_MMDD_HHMMSS__NN`）- 由 C 层的 mobile_log_d 轮转逻辑生成
- 日志归档/压缩 - 由 mobile_log_d 的 `logging.c` 完成
- SOS 日志的导出 - 可能由其他工具或脚本完成
- VLOG Bridge 的管理 - 这是 Yocto 侧独有的功能

---

## APLog 命名格式

```
APLog_2025_0101_080037__9.tar.gz
       ^^^^ ^^^^ ^^^^^^  ^
       year mmdd HHMMSS  __NN (源码验证的 APLog folder/boot round 序号)
```

- `YYYY`：年份（4 位）
- `MMDD`：月日（4 位）
- `HHMMSS`：时分秒（6 位）
- `__NN`：源码验证的 APLog folder/boot round 序号（注意是双下划线）

**`__NN` 的语义**：位于 C 层的 mobile_log_d，不在 MTKLogger Java 代码中。`daemon.c` 调用 `read_folder_index()` 取得持久化 folder index，以 `APLog_%s__%lu/` 生成目录名，再由 `update_folder_list()` 更新目录记录和索引状态；`size_control.c` 实现索引文件的读取与写回。因此 `__NN` 是源码验证的 round 计数器，可用于确定 APLog boot/collection round 顺序。

### 与 SOS 日志的对应关系

```
APLog (Android 侧)              SOS/TBox (Yocto 侧)
─────────────────────          ─────────────────────
APLog_*__N.tar.gz               Linux_Log/logNN/
  ├── main_log                    ├── main_log.log.*.gz
  ├── kernel_log                  ├── kernel_log.log.*.gz
  ├── events_log                  ├── events_log.log.*.gz
  └── ...                         └── ...
                                  syslog.log.*.gz (vlog bridge, Android 侧无)
                                  Mcu_Log/ (独立)
```

---

## Android vs Yocto mobile_log_d 对比

| 特性 | Android 版 | Yocto 版 |
|------|-----------|----------|
| 编译系统 | Android.bp | Makefile |
| 安装路径 | `/system_ext/bin/mobile_log_d` | `/usr/bin/mobile_log_d` |
| 启动方式 | init.rc service | systemd service |
| VLOG Bridge | **无** | **有**（vlog_bridge_thread_loop） |
| syslog.log.* | **无** | **有**（vlog bridge 输出） |
| 控制 socket | `/run/mobilelogd` (同样的) | `/run/mobilelogd` (同样的) |
| 日志目录 | `/data/debuglogger/mobilelog/` | `/log/Linux_Log/logNN/` |
| 源码 | 同一套 C 代码 | 同一套 C 代码 |

**关键结论**：Android 和 Yocto 的 `mobile_log_d` 是**同一套代码**，只是编译配置不同。Yocto 版多了 VLOG Bridge 子系统（接收 `vlog` 库的 socket 日志），Android 版没有这个功能。两者通过同一个控制 socket 协议通信。

---

## Agent 影响

### 1. APLog 的 `__NN` 编号

`__NN` 是源码验证的 round 序号，和 Yocto `logNN` 一样可以确定 round 顺序。但编号只回答先后关系，不回答事故位于哪个 round；同一个事故可能横跨多个 `__NN` rounds，例如事故触发 reboot 后进入下一编号。

### 2. Android 侧没有 `syslog.log.*`

Android 的 `mobile_log_d` 没有 VLOG Bridge，因此 APLog 归档中**不会有** `syslog.log.*` 文件。如果 Agent 在 APLog 中寻找 `syslog.log.*`，应该报告 `missing_evidence`。

### 3. `boot__normal` 目录

`boot__normal` 是早期 boot 日志的保留目录，在 `mobile_log_d` 的 `copy_and_dump()` 流程中创建。其保留/覆盖策略是产品配置，应在分析时通过 `mblog_history` 或 `file_tree.txt` 确认其 boot 身份。

### 4. bind mount 的日志目录

Android 侧的大量日志目录是通过 bind mount 连接的：
- `/data/debuglogger` ↔ `/log/debuglogger`
- `/data/anr` ↔ `/log/anr`
- `/data/tombstones` ↔ `/log/tombstones`

导出时，归档中的路径可能是 `/log/` 前缀，但实际存储位置可能在 `/data/` 下。Agent 不应假设路径前缀的绝对含义。
