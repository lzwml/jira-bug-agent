# MTK Android VM Evidence Reference

> 源码验证。基于 `b_android/vendor/mediatek/proprietary/external/mobile_log_d` 和 `MTKLogger` 的实际代码。Android 侧完整设计文档见 `references/android-mobile-log-design.md`。

## Android 与 Yocto 的关系

Android 和 Yocto 的 `mobile_log_d` 是**同一套 C 源码**，编译配置不同：
- Android 版：无 VLOG Bridge，无 `syslog.log.*` 输出
- Yocto 版：有 VLOG Bridge，产生 `syslog.log.*` 文件
- 两者通过同一个控制 socket `"mobilelogd"` 通信

## MobileLog

完整的 26 种日志源清单和目录树见 `references/log-directory-reference.md`。以下是 Android 侧关键文件摘要：

APLog 归档内常见文件：`main_log`, `kernel_log`, `events_log`, `radio_log`, `crash_log`, `sys_log`, `stats_log`, `security_log`（7 种 Android logd）+ `atf_log.log`, `bsp_log.log`, `scp_log.log`, `sspm_log.log`, `adsp_0_log.log`, `adsp_1_log.log`, `mcupm_log.log`, `connsys_picus_log.log`, `apusys_log.log`, `vcp_log.log`, `nebula_tee_log.log`, `nebula_hypervisor_log.log`, `wifi_driver_log.log`, `gz_log.log`, `scp_b_log.log`, `ccci_dpmaif_debug`, `vm_alps_klog`, `vm_tbox_klog`（19 种平台子系统）+ `bootprof`, `pl_lk`, `properties`, `mblog_history`（4 种元数据）。

`boot__normal` is an early-boot log snapshot copied at boot completion. Its retention and overwrite behavior are product configuration. Confirm its boot identity rather than treating the directory name as proof.

## AEE, ANR and Dropbox

- Start AEE discovery from `aee_exp/db_history`; use its event path, subtype, process and time to select the matching database directory.
- Match an ANR using process, timestamp and subject before reading thread stacks. Main-thread state alone is not an ANR cause; inspect the blocking resource and binder relationship.
- Use `binderinfo` to support a concrete transaction-chain hypothesis, not as a global traffic ranking exercise.
- Dropbox entries such as `SYSTEM_BOOT`, `SYSTEM_RESTART`, `system_app_anr` and `system_server_lowmem` are incident indexes or summaries. Correlate them with primary logs.
- Match tombstone process/build identity and crash time before connecting it to the reported symptom.

## Common formats

- Logcat commonly begins with `MM-DD HH:MM:SS.mmm PID TID LEVEL TAG: message` and belongs to a wall-clock domain.
- MTK kernel streams commonly contain priority plus boot-relative seconds. Preserve the raw timestamp and priority.
- ANR traces consist of process metadata and per-thread states/stacks; thread names and TIDs must remain associated with their process.

Avoid universal regex assumptions when a product build changes spacing, prefixes or tag formatting. Deterministic format support belongs in `log-analysis-core` with fixtures from real anonymized logs.
