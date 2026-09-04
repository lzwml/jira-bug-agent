# Cross-Domain Clock Normalization

> 源码验证。基于 `tcl/cluster/logs/vlog.c` 和 `mobile_log_d/logging.c` 的时钟源分析。

## Clock classes

| Domain | Source | Typical representation | Main risk |
|---|---|---|---|
| Android wall clock | logcat | `MM-DD HH:MM:SS.mmm` | missing year, clock correction |
| **vlog wall clock** | `clock_gettime(CLOCK_REALTIME)` | `YYYY-MM-DD HH:MM:SS.ms` | **设备时钟未同步时可能完全错误**（如 1970 年） |
| **vlog uptime** | `clock_gettime(CLOCK_MONOTONIC)` | `seconds.milliseconds` | **同一 boot 内始终可靠**，跨 boot 不可比 |
| Kernel/ftrace monotonic | `local_clock()` | seconds since boot | resets every boot |
| SCP/hypervisor counter | firmware-local | seconds/ticks | frequency and epoch may differ |
| MCU dual clock | MCU firmware | wall time + local counter | clocks may drift independently |
| CAN capture | capture tool | relative or absolute seconds | capture start is not boot start |

### vlog 双时间戳（源码证据）

```c
// vlog.c: vlog_output()
msg.timestamp_ms = get_timestamp_ms();  // CLOCK_REALTIME → 毫秒
msg.uptime_ms    = get_uptime_ms();     // CLOCK_MONOTONIC → 毫秒

static uint64_t get_timestamp_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static uint64_t get_uptime_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
```

**Agent 使用建议**：

- `uptime_ms`：用于同一 boot 内的日志排序和时序分析（始终可靠）
- `timestamp_ms`：用于跨 boot 的日志关联（需验证时钟同步状态）
- 当 `timestamp_ms` 异常（如 < 2024 或 > 2030）时，优先使用 `uptime_ms` 进行同一 boot 内的排序

### vlog syslog 输出格式中的时间

```
[2026-09-04 10:30:45.123][123.456][I]...
 ^^^^^^^^^^^^^^^^^^^^^^^^ ^^^^^^^^
 timestamp_ms 格式化        uptime_ms 格式化
```

- `timestamp_ms` 格式化为 `%Y-%m-%d %H:%M:%S.ms`（完整日期，含年）
- `uptime_ms` 格式化为 `seconds.milliseconds`（纯数字，无日期）

---

## Normalization requirements

Normalize only when there is an observed anchor, such as:

- a boot-complete or reboot event represented in two domains;
- a request/response identifier logged on both sides of an IPC boundary;
- a capture header with an explicit absolute start time;
- paired wall and monotonic timestamps in one artifact（vlog syslog 天然提供）；
- a known boot identity plus reliable boot-time metadata.

For every normalized event retain:

- raw timestamp;
- clock domain;
- boot/capture identity;
- anchor used;
- calculated offset and any uncertainty.

Ordering within one clock domain can remain useful without normalization. When no anchor exists, present separate timelines and state that cross-domain ordering is unconfirmed.

---

## Cross-domain ordering with vlog syslog

vlog syslog 的 `[timestamp][uptime]` 双时间戳格式使跨域排序变得简单：

1. **同一 boot 内**：使用 `uptime` 字段直接排序（最可靠）
2. **跨 boot**：使用 `timestamp` 字段，但需要验证时钟同步状态
3. **与 Android logcat 关联**：两端都是 wall clock，但 logcat 通常不含年份。需要从 vlog syslog 的完整日期推断 logcat 的年份
4. **与 kernel ftrace 关联**：kernel 使用 boot-relative 时间，需要找到共享的 boot 事件（如 `bootanimation`）作为对齐锚点。vlog 的 `uptime` 字段在同一 boot 内可直接与 kernel monotonic 对齐（偏移量可能不同但顺序一致）