# 08 — Scorebug, overlay scenes, and the front-end / menu system

One-line summary: the in-game scorebug, all HUD overlays, and the front-end menus are one hash-keyed scene-graph system; `overlay_static.iff` holds the entire overlay scene graph (513 nodes) plus its texture atlas, per-element scorebug layout is editable via a serialized Maya-style scene, and menu/logo/jersey/crowd assets each have a known (and sometimes surprising) storage story.

Status: **mostly verified.** Whole-bug placement, per-element scorebug X/Y (mesh + text), text color, whole-atlas recolor, texture replacement, logo bundles, the jersey map, and **Shots-On-Goal on the bug (working in-game 2026-07-28, §5)** are all confirmed in-game. **Active/incomplete:** SOG FPS choppiness A/B, font-redirect (`+0xDC`) and glyph-scale (anim consts) in-game verification. A few per-element behaviors (period "1st" and the tens digit are runtime-re-anchored) are flagged below.

---

## 1. The scorebug is a scene-graph overlay node

The in-game scoreboard ("Scorebug") is a UI overlay node in the **same scene-graph / hash-keyed system as the front-end menus**. Identity strings `@0x83b382xx`: "Overlay: Scorebug" / "SCOREBUG" / "ScorebugObject".

**Name hashing:** `crc32` of the **UPPERCASED ASCII** name (standard table `@0x83acd550`; UTF-16 in, high bytes skipped). Python: `zlib.crc32(name.upper().encode("ascii")) & 0xFFFFFFFF`. `hash("Overlay: Scorebug") = 0x66EF5FB6`.
**Intra-scene name refs** (skeleton/mesh names inside a scene) use **crc32 of the RAW case-sensitive** string — a separate convention from the asset/node-lookup hash.

**Build / per-frame path** (Ghidra, renamed):

```
Overlay_ActivateScorebug        @0x840C07C0  → hash "Overlay: Scorebug"
  Scene_InstantiateByNameHash   @0x83B651F0  → Registry_BinarySearch_849146a0 @0x83B577A8
                                               → Scene_BuildTemplateInstance  @0x83B57EB0
Per frame:
Scorebug_FrameDispatch          @0x840C1518  switch on DAT_850cbd78 (0 deact/1 act/2 update)
  state 2 → Scorebug_UpdateActive       @0x840C1160  (sets root anchor mode 7, pos/scale)
          → Scorebug_BindElementsBySize @0x840C0198  (binds 3 sub-elements, sizes from resource)
Layout each frame:
Scene_LayoutWalkRecursive       @0x83D2A5F0  → Scene_AnchorSolvePosition @0x83D2A2E8
```

The 3 bound element hashes (read live `@0x82090930/34/38`): `0x41267075` (container), `0x869E1938`, `0x564A4B0F` (secondary `0x3D0BD6BB`).

---

## 2. `overlay_static.iff` = the whole overlay scene graph + HUD atlas

`overlay_static.iff` blob0 (DRAM) holds the **entire in-game 2D/3D overlay scene graph — 513 distinct node names**, not just the scorebug. Regenerate the roster anytime with **`launcher/dump_overlay_names.py`** (scans inline UTF-16BE node names, tags `[MESH]` when a geometry header exists, prints crc32-RAW + earliest DRAM offset = which scene cluster the node belongs to). Node offset ≈ scene cluster, so nodes with nearby offsets belong to the same UI screen.

**Overlays sharing this one file** (~20+, all using the same joint/mesh/text machinery, so the scorebug tooling generalizes): scorebug, `stat_panel_group`, `intermission_report`, `shootout`, `camera_options`, `coach_settings`, `online_ticker`, `line_changes`, `zamboni_countdown_clock`, `teammate_grade_meter`, `replay_watermark`, `end_of_period`, `fight_control`, `play_call`, `skills_countdown`, `event_messages`, `pip_window`, `g_meter`, `league_logo`, and more. The scorebug cluster is roughly DRAM `0x214xxx–0x219xxx`.

### Textures (atlas)

`overlay_static.iff` is a contiguous multi-texture pack (`count@0x20 == 0`); **21 sub-textures** (formal resource tree = 18; a full 0xE0-fetch-record scan finds 3 more — `_extra_fetch_records` surfaces them, marked `packing="scatter"` = in-place DXT edit only, never relocate). Each `0xE0` record stores its resolved VRAM offset `@+0x6C` (`offset = ((BE32(+0x6C)−1)//align)*align`, `align=0x1000`). Two blobs: blob0 = DRAM records, blob1 = texture data.

Whole-atlas **recolor works today** (launcher / UI-customizer). **Multi-texture relocation is safe** (`archive_textures.replace_multitex_grow` — append to the last blob, redirect record `+0x6C`, patch section alloc-size, repoint TOC), confirmed boots clean + sharp. Texture labels (scoreclock-relevant): `[9]` silver base panel, `[10]` team-color bar (tinted red/green), `[14]` segment/glint bar, `[0/5/8/17]` glint masks, `[6/11/7]` 2K/brand logos, `[12/13]` glows, `[2]` NHL shield. **Team logos on the bug are per-matchup `logo_<team>.iff`, NOT in overlay_static.**

