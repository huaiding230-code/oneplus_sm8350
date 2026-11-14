# OP9P Android 17 graphics compatibility

This directory stores source patches for the Android framework graphics stack.
These files are deliberately separate from the Linux 5.4 kernel patches: the
current `oneplus_sm8350` kernel workflow does not apply them.

## Stage 1 — BufferQueue producer-throttling compatibility

Patch:
`lineage-24.0/0001-bufferqueue-async-only-default.patch`

Target repository: `LineageOS/android_frameworks_native`
Target branch: `lineage-24.0`
Source branch HEAD reviewed: `a96adf2c6e62dbb22299cd83bbb9ea4bc569e985`

The patch is based on the actual Android 17 source locations and blob versions:
- `libs/gui/include/gui/BufferQueueCore.h`: blob `0042d35e03725b42e0d033aab8d8773d9e395064`
- `libs/gui/BufferQueueProducer.cpp`: blob `a8b95da2fd9f51d9453167396648fe14da36393a`

### Why these changes are needed

On the Android 16 / LineageOS 23.2 source, the enabled
`bq_producer_throttles_only_async_mode` flag made `queueBuffer()` wait on the
previous fence only for async or non-blocking queues.

The Android 17 source adds `mProducerThrottlingEnabled = true` and initializes
`enableEglCpuThrottling = true`. Its API 37 back-pressure interface can disable
that wait, but the stock default remains enabled. Changing only the member
default would be insufficient because the local variable would still default
to true when the new flag branch is inactive.

This patch therefore:
1. Defaults the per-queue opt-in state to false.
2. Initializes the local wait decision to false.
3. Always preserves the async/non-blocking queue rule.
4. Allows the API 37 back-pressure flag plus an explicit per-queue true setting
   to re-enable regular producer throttling.

Vulkan swapchains also connect to BufferQueue as
`NATIVE_WINDOW_API_EGL`, so this policy reaches both EGL and Vulkan producers.

### Expected behavior and risks

- Regular EGL/Vulkan producers no longer wait in `queueBuffer()` for the
  previous queued frame by default.
- Async and non-blocking queues keep the existing throttle rule.
- BufferQueue capacity, dequeue-side back-pressure, acquire/release fences,
  fence ownership, and GPU synchronization are not removed.
- A producer can still block when it exhausts available BufferQueue slots.
- More frames may remain in flight before that queue-capacity back-pressure
  occurs. This can change latency, memory use, power, and frame pacing.
- Applications that implicitly relied on the old `queueBuffer()` wait as a
  producer/consumer synchronization mechanism may behave differently.
- The API 37 opt-in remains conditional on the platform
  `bq_producer_backpressure_control` feature/API being exposed. Async mode
  remains throttled even when the per-queue option is false.

The Android 17 Java API documentation still describes the stock default as
enabled. This patch intentionally overrides that default for this compatibility
test; update the corresponding `frameworks/base` API documentation before
considering it a permanent upstream-style change.

### Validation status

The changed contexts and line positions were checked against the listed
LineageOS 24.0 source blobs. The unified diff parses successfully. A complete
framework source checkout was not available here, so this is **not** claimed
to have passed a real-tree `git apply --check`, a `libgui` build, or device
testing.

This patch does not modify KGSL/DRM kernel code and is not expected to directly
fix the browser GPU-channel timeout. It is a frame-submission/back-pressure
compatibility test, separate from the Gralloc R8 format-`0x38` workaround.
