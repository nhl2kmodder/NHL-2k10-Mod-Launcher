# 11 — PC recompilation (RexGlue static recomp)

One-line summary: a **separate** project from the Xenia Mod Launcher — a static recompilation of the Xbox 360 PowerPC executable into a native Windows binary using the RexGlue SDK (a Xenia-derived runtime); the clean no-hacks rebuild reaches **gameplay + the main menu**, and this doc records its setup, workflow, and the blockers solved to get there.

Status: **partially working / active investigation.** As of 2026-06-16 the clean rebuild (`C:\NHL2k10_Recomp`, RelWithDebInfo) boots deterministically, renders the intro, passes the intro→menu transition, and plays several minutes of gameplay with no flicker. Two known bugs remain open: a ~3-minute gameplay crash (`sub_83DCD6C8`) and unspecified menu glitches. Dates/offsets below are point-in-time observations from the recomp sessions and may drift; verify against the in-tree handoffs before trusting a line number.

> **This is NOT the Mod Launcher.** The Mod Launcher (documented elsewhere in this folder) edits the game archives and runs the *stock* game under the Xenia emulator. This effort instead compiles the game to a standalone PC executable with no emulator. They share only the reverse-engineering knowledge (function addresses, structs), not code or build trees.

---

## Where it lives

- **Clean rebuild (the current / good one):** `C:\NHL2k10_Recomp` — the proper no-hacks recompilation. Created 2026-06-12 to redo the recomp correctly (no read-fault zero-fill, no clock injection, no iteration caps/guards — those masked or *caused* corruption in the older attempt).
- **Older band-aided attempt:** `C:\nhl2k10` — renders the arena intro but is riddled with guard hacks; superseded (see "The older band-aided recomp" below).
- **A third fresh tree:** `C:\NHL2k10_PC` — an earlier fresh recomp that achieved a stable 10-minute *headless* run (`GameEngine_UpdateFrameTick` loops indefinitely, zero crash/assert). This was a stepping stone; the clean tree at `C:\NHL2k10_Recomp` is the active one.
- **In-tree handoffs (authoritative, most detailed):** `C:\NHL2k10_Recomp\HANDOFF_2026-06-16.md` and `HANDOFF_2026-06-15.md`.
- **RexGlue SDK (prebuilt):** `C:\Users\cloug\rexglue-sdk\win-amd64\`.
- **RexGlue SDK (from source, with our codegen fixes):** `C:\Users\cloug\Documents\_rexinvestigate\rexglue-sdk`.

---

## What RexGlue is

RexGlue is a static-recompilation toolchain (in the lineage of the Xenia emulator / the "N64: Recompiled" family of projects). It reads the Xbox 360 XEX, discovers function boundaries, and emits C++ source files where each guest PowerPC function becomes a host function operating on a `PPCContext`. That generated code links against a **runtime** (`rexruntimed.dll` / `rexruntimerd.dll`) that supplies the kernel, GPU (D3D12), threading, and filesystem emulation ported from Xenia. The result is a normal Windows `.exe`.

Key fixed facts:

- **RexGlue SDK version:** v0.8.0. Downloaded from `github.com/rexglue/rexglue-sdk` release `v0.8.0` (`rexglue-sdk-0.8.0-win-amd64.zip`), extracted to `C:\Users\cloug\rexglue-sdk\win-amd64\`.
- **CMake discovery:** a registry entry `HKCU:\Software\Kitware\CMake\Packages\rexglue` → `...\lib\cmake\rexglue` lets `find_package(rexglue)` resolve. Re-add it if missing.
- **XEX image base:** `0x82000000`.
- **virtual_membase:** `0x100000000` (guest virtual address V → host `0x100000000 + V`).
- **physical_membase:** `0x200000000` (guest physical P → host `0x200000000 + P`).
- **Guest tick frequency:** 50 MHz (`set_guest_tick_frequency(50000000)`), matches Xenia.
- **`PPCContext` GPR layout:** GPRs at offset 0, ordered `r3, r0, r1, r2, r4..r31` (8 bytes each). In recompiled functions the context pointer is spilled at `[rsp+0x28]`.

---

## SDK setup, manifest, and CLI

### Project init (already done)

```powershell
& "C:\Users\cloug\rexglue-sdk\win-amd64\bin\rexglue.exe" init `
    --project-name "nhl2k10" `
    --xex-path "assets/default.xex" `
    --project-root "C:\NHL2k10_Recomp"
```

