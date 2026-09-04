# Yocto 侧日志系统设计（源码验证）

基于 `G:\work\N60\yocto\src\tcl\cluster\logs` 和 `G:\work\N60\yocto\src\devtools\mobile_log_d` 的源码分析。最后更新：2026-09-04。

---

## 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│  Yocto/Linux 业务模块                                             │
│  IPCL / Media / Network / Screen / PowerManager / Update ...     │
│  各模块通过 *_log.h 宏调用 vlog API                                │
└────────────────────────────┬─────────────────────────────────────┘
                             │ vlog_output()
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  vlog 客户端库 (tcl/cluster/logs/src/vlog.c)                      │
│  - Unix Abstract Socket DGRAM → @mobilelogd_vlog                  │
│  - 非阻塞 sendto，发送失败入 pending 队列（128条）                  │
│  - 自动启动 mobilelogd bridge（fork + exec）                       │
│  - 双时间戳：CLOCK_REALTIME + CLOCK_MONOTONIC                     │
└────────────────────────────┬─────────────────────────────────────┘
                             │ vlog_msg_t (vlog_bridge_protocol.h)
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  mobile_log_d VLOG Bridge (devtools/mobile_log_d/logging.c)       │
│  - vlog_bridge_thread_loop() 线程                                 │
│  - bind(@mobilelogd_vlog) → recv() → vlog_bridge_write_log()     │
│  - 双通道：g_vlog_bridge (syslog) + g_mculog_bridge (mculog)     │
│  - 按 module 字段路由：MCU* → Mcu_Log, 其他 → Linux_Log          │
│  - 轮转/压缩/空间管理                                             │
└────────────────────────────┬─────────────────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                              ▼
   /log/Linux_Log/logNN/          /log/Mcu_Log/
   syslog.log.NNNN.*.log.gz       mculog.log.N.*.log.gz
```

---

## 通信协议 (vlog_bridge_protocol.h)

### Socket 名称

```c
#define VLOG_BRIDGE_SOCKET_NAME  "mobilelogd_vlog"
```

Unix Abstract Socket（路径以 `\0` 开头），进程退出自动清理，不依赖文件系统。

### 消息结构体

```c
typedef struct {
    uint32_t magic;             // 0x564C4F47 ("VLOG")
    uint32_t sequence;          // 全局递增序列号
    vlog_level_t level;         // 0=ERROR 1=WARNING 2=INFO 3=DEBUG 4=VERBOSE
    pid_t pid;                  // 进程 PID
    pid_t tid;                  // 线程 TID
    uint64_t timestamp_ms;      // CLOCK_REALTIME 毫秒时间戳
    uint64_t uptime_ms;         // CLOCK_MONOTONIC 毫秒时间戳（开机后）
    char module[32];            // 模块名（IPCL/Media/Network/Screen/MCU...）
    char submodule[32];         // 子模块名（Main/Download/Display...）
    char file[128];             // 源文件名（basename）
    int line;                   // 行号
    char func[64];              // 函数名
    char message[4096];         // 日志内容（格式化后）
} __attribute__((packed)) vlog_msg_t;
```

### 时间戳来源

| 字段 | 时钟源 | 语义 |
|------|--------|------|
| `timestamp_ms` | `clock_gettime(CLOCK_REALTIME)` | 系统 wall clock，受校时/NTP 影响 |
| `uptime_ms` | `clock_gettime(CLOCK_MONOTONIC)` | 开机后单调递增，不受校时影响 |

**关键**：`timestamp_ms` 在设备时钟未同步时可能完全错误（如 1970 年或 2038 年），但 `uptime_ms` 在同一 boot 内始终可靠。

---

## VLOG Bridge 落盘格式

### 输出格式（logging.c `vlog_channel_write`）

```
[2026-09-04 10:30:45.123][123.456][I][42][IPCL][Stats][PID:1234][ipcl_stats.c:89 ipcl_report]tx data rate: 1234 Bps
 ^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^ ^^ ^^^^ ^^^^^^ ^^^^^^ ^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^
 timestamp (wall)          uptime   lv seq  module submod  PID       file:line func                 message
