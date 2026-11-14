# Framework patches for OnePlus 9 Pro / LineageOS 24.0

These patches target the Android framework source, not the Linux 5.4 kernel.
They must be applied to a matching LineageOS 24.0 source checkout and included
in a ROM build. They do not take effect when flashing a kernel AK3 ZIP.

## Stage 2 — AppZygote isolated UID range lifetime

Patch:
`lineage-24.0/0002-appzygote-shared-uid-range-lifetime.patch`

Target repository: `LineageOS/android_frameworks_base`
Target branch: `lineage-24.0`
Reviewed source branch HEAD: `1b59f14f795357709e10b07d0c6cf1080167c43b`

Source blobs reviewed:
- `services/core/java/com/android/server/am/ProcessList.java`:
  `d8e2cfaae0671d20740f11da8698eace47e925d4`
- `services/tests/mockingservicestests/src/com/android/server/am/ActivityManagerServiceTest.java`:
  `9e9fbcde70b61705e3c6ba36a1c029f7475fcf5a`

### Evidence from the device logs

The 2026-11-13 bugreport contains repeated failures to start Chromium's
`NativeOnlySandboxedProcessService0` child processes. The stack reaches
`ProcessList.createAppZygoteForProcessIfNeeded()` and dereferences
`IsolatedUidRange.mFirstUid` after `getIsolatedUidRangeLocked()` returned
null. The same period contains AppZygote socket connection-refused errors.
This is a real system_server process-start failure, not a KGSL reset trace.

### Source-level cause

Android 17 can keep separate Managed and Native AppZygote objects for one
application. `makeAppZygoteName()` uses different `_zygote` and
`_zygote_native` keys, but `IsolatedUidRangeAllocator` remains keyed only by
the app process name and defining UID. Therefore both AppZygotes share one
UID-range allocation.

Before this patch, `killAppZygoteIfNeededLocked()` freed that shared range as
soon as any one AppZygote's process list became empty. It did not check:
- whether the sibling Managed/Native AppZygote still existed; or
- whether an isolated ProcessRecord had already reserved a UID but had not yet
  been inserted into that AppZygote's process list.

A later Chromium sandbox start could then find its AppZygote entry missing and
dereference a null UID range. Replacing the lookup with get-or-create would be
unsafe: a ProcessRecord may already hold an isolated UID from the old range,
so silently assigning a different range could violate the UID range configured
inside an existing Zygote.

### Change

The patch defers stopping an idle AppZygote while any matching isolated
ProcessRecord remains. It releases the shared allocator range only after no
Managed/Native AppZygote for the app remains and no isolated ProcessRecord
still refers to that app's AppZygote. Cleanup also retries range release after
the last isolated ProcessRecord is removed, covering forced teardown.

A focused service test creates both Managed and Native AppZygotes, simulates a
pre-start isolated ProcessRecord, and verifies that the shared range survives
the first teardown and is released only after the pending process and both
zygotes are gone.

### Scope and validation

This repairs a confirmed ActivityManager/AppZygote UID-range lifetime defect
that can prevent Chromium sandbox processes from starting. It may remove one
cause of the browser's GPU-channel timeout, because Chromium cannot establish
its normal renderer/GPU process topology when its sandbox child fails to start.
It does not prove that every GPU-channel timeout is caused by this defect and
does not modify KGSL, EGL, Vulkan, SurfaceFlinger, or DRM drivers.

The patch was prepared against the exact source blobs above. It has not yet
been applied to a complete frameworks/base checkout, compiled, or exercised
on the device. The added regression test also still needs to be run as part of
the framework service test suite before treating this as a final upstream fix.
