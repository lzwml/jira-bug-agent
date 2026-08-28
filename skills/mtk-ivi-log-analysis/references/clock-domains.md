# Cross-Domain Clock Normalization

Use this reference when adding timestamp parsers or evaluating a cross-VM timeline.

## Clock classes

| Domain | Typical representation | Main risk |
|---|---|---|
| Android wall clock | `MM-DD HH:MM:SS.mmm` | missing year, clock correction |
| Kernel/ftrace monotonic | seconds since boot | resets every boot |
| SCP/hypervisor counter | firmware-local seconds/ticks | frequency and epoch may differ |
| MCU dual clock | wall time plus local counter | clocks may drift independently |
| CAN capture | relative or absolute seconds | capture start is not boot start |

## Normalization requirements

Normalize only when there is an observed anchor, such as:

- a boot-complete or reboot event represented in two domains;
- a request/response identifier logged on both sides of an IPC boundary;
- a capture header with an explicit absolute start time;
- paired wall and monotonic timestamps in one artifact;
- a known boot identity plus reliable boot-time metadata.

For every normalized event retain:

- raw timestamp;
- clock domain;
- boot/capture identity;
- anchor used;
- calculated offset and any uncertainty.

Ordering within one clock domain can remain useful without normalization. When no anchor exists, present separate timelines and state that cross-domain ordering is unconfirmed.
