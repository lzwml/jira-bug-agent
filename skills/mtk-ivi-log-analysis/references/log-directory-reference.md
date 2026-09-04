# MTK 车机日志目录完整参考

> 源码验证。基于 `mobile_log_d/logging.c` 的 `log_dev_map`、`mobile_log_d.rc`、`tcl/cluster/logs` 的所有 26 种日志源。最后更新：2026-09-04。

此文件是 Agent 在 `inspect_case` 后识别日志文件的唯一权威参考。Skill 文档不应重复此列表。

---

## 一、APLog 归档（Android 侧）

### 归档命名

```
APLog_YYYY_MMDD_HHMMSS__N.tar.gz     ← 历史归档（压缩）
APLog_YYYY_MMDD_HHMMSS__N.curf       ← 当前正在写入
```

### 归档内文件清单

```
APLog_YYYY_MMDD_HHMMSS__N/
  ├── main_log                ← Android logd: Framework/SystemService/App
  ├── radio_log               ← Android logd: 射频/Modem 通信
  ├── events_log              ← Android logd: 结构化事件标签
  ├── sys_log                 ← Android logd: system 日志
  ├── crash_log               ← Android logd: 崩溃摘要
  ├── stats_log               ← Android logd: 统计信息
  ├── security_log            ← Android logd: 安全审计
  ├── kernel_log              ← /proc/kmsg: 内核日志
  ├── atf_log.log             ← /proc/atf_log/atf_log: Arm Trusted Firmware
  ├── gz_log.log              ← /proc/gz_log: 安全域日志
  ├── bsp_log.log             ← ftrace bsp instance: BSP tracepoint
  ├── wifi_driver_log.log     ← /dev/wifi_salog: Wi-Fi 驱动
  ├── scp_log.log             ← /dev/scp: Sensor Control Processor
  ├── scp_b_log.log           ← /dev/scp_B: SCP 备份
  ├── sspm_log.log            ← /dev/sspm: 协处理器
  ├── adsp_0_log.log          ← /dev/adsp_0: Audio DSP 0
  ├── adsp_1_log.log          ← /dev/adsp_1: Audio DSP 1
  ├── mcupm_log.log           ← /dev/mcupm: MCU Power Manager
  ├── connsys_picus_log.log   ← ftrace connsys: 连接子系统
  ├── apusys_log.log          ← /proc/apusys_logger/seq_logl: APU 子系统
  ├── vcp_log.log             ← /dev/vcp: Video Codec Processor
  ├── ccci_dpmaif_debug       ← /proc/dpmaif_debug: DPMAIF 调试
  ├── nebula_tee_log.log      ← /dev/nebula-log-dev0: TEE 可信执行环境
  ├── nebula_hypervisor_log.log ← /dev/thyp-log-dev0: Hypervisor
  ├── vm_alps_klog            ← /dev/vmlog-alps: VM ALPS 内核日志
  ├── vm_tbox_klog            ← /dev/vmlog-tbox: VM TBox 内核日志
  ├── bootprof                ← 启动性能分析
  ├── pl_lk                   ← preloader/LK 日志
  ├── properties              ← 系统属性快照
  └── mblog_history           ← mobile_log_d 自身运行日志
```

### 相邻目录

```
APLog_YYYY_MMDD_HHMMSS__N.tar.gz
boot__normal/                 ← 早期 boot 日志快照（boot 完成时拷贝）
  ├── main_log
  ├── kernel_log
  └── events_log
anr/                          ← ANR traces
  └── anr_YYYY-MM-DD-HH-MM-SS-NNN
tombstones/                   ← Native crash
  └── tombstone_NN
aee_exp/                      ← AEE crash dumps
  ├── db_history
  └── db.NN/
dropbox/                      ← System dropbox
  └── system_app_anr@xxx.txt
```

### 关键特征

