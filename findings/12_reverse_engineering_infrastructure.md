# 12 — Reverse-engineering infrastructure & toolkit

One-line summary: the RE scaffolding behind every other finding — the named application/GPU function map, the VCFILEDEVICE file-I/O class hierarchy, the evidence-only naming sweep, the Ghidra MCP bridge, and the game-window-icon XEX patch.

Status: **verified / reference.** These are tools and named-symbol maps, not runtime behavior; the addresses below were captured against the NHL 2K10 `default.xex` / `default_unpacked.exe` (image base `0x82000000`) and are stable for that binary. Naming is ongoing (thousands of leaf functions remain `FUN_`), so the function-map sections are a *snapshot* of what was named, not a complete symbol table.

> This doc underpins **both** the Xenia Mod Launcher and the PC recompilation (doc 11). The two share this reverse-engineering knowledge — function addresses, struct layouts, the Ghidra project — but nothing else.

---

## The Ghidra project & environment

- **Binary loaded:** two ways —
  - `default_unpacked.exe` (49 MB, the inner PE extracted from the XEX via xextool) — a **flat image**, so **file offset = VA − 0x82000000**. Used for the older analysis and the bad-instruction sweep.
  - `default.xex` imported via the **XEXLoaderWV** extension (`zeroKilo/XEXLoaderWV`) — gives proper section names, import/export resolution, XEXP/PDB. Feed it the *raw* `default.xex`; it decrypts (retail key) and decompresses internally.
- **Load addresses:** code section at `0x82000000`, higher sections up to `~0x85000000`.
- **Language:** `PowerPC:BE:64:Xenon-32addr` — a patched Ghidra 12.0.4 with the `0dinD/ghidra` VMX128 fork (32-bit addressing + Altivec + VMX128). This clears the ~4,586 real VMX128 "bad instruction" bookmarks. (See doc 11 for the bad-instruction taxonomy; the language install is what makes those instructions decode.)

**XEXLoaderWV int-overflow bug (found + fixed):** import failed with a bare `IOException` because `LzxDecompression.DecompressLZX` computed `data.length * 100` as a 32-bit int; NHL 2K10's de-framed LZX stream is 0x18ffdae (~26.2 MB), so `×100` overflowed → negative → "output_length < 0". Fix: `(long)data.length * 100L` (line 170), recompiled just that class into the extension jar. Affects any game whose LZX stream exceeds ~21.5 MB.

---

## The Ghidra MCP bridge

Lets tools query and edit the live Ghidra analysis (function names, xrefs, disassembly, prototypes, comments) without manual copy-paste.

