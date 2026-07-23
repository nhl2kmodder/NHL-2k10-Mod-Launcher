# 09 — Asset discovery & live capture

One-line summary: A full sweep of the archive TOC classified every `.iff` the game references and turned up ~1147 texture assets the launcher never exposed; because many packs store their texture offsets nowhere in the file, three separate mechanisms are used to recover them — stored file offsets, live capture from the running game (record `+0x6C` VRAM pointers), and GPU-trace extraction for loader-repacked atlases.

Status: **mostly verified, some parked.** The TOC sweep, the `.iff` classifier, the `disc_<crc>` launcher wiring, live capture (`live_capture.py`), and the GPU-trace dumper (`nhl2k10_trace_dump.py`) all work and are validated. Still open: the `0xF0985030` atlas full-extract, the FF3BEF94 arena interiors, and a persistent (non-restart) way to replace repacked atlas pixels. One old-doc claim is corrected below (live VRAM patch is **not** viable).

---

## 1. Classifying every `.iff` (the definitive method)

Given an `.iff` name, decide what it actually is:

1. Resolve the name in the TOC: `crc32(NAME.IFF uppercased)` → `0A` TOC entry (see finding 01 for TOC layout).
2. Read the resource header. Magic `0xFF3BEF94` = standard IFF → parse the section table at `0x20` (stride `0x20`, type at `+0`). **A `0x411536D5` (VRAM) section present ⇒ it is a texture asset.**
3. If still ambiguous, decompress the `0x0E4837` blobs and count valid Xenos fetch constants: `(d0 & 3) == 2 && (d1 & 0x3F) in _FETCH_FMT` with sane dims. `>0` ⇒ texture-bearing.

Caveat: the naive "audio-bank" heuristic (dwords that equal catalogued `1B` offsets) **false-positives on texture packs** (goalie helmet/pad/blocker/catcher descriptors look like bank offset tables). Always confirm with the section table or fetch scan, never the offset heuristic alone.

### Container types found

- **Standard IFF texture** — magic `0xFF3BEF94`, has a `0x411536D5` VRAM section. Editable through the launcher's IFF Textures tab.
- **`0xF0985030` logo/portrait/icon container** — NOT standard IFF. A loader-repacked *index*: section `dataoff` points past EOF, low entropy (~5.1, not DXT), no `0x0E4837` blob. **Pixels live in VRAM, not the file.** Parser `launcher/f0985030.py` reads the header (`entry_count@0x18`, descriptor array `@0x20`, VRAM-index array `@0x24`, section table `@0x28`). Confirmed entry counts: `iconnav` 58, `logos_large` 120, `portrait.iff` 1478. Full pixel extraction still needs GPU trace (§5) or live VRAM capture.
- **Descriptor / non-texture** — `frontend.iff` (menu loads via the cataloged `frontend_sync.iff`), `bootup.iff`/`bootup_dummy.iff` (magic `0xE4791207`), `Director.iff`/`DirectorCareerMode.iff` (presentation scripts), `overlay_set.iff`, `onlineavatarindex.iff`.
- **Audio descriptor banks** — `pasfx.iff`, `sfx_arena.iff`, `paintro.iff`, `loading_audio.iff`, `bootup_audio.iff`, `arena_<code>.iff`. Event/cue → `0x800`-aligned offset into raw-XMA `1A/1B` (see the audio findings). Not textures.
- **Data / localization** — `loc.iff` (team display names + all UI text), `droster.iff`, `roster.iff`.
- **Runtime / device / DLC (not in the clean TOC)** — `snapshot_goal/save_{lefty,righty}{N}.iff` (runtime replay stills), `team2k.iff` (`twokphoto:`), `serverplaylist.iff` (`playlist:`), `UPDATE:*PATCH.IFF`, `TMP:tr/ts%06d.iff`, `rostermem:roster/droster.iff`, `titlepage_bootup/career.iff`, `biggs_trailer.iff`.

---