- 归档内文件名**不带 `.log` 后缀**（`main_log` 而非 `main_log.log`）
- `boot__normal` 与 `APLog_*` 是同一 boot 的不同阶段——前者是早期快照，后者是完整 round
- Android 侧**没有 `syslog.log.*`**（那是 Yocto 侧 vlog bridge 独有的）

---

## 二、SOS/TBox 归档（Yocto 侧）

### 归档结构

```
SOS_TBox_xxx.tar.gz（或直接目录）
  ├── Linux_Log/
  │     ├── log00/                              ← 最早一次 boot
  │     │     ├── syslog.log.0001.YYYYMMDD_HHMMSS.log.gz   ← vlog bridge 业务日志
  │     │     ├── syslog.log.0002.YYYYMMDD_HHMMSS.log.gz   ← 同 boot 内轮转
  │     │     ├── main_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── radio_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── events_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── sys_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── crash_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── stats_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── security_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── kernel_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── bsp_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── scp_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── scp_b_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── sspm_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── adsp_0_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── adsp_1_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── mcupm_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── connsys_picus_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── apusys_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── vcp_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── ccci_dpmaif_debug.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── nebula_tee_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── nebula_hypervisor_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── atf_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── gz_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── wifi_driver_log.log.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── vm_alps_klog.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── vm_tbox_klog.0001.YYYYMMDD_HHMMSS.log.gz
  │     │     ├── bootprof
  │     │     ├── pl_lk
  │     │     ├── reboot-reason
  │     │     ├── properties
  │     │     └── mblog_history
  │     ├── log01/                              ← 第二次 boot
  │     │     └── ...（同上）
  │     └── file_tree.txt                       ← 所有 logNN 目录的创建记录
  │
  ├── Mcu_Log/
  │     └── mculog.log.NNNN.YYYYMMDD_HHMMSS.log.gz   ← MCU 日志
  │
  ├── can_log/         ← CAN 总线日志
  ├── ota/             ← OTA 升级日志
  ├── pki/             ← PKI/认证日志
  └── data/            ← 其他数据
        └── mblog_history   ← 可能在此出现
```

### 关键特征

- 文件名带 `.log.NNNN.YYYYMMDD_HHMMSS.log.gz` 后缀
- `NNNN` = `current_file_index`，同一 boot 内递增，跨 boot 从 1 重新开始
- `YYYYMMDD_HHMMSS` = `st.st_mtime`（文件最后修改/压缩时间），不是日志内容时间
- **`syslog.log.*` 是 SOS 独有、APLog 没有的**——vlog bridge 的业务日志
- SOS 归档中也包含完整的 26 种 Android 日志（`main_log.log.*` 等），与 APLog 的内容重叠
- MCU 日志在独立目录 `/log/Mcu_Log/`，不走 `logNN`

---

## 三、26 种日志源完整索引

