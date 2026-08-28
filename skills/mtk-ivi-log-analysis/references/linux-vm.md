# MTK Linux VM and Peripheral Evidence Reference

Use this reference when maintaining Linux/TBox, hypervisor, MCU, CAN, OTA or PKI parsing rules.

## Boot rounds and core streams

`Linux_Log/logNN` commonly represents MobileLog rounds, but `log00` is not universally “previous” and `log01` is not universally “current.” Establish the active round from `mblog_history`, boot markers, directory/file timestamps and reboot identity.

Common artifacts:

- `main_log.log`: Android-style userspace logging;
- `kernel_log.log`: Linux kernel evidence;
- `syslog.log.*` and `bsp_log.log`: often ftrace-style tracepoint streams rather than traditional syslog;
- `scp_log.log`: Sensor Control Processor firmware evidence;
- `nebula_hypervisor_log.log`: VM/vCPU scheduling and lifecycle evidence;
- `atf_log.log`: trusted firmware and SMC access evidence;
- `bootprof`, `pl_lk`, `reboot-reason`, `properties`, `mblog_history`: boot/capture identity.

Tracepoint streams commonly resemble `comm-pid [cpu] flags seconds: event: data`. Their seconds are boot-relative and cannot be compared directly with Android wall time.

## SCP, hypervisor, ATF and MCU

- Treat SCP temperature and sensor samples as measurements, not fault evidence without thresholds and symptom correlation.
- Use hypervisor logs to establish VM/vCPU lifecycle, starvation or reset only when the affected VM and incident window are identified.
- ATF `deny access` messages show a rejected secure monitor call; identify caller, VM and functional impact before treating one as causal.
- MCU logs may carry both wall time and a local counter. Preserve module, level and power/IPC state transitions.

## CAN, OTA, PKI and application logs

- For CAN ASC, establish whether timestamps are relative or absolute from the header. Analyze requested IDs/signals and an incident window; high frequency alone is not an error.
- For OTA/HMI, reconstruct the operation state machine: request, download, validation, installation, reboot and result reporting. Find the first failed transition.
- For PKI, redact tokens, certificates, VINs and key material. Report operation and error class without exposing full sensitive values.
- For application logs, connect application failures to IPC/device/service transitions rather than ranking generic `ERROR` strings.

Compressed rotations must be exposed through a controlled extraction boundary before analysis. Do not make a conclusion from the archive name or index alone.
