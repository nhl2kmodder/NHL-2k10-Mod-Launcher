# 08 — Scorebug, overlay scenes, and the front-end / menu system

One-line summary: the in-game scorebug, all HUD overlays, and the front-end menus are one hash-keyed scene-graph system; `overlay_static.iff` holds the entire overlay scene graph (513 nodes) plus its texture atlas, per-element scorebug layout is editable via a serialized Maya-style scene, and menu/logo/jersey/crowd assets each have a known (and sometimes surprising) storage story.

Status: **mostly verified.** Whole-bug placement, per-element scorebug X/Y (mesh + text), text color, whole-atlas recolor, texture replacement, logo bundles, and the jersey map are all confirmed in-game. **Active/incomplete:** adding Shots-On-Goal to the bug (data source unpinned, text-draw function unfound). A few per-element behaviors (period "1st" and the tens digit are runtime-re-anchored; font-size lever unconfirmed) are flagged below.

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

## 5. Shots-On-Goal on the scorebug — ACTIVE, incomplete

**Goal:** 3 new elements — Away Shots (dynamic), Home Shots (dynamic), static "SHOTS" label — delivered as a **persistent, shippable XEX hook** that draws the strings directly (piggybacking the game's own font/text-draw). This deliberately avoids the *serialized scene-node insert* path, which is blocked (relocated name-pointer node format; element cap = 10 in `Scene_BuildTemplateInstance`).

**Four pieces needed:**

1. **Live shots data source — UNPINNED.** RE ruled out the easy paths: `Stats_BuildStatTable @0x83F88AA0` is a *static* stats-SCREEN template (row 5 = "Shots" `@0x83b2ad88`, but it only loads a layout constant, never the live count); `Stats_AccumulateZones @0x83b6f5a0` = shot-chart heatmap bins; `StatsEntry_Begin/End @0x83b72cf0/0x83b73138` = per-event log. The live SOG total is a plain per-team counter, still unpinned — pin it live by value-scan, then correlate to a BSS base in Ghidra.
2. **Game text/font draw-string function — NOT FOUND.** `Frontend_DrawElementList @0x83beedb0` only draws type-1 sprite / type-2 glow (white + alpha) — no text branch. Clock/score digits use a separate, still-unnamed font path (digit formatters `Runtime_FormatFloatDigits @0x841D6A08` / `DecimalString_RoundAndCopyDigits @0x841D8AE0` are generic sprintf internals, no scorebug lead). Next: trace how a scorebug clock/score text element gets its string set each frame.
3. **Per-frame hook point — FOUND:** `Scorebug_FrameDispatch @0x840c1518` (state 2 → `Scorebug_UpdateActive` → `Scorebug_BindElementsBySize`). Note update ≠ draw — the text render happens later in the frame's scene walk, so the draw hook likely wants the render pass.
4. **Codecave** — pending.

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

- **SOG on the bug is unfinished** — the live per-team shots counter is unpinned and the game's text/font draw-string function is unidentified. (Hook point and display approach are decided.)
- **Runtime-re-anchored scorebug elements** don't respond to position edits: the **period "1st"**, the clock **tens digit** (`gameclock4`), and the **away team abbreviation** are placed by the game at draw time relative to the clock/each other. The away abbrev is a text draw record with a **blank font/name hash (`0x00000000`)**, one 0xE0 stride before `team2`'s record — colorable/sizable but position writes the draw record directly (experimental).
- **Font-size lever (`+0x7C`)** is not confirmed in-game.
- **Grey "2K" backdrop behind a custom SN logo:** definitively a **2K-shaped MESH drawn grey** (Xenia paints the empty/unbound texture slot grey; `logo_2k_mesh` geometry), NOT a texture — so no texture edit removes it. Hiding it by zeroing `logo_2k_mesh`'s index buffer was confirmed in one reboot but is coupled to the SN quad (both render from the same mesh geometry / share material `0x218698`); a clean SN-preserving hide is still open. It is a scene-node `type` (1 = skipped, 0 = drawn) away from a clean skip, but the source node records use relocated name pointers (serialized format not yet cracked → the real add/remove-element lever).
- **Adding/removing a scorebug element** needs the serialized scene node/template format cracked (`Scene_InstantiateNodeTree` node list uses relocated name pointers; `Scene_BuildTemplateInstance` element cap = 10) — a focused multi-session RE effort.
- **Menu drag/scale layout editor** (`roster.iff` widgets) is a serialized memory-image with pointer fixups → needs the widget property-graph reversed (major). Texture reskin of menus works today.

### Superseded old-doc claims

- `SCOREBUG_UI_WIDGET_HANDOFF.md` marked **per-element layout "❌ NOT ACHIEVED"** and per-element **text color "❌ NOT ACHIEVED"**. Both are now **DONE**: per-element X/Y is editable via the serialized `overlay_static` Maya-scene (joint pos2 `@+0x1C` for text, vertex translate for mesh — 22 elements verified), and per-element text color is the draw-table bytes `+0x89..+0x8B` (confirmed byte-exact in-game). The handoff's conclusion that layout lives only in `global.iff` (recursively-instantiated child descriptors) was the wrong file — the editable per-element data is the `overlay_static` scene.
- The handoff's "cyan bar" / glow attributions predate the GPU draw-call trace: the bottom cyan bar is **`glow_white`** (off-by-one from `glow_cylinder_color`).
- `06_crowd_menu_misc.md` is accurate on crowd and the front-end load chain; this doc adds the logo-bundle, banner, and jersey-map detail it lacked.