### Two scorebug mesh classes

- **Flat-quad, vertex-editable** (move/scale/recolor by translating verts; color comes from a team-tinted *texture*, verts are white): `team1_color`/`team2_color`, `team1/2_glint1/2`, `glint_separator`/`_separator1`, `logo_away`, `logo_home`.
- **Complex INDEXED glow meshes** (`_mesh_vertices` returns 0 — index buffer + material + separate vertex buffer; NOT flat quads): `glow_cylinder_color` (a bottom bar), `glow_white`, `logo_2k_mesh`. These can't be moved/recolored like a color bar; they were hidden by zeroing the index buffer (works only for file-index-driven draws).

**The "cyan bar below the scoreclock" is NOT a missed color-bar element** — it is the `glow_white` glow mesh (originally mis-attributed to `glow_cylinder_color`; the off-by-one was corrected via a GPU draw-call trace). The only flat quads on that row (`glint_separator`/`_separator1`) are white. Extra glows `glow_cylinder_color1`, `glow_division1/2` live at DRAM ~`0x1C0000` = a *different* overlay scene — don't touch them for scorebug work.

---

## 3. Whole-bug placement — the proven position lever

Element positions are **computed each frame** by `Scene_AnchorSolvePosition` from an anchor mode + the element's rect/size — they are NOT read from stored on-screen coords. The whole-bug lever is the **global anchor-mode table**:

`DAT_8499EF10` (Ghidra) = file `0x2354F10` in the flat XEX. **10 entries × 8 bytes**, each `(x_type, y_type)` BE u32. `mode0 = (0,0)` = no-anchor; **modes 1–9 = the full 3×3 grid**:

```
1 (1,3) TL   2 (5,3) TR   3 (2,3) TC
4 (1,5) ML   5 (5,5) MR   6 (2,5) MC
7 (1,4) BL   8 (5,4) BR   9 (2,4) BC
x: 1=Left 2=Center 5=Right     y: 3=Top 4=Bottom 5=Middle
```

**The scoreclock root is hardcoded to mode 7** (`Scorebug_UpdateActive`). Patching mode 7's pair = the whole-scoreclock 3×3 lever (proven live 2026-06-25 and by the user's own XEX diff — the single non-icon byte changed in their modified XEX was `0x2354F4F: 04→03`, moving the bug up). **Caveat:** everything sharing mode 7 (the instant-replay watermark) moves too.

**Shipped: "Scoreclock" tab** (`launcher/scorebug_anchors.py`) — a 3×3 whole-bug picker that patches mode 7 in the XEX (resolves the file offset per-XEX via `xex_patch.va_to_offset`; validates mode-0 zeros + legal values + the trailing record-array signature before writing; stock constants baked in so restore needs no backup). An advanced section exposes all 9 slots × X/Y for the marker workflow (patch a slot to an extreme anchor, see which element jumps).

XEX VA↔file mapping: the flat XEX uses XEX2 basic compression (BSS-compacted), so `VA − 0x82000000` is **not** linear — use `xex_patch.va_to_offset` (block walker). Verified: VA `0x8499EF48` → file `0x2354F48`.

---

## 4. Per-element scorebug layout — SOLVED (file edit + relaunch)

The scorebug is a small **Maya-style 3D scene serialized in `overlay_static.iff` DRAM blob0** — NOT positioned by the vestigial `0x40`/`0x80`-byte records that earlier edits kept failing on. **22 elements** (10 text + 12 mesh) are editable, both kinds verified in-game.

- **MESH elements** (`logo_away/home`, `team1/2_color`, glints, separators) = vertex buffers. **Move = translate all vertices** (also enables scale about centroid). Verified: `logo_away +10y` moved UP, `team2_color −10x` moved LEFT → **+X = right, +Y = up (math coords)**.
- **TEXT elements** hang off a 13-joint skeleton `@DRAM 0x2146B4` (stride 0x30). The consumed copy that actually moves text is **joint POS2 @+0x1C** (pos1 `@+0xC` and a matrix copy are dead copies; the module writes all three + the draw-table row to stay in sync). Verified: `team2 +10y` moved the abbrev.
- **Text color** = draw-table record (stride `0xE0`) bytes **`+0x89/+0x8A/+0x8B` (R/G/B)**; `+0x88` is alpha/authored, left untouched. **Confirmed byte-exact in-game.**
- **Font size** = draw-table `+0x7C` (**experimental — not confirmed in-game**; earlier tests hit runtime-re-anchored elements).

**Side convention** (geometry-consistent, both tests): strip = AWAY left / HOME right. 2K numbered text and meshes oppositely — TEXT `team1`=left/away, `team2`=right/home; MESH `team2_*`=left/away, `team1_*`=right/home. Record names are data-bound, not literal (e.g. `team1_score` is authored cyan and is the teal **"1st"** period text).