## 2. The TOC texture sweep — ~1147 undiscovered assets

Date: 2026-07-05. One sweep over all **2407** TOC entries of the pristine archive (`0A/0B/1A/1B`).

- **2026 texture-bearing assets total.** The launcher previously exposed **879**. That left **1147 undiscovered** (≈**4142** individual textures inside them).
- Method: parse the TOC by index, map `f3 × 0x800` → concat offset → archive + local offset, walk the `0x0E4837` blob **headers** (read `dec_sz` from the header — do not decompress the giant VRAM), decompress only the small DRAM record blob, count fetch records via the `count@0x20` tree + a scatter scan + a `_find_fetch` single-primary fallback.

### Recovering real names (dictionary attack)

`crc32` is one-way, so the real `.iff` filenames were recovered by a **dictionary attack using the game's own format templates**. The templates are **not** in `default_flat.xex` (its code/rdata is still packed — 0 `.iff` strings). They **are** in the Ghidra project (properly XEXLoaderWV-loaded `default.xex`) as ASCII strings around **`0x83B197xx` / `0x83B30xxx` / `0x83B37xxx`–`0x83B38xxx`**; pulled with `list_strings filter=".iff"` / `"{0"`. Key templates (.NET `{0:D4}` = zero-pad):

- `player_head_id_{0:D4}.iff` — player portraits/faces (165 exist, IDs sparse; +8000–8003 cap)
- `uniform_{0}{1}.iff` / `uniform_base_{0}{1}.iff` — `{0}` = team code, `{1}` = `_home/_away/_alt`. **51 team codes** recovered (30 NHL + intl `aut/bel/can/cze/dmk/fin/fra/ger/kaz/lat/rus/slo/sui/swe/ukr/usa` + `eca/wca/2k allstar`). `uniform_` = 2048×512 DXN 6-tex; `uniform_base_` = 1024²:565 3-tex.
- `pad_g{0:D2}.iff`, `blocker_g{0:D2}.iff`, `catcher_g{0:D2}.iff` (+`_logos`/`_logos_l/_r`) — goalie gear
- `helmet_g{0:D2}_pattern_{1:D2}.iff` — goalie masks
- `snapshot_goal_lefty{0:d}.iff` / `_righty` / `snapshot_save_*` — replay stills (202)
- `arena_{0}`, `led_{0}`, `rink_{0}`, `ice_{0}_{1}`, `zamboni_{0}`, `zamboni_team_{0}` — team-keyed
- `logos_large/medium/small.iff`, `ADSPACE_{0:D2}`, `logo_{0:D2}`, `{0:d4}_image`

**335 of 1147 named deterministically.** The remaining families were confirmed by decoding samples but their filenames are **not crc-recoverable** (parameter scheme unknown):

- **461 uniform_component** (2048×512 4444/DXN — jersey numbers, logo, Stanley Cup/NHL/CCM patches, A–Z lettering atlas)
- **172 uniform_base / scenery** (1024²:565)
- **71 lone 512²:DXT4_5** — country flags + decals (Czech flag decoded)
- **9 atlases** (256²:DXT1, 64–67 grayscale tiles = crowd/lighting/spotlights, ~521 tex)
- **28 banner/scoreboard**

Editing does not need the name — decode/preview/replace all work off `(archive, local_off)`.

### Launcher wiring — `discovered_assets.csv` + `disc_<crc>` alias

All 1147 now show in the IFF Textures tab. Generated `launcher/data/discovered_assets.csv` (rows: `team, category, iff, archive, offset, size, resolved, label, crc32`) with the real name where recovered, else a synthetic `disc_<crc8>.iff`. In `archive_textures.py`:

- new `DISCOVERED_CSV`;
- `_crc_alias()` builds `{SYNTH_NAME.IFF → real crc}` for rows where `crc32(iff) != crc32col`;
- **`resolve()` checks the alias before `crc32`**, so synthetic names route to the real TOC entry → decode/preview/extract/replace all work unchanged (everything funnels through `resolve`);
- `load_catalog()` appends discovered rows and keeps their label.