- **Bridge script:** `C:\Users\cloug\Documents\NHL 2k10 Extracted\bridge_mcp_ghidra.py` — a FastMCP server.
- **Listens on:** `http://127.0.0.1:8080/` (proxies to Ghidra's own HTTP server plugin).
- **MCP server name:** `ghidra`, configured in `C:\Users\cloug\.claude\mcp.json`. `enableAllProjectMcpServers: true` in `~/.claude/settings.json` auto-approves it.

To use: open Ghidra with the project, start the HTTP server plugin (port 8080); MCP `ghidra` tools then become available.

**Bridge limitations (important — they shape the whole workflow):** the bridge can **rename** functions/data/variables, set **prototypes**, and add **comments** — and nothing else. It **cannot**:
- create structs / data types (must be done by a Ghidra Java script — several were written, e.g. `CreateGraphicsTypes.java`, `CreateVCFileTypes.java`, `AutoNameBySubsystem.java`, all in `C:\Users\cloug\ghidra_scripts`; the user runs them inside Ghidra),
- re-disassemble, clear code, define data, remove bookmarks, or change the processor language.

So the VMX128 "bad instruction" cleanup and any type creation must run *inside* Ghidra (script or GUI); only rename/prototype/comment can be driven live over MCP. Two useful read endpoints: `list_functions` (returns "name at ADDR", entries only — no size) and `get_function_by_address` ("Body: START - END" for the exact entry). To map an arbitrary address to its containing function, take the largest entry ≤ addr and fetch its Body.

---

## Named application spine (XEX function map)

A high-confidence RE pass from `main` outward. Core application flow:

| Function | Address | Role |
|---|---|---|
| `mainCRTStartup` | `0x841E02B0` | CRT entry: init, argv parse, call `main`, terminate |
| `main` | `0x8423F010` | thin: `GetAppInstance(); RunStaticInitializers(); App_Run();` returns `g_appInstance->exitCode` |
| `GetAppInstance` | `0x84179060` | lazy singleton accessor for `g_appInstance` (`DAT_850DEC80`) |
| `RunStaticInitializers` | `0x84133458` | walks init-object list (node+0=vtable, node+8=next), calls each `vtable[0](this)` |
| `App_Run` | `0x83BF4208` | `App_InitMemoryAndVideo → App_Init → while(App_RunFrame()) → App_Shutdown` |
| `App_InitMemoryAndVideo` | `0x83BF3268` | `XGetVideoMode` + 4 heap-pool allocs (0x708000/0x400000/0x500000/0x1600000) |
| `App_Init` | `0x83BF3D60` | big init: `'VCLoader'` launch-data magic; loads global.iff/roster.iff/portrait.iff via `Res_LoadAsset`; registers subsystem factories via `FUN_83ca8d60(id,ctor)` |
| `App_RunFrame` | `0x83BF3B18` | per-frame tick; runs while `App_IsRunning()` |
| `App_Shutdown` | `0x83BF3580` | post-loop teardown |
| `Res_LoadAsset` | `0x83BEF448` | `(mgr, dest, id, wchar* name, ...)` asset loader w/ completion callback; real mgr = `FUN_841d3f28()+0x28880` |
| `App_IsRunning` | `0x84109D08` | returns `g_appInstance->field_4 != 0` |

**`AppInstance` struct (`g_appInstance @0x850DEC80`):** `+0x0` = exitCode, `+0x4` = isRunning flag.

**Architecture note:** a large global-state block is reached via the `FUN_841d3f2x()` accessor family (`841d3f28/30/34/38/3c`, renamed `GetGlobalState`, ~250 callers). Subsystems live at fixed offsets in it (resource mgr @ `+0x28880`; the D3D device struct is another sub-region). The sibling accessors decompile as empty stubs (return reg not recovered) — don't trust individual ones. Per-frame context global = `DAT_84c7e608`.

### Graphics / GPU runtime (~75 functions, exhaustively mapped)

All `Vd*` kernel imports are present and named (`VdSwap @84263bec`, `VdInitializeRingBuffer @84263c5c`, `VdEnableRingBufferRPtrWriteBack @84263c4c`, `VdSetGraphicsInterruptCallback @84263e4c`, `VdInitializeEngines @84263e5c`, `VdGetSystemCommandBuffer @84263bfc`, …). Highlights of the named D3D9/Xenon runtime — the layer the PC recomp (doc 11) leans on hardest:

- **Ring buffer / setup:** `Gpu_InitRingBuffer @0x841BACF8` (calls `VdInitializeRingBuffer` + `VdEnableRingBufferRPtrWriteBack` — sets the RPTR-writeback address, the recomp's ring blocker), `Gpu_InitEnginesAndInterrupt @0x841D08B8`, `D3DDevice_Swap @0x841B83E8`.
- **Ring/command core:** `Gpu_WriteRingBuffer @0x841B9620`, `Gpu_WaitForRingSpace @0x841B9570`, `Gpu_SubmitAndWaitForRing @0x841BA090`, `Gpu_AdvanceCommandBuffer @0x841BA980`, `Gpu_WaitForIdle @0x841BAC28`, `Gpu_WriteSyncPacket @0x841BA700`.
- **Ring-wait internals (most recomp-critical):** `Gpu_PollWaitRptr @0x841B8C50` (the spin: reads RPTR, watchdog >4999 ticks → recover, returns 0), `Gpu_RecoverFromHang @0x841D3748` (on stall forces RPTR = ringSizeCode-2, clears fence `+0x2b04`, sets `+0x2abd|=3`), `Gpu_WaitBegin @0x841B8B58` / `Gpu_WaitEnd @0x841B8B88` (read `KTHREAD+0x58` = the `unk_58` clock the recomp injects).
- **Draw/state emit:** `Gpu_EmitDraw @0x841CAE80` (PM4 DRAW_INDEX_AUTO 0xc0003600), `D3DDevice_SetRenderTarget @0x841C0658`, `D3DDevice_SetTexture @0x841BFC10` (16 slots), `D3DDevice_SetVertexShader @0x841B4BE8`, `D3DDevice_SetPixelShader @0x841B4A28`.

**D3D device-state struct** (a sub-region of the `GetGlobalState` block) key fields: `+0x2a90` = ptr→RPTR write-back buffer (`**(device+0x2a90)` = live GPU read pointer; `+0x3c` within it = RPTR), `+0x2acc` = ring WPTR index, `+0x2b04` = GPU fence/pending-op count, `+0x3a30` = ring base, `+0x39e8/+0x39ec/+0x39f0` = depth/front/back surfaces, `+0x5428/+0x542c` = width/height. The RPTR write-back read path is exactly the recomp ring blocker discussed in doc 11.

**Coverage:** the D3D9/Xenon GPU runtime is exhaustively mapped (~75 fns). The **game renderer layer** (scene/mesh/material code that *calls* this runtime) is a separate subsystem, not yet mapped.

---

## VCFILEDEVICE — the file-I/O class hierarchy

The Visual Concepts engine's file layer, recovered from descriptive `Class::Method` error strings (`@0x83b39a10..` / `0x83b3a840..`) — one of only two "gold" 1:1 evidence veins in the binary.

- **`VCFILEDEVICE`** (abstract base, cluster `0x8414E908`–`0x84150250`): `OpenForRead @8414E9C0`, `OpenForWrite @8414EB50`, `OpenForAppend @8414ED18`, `CreateForWrite @8414F0E8`, `Read @8414FDE8`, `Write @8414FF88`, `Close @84150130`, `MarkHandleClosed @8414E908`, **`ReadAndDecompress @84150210`**. Public methods are non-virtual wrappers that validate the handle, then dispatch to the device vtable impl.
- **`VCWIN32FILEDEVICE`** (concrete, Xbox-kernel-backed, cluster `0x84177948`–`0x841787A0`): `OpenForRead @84177948`, `OpenForWrite @84177A48`, `OpenForAppend @84177B30`, `CreateForWrite @84177C18`, `DeleteFile @84177CF8`, `CreateFolder @84177DA8`, `DeleteFolder @84177E60`, `Rename @84177F10`, `GetFileInfo @84178310`, `Close @841785B0`, `Read @84178660`, `Write @841787A0`.
- **`VCMEMORYFILEDEVICE`** (in-memory, cluster `0x84150DD8`–`0x841510C8`): `OpenForRead @84150DD8`, `DeleteFile @84150E80`, `Rename @84150F08`, `GetFirstFileInfo @841510C8`.
- **`VCKERNELFILEDEVICE`** (OS backend): `VCKernel_OpenFile @841DEF48` (`NtCreateFile` via dispatch table `PTR_DAT_84b009f8+0xc`), `VCKernel_ReadFile @841DF140`, `VCKernel_GetFileSize @841E1EC0`. Close uses `wrap_NtClose` directly.

**The decompression path:** `VCFILEDEVICE_ReadAndDecompress @0x84150210` uses decompressor `Function_8414BDD8` (0x22200-byte workspace). The engine-wide analog is `VC_Decompress` — the **0x0E4837 dispatcher** — which fans out to `VCDecompress_Codec1..15`. That is the same custom flag-byte LZ77 compression documented in the archive/IFF findings; the file device is where it's invoked at load time.

**`VCFILEHANDLE` struct** (defined by `CreateVCFileTypes.java`): `+0x00` u64 fileSize, `+0x08` u64 position, `+0x10` `VCFILEDEVICE*` device (0=closed), `+0x14` u32 openMode (0=closed/1=read/2=write/3=append), `+0x24` u32 osHandle, `+0x28/+0x2c` device-data.

**Device vtable offsets:** `+0x20` IsDevicePresent, `+0x28` IsMediaPresent, `+0x2c` CanRead, `+0x15c` OpenForRead-impl, `+0x1b8` Close-impl, `+0x1bc` Read-impl, `+0x1d0` ReportError, `+0x1ec` BuildNativePath. Vtable instances: WIN32 @`0x82007498` (+ `0x83acf470/660/850` variants), MEMORY @`0x82001d04`.

**Caveat:** these methods fetch the device from the global-state accessor family (`FUN_841d3f3x`), **not** from `this`/`param_1` — so `param_1` is nominal, and some bodies still show raw `+offsets` because the code recasts the handle to `ulonglong*` internally.

---

## The evidence-only naming sweep

A directive to reverse-engineer the whole project by naming every *grounded* function. Method and status:

- **Policy:** **evidence-only** naming (leave true evidence-free leaves as `FUN_`; do not mass-apply generic names), done **manually** one function at a time (no naming scripts). One `.txt` report per system in `C:\Users\cloug\Documents\NHL 2k10 Extracted\Nhl2k10_Findings\`.
- **Scale reality:** ~10,000+ unnamed `FUN_`/`Function_`. Most of the tail is evidence-free leaves (single-field setters, array getters, jump-table trampolines) — so literal "zero FUN_" is unreachable; the deliverable is every *grounded* function named. As of the last snapshot, ~881 functions were named across the sequential sweep of `0x83B4xxxx`–`0x83B8xxxx` (VC engine core: animation/clip, geometry/math, intrusive lists, resource relocation, input, audio mixer/voice/bus, id-tables, presentation/stats).
- **Best evidence veins:** C++ `Class::Method` trace strings = 1:1 gold, but **exhausted** — only the Massive online-ad SDK and the VC file devices have them. There is **no RTTI and no mangled C++ names** in this binary (release build; only `std::bad_alloc`/`std::exception` present). `list_imports` is empty of useful names. Remaining lower-yield veins: descriptive log strings, behavioral patterns (crc/memcpy), caller/callee propagation, vtable walks.
- **Key blocker:** the `__FILE__` source-path assert strings (`d:/builds2k10/vcsports/nhl/code/...`) give a master module map (aigamelib: ai/{clock,control,fight,brain,strategy,tactical}, event, physics, gamemodes) but have **no Ghidra xrefs** — they're loaded via split `lis`/`addi` PPC pairs, so they can't be turned into per-function names by xref.

**Systems completed (reports `00`–`07` in `Nhl2k10_Findings\`):** 01 Asset/Texture/IFF (~118 fns), 02 Massive online-ad SDK (~50 fns), 03 file devices, 04 commentary/audio-event, 05 debug command channel, 06 AI game-library module map, 07 animation/motion (in progress). Later grind covered the dense VC clip/anim/stats module through ~`0x83B8Bxxx`.

---

## Game-window-icon XEX patch

The icon Xenia shows in the window title bar / taskbar while NHL 2K10 runs is the game's **Xbox 360 title icon**, embedded in the XEX — **not** a Xenia setting. It is an XDBF image resource: **namespace 2 (image), entry id `0x8000`, a 64×64 PNG**.

- In the user's decompressed `default.xex` the icon PNG sits at file offset **`0x24E8088`, max 8182 bytes** (the XDBF resource block itself starts at `0x248E000`). Xenia re-reads it from the XEX every launch — there is no cache to clear (`xenia_master\Icons` is the Xenia *manager*'s library cache; `content/54540853` is saves).
- **Launcher feature (shipped):** the Mod Launcher bundles `NHL 2k27 Game Icon.png` and, on startup, calls `archive_textures.ensure_game_icon(game_path, ...)` to keep the XEX title icon set to that art. Implementation:
  - `_find_xex_title_icon(data)` locates the icon **dynamically** (scan for "XDBF" → parse 24-byte header → 18-byte entry table `>HQII` = ns,id,off,len → `data_base = ent + etl*18 + fstl*8`; match ns==2, id==0x8000, verify PNG signature). A rebuilt XEX still works — no hardcoded offset.
  - `_encode_icon_fit` writes a 64×64 PNG ≤ maxlen, palette-reducing (256/128/64) if a truecolor PNG is too big.
  - **Idempotent:** only writes if the current icon's decoded pixels differ. One-time `default.xex.iconbak` backup before first write; catches `PermissionError` (XEX locked while the game runs → logs, applies next time the game is closed).
- **Standalone manual tool:** `NHL 2k10 Extracted\set_game_icon.py <img> [xex]` (hardcoded `0x24E8088`/8182). Constraint: any custom icon PNG must be ≤ 8182 bytes (trivial for 64×64).

---

## Open questions / caveats

- **Naming is incomplete by design** — the function maps above are a snapshot of *grounded* names; thousands of evidence-free leaf functions remain `FUN_`. A later directive shifted toward generic structural names (address-suffixed, e.g. `Tbl84b07f7c_ClearRecord`) for the remaining leaves, but the bulk of the tail is unnamed.
- **The game renderer layer is unmapped** — the GPU runtime is exhaustively named, but the scene/mesh/material code that drives it is untouched.
- **AI game-library naming is blocked** — the `__FILE__` module map exists but can't be xref'd to functions (split-load strings); per-function naming there would need slow decompile-and-infer.
- **The bridge cannot re-disassemble or create types** — all VMX128 cleanup and struct definition must run inside Ghidra via script; only rename/prototype/comment work over MCP.
- **Addresses are binary-specific** — every VA here is for this exact `default.xex` (base `0x82000000`). A rebuilt/re-packed XEX shifts file offsets (the icon finder handles this dynamically; raw offset tools like `set_game_icon.py` do not).
- **Bridge must be running** — `mcp__ghidra__*` calls fail with connection errors unless Ghidra + the HTTP server plugin (port 8080) are up.

### Old-doc note

There is no prior standalone RE-infrastructure doc to supersede. `docs/07_pc_recompilation_status.md` lists the memory notes this doc consolidates (`project_xex_function_map`, `project_vcfiledevice`, `project_re_sweep`, `project_ghidra_bridge`, `project_bad_instructions`, `project_game_window_icon`) but does not itself cover the toolkit. The VMX128 bad-instruction *taxonomy* is documented in the companion PC-recomp doc (11); this doc covers the Ghidra *language install* side that makes those instructions decode.