**Definitive color→glyph map** (user recolored each element unique, screenshotted): `team1`=AWAY SCORE, `team2`=HOME ABBREV, `team2_score`=HOME SCORE, `quarter`=clock digit1, `gameclock1`=digit2, `gameclock_semi`=COLON, `gameclock2`=digit3, `gameclock3`=digit4, `gameclock4` + the tens digit + the AWAY ABBREV + period "1st" = **runtime-placed / separate elements** (see caveats).

**Key behavior:** **color edits always work** (read from file). **Position edits work for abbrevs/logos/bars but NOT for the scores/clock-digits/period** — those are **re-anchored by the game at draw time** relative to the clock extent. **Nothing moves purely live** (positions are captured at scorebug activation → rebuild-gated). The workflow is **edit the file + relaunch**, ~2 min per test.

**Tool: `launcher/scorebug_layout.py`** — `list_elements()`, `apply_edits({name:{dx,dy,sx,sy,size,color,hidden}})` (text pos writes pos1+pos2+draw-table; mesh = vertex translate/scale about centroid), `restore_stock` (STOCK_POS/STOCK_META baked from the pristine scene). Hide = text off-screen (`dx += HIDE_OFFSET`, within the vertex sanity bound), mesh collapse in place (`sx=sy≈0.001`). **Shipped as the "Scoreclock" tab body** (`_build_scorebug_tab`) — element treeview + edit panel + a calibrated live schematic preview canvas (raw scene coords ≠ on-screen because elements are re-anchored through a parent joint hierarchy; only deltas transfer, so the preview uses a hand-calibrated `SCREEN_LAYOUT` + `_SCENE_TO_NORM` delta scaling) + Apply/Restore/Reset. Whole-bug anchor moved to a secondary dialog. An in-tab texture gallery jumps to overlay_static's textures.

**Coexistence bug (fixed):** IFF-tab "Apply ALL" used to `ensure_clean(iff)` before replacing textures, which reset blob0 and wiped scoreclock layout edits for the shared `overlay_static.iff`. Fixed to replace textures **in place** for `overlay_static.iff` (layout blob0 preserved); layout + texture edits now coexist across repeated applies.

**Font** identified = **"Avenir Heavy"** (`avenir_heavy_24 @DRAM 0x13CCFC`; text records reference the font by `+0x00` hash) — a font change = repoint that hash to another loaded font resource (charset atlas / TTF-splice path is future work).

### Global template (`global.iff`)