Filter categories: `uniform_components`(461), `uniform_base_extra`(172), `player_portraits`(165), `unknown_textures`(80), `flags_decals`(71), `goalie_gear`(58), `uniforms`(38), `uniform_base`(36), `banners`(28), `logos_extra`(23), `atlases`(9), `goalie_masks`(6). Teams column: `uniforms/players/misc/goalie/arena/frontend` + real codes.

Deliverables live under `Nhl2k10_Findings\10_Undiscovered_Textures\`: `DISCOVERY_REPORT.md`, `undiscovered_inventory.csv` (1147 rows), `named_assets.csv`, `undiscovered_textures.csv`, `samples/` (decoded PNG proofs).

Caveat: 8888/hi-res auto-apply is still gated by pack type — uniform-component packs' grow-capability was untested in-game at the time. Verify a replace in-game before trusting hi-res on those specific packs.

---

## 3. How texture offsets get resolved — three mechanisms

A texture record's VRAM offset lives at `+0x6C` in its `0xE0`-byte descriptor. Whether that field is usable from the file alone splits every pack into three cases:

### (a) Stored-offset packs — offsets are real file offsets
`overlay_static.iff`, `titlepage.iff`, and most standard IFF packs store **ascending, non-overlapping file offsets**. `_stored_offset_records` reads them directly; decode is crystal clear straight from the file (verified: `titlepage.iff` decodes "PRESENTS / NHL 2K10 / Ovechkin"). This is the normal, offline-editable path.

### (b) Loader-repacked packs — offsets resolved at runtime, recovered by live capture
Some packs' `+0x6C` is a **runtime, per-group VRAM offset** (or a placeholder `0x1` = unresolved). Offsets can even **overlap between groups** because each group is a separate GPU allocation loaded at a different time. Decoding at those file positions yields scrambled garbage. `Loading.iff` is the canonical example (§4).

Recovery = **`launcher/live_capture.py`**: with the pack resident in the running game (Xenia), sweep guest RAM and content-match the resolved VRAM blob back to the file record (byte-identical VRAM→file blob). Output → `live_capture/live_offsets.json` → `archive_textures.catalog_records` → `list_textures`.

- `capture()` originally located the loaded records blob via `fdram[:16]`, which is too weak for some packs (e.g. `gamedata` header = `00009d91` + 12 zeros → false "not loaded"). Fix: **`_strong_sig(dram)`** = a distinctive 24-byte 16-aligned window (≥14 distinct nonzero bytes); when the `[:16]` scan fails, scan for the strong window and back-compute `rec_base = match − offset`. That unlocked `gamedata`/`frontend_sync`/etc.
- `list_textures` now `if len(cat) > len(recs): return cat` — prefers the live catalog when it holds **more** textures than the file tree (so `Loading.iff` lists its 3 captured records, not the 1 bogus runtime-resolved "primary"). Only fires when a catalog exists (no regression).

**Live-capture catalog (per-iff unique file offsets, 2026-07-03):** `global.iff` 428, `gamedata.iff` 95, `frontend_sync.iff` 37, `playercreate.iff` 11, `Loading.iff` 3, `crowdanim.iff` 1 (= 575). Plus `overlay_static.iff` 18 which is offline-decodable (stored offsets, already listed).

**Residency map (Xenia, offline Play Now):** `global`/`overlay_static`/`online` resident in gameplay; `gamedata` + `frontend_sync` resolve around the menu/player-create area (not on-ice, not the per-game loading screen); `crowdanim` in gameplay (only its 1 standard 1024² 565 tex resolves — the rest is the custom sprite atlas). `franchise.iff` + `online.iff` load DRAM metadata but their textures don't resolve on screens tried (likely reuse `global.iff` art) — still uncaptured. Gotcha: `overlay_static.iff` has DRAM > VRAM, inverting `live_capture`'s `min=DRAM/max=VRAM` assumption — it's offline-decodable so N/A here, but any future DRAM>VRAM pack needs that handled.

**Tooling:** `launcher/capture_watch.py` — one memory sweep per cycle checks strong sigs for ALL target packs at once, then runs the content-matching `capture()` only for resident ones; survives Xenia restart (re-acquires PID). Run it, then just navigate the game; packs accumulate on Reload with no rebuild. `live_capture.py <iff>` = single one-shot.

### (c) Loader-repacked *atlases* — pixels not in the file at all, recovered by GPU trace
The `0xF0985030` containers (`portrait.iff`, `logos_*.iff`, `iconnav.iff`) are pure indices — pixels are runtime-filled into VRAM and never appear in any decodable file blob. Live capture (b) can't content-match them because there is no source blob in the file. These need a GPU trace (§5).

---

## 4. `Loading.iff` — a scene-graph asset (the archetype for case 3b)

`Loading.iff` (`0A:0x101F800`, TOC #1679) is **not** a flat multi-texture pack — it is a **serialized scene graph**, the same class as the scorebug/menu widgets:

- **DRAM blob** (661,920 B, codec 8) = scene graph: nodes carry 32-bit name-hashes (`FCC5F5D4`, `C6B61DE2`, `A0AACBC1`, `13258BBC`…), BE-float transforms (`BF800000` = −1, `3F800000` = 1), and self-relative child pointers (`field_addr + val − 1`). Header `@0x20/0x24` = (count=1, reloff=413) → first `0xE0` texture record `@0x1C0`.
- **VRAM blob** (4,370,432 B ≈ 4.3 MB, codec 7) = all textures for every loading-screen variant, concatenated.
- **20 texture records** (9 stride-`0xE0` groups = scene layers / loading-screen variants), e.g. G0@0x1C0 512² DXT4_5, G2@0x11C20 mixes 128²·8/256² DXT4_5/64²/256×64 DXT1, G7@0x86400 512² DXT4_5, etc.

The launcher showed only 1: `_texture_tree` count `@0x20` = 1 → 1 record; `_scatter_records`' exact-VRAM-fill self-validation rejects it (offsets don't tile) so it stays primary-only. It won't decode from the file because each record's `+0x6C` is runtime/per-group (overlapping between groups) — decoding gives scrambled "green-top/grey-below" garbage. The decode pipeline itself is proven correct (`titlepage`/`overlay_static` decode cleanly), so `Loading.iff` is uniquely the runtime-resolved case.

**Live capture (partial, done):** `live_capture.py Loading.iff` recovered 3 real textures by content-match: file `@0x15B000` 256² DXT4_5 = **2K Sports logo** (dup=2), file `@0x305000` 256² DXT4_5 = glow gradient, file `@0x424000` 128²·8 = animation stripe mask — all decode crystal clear. These offsets match no file-structure guess, confirming 100% runtime resolution.

**Key residency finding:** `Loading.iff` is loaded **only during the 2K Sports boot splash** — not during menus, per-game loading screens, or the boot logos after the 2K one (Visual Concepts / NHL-title splashes are separate assets). Only the 2K scene's 3 records get `+0x6C` resolved; the other ~17 belong to scene groups that don't activate in the boot/menu/load paths tried. To get them: capture while their specific scenes are active (online/franchise/draft loads?), or offline brute-force (`_loadbrute.py`: flat-block-fraction + mip-chain scorer, calibrated on the 3 known).

---

## 5. GPU-trace extraction (case 3c — repacked atlases)

Status: **method proven** (2026-07-03), then parked. This is the tool for loader-repacked atlases whose pixels live in VRAM, not the file.

**When to use vs. the file pipeline:** file pipeline for any standard IFF whose pixels are in the file (team logos `logo_<code>.iff`, uniforms, rink/led, goalie masks+gear, overlays). GPU trace for `0xF0985030` assets (`portrait.iff` = 1478 portraits, `logos_*.iff` atlases, `iconnav.iff`) and anything where `list_textures` finds nothing and the pixels are runtime-filled.

**Capture a trace:**
1. Find the Xenia the game actually runs from. In this project the live one was `xenia_canary.exe` — **but canary did NOT emit a trace**; the working build was **`xenia_master\Xenia Stable`** (trace landed in its `scratch\gpu\`).
2. With Xenia **closed**, set `trace_gpu_stream = true` in that build's `*.config.toml` (Xenia **rewrites config on exit**, reverting it — set it while closed). Trace → `<xenia>\scratch\gpu\<titleid>_stream.xtr`.
3. Launch, navigate to the screen that renders what you want, let it draw a few seconds (continuous tracing ≈ 2 GB for a few seconds — get there promptly).
4. Dump: `python launcher/nhl2k10_trace_dump.py <trace.xtr> <out_dir> [max]` — 2-pass streaming (pass 1 collects every texture fetch fmt/dims/base; pass 2 decodes only the needed memory → PNGs `<fmt>_<w>x<h>_0x<vram_base>.png`).

**What one player-screen trace yielded:** 64 textures via the verified `GetTiledOffset2D` tiler —
- Player **portraits** — 256² DXT4_5 at `0x05AD5000`, `0x05AF5000`, `0x05B15000`
- Team-**logo atlas** — 18× 256² DXT4_5, contiguous `0x0BFC6000 → 0x0C2C6000`
- Menu **icons** (`iconnav`) — 256² DXT4_5, contiguous `0x0B143000 → 0x0B1D3000`
- also a 2048² player render, NHL 2K wordmark 2048×1024, loading photos 256×133, uniform bases, HUD atlases, font sheets, masks. Sorted output: `trace_textures_portraits/_sorted/{portraits,logos,icons,art_photos}`.

**The pattern:** each category is a **contiguous VRAM atlas of same-size textures** — portraits stride `0x20000`, icons `0x10000`, logos `0x20000`. A screen that loads a batch loads them back-to-back. **To get all 1478 portraits, trace a roster / line-combo / trade screen** (dozens load at once); the single-player screen only rendered ~4.

**Source mapping (Ghidra):**
- **Logos** — atlas index = `logos_*.iff`, but the pixels are `logo_<code>.iff` (per-team, standard IFF, already editable). Verified: atlas Bruins == `logo_bos.iff` downscaled. → **logos are already replaceable via the file pipeline; ignore the atlas.**
- **Portraits** — `portrait.iff` (F0985030 index, referenced once in `App_Init`) + the `twokphoto:` device. No per-player file / editable pixel blob — resolved at runtime (possibly render-generated).
- **Icons** — `iconnav.iff` (F0985030 index), same repacked story.

**Guest→host memory map (Xenia, validated):** host = `0x100000000 + guestVA`; guest physical `P` → host `0x1A0000000 + P` (`launcher/xenia_mem.py`, `PHYS_BASE 0x1A0000000`).

> **Correction to old doc 11:** doc `11_gpu_trace_extraction.md` listed "Live VRAM patch (feasible now)" as a way to replace portrait/icon pixels. A live test on 2026-07-06 **disproved this** — see §7. Persistent replacement of repacked atlas pixels requires a loader hook (recomp/XEX), not a memory poke.

---

## 6. The VFS override-device chain (the engine's built-in file-override hook)

Mapped 2026-06-17. **Asset opens go through a chain of override devices.** Each device checks its own list and, on a name match, serves an **external** file; otherwise it falls through to the next device (`(**(code**)(*dev[7]+0x78))(dev[7],handle,path)`), ending at the main `0A/0B` archive device.

- **DlcDevice** — `DlcDevice_OpenForRead @0x83BA74A8`, `_Read @0x83BA77C0`, `_Close @0x83BA7730`. Checks list `DAT_84B7CFF8` (count, max 0x20) / `DAT_84B7CB74` (entries, 0x24B each = `[int contentId][16×UTF16 name]`). On match: builds `dco:\<name>` and opens via `VCKernel_OpenFile` (the DLC content device) → the override file lives in the mounted DLC content package; else falls through. Live strings `@0x83B19698` = `"dcr:\roster.dat"`, `"dcr"`, `"dcr:\*"`.
- **PreloaderDevice** — `PreloaderDevice_OpenForRead @0x83D34398` (device `@0x84D198B4`, vtable `0x8203BA60`). Its content list is an **in-memory object `DAT_850AE688`** built by engine code, **not** a manifest file — it is a **cache of existing archive assets**, not a new-file hook. Not usable for adding files.
- **Registrar** `DlcDevice_RegisterOverride @0x83BA79F0(contentId, asciiName)` — appends an override entry. Name cap = **18-char UTF16 buffer** (stride 0x24, check `< 0x10`).
- **Enumerator** `Function_83BA7B28 @0x83BA7B28` — opens a DLC content package (XContentCreate by id; package list `DAT_84B7B7F4`, max 16), enumerates files, and registers **only `roster.dat`** as an override. So **stock, the only overridden file is `roster.dat`** — the mechanism itself is filename-general.

**Consequence for modding:** "CustomTextures" is the right design and the engine supports the pattern, but textures aren't registered out of the box. Two viable homes:
- **Recomp (`C:\NHL2k10_Recomp`, cleanest):** add a small override VFS device / mod-folder that intercepts asset opens by name from a `CustomTextures\` dir — no archive edits, no size cap.
- **Xenia (where edits are tested):** patch the name-emitter to emit a **prefixed** name that routes to an external device. The loose-file device already exists: **`VCWIN32FILEDEVICE_OpenForRead @0x841779A8`** builds a native path (root + filename) → `VCKernel_OpenFile @0x841DEF48` (thin `NtCreateFile` wrapper) → the game already reads its own `0A/0B` archives as loose files this way, so **loose files in the game folder ARE readable by the engine.** Cleanest ADD build: code-inject a `VCWIN32FILEDEVICE` rooted at `<gamefolder>\mods\`, register it (`VFS_FindDeviceByHash @0x84151F38`), and patch the asset name-emitter to emit `mods:\<name>` for custom slots only (bare → archive for stock).

**New MAIN archive (2A) = not recommended:** the `0A` header's archive table (4×`[size,0,nameUTF16,0]` `@0x18`) abuts the TOC at `0x58`, so the layout looks fixed at 4; adding a 5th shoves the TOC and needs loader cooperation. Override device or `1B` relocation are cleaner.

**Related hard finding (from the goalie-mask ADD, finding 13-goalie doc):** archive-**append** is dead under Xenia — the loader will not serve bytes appended past an archive's original size (in-bounds edits render; appends are invisible). Two real sub-findings that *did* work: (1) the TOC is binary-searched by name-hash, so a new entry must be **sorted-inserted**, not appended; (2) an entry aliased to existing in-bounds data renders. External override / loose-file device is the supported way to add net-new files.

---

## 7. Live texture patch — NOT viable (tested, corrects old doc 11)

2026-07-06: tested replacing a loaded texture's pixels in guest RAM to refresh the running game without a restart. **Conclusion: guest-memory patching does NOT refresh the display. Not viable.**

- Found `logo_van` at guest `0xB1373000` / host `0x1B1373000` (512² DXT4_5, mip0 256 KB). `write_bytes` succeeded (page protect RO — Xenia does guard it).
- Painting the whole mip0 to `0xFF` (verified 262144/262144 bytes = FF, stable 1.5 s): the logo **still rendered perfectly.** The GPU draws from a decoded, cached D3D12 texture **decoupled** from the guest RAM we can reach.
- Both `xenia.exe` and `xenia_canary.exe`: `GetWriteWatch` = 0 hits, `AddVectoredExceptionHandler` = present → Xenia tracks guest writes with **VEH/SEH page guards**, not kernel `MEM_WRITE_WATCH`. An external `WriteProcessMemory` (which uses `VirtualProtectEx`) does not trip that watch; an injected fault-touch thread didn't either (Xenia handles watch-faults inside its guest-CPU threads).

What live editing would actually take (all big/fragile): a DLL injected into Xenia that hooks its `TextureCache`; hijacking a guest-CPU thread via `SetThreadContext` to do the write; or a Xenia build with native live-replace. Not worth it vs. the reliable offline flow. **Shipped instead:** a "Restart Required" prompt at the end of every apply, only when NHL 2K10 is live in Xenia (`App._xenia_game_pids()` matches title `54540853` / "NHL 2K10" AND owning process starts with `xenia`).

---

## 8. Arena interiors — TODO (FF3BEF94 raw-VRAM scenes)

2026-07-12. The FF3BEF94 arena interior scenes are the one texture territory that **cannot** be cracked from the file alone. Structure: a tiny compressed DRAM node tree (~12 KB, at file offset = header `@4`) + a huge **raw, uncompressed, tiled-DXT** texture tail (~15 MB) to EOF. The DRAM tree holds **no** standard Xenos fetch constants (0 found), so texture dims/offsets in the raw tail are unknown from the file — the game resolves them at runtime. Same class as `titlepage.iff` / ice surfaces, which were done via GPU trace.

Entries (catalogued `category=scene_arena, team=scenes` in `discovered_assets.csv`):
`disc_149cd4f2` (toc#217), `disc_6e5c8792` (#1045), `disc_29fcfd42` (#409), `disc_533cae22` (#820) — the 4×15 MB arenas; plus `disc_0ec99461` (#148, 2.6 MB), `disc_01d45ee5` (#16, 702 KB), `disc_a4fc6431` (#1549, 552 KB).

**Recipe to crack (~2 min):** load the game to an arena → CE-dump 512 MB guest RAM (`0x1C0000000`–`0x1E0000000` in 4×128 MB slabs) → run the fetch-constant scan (`ram_fetch_scan.py` / `ram_sheet_all.py` pattern) to inventory resident textures with dims → content-match / needle-match each raw-tail region to identify + get dims → add per-texture records (like a captured multitex catalog). OR set `trace_gpu_stream=true` in Xenia Stable while closed, play into an arena, parse the `.xtr` with `nhl2k10_trace_dump.py` (§5).

---

## Open questions / caveats

- **`0xF0985030` full extract not done.** The parser reads the index (counts confirmed: iconnav 58, logos_large 120, portrait 1478) but pixel extraction still requires a GPU trace or live VRAM capture. Team logos are already editable via `logo_<code>.iff`, so the atlas is only needed for portraits/icons.
- **Portraits/icons have no editable source file** and (per §7) can't be live-patched. Persistent replacement needs a loader hook (recomp/XEX). Portraits may even be render-generated rather than stored.
- **Arena interiors (§8) uncracked** — need a live RAM dump or GPU trace to recover texture dims.
- **~812 of 1147 discovered assets are unnamed** (`disc_<crc>`) — editable but not crc-name-recoverable (uniform-component/base/flag parameter schemes unknown). This does not block editing (works off archive+offset).
- **Hi-res / 8888 auto-apply** on uniform-component packs was untested in-game at sweep time — verify a single replace before trusting it on those packs.
- **`franchise.iff` / `online.iff` textures never captured** — they load DRAM metadata but their textures don't resolve on screens tried (likely reuse `global.iff` art).
- **~17 of `Loading.iff`'s 20 records uncaptured** — only the 2K-splash scene's 3 activate in boot/menu/load; the rest need their specific scenes resident, or offline brute-force.
- **Superseded old-doc claims:** doc 10's "all addresses accounted for" framing predates the +1147 sweep — the catalog was NOT complete. Doc 11's "Live VRAM patch (feasible now)" was disproven 2026-07-06 (§7).