```

- `timestamp` 由 `msg->timestamp_ms` 格式化（`%Y-%m-%d %H:%M:%S.ms`）
- `uptime` 由 `msg->uptime_ms` 格式化（`seconds.milliseconds`）
- 格式中的 `[I]` 是 level 缩写：E/W/I/D/V

### 路由规则

```c
// logging.c: vlog_bridge_write_log()
if (strncmp(msg->module, "MCU", 3) == 0)
    ch = &g_mculog_bridge;   // → /log/Mcu_Log/mculog.log.*
else
    ch = &g_vlog_bridge;     // → /log/Linux_Log/logNN/syslog.log.*
```

**重要**：MCU 日志不走 `logNN` 目录，而是直接落在 `/log/Mcu_Log/` 下。

---

## 双通道配置

| 参数 | g_vlog_bridge (Linux_Log) | g_mculog_bridge (Mcu_Log) |
|------|--------------------------|---------------------------|
| 文件前缀 | `syslog.log` | `mculog.log` |
| 单文件大小 | 20MB | 20MB |
| 总空间上限 | 2500MB | 300MB |
| 可配置属性 | `persist.vendor.vlog.linux_space_mb` | `persist.vendor.vlog.mcu_space_mb` |
| 单文件大小属性 | `persist.vendor.vlog.single_file_mb` | 同左 |

---

## 文件命名与轮转

### 文件名格式

```
syslog.log.0001.20260904_103045.log.gz
^^^^^^^^^^  ^^^^  ^^^^^^^^^^^^^^^^^^^^
file_prefix index  timestamp (YYYYMMDD_HHMMSS)
```

- `index`（NNNN）：`ch->current_file_index`，同一 boot 内递增，跨 boot 从 1 重新开始
- `timestamp`：`vlog_bridge_get_file_timestamp()` 取的是 `st.st_mtime`（文件最后修改时间），**不是日志内容时间**

### 轮转流程

```
写日志 → current_file_size >= 20MB?
  ├── 是 → vlog_channel_package_log()
  │         ├── fclose() 当前文件
  │         ├── vlog_compress_file_gz() 用 zlib 压缩
  │         ├── 删除原始 .log 文件
  │         ├── ch->total_channel_size += 压缩后大小
  │         └── ch->current_file_index++
  │
  ├── total_channel_size > max_total_size?
  │   └── 是 → vlog_channel_delete_oldest()
  │             删除目录中时间戳最老的 .gz 文件
  │
  └── 创建下一个编号的文件继续写入