| 编号 | 文件名（APLog 内） | 文件名（SOS 内） | 来源设备 | 说明 |
|------|-------------------|-----------------|---------|------|
| 1 | `main_log` | `main_log.log.*.gz` | Android logd | Framework/SystemService/App |
| 2 | `radio_log` | `radio_log.log.*.gz` | Android logd | 射频/Modem |
| 3 | `events_log` | `events_log.log.*.gz` | Android logd | 结构化事件 |
| 4 | `sys_log` | `sys_log.log.*.gz` | Android logd | system 日志 |
| 5 | `crash_log` | `crash_log.log.*.gz` | Android logd | 崩溃摘要 |
| 6 | `stats_log` | `stats_log.log.*.gz` | Android logd | 统计信息 |
| 7 | `security_log` | `security_log.log.*.gz` | Android logd | 安全审计 |
| 8 | `kernel_log` | `kernel_log.log.*.gz` | `/proc/kmsg` | 内核日志 |
| 9 | `atf_log.log` | `atf_log.log.*.gz` | `/proc/atf_log/atf_log` | Arm Trusted Firmware |
| 10 | `gz_log.log` | `gz_log.log.*.gz` | `/proc/gz_log` | 安全域日志 |
| 11 | `bsp_log.log` | `bsp_log.log.*.gz` | ftrace bsp | BSP tracepoint |
| 12 | `wifi_driver_log.log` | `wifi_driver_log.log.*.gz` | `/dev/wifi_salog` | Wi-Fi 驱动 |
| 13 | `scp_log.log` | `scp_log.log.*.gz` | `/dev/scp` | Sensor Control Processor |
| 14 | `scp_b_log.log` | `scp_b_log.log.*.gz` | `/dev/scp_B` | SCP 备份 |
| 15 | `sspm_log.log` | `sspm_log.log.*.gz` | `/dev/sspm` | 协处理器 |
| 16 | `adsp_0_log.log` | `adsp_0_log.log.*.gz` | `/dev/adsp_0` | Audio DSP 0 |
| 17 | `adsp_1_log.log` | `adsp_1_log.log.*.gz` | `/dev/adsp_1` | Audio DSP 1 |
| 18 | `mcupm_log.log` | `mcupm_log.log.*.gz` | `/dev/mcupm` | MCU Power Manager |
| 19 | `connsys_picus_log.log` | `connsys_picus_log.log.*.gz` | ftrace connsys | 连接子系统 |
| 20 | `apusys_log.log` | `apusys_log.log.*.gz` | `/proc/apusys_logger/seq_logl` | APU 子系统 |
| 21 | `vcp_log.log` | `vcp_log.log.*.gz` | `/dev/vcp` | Video Codec Processor |
| 22 | `ccci_dpmaif_debug` | `ccci_dpmaif_debug.*.gz` | `/proc/dpmaif_debug` | DPMAIF 调试 |
| 23 | `nebula_tee_log.log` | `nebula_tee_log.log.*.gz` | `/dev/nebula-log-dev0` | TEE 可信执行环境 |
| 24 | `nebula_hypervisor_log.log` | `nebula_hypervisor_log.log.*.gz` | `/dev/thyp-log-dev0` | Hypervisor |
| 25 | `vm_alps_klog` | `vm_alps_klog.*.gz` | `/dev/vmlog-alps` | VM ALPS 内核日志 |
| 26 | `vm_tbox_klog` | `vm_tbox_klog.*.gz` | `/dev/vmlog-tbox` | VM TBox 内核日志 |

## 四、元数据文件

| 文件名 | 位置 | 说明 |
|--------|------|------|
| `mblog_history` | `mobilelog/` 或 `data/` | mobile_log_d 运行日志，`=====MOBILELOG START=======` 和 `log dir:` 是 boot 身份锚点 |
| `bootprof` | `APLog/` 或 `logNN/` | 启动性能分析 |
| `pl_lk` | `APLog/` 或 `logNN/` | preloader/LK 日志 |
| `reboot-reason` | `logNN/`（SOS 独有） | 重启原因（panic/watchdog/正常关机） |
| `properties` | `APLog/` 或 `logNN/` | 系统属性快照 |
| `file_tree.txt` | `Linux_Log/`（SOS 独有） | 所有 logNN 目录的创建记录 |

## 五、SOS 独有文件

| 文件名 | 位置 | 说明 |
|--------|------|------|
| `syslog.log.NNNN.*.log.gz` | `logNN/` | vlog bridge 业务日志（IPCL/Media/Network/Screen/PowerManager/Update） |
| `mculog.log.NNNN.*.log.gz` | `Mcu_Log/` | MCU 日志 |
| `reboot-reason` | `logNN/` | 重启原因 |
| `file_tree.txt` | `Linux_Log/` | 目录创建记录 |

## 六、APLog 独有文件

| 文件名 | 位置 | 说明 |
|--------|------|------|
| `boot__normal/` | 与 APLog 同级 | 早期 boot 日志快照 |
| `anr/` | 与 APLog 同级 | ANR traces |
| `tombstones/` | 与 APLog 同级 | Native crash |
| `aee_exp/` | 与 APLog 同级 | AEE crash dumps |
| `dropbox/` | 与 APLog 同级 | System dropbox |