The "Overlay: Scorebug" scene *template* lives in `global.iff` blob0 (~21 MB DRAM), registered in a sorted hash→offset registry (`Registry_BinarySearch_849146a0`, 12-byte entries `[hash, type, offset]`, **offset is self-relative: `target = offset_field_addr + value − 1``**). The scorebug entry `@0x147B30C = {0x66EF5FB6, type 1, offset 0xCD9}` → template `@0x147BFEC`. **The template is an animation/wipe + opacity sequence, NOT the pixel layout** — editing the root descriptor there moves the *whole* bug (proven), but per-element child descriptors are recursively instantiated and were never individually located. This is why the per-element lever is the serialized `overlay_static` scene (§4), not `global.iff`.

---

## 5. Shots-On-Goal on the scorebug — ACTIVE; overlay text-binding architecture CRACKED (2026-07-27, static)

**Goal:** 3 new elements — Away Shots (dynamic), Home Shots (dynamic), static "SHOTS" label.

### 5.1 The in-game overlay TEXT-BINDING system (verified static, XEX .data — needs in-game confirmation of behavior)

The engine binds overlay text elements to per-frame **provider callbacks** via plain data tables in the XEX (flat-XEX file offsets via `xex_patch.va_to_offset`):

- **Binding tables @ VA `0x8499F000–0x849A0D40`.** Row = 16 bytes: `[scene crc32-raw][element crc32 (mostly of UPPERCASED node name)][provider callback][slot global]`. The slot is a BSS global that receives the resolved element handle.
- **Per-overlay registry @ ~`0x849A0DF0`+, stride 0x24**, keyed by root node hash: entry `+0x1C` = root hash, `+0x20` = bind-table ptr, `+0x04` = aux table. Scorebug entry `@0x849A0E5C` → root `41267075` (= crc32-raw `"scorebug"`), bind table `0x8499FE50`.
- **Scorebug table `0x8499FE50–0x8499FFE0`**, slots `0x8203B3A8–0x8203B3D0`. Identified elements (crc32 of UPPER name): `C22AE09C`=TEAM1, `502105EE`=TEAM1_SCORE, `5B23B126`=TEAM2, `61C91F73`=TEAM2_SCORE, `23C41F1D`=QUARTER, `6E9019D0`=GAMECLOCK_SEMI. **Unidentified:** F2BA1BE9, 6BB34A53, FEC8BC6C, 67C1EDD6, AAEC8F88, A6D4D322, F1E25DC9, 219DD0D3, 715D7D23, 77762F67, B87FB6D2, E6370495, 365A707C (this one's cb `0x840C15D0` is a transform updater). These are NOT GAMECLOCK1–4 in either case convention (checked) — the clock-digit *content* is scene-data-driven; those hashes appear nowhere in the XEX.
- **`intermission_report` table @`0x8499F370`** — 26 text elements (`25BFE697`=away_score, `3963CEC1`=home_score); **the intermission report displays SOG, so its providers reach the live shots data.** `end_of_period` @`0x8499F000` (`B83232BD`=PERIODNUMBER); `fight_control` rows resolve to roster names (title/RT_button…), confirming the element-hash convention.
- **Text-set chain:** common provider cb `0x83BE6018` (7 instrs): `r4=**(ctx+4)`, `r3=*(ctx+0x14)` (element), tail-call **`Element_SetText_WithTokenResolver @0x83D79110`** → **`Element_SetTextByStringId @0x8415A968`** = `element->vtbl[+8](stringObj, argpack)`, where the argpack carries a **token resolver**.
- **`{TOKEN}` system:** localized strings contain `{NAME:arg}` placeholders. `TextToken_ParseName @0x841595F0` splits at `:`/`}` and hashes with `Function_84113810` (same hash as `"Overlay: Scorebug"`). `TextToken_ResolveAndDispatch @0x841597A8` walks a **runtime-registered handler list @`0x849E29B0`** (node `+0x4`=token hash, `+0xC`=next, `vtbl+4`=resolve). Handlers register at runtime (the static-init stub at `0x8424FD80` only installs the list object's vtable `0x8203CA78`) — **so enumerating them requires a live session.**
- **Frontend menus have a parallel system** @`0x8492D374–0x8492DAxx` (scene `68B8F624` = a 4-team scores screen: team1–4, team3/4_score, time, stats1–3; providers `FeText_Provider_* @0x83BE4318/0x83BE4750/0x83BE3F20`, the latter dispatching a 21-case jumptable @`0x83BE48AC` of Ghidra-unrecovered blocks — every case funnels into `Element_SetTextByStringId`).

**Confirmed dead ends:** no `{TOKEN}` text exists in the XEX, `overlay_static`, `gamedata`, or `global` DRAM (brace matches are font charset tables) — the strings are runtime string *objects* (`StringObj_AssignCopy @0x83D87178` is a refcounted copy, not a table lookup). `loc.iff` = 6 compressed blobs with **no text** (fonts). The `"Update Clocks"` scheduled task (`0x83EB4E00`) is a generic timer-queue ticker. `"BoxScore%02d"` is a save-slot writer. `Stats_BuildStatTable`'s apparent base-getter `FUN_841d3f34` is a `__savegprlr_27` stub — the row-struct base arrives in r3 from an unfound fn-pointer caller.

### 5.2 Remaining pieces

1. **Live shots data source — SOLVED & VERIFIED (2026-07-27, live CE, 4 checkpoints exact: away 8→10→12→12, home 0→1→1→3):**

   **Team SOG = the opposing team's goalies' shots-against, summed.** No team-level SOG counter exists anywhere (session object, statics band, and the game-level stat region at `sess+0x1F48` were all searched/diffed — that region holds faceoffs/clock).

   ```
   blk(team, idx) = sess + 0x994 + 0x74 + (team*20 + idx)*0x88      // per-player live-stat block (0x88 B)
   sess           = *(u32*)0x84FEE8E4                                // per-game heap object — always deref
   team: 0 = HOME, 1 = AWAY

   goalie shots-against = *(s16*)(blk + 0x24)                        // == opponent SOG
   // other block fields: +0x04 u32 TOI-seconds, +0x08 shift count,
   // +0x3A/+0x3C per-player shot ATTEMPTS (not SOG — summed 21/3 when SOG read 10/1)

   // goalie identification — the game's own entity walk (handles goalie swaps):
   for team in 0,1:
     mgr = *(u32*)(0x84FD0C34 + team*4)                              // per-team manager global
     for slot in 0..27:
       ent = *(u32*)(mgr + 0x28 + slot*4);  if !ent: continue
       sub = *(u32*)(ent + 0x1BB4);         if !sub: continue
       idx = *(u32*)(sub + 8);              if idx >= 20: continue   // stat-block index
       rec = *(u32*)( *(u32*)(sub + 0x10) )                          // roster record (0x1A4)
       if ((*(u32*)(rec + 0x40)) >> 3) & 7 == 0:                     // position == G
           SA += *(s16*)(blk(team, idx) + 0x24)
     SOG[other team] = SA
   ```

   Verified live end-to-end: home goalies blk0 (SA=12) + blk10 (0) → away SOG 12; away goalies blk15 (SA=3) + blk19 (0) → home SOG 3. **Caveat (untested):** shots taken at an empty net (goalie pulled) may not credit any goalie's SA — the display could undercount during 6-on-5.

   The token-handler registry was also walked live: 12 static nodes `@0x8492F268` (stride 0x18), all sharing resolver `Function_840DD1E0` — a nested `{CTX:SUB}` switch serving roster/season data (team leaders via `Function_840DD038`, W/L via `Function_83D602B0`), *not* the live match counters.
2. **Text draw — effectively solved for hook purposes:** call `Element_SetText_WithTokenResolver(element, stringObj)` with an element handle from the slot globals, or reuse a bind-table row. The raw-text vtable entry is likely adjacent to `+8` in the element vtable (confirm live).
3. **Per-frame hook point — FOUND** (unchanged): `Scorebug_FrameDispatch @0x840c1518`.
4. **New lever — the binding tables are plain XEX data:** rows can be retargeted or appended (null-terminated blocks with slack), e.g. rebind an unused scene text element (`gameclock4`, `team1_score`/Period) to a shots provider once one is known — potentially **no codecave needed**.

### 5.3 Add-element build status (2026-07-27, two in-game iterations)

**Verified working in-game:**
- **XEX bind-table relocation** (`launcher/scorebug_xex_rows.py` v2): table copied to the `.reloc` tail padding @VA `0x851A7000` (in place, no resize — **do NOT convert zero-blocks to data; Xenia unmaps the region**, that was v1's failure), registry ptr `@0x849A0E7C` repointed, 3 rows added binding SHOTS_AWAY/LABEL/HOME to existing bank ids `34EF4867`/`125F4849`/`D09D14AD`. All stock text renders through the relocated table.
- **overlay_static record-table relocation** (`launcher/scorebug_add_shots.py`): 11 records + 2 clones moved to blob0 end; count `@0x209698` 11→13; both self-rel base ptrs (`0x209650`/`0x20969C` → firstkey+0x18) rewritten; per-record the only self-rel field is `+0x08` (name). Engine consumes the relocated table (records runtime-filled in the instance).

**Anim-descriptor table MAPPED + patch applied (2026-07-28, static; in-game test pending):** the earlier "never instanced / opacity channel holds 0" theory was refined. The table `@0x2096A0` is **stride 0x1C, count 54 `@0x209618`** (self-rel table ptr `@0x20961C`); desc = `[k1][k2][flags][-1.0f][p10][p14][p18→channel data]`, flags = AnimEval channel mask (bits 20+) / const-vs-spline bits (8+) / property-class low byte. Each text element has **two descs**: `[crc-UPPER(bindname)][0x0CED9417]` flags `0x00100012` = **one spline = OPACITY** (all 10 stock splines byte-identical — 7-key intro fade settling at alpha 1.0), and `[E9015CE9][crc-raw(name)]` flags `0x03F03F21` = 6 const floats (transform/scale). **Key finding: the opacity desc is keyed by the element's BIND hash (record `+0xD8`), not positionally paired** — `gameclock4`'s stock bind `365A707C` is the only record bind with no opacity desc, so its alpha defaults 0 (positional pairing disproven: gameclock3, which *binds* DIGIT4, draws; gameclock4, adjacent to DIGIT4's desc, doesn't). The shots elements were invisible for the same reason. Also mapped: **scene node array `@0x209FD0` stride 0xB0 (17 nodes**, name crc `+0x04`; node[0] `scorebug_text` `+0x60` = 13 = joint count; node[1] `+0x48` = duplicate desc count) and the **joint table `@0x2146B4` stride 0x30** (name crc `+0x00`; `away_abbrev` has no joint and draws → joints optional). **Applied (`launcher/scorebug_anim_descs.py`, dry-run validated):** desc table relocated to blob0 end, 5 descs appended (opacity for SHOTS_AWAY/HOME/LABEL cloned from TEAM2's spline + transforms for shots_away/home), count 54→59 at both sites, old keys zeroed. **★ VERIFIED IN-GAME (2026-07-28): all three SOG elements render live on the scorebug** — the bind-hash-keyed opacity desc was the missing instancing/visibility piece. The label draws at the record position (joint j12 does not override).

**Font + glyph-scale levers (2026-07-28, follow-up RE):**
- **Record `+0xDC` = the per-element FONT hash** (`+0x00` is only the table key — the older "font @+0x00" claim was wrong; `scorebug_layout` corrected). Ground truth: `Function_83C9EDA0` (record property parser) — records are baked from 0x20-stride key/value property lists; keys: `0xBF045BDB`=font (inline string OR precomputed crc), `0x7714781F`=bind name (string, crc-upper-hashed at parse — the origin of bind hashes), `0x11FA397C`=size, plus X/Y/Z/W/color/flags keys. Non-scorebug scenes store text elements as raw property lists with **inline font-name strings** (`avenir_heavy_24`).
- **Stock scorebug fonts (`+0xDC`):** `3DD873F2`=`'AVENIR_HEAVY_24'`, `F57C40A5`=`'AVENIR_ROMAN_22'`, `4A63F778`=`crc('avenir_heavy_24')` (SOG elements). **Font redirect = write `+0xDC`** — wired into the Scoreclock tab font picker with a curated 11-font set (in-game verify pending).
- **★ FONT SYSTEM FULLY MAPPED (2026-07-28): `english.iff` blob0 holds the font-instance registry** @`0x129000–0x12CC90`: **103 alias entries**, stride 0x90, each `[utf16 name][hash @name+0x40][base-font hash +0x44][1.0f][scaleX +0x4C][scaleY +0x50]`, resolving to **8 base typefaces** (`avenir_heavy_24`, `avenir_heavy_40`, `avenir_roman_18`, `avenir_roman_22`, `avenir_light_40`, `avenir_black_95`, `arial_black_20`, `prison_aoe`; one unresolved base `69F88471`). Every "font" the game names (ARIAL_15, AGBOLDCN 26, Stratum2 40, LREGULAR 28 …) is just a base typeface × scale pair — e.g. `'ARIAL BOLD 13'` = avenir_heavy_40 × 0.35. Any of the 103 hashes is a valid `+0xDC` target. Per-alias scale floats are editable in place (a "custom font size" = retune an unused alias's target+scale). Typeface data: blob0 headers (`'Avenir 85 Heavy'` @0x1C628 etc.) carry charset-range→glyph-index tables + metrics; glyph atlases = blob1 (3.7 MB). **Custom font texture** = replace a base typeface's atlas (affects every alias of that base; the isolated-per-element version = clone a typeface + new alias — future project, format partially mapped).
- **Glyph scale**: the transform anim-desc consts `[tx,ty,tz,sx,sy,sz]` (stock 1.1/1.2) are the per-element scale; `scorebug_anim_descs.set_text_scale` + tab "Set scale" write them (in-game verify pending; the record matrix diagonal is also still written).
- The three SOG elements are full Scoreclock-tab citizens (position/width/color/font/scale/hide); label clip width set 20→48 to stop the "Shots" ellipsis truncation.
- **OPEN: user reports choppy FPS with SOG live.** Suspect: 3 extra per-frame `{PT_SUBJECT:…:STAT:SHOTS:VALUE}` resolutions (nested token handler + stat walk under Xenia JIT). A/B: restore the XEX `.sogbak` (kills binds, overlay untouched) and compare; if confirmed, the fix is a throttling hook (update every N frames).

### ★ CRITICAL Cheat-Engine-over-Xenia lesson

**NEVER create big CE memscan SESSIONS over Xenia's `MEM_MAPPED` guest RAM.** A single-value BE scan (e.g. `00 00 00 05`) returns 112k–593k hits; CE's `nextScan`/`createFoundList` then re-reads every candidate from `MEM_MAPPED` memory, which is glacially slow and **wedges CE's single Lua thread** (every later request queues behind it; `TaskStop` only detaches your wait, not CE's work → required a full CE restart, cost ~an overnight wedge).

**What is fast / the safe method:**
- One-shot native `aob_scan` (returns ~200 hits instantly).
- `createMemScan`+`createFoundList` **only when the result set is hundreds** (pair scans).
- To pin a counter: fast **adjacent-pair** scan for the current `[away, home]` (both byte orders) → hundreds of hits; change the value; re-scan the new pair; **intersect the two hit-lists in your own code** — never in a CE session.
- Address map: guest→host virtual = `VA + 0x100000000`; heap = phys window host `0x1A0000000..0x1C0000000` (mirrors `+0x20000000`); guest is **big-endian**, and **CE `readFloat`/`writeFloat` are little-endian** → always `readBytes`/`writeBytes` with reversed bytes.
- CE MCP bridge **drops on CE restart** → reconnect via `/mcp` then re-`openProcess('xenia_canary.exe')`.

---

## 6. Front-end / menu system

Menus are the **same scene-graph UI** as the scorebug (`BB05A9C1` scene descriptors + textures).

**Load chain** (all by NAME → `crc32(upper)` → TOC):
```
Frontend_LoadAndInit @0x83BB0750 → frontend.iff (id 0x0A2282D0)
Frontend_LoadLogos   @0x83BB0450 → logos_large/medium/small.iff (0xFC982140 / 0xFC86B181 / 0x8615D6F2)
TitlePage_Load       @0x83C9C0D8 → titlepage.iff (id 0xC3AD8910)
```

**Scene graph = the SIZE/POSITION lever.** `Scene_InstantiateNodeTree @0x83CC1A70`: node `+0x04` type (0 sprite leaf, 2 named group, 4 subnode), `+0x20` name ptr, `+0x2C` scale, `+0x34` width, `+0x38` height (if 0 → texture dims), `+0x70/+0x74` resolved on-screen W/H. **Element draw size = authored node w/h, NOT texture size** — a bigger texture just gets minified into the fixed quad (blocky). To enlarge sharply you must edit node w/h/scale AND supply a matching-res texture. (Menu *screen* widgets differ: their size IS texture-driven via `Ui_SetWidgetSizeFromResource @0x83CAD058`, so a bigger texture resizes them; position there is layout/anchor-computed — a drag editor needs the widget property-graph reversed, a major sub-project.)

**Titlepage** is a compiled render scene (D3D9 shader constant tables + baked geometry verts + a multi-texture VRAM pool addressed by the scene format, not standard fetch constants) — static decode is unreliable; textures were located via a **title-screen GPU trace + byte-match** and registered in `archive_textures.SCENE_ASSETS` / `MIP_CHAINS` (cover `@0x125000` = Ovechkin/1024², NHL 2K10 wordmark `@0x29000`, ESRB `@0x3D000`, 2K logo `@0xF1000`, STATS `@0x111000`). Replaceable via the texture pipeline **including mip chains** (rewrite every mip — a mip0-only replace ghosts against the original mips under trilinear filtering). Author uncompressed, mipmaps OFF, **premultiply OFF** (game uses straight alpha).

### Logos — index-only atlases, NO baked copies

- The three `logos_small/medium/large.iff` (magic **`0xF0985030`**, 0x1528 bytes each, entry_count=120) are **INDEX-ONLY** — no pixels, no `0E4837` blobs.
- The runtime menu logo atlas is rebuilt from `logo_<code>.iff`, so **menu logos DO update from `logo_<code>.iff` edits after restart**.
- Content-matched all 30 team logos against every record of `global.iff` (446), `gamedata.iff`, `overlay_static.iff`, and 60 discovered packs → **zero matches. No baked logo copies exist anywhere.**
- The actual pixel bundles are three flat `[0xE0 + tex]` pair streams (120 pairs each, portrait-pack style): `disc_b6b4e9c8.iff` (LARGE 512²), `disc_a300d85f.iff` (MEDIUM 256²), `disc_a38365c6.iff` (SMALL 128²), indexed by the `logos_*.iff` read tables. The launcher exposes only the LARGE bundle ("Team Logos - Menus/Team Select"); a `BUNDLE_CASCADE` auto-syncs any tile replace to medium + small (the hidden rows are kept in the CSV — required for crc-alias resolve, do not delete).

### "Banners" = per-arena ICE-SURFACE textures (center-ice logos)

The 28 "banners" packs are **per-arena ice-surface textures** (e.g. `disc_02b3bb40.iff` = 1024×4096 DXT1: center-ice logo + board ads + rink lines, magic `0xFF3BEF94`). `list_textures` returns [] (no formal tree) but the **primary/single-texture flow works** (`primary_fetch`/`decode_preview`/`replace`). **Center-ice logos live here.** (`logos_extra` (23) = country/2K/NHL logos, standard editable.)

---

## 7. Jerseys — edit once, applies in-game AND front-end (`jersey_map.json`)

**The key fact:** the front-end team-select sheet's `decals` texture is a **byte-identical copy** of the in-game uniform's `stamps` texture (both 2048×512 `4444`) — proven MSE 0.0 (`uniform_ana_home.iff[stamps]` vs `disc_b5ff12be.iff[decals]`). That is why editing a jersey used to take two passes.

**Two asset types** (not missing subtextures):
- In-game uniform — 6 tex: `stamps, normal, helmet, letters, letters_normal, crowd` (+ a separate `uniform_base_<team>_<slot>.iff` fabric).
- Front-end team-select sheet — 2 tex: `decals, decals_normal`.

**`launcher/data/jersey_map.json`** (built + wired): one entry per jersey = one uniform + one front-end sheet. **224 jerseys / 563 assets** (content-hashed; the memory also cites a 288-jersey superset build). **Pairing rule (validated, not guessed): within an archive, the Nth uniform by offset pairs with the Nth front-end sheet by offset** — reproduces 145/145 content-matched pairs. `idx0 stamps↔decals` is a verified invariant for every jersey; `idx1 normal↔decals_normal` usually matches (not always — still fanned out to keep edits in sync); idx2..5 exist on the uniform only. Editing writes texture0 to every member (`launcher/jerseys.py`: `hidden_members()`, `twins()`, `twin_edits()`).

**Corrections to earlier hand-labels (content beats crc guesses):**
- `disc_f9af1b9d.iff` / `disc_600368e7.iff` = **Austria** home/away, **NOT Latvia** (this resolves the "multiple Latvias" mystery).
- `disc_097f9ca3.iff` = `dal_alt`; `disc_d2f669a2.iff` = `dal_away` (were swapped).
- **20 sheets are genuinely un-disambiguatable home vs away** because home and away share ONE image (CAN, CZE, FIN, KAZ, NJD, PHO, RUS, SLO, UKR, USA) — "edit once" writes 4 assets for those.
- **Bug caught — do not regress:** grouping purely by image *fused home+away* for the identical-art teams, which would make editing home silently rewrite away. Every `uniform_*` must always stay visible — invariant: no `uniform_*` may ever appear in `hidden_members()`.

**"All addresses accounted for" was WRONG:** there are 1147 undiscovered texture assets, 379 unexplored TOC entries, and **172 six-texture `disc_*` = full uniforms absent from the named catalog** vs only 128 named `uniform_*`.

Jersey **display names** live in `Roster.ROS` (UTF-16BE, string-pool chunk `0xEB69DFB9`), encoded by ordering as `[asset_code, display_name]` pairs (e.g. `ANA_CL05`→Home'03/Away'03/Alternate, `CHI_WC09`→"Winter Classic '09"; full creation set `<TEAM>_CRT`, `CRT00..25_CRT`). The slot code → `disc_` asset tie is unresolved (not crc, not a raw pool byte-offset — indirect string index).

---

## 8. Crowd = 2D billboard sprites (not 3D)

The crowd is a **2D sprite / billboard system**, which is the flat "PS1" look.
- **`crowdanim.iff`** (0A, ~2.96 MB) = the visual: blob0 ≈ 1.4 MB DRAM descriptor (placement/animation/instance data, custom format), blob1 ≈ 3.3 MB VRAM sprite atlas (**custom-packed** — does not decode as any standard single texture; `list_textures` = 0).
- **`crowdloops.iff`** = crowd AUDIO only (SE_APPLAUD/BOO/CHEER/etc.), separate. `Crowd_RegisterAmbientEvent @0x83FA29A8`.
- Consumer/builder: `Function_84254EA8` (writes the crowd handle; too big for a 5 s decompile — not fully read). Loader: `CrowdAnim_LoadOrUnload @0x83FDD428`.
- **Improvement paths** (practicality order): (1) live-tune sprite scale + instance density in CE, then bake as a patch — most impactful, no format cracking; (2) crack the custom atlas packing and re-author (medium-hard); (3) rebuild as 3D — huge, hits the geometry-reimport wall, not realistic.

---

## 9. Open questions / caveats

- **SOG on the bug: WORKING IN-GAME (verified 2026-07-28)** — see §5.3. Open items: FPS choppiness A/B (suspect per-frame token resolution), and in-game verification of the new font-redirect (`+0xDC`) and glyph-scale (anim consts) levers.
- **Runtime-re-anchored scorebug elements** don't respond to position edits: the **period "1st"**, the clock **tens digit** (`gameclock4`), and the **away team abbreviation** are placed by the game at draw time relative to the clock/each other. The away abbrev is a text draw record with a **blank font/name hash (`0x00000000`)**, one 0xE0 stride before `team2`'s record — colorable/sizable but position writes the draw record directly (experimental).
- **Font-size lever (`+0x7C`)** is not confirmed in-game.
- **Grey "2K" backdrop behind a custom SN logo:** definitively a **2K-shaped MESH drawn grey** (Xenia paints the empty/unbound texture slot grey; `logo_2k_mesh` geometry), NOT a texture — so no texture edit removes it. Hiding it by zeroing `logo_2k_mesh`'s index buffer was confirmed in one reboot but is coupled to the SN quad (both render from the same mesh geometry / share material `0x218698`); a clean SN-preserving hide is still open. It is a scene-node `type` (1 = skipped, 0 = drawn) away from a clean skip, but the source node records use relocated name pointers (serialized format not yet cracked → the real add/remove-element lever).
- **Adding/removing a scorebug element** needs the serialized scene node/template format cracked (`Scene_InstantiateNodeTree` node list uses relocated name pointers; `Scene_BuildTemplateInstance` element cap = 10) — a focused multi-session RE effort.
- **Menu drag/scale layout editor** (`roster.iff` widgets) is a serialized memory-image with pointer fixups → needs the widget property-graph reversed (major). Texture reskin of menus works today.

### Superseded old-doc claims

- `SCOREBUG_UI_WIDGET_HANDOFF.md` marked **per-element layout "❌ NOT ACHIEVED"** and per-element **text color "❌ NOT ACHIEVED"**. Both are now **DONE**: per-element X/Y is editable via the serialized `overlay_static` Maya-scene (joint pos2 `@+0x1C` for text, vertex translate for mesh — 22 elements verified), and per-element text color is the draw-table bytes `+0x89..+0x8B` (confirmed byte-exact in-game). The handoff's conclusion that layout lives only in `global.iff` (recursively-instantiated child descriptors) was the wrong file — the editable per-element data is the `overlay_static` scene.
- The handoff's "cyan bar" / glow attributions predate the GPU draw-call trace: the bottom cyan bar is **`glow_white`** (off-by-one from `glow_cylinder_color`).
- `06_crowd_menu_misc.md` is accurate on crowd and the front-end load chain; this doc adds the logo-bundle, banner, and jersey-map detail it lacked.