```

### 启动时状态恢复

`vlog_channel_restore_state()` 在 bridge 启动时：
1. 将所有未压缩的 `.log` 文件全部 gzip 压缩
2. 扫描目录中的 `.log.gz` 文件，统计 `total_channel_size`
3. 取最大 `index` + 1 作为下一个文件编号
4. 如果 `total_channel_size > max_total_size`，删除最老文件

---

## `logNN` 目录编号

### 源码证据

```c
// logging.c: vlog_bridge_scan_boot_index()
int vlog_bridge_scan_boot_index(const char *parent_dir) {
    DIR *dir = opendir(parent_dir);
    int max_idx = -1;
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        if (strncmp(entry->d_name, "log", 3) == 0 && entry->d_type == DT_DIR) {
            int idx = atoi(entry->d_name + 3);
            if (idx > max_idx) max_idx = idx;
        }
    }
    closedir(dir);
    return max_idx + 1;  // ← 返回 max_idx + 1
}
```

### 结论

| 属性 | 结论 |
|------|------|
| 生成方式 | 扫描已有 `logNN` 目录，取 `max_idx + 1` |
| 是否依赖时钟 | **否**，纯计数器 |
| 是否可靠递增 | **是**，源码验证 |
| 跨 boot 排序 | **可靠**：`log00` < `log01` < `log02` < ... |
| Agent 用法 | 可以用 `logNN` 编号直接确定 boot 顺序 |

**之前的误区**：Skill 说 "不要从 log00/log01 编号推断 boot 顺序" 是过度保守的。`logNN` 是可靠的递增 boot 计数器，Agent 可以直接信任。

### 事故定位

事故可能触发 reboot，因此事故日志通常在 `log(N-1)` 而非 `logN`（当前启动）。Agent 应优先检查事故时间窗口对应的 boot round，而非默认选择最新或最早的。

---

## SOS 归档目录结构

```
SOS_Archive/
├── Linux_Log/
│   ├── log00/                             ← 最早一次 boot
│   │   ├── syslog.log.0001.YYYYMMDD_HHMMSS.log.gz
│   │   ├── syslog.log.0002.YYYYMMDD_HHMMSS.log.gz
│   │   ├── main_log.log.0001.YYYYMMDD_HHMMSS.log.gz
│   │   ├── kernel_log.log.0001.YYYYMMDD_HHMMSS.log.gz
│   │   └── ...
│   ├── log01/                             ← 第二次 boot
│   │   └── ...
│   └── file_tree.txt                      ← mobile_log_d 创建的目录记录
├── Mcu_Log/
│   └── mculog.log.0001.YYYYMMDD_HHMMSS.log.gz
├── can_log/        ← CAN 日志
├── ota/            ← OTA 日志
├── pki/            ← PKI/认证日志
└── data/           ← 其他数据
```

---

## 有价值的关键锚点

### mblog_history

路径：`/data/misc/mblog/mblog_history`

`mobile_log_d` 自身的运行日志，格式为 `YYYY:MM:DD HH:MM:SS message(PID)`。包含：

- `=====MOBILELOG START=======` — 每次启动后的第一条日志
- 目录创建记录（`log dir: /log/Linux_Log/logNN/`）
- 配置变更、错误等

**Agent 用法**：`mblog_history` 是验证 boot 身份和 `logNN` 对应关系的最可靠锚点。搜索 `log dir:` 可以找到每个 boot 对应的目录。

### file_tree.txt

路径：`/log/Linux_Log/file_tree.txt`

记录每个被创建的 `logNN` 目录路径，可用于验证哪些 boot round 目录是真实存在过的。

### reboot-reason / pl_lk / bootprof

- `reboot-reason`：重启原因（panic、watchdog、正常关机等）
- `pl_lk`：preloader/LK 日志
- `bootprof`：启动性能分析

**Agent 用法**：reboot 分析时，始终包含 post-reboot round 的这三个文件。

---

## VLOG Bridge 启动时序

VLOG Bridge 不是随 `mobile_log_d` 自动启动的，它需要显式的 `bridge_start` 命令：

```
vlog_init() 调用链:
  → vlog_start_mobilelog_bridge()
    → 检查 socket 是否已存在
    → systemctl start mobile_log_d.service（如果未运行）
    → mobile_log_d --control bridge_start
    → 等待 bridge socket 就绪（最多 3 秒）
```

**Agent 影响**：如果某个业务模块在 `vlog_init()` 之前打日志，或 VLOG Bridge 从未被启动，`syslog.log.*` 将不存在。Agent 在分析"为什么缺少 syslog"时，应考虑这个时序问题。

---

## 业务模块与日志标签

| 模块 | 头文件 | 模块名 | 短别名 |
|------|--------|--------|--------|
| IPCL | `ipcl_log.h` | `IPCL` | `ipcl_*` |
| Media | `media_log.h` | `Media` | `MEDIA_*` |
| Network | `network_log.h` | `Network` | `NETWORK_*` |
| Screen | `screen_log.h` | `Screen` | `SCREEN_*` |
| PowerManager | `powermanager_log.h` | `PowerManager` | `PM_*` |
| Update | `update_log.h` | `Update` | `UPDATE_*` |
| MCU 桥接 | `mcu_log_receiver.cpp` | `MCU` | — |

---

## 编译依赖

```makefile
# mobile_log_d/Makefile
INCLUDES := -I. -I./tcl/cluster/logs/include
```

`mobile_log_d` 编译时直接引用 `vlog` 库的头文件（`vlog_bridge_protocol.h`），确保客户端和服务端使用相同的消息结构、socket 名称和魔数。