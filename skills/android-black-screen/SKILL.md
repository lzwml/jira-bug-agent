---
name: android-black-screen
description: Analyze Android black-screen, no-display, delayed-display, or display-freeze bugs across app buffers, SurfaceFlinger, HWC, display driver, power, and backlight layers.
category: symptom
---

# Android Black Screen Analysis

Determine the lowest layer at which the expected frame or display transition stops. Do not assume every black screen is a display-driver failure.

## Establish the observation window

Call `open_case` and `inspect_case`. Identify whether the Case contains logcat, kernel logs, dumpsys/SurfaceFlinger state, tombstones, ANR traces, or display traces. Establish the user-visible start/end event from the Issue when possible. If the reproduction time is unknown, report that limitation before making timing claims.

## Build the display timeline

Call `extract_timeline` with a focused subset of these anchors, adding platform-specific names discovered in the logs:

`bootanimation`, `SurfaceFlinger`, `WindowManager`, `BufferQueue`, `BLASTBufferQueue`, `HWC`, `Composer`, `present`, `validateDisplay`, `fence`, `DRM`, `CRTC`, `panel`, `backlight`, `DisplayPowerController`, `screenState`.

Do not compare kernel monotonic events directly with Android wall-clock events unless the Case contains a clock synchronization point.

## Test layer hypotheses

- **App/window layer:** search for window visibility, relayout, first frame, buffer dequeue/queue, abandoned BufferQueue, ANR, or process death. Lack of submitted buffers points upward, not automatically to SurfaceFlinger.
- **SurfaceFlinger/HWC:** search for composition validation, present failures, fence timeout, layer rejection, transaction stalls, HWC reset, and display hotplug changes.
- **Kernel/display driver:** search for DRM/CRTC/DSI/panel errors, underrun, timeout, ESD recovery, IOMMU faults, reset, and relevant Call Trace. A driver warning outside the symptom window is not sufficient.
- **Power/backlight:** search for requested versus actual screen state, brightness, blank/unblank, doze, suspend/resume, and backlight enable timing. Separate “frame exists but panel is dark” from “no frame was produced.”

Use `parse_diagnostics` when Fatal, ANR, AVC, or Call Trace signals exist. Use `search_evidence` to collect the smallest line windows that prove or contradict each hypothesis.

## Evidence standard

A supported conclusion should connect at least: the user-visible symptom window, the last successful upstream event, and the first failed or missing downstream transition. Prefer two independent artifacts when crossing framework/kernel boundaries.

Report `insufficient_evidence` if the necessary layer boundary cannot be observed. Keep plausible alternatives in `hypotheses`; do not label a component as root cause solely because its name appears near an error.