### Project layout (`C:\NHL2k10_Recomp`)

- `nhl2k10_manifest.toml` — entrypoint = `assets/default.xex`, out dir, `switch_tables`, and `includes` that pull in `config/functions.toml`.
- `config/functions.toml` — **function boundaries** (the single most important correctness lever) + `[[switch_tables]]` entries. Note: `[[switch_tables]]` must live here, in the *included* config, **not** in the manifest — codegen only reads switch tables from the included config.
- `src/` — `main.cpp`, `crash_logger.cpp` (all-thread stack dumper, see below), `nhl2k10_app.h` (app hooks), msvcstl_compat, netdll_stubs, `file_trace.cpp`, `dbg_trace.cpp`.
- `assets/default.xex` (+ `.nxeart`). The big archives (0A/0B/1A/1B) are **not** copied into the tree — the runtime reads them from a hardcoded `game_data_root = C:\Users\cloug\Documents\NHL 2k10 Extracted` (set in `src/nhl2k10_app.h`).
- `generated/default/` — the 65 generated C++ files (`nhl2k10_recomp.N.cpp`, `nhl2k10_init.cpp/.h`, `nhl2k10_register.cpp`).
- `apply_patches.reference.ps1` — the OLD band-aids from the `C:\nhl2k10` tree, kept only as reference. **Do not run it.**

### Manifest shape

```toml
[project]
name = "nhl2k10"
sdk_version = "0.8.0"
game_root = "assets"

[entrypoint]
file_path = "assets/default.xex"        # resolved as manifestDir / file_path
out_directory_path = "generated/default"
includes = []

[analysis]
max_jump_extension = 65536
```

`config/functions.toml` entries look like `0x84129748 = { name = "...", end = 0x84129798 }` (or a `size`), plus `[[switch_tables]]` blocks:

```toml
[[switch_tables]]
address  = 0x84188CE0
register = 11
labels   = [ ... ]   # absolute targets, often read live from guest memory
```

### App customization hooks (`src/nhl2k10_app.h`)

Override as needed: `OnConfigurePaths` (set `game_data_root` / `update_data_root`), `OnPreSetup`, `OnLoadXexImage`, `OnPostSetup`, `OnCreateDialogs` (ImGui debug UI), `OnShutdown`.

### Build output

- Active run config: `out/build/win-amd64-relwithdebinfo/nhl2k10.exe` + `rexruntimerd.dll`.
- Debug (diagnosis only): `out/build/win-amd64-debug/nhl2k10.exe`.

---

## The regen workflow (and the ABI-skew gotcha)

Re-running codegen (e.g. after changing a function boundary) without breaking things:

**Codegen command** (cwd MUST be the project dir or `assets/default.xex` won't resolve):

```powershell
& "C:\Users\cloug\Documents\_rexinvestigate\rexglue-sdk\out\win-amd64\Debug\rexglue.exe" `
    codegen nhl2k10_manifest.toml --log-file cw_regen.txt
```

Use the **from-source Debug `rexglue.exe`** (it carries our codegen fixes: switch-table target discovery, out-of-line branch handling, `scanForBounds` index-redefinition fix), **not** the installed prebuilt one. Output → `generated/default/` (65 files), ~90 s.

**⚠ ABI-SKEW GOTCHA (cost an hour to diagnose):** `rexglue.exe` **dynamically loads `rexruntimed.dll`**. If you edit a runtime **header** (e.g. `include/rex/system/xevent.h` — add a member/method) and rebuild only the runtime DLL, the existing `rexglue.exe` is compiled against the *old* header layout but loads the *new* DLL → kernel-object ABI mismatch → corruption that surfaces as a **bogus** `Failed to load entrypoint XEX: 0xc000000f` (STATUS_NO_SUCH_FILE) at `LoadXexImage`, even though the XEX is right there. **Fix: after any runtime header/ABI change, rebuild the tool too:**

```powershell
cmake --build --preset win-amd64-debug --target rexglue -j 8   # under vcvars64, clang 19 on PATH
```

**Teardown assert (cosmetic):** codegen finishes writing all 65 files, then aborts with `Assertion failed ... xthread.cpp:132` during runtime teardown (the `unk_58` GPU-wait clock thread touching kernel state at shutdown). **The output is already written and valid — ignore the nonzero exit.**

**Generated-file patches that regen ALWAYS reverts (re-apply after every codegen):**
- `recomp.2.cpp` — the `vmaxuw` implementation (`simde_mm_max_epu32` on `.u32` lanes).
- `recomp.49.cpp` — the `sub_84138350` finalize-once guard.

`config/functions.toml` **survives** regen; `.cpp` patches do not. An incremental-rebuild trick avoids a full multi-hour `-O2` rebuild: back up `generated/default`, regen, then `cp -p`-restore byte-identical files from the backup so ninja skips them, and `touch -r` the unchanged `nhl2k10_init.h` back to its old mtime so only the 3 files that actually reference a new symbol recompile. Adding one function typically rebuilds in <1 min instead of ~1 hr.

---

## Solved blockers (in the order they were hit)

The clean rebuild fixed five blockers in sequence — each was the next crash exposed after the prior fix.

### 1. Mount / loader-lock deadlock → fixed by release CRT

The intermittent (~50%) hang at boot, right after the audio worker threads start and before "Mounted", was root-caused with **cdb** (`!locks` + `~*k`). It is a classic **debug-CRT × loader-lock deadlock**:
- `ucrtbased!__acrt_lock_table` (the debug CRT lock, taken on *every* malloc/free in a debug build) is held by the main thread deep in `rex::filesystem::HostPathDevice::PopulateEntry` — it recursively enumerates the entire game directory at mount time, a massive new/delete storm.
- `ntdll!LdrpLoaderLock` is held by a thread mid-`DllMain` (a guest worker thread create/exit).
- One thread holds the CRT lock and needs the loader lock (thread create); the thread being created holds the loader lock and its CRT init needs the CRT lock → cycle. Intermittent because it depends on a thread create/exit landing inside the storm.

**Fix = build/run in a non-debug config (release CRT):** the `win-amd64-relwithdebinfo` preset. The release CRT (`ucrtbase`) has no per-allocation `__acrt_lock_table` locking, so the cycle can't form. This is a build-config change, not code. (`_NO_DEBUG_HEAP=1` only dropped it 50%→33% — it disables the OS debug heap but not the CRT lock layer.) A secondary build fix: `CMakeLists.txt` unconditionally linked `msvcprtd` (debug STL) → `/failifmismatch`; changed to `$<IF:$<CONFIG:Debug>,msvcprtd,msvcprt>`.

**Build gotcha:** do not `cmake --preset win-amd64` (reconfigure) from a plain vcvars shell — it re-detects a stale Clang 16 from PATH, but rexglue needs Clang 18+. The correct compiler is the **VS-bundled clang 19.1.1** at `C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\Llvm\x64\bin`. For incremental builds just use `cmake --build --preset ... --target <t>` (no reconfigure).

### 2. `vmaxuw` unimplemented → implemented

`sub_83B89308` (recomp.2) emitted `REX_UNIMPLEMENTED` (a `throw`) for the VMX `vmaxuw` (max unsigned word). Implemented as `simde_mm_max_epu32` on the `.u32` lanes. This is a generated-file patch — re-apply after every regen.

### 3. `InvalidFunctionTrap @0x83E32A50` → boundary fix

An over-sized `functions.toml` entry (`Sub_83E32A40 size=0x20`) absorbed a vtable thunk at `0x83E32A50` → the codegen vtable scan skipped it → unregistered → trap at an indirect call. Split into `0x10 + 0x10` in `functions.toml` and regenerated. This "over-sized TOML entry absorbs a blr-stub vtable thunk" pattern is **recurring** (also hit at `0x83F79B30` and `0x84188C38`); the fix is always to shrink the entry and add the absorbed stub addresses.

### 4. Black-flicker (menu + gameplay) → frame-sync fix

Root: the main thread hangs in `App_RunFrame (0x83BF3B18)` → **`Render_FrameSync_WaitBuffer @0x84116318`** → `NtWaitForSingleObjectEx(INFINITE)` on `g_FrameSync_DoneEvents[g_FrameSync_BufferIndex]`. It's a **lost-wakeup race** on a double-buffered done-event: the CommandProcessor worker thread (producer) misses re-signaling the exact per-buffer event the main thread waits on after a heavy/long menu frame, so the wait is permanent. (Not a GPU ring spin, not the RPTR mismatch — at the hang *no* thread is in a GPU ring wait.)

**Frame-sync map (renamed in Ghidra):**
- `Render_FrameSync_WaitBuffer @0x84116318` — BeginFrame, waits on done-event[idx].
- `Render_FrameSync_EndAndAdvance @0x841163B0` — EndFrame, dispatches render cmds, `NtClearEvent`+`NtSetEvent`, advances `g_FrameSync_BufferIndex`.
- `Render_FrameSync_Init @0x84116EA8` — creates the "ready"/"done" event arrays, resumes the consumer thread.
- Globals: `g_FrameSync_BufferIndex @0x84ac3218`, `g_FrameSync_BufferCount @0x84ac3220`, `g_FrameSync_DoneEvents @0x84ac34b0`.

**Fix:** ported the GPU/sync fix from the GoldenEye-Recomp-rexglue fork (`SunJaycy/GoldenEye-Recomp-rexglue`) into our forked runtime. The port had **two** parts, and only **one** is correct:
- ✅ **`SetGuestSignalState`** — mirror the guest `KEVENT.Header.SignalState` on Set/Reset/Pulse/Clear. This is the *actual* fix: the render thread reads `SignalState` to choose a timed vs. INFINITE wait; without mirroring it picked INFINITE and deadlocked.
- ❌ **the host-side "sticky-Set"** (`is_render_`/`render_pending_`: a Reset becomes a no-op while a render Set is pending, only a Wait clears it) — **WRONG.** It latches a stale GPU-completion Set across the game's legitimate `Reset(E)→Submit→Wait(E)` double-buffer loop, so `Wait` returns *before* the GPU finished the buffer → the game presents an unrendered buffer → **black-flicker every other frame**.

**Final state: removed the sticky-Set entirely; kept `SetGuestSignalState`.** Confirmed: flicker gone, transition still passes, gameplay runs. Also fixed: the vsync worker now fires at most one vblank per wake and drops backlog (was a `while` loop that burst-flooded the ISR after a heavy frame). **Do not re-add the sticky-Set.**

### 5. Menu-finalize crash (~4 min, Boot→Start→Main Menu) → finalize-once guard

A second finalize call crashed `sub_84137288` walking a zeroed pending-GPU-resource list. Fix in `recomp.49.cpp` `sub_84138350`: guard the three finalize calls behind the game's own done-flag —
`if (!(REX_LOAD_U32(ctx.r31.u32+0x5c) & 0x40000)) { ...finalize... }`. The IFF-section finalize (`sub_84137AE0`) sets `[sec+0x5c]|=0x40000` and zeros the list head `[sec+0x50/0x54]`; a second finalize then dereferenced the zeroed list. This is a generated-file patch — re-apply after regen. (The guard enforces the invariant but the *deeper* "why does rexglue finalize twice" — a divergence in loader `sub_8416DB58` — is still unfound.)

### Related runtime work

- **`unk_58` GPU-wait clock fix:** on real HW, `KTHREAD+0x58` (`unk_58`) advances via the per-thread clock; rexglue (like Xenia) left it frozen at 0, so the title's GPU ring-wait safety timeout (`Gpu_PollWaitRptr @0x841B8C50`: `elapsed = unk_58 − baseline ≥ 5000 → Gpu_RecoverFromHang @0x841D3748`) never fired. Added a dedicated `gpu_wait_clock_thread_` in `kernel_state.cpp` that advances each thread's `unk_58` every 2 ms. It is correct/faithful and helps the *busy-spin* variant of the transition hang, but it did **not** resolve the *idle-deadlock* variant (that was the frame-sync race above). Now largely unnecessary post frame-sync fix — flagged for cleanup removal.

---

## Superseded theory: the RPTR-writeback / transition-hang root cause

An earlier, extensively live-debugged (Cheat Engine on the running game) diagnosis blamed the intro→menu hang on a **GPU ring RPTR-writeback mismatch**: the game's secondary ring (`interrupt_callback_data_` from `VdSetGraphicsInterruptCallback(callback=0x841B97D0, user_data=0x40007B00)`) reads its RPTR from phys `0x1FCA3000` while rexglue's CP writes the writeback to `0x1FCA403C` (primary ring), so `Gpu_SubmitAndWaitForRing @0x841BA090` never sees RPTR advance.

**This theory is superseded by the frame-sync fix (blocker #4).** It was *disproven* live: force-writing `RPTR = WPTR-2` and the completion flag via Cheat Engine did **not** unblock the game — the render thread is in `Wait/UserRequest` on a kernel event, not spinning on the ring. The RPTR offsets and CE gotchas remain useful reference (see the memory note `project_transition_hang_rootcause`), but the actual blocking wait was the double-buffer done-event lost-wakeup, not the ring RPTR. Useful salvaged facts:
- Guest virtual V → host `0x100000000+V`; guest phys P → host `0x200000000+P`. Cheat Engine `read_memory` works on these host addresses on the live game.
- CE gotchas: non-breaking (VEH logging) breakpoints captured **zero** hits even on a hammered address — do not trust "0 hits". `debug_detach` returned `false` **and killed the process**. Prefer raw `read_memory` polling over breakpoints.

---

## The older band-aided recomp (`C:\nhl2k10`) — superseded

The prior tree boots and renders the in-engine arena intro cinematic (rafters, lights, smoke, animated crowd, camera flythrough), far past the old "soft hang at loading screen", but only via a stack of guard hacks. Its **corrected** root-cause diagnosis:

- The "GPU is hung" watchdog message and the "unknown GPU register 0x0069 write" warnings are **downstream symptoms / cosmetic**, not the blocker (0x69/0xC80/0xC82/0xC38 are the game's own hang-diagnostic debug registers).
- The real blocker is **corrupt scheduler entity data**: the chain `sub_83F85680 → sub_83F75190 → sub_83F64988 → sub_83F87B30 → sub_841766A0 → sub_8416A690 → sub_84166C40` processes entity lists containing stale/garbage nodes, producing three sequential symptoms — a crash (garbage pointer deref), then a livelock (circular list never terminates), then a deadlock (`RtlEnterCriticalSection` on a corrupt embedded critical section with garbage owner/lock-count).
- It was kept alive with hacks the clean rebuild deliberately **rejects**: a read-fault page-commit handler (commits zero pages on read faults in `[0x100000000, 0x200000000)` — this *masks* the corruption, turning crashes into livelock/deadlock), iteration caps on list traversals, and a clock-injection into `sub_841B8C50`. These hid the underlying value/logic miscompile rather than fixing it — which is exactly why `C:\NHL2k10_Recomp` was started from scratch.

Do not build on `C:\nhl2k10`; it exists as a reference for the entity-corruption chain only.

---

## The VMX128 "bad instructions" checklist

The Ghidra project for the unpacked exe carries **11,813 "Bad Instruction" bookmarks**, fully classified:

- Flat mapping (reusable): `default_unpacked.exe` is a flat memory image → **file offset = VA − 0x82000000** (verified). Only `.text` (`0x83b40000`, size `0x72484c`) is code.
- **4,586 = real VMX128 vector instructions** — the *only* genuine issue. Stock Ghidra PPC SLEIGH can't decode Xenon VMX128; this list is the recompiler's VMX128 codegen checklist (recurring op4 forms xo=0x167/0x187/0x3c3/0x147; `op6 ..0774` = `stvx128`/`lvx128` callee-saved save/restore).
- The other ~7,227 (~61%) are **false positives**: 4,435 misaligned-in-`.text` cascade artifacts, 2,040 data-as-code, 690 zero-padding, 62 valid op31/op33 blocked by "conflict".

Fix path: install a VMX128-capable PPC language and re-analyze. This was **done** — Ghidra 12.0.4 was patched with the `0dinD/ghidra` `vmx128` fork (`vmx128.sinc` + `ppc_64_xenon.slaspec`), adding two languages: `PowerPC:BE:64:Xenon` (64-bit addressing — use only for Set-Language from the old 64-addr project) and `PowerPC:BE:64:Xenon-32addr` (32-bit addressing + Altivec + VMX128 — **use this for fresh XEXLoaderWV imports**; Xbox 360 is 32-bit addressing). See doc 12 for the RE-tooling side of this.

---

## Open questions / caveats

- **Gameplay crash `sub_83DCD6C8`** — null `[guest+4]` deref, ~3 minutes in, in a hash/lookup loop. Possibly correlated with alt-tab / focus-loss (observed when switching to another window). The main remaining stability bug.
- **Menu glitches** — user-noted, unspecified.
- **The deeper "why finalize twice"** — the finalize-once guard (#5) enforces the invariant but the divergence in loader `sub_8416DB58` that causes the double finalize is unfound.
- **Latent cold-path traps** — ~212 remaining function-boundary warnings compile to guarded `REX_FATAL`/`return` (crash/wrong-flow *if taken*). The game ran with them, so they're latent, but any new code path could hit one. Boundary methodology: reconcile with Ghidra bodies, but use Ghidra's size **only when it is larger** than the current entry — Ghidra sometimes under-sizes (stops at an early `blr`), and forcing a too-small size truncates a real function.
- **Cleanup debt** — strip DIAG logging, remove the now-unneeded `unk_58` clock thread and the old ring-watchdog, reconsider the SetupVfs reorder.
- **Build-silent-fail hazard** — building the runtime or launcher via a `.bat` through a tool wrapper can **silently fail** (vcvars not set up) and leave a stale DLL/EXE while printing success. Always build directly under vcvars64 and verify by output mtime + grepping the binary for a known new string.
- **Dates are point-in-time** — the "reaches gameplay + menu" milestone is from 2026-06-16. Line numbers in generated files shift on regen; treat the in-tree `HANDOFF_*.md` as authoritative over any address here.

### Old-doc corrections

Supersedes `docs/07_pc_recompilation_status.md`, which is broadly correct but lighter on specifics. Concrete corrections/additions vs. that doc:
- The runtime DLL for the RelWithDebInfo build is `rexruntimerd.dll` (not the debug `rexruntimed.dll`).
- The black-flicker was the frame-sync **sticky-Set** half of the ported GoldenEye fix — the fix is `SetGuestSignalState` alone; the sticky-Set must stay removed.
- "2 unresolved codegen calls" from the old doc: in the clean tree the codegen reaches **0 unresolved calls** after the boundary/switch-table fixes; the older figure referred to earlier states. The remaining ~212 items are latent *boundary warnings*, not unresolved calls.
