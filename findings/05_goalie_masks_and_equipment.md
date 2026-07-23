# 05 — Goalie Masks & Equipment

**Summary:** Goalie mask/gear *designs* are ordinary name-hashed file textures you can repaint (crisp, high-res) with the standard texture pipeline; a mask is *assigned* to a goalie by live-patching two bit-fields in the running game's player record. The mask *geometry* (3D mesh) and *adding brand-new mask slots* are the hard/blocked parts.

**Status:** Texture repaint = **WORKS in-game** (verified, any detail/resolution, stored crisp). Live assignment = **WORKS** (299 goalies enumerated, read-modify-write verified). Team-tint recolor bypass = **WORKS** (verified in-game). Custom-mask ADD/EXPAND = **partly blocked** — relocate/grow proven on shells g01–g06, but g07+ (no mesh) and pattern 33+ (5-bit cap) are hard walls. Roster-file (on-disk) assignment = **not possible** (field re-salts per save → assignment is live-memory only). Mask geometry reimport = **WALL** (file buffer is a secondary copy; GPU renders a different stream).

Launcher module: `NHL2K10 Mod Launcher/launcher/goalie_equipment.py` (+ `customasset.py`, `_build_goalie_tab` in `nhl2k10_launcher.py`).
Ghidra source report: `ghidra_re_reports/08_GoalieMask_Equipment_System.txt`.

---

## 1. Asset naming (mask + gear textures)

Goalie mask designs are normal single-primary IFF textures, stored in archive `0B`, addressed by a **crc32 (UPPERCASE) name hash** in the `0A` TOC. The name format (`s_goalie_mask_fmt @0x83B302E8`) is:

```
helmet_g{shell:02d}_pattern_{pat:02d}.iff
```

- **6 shell shapes: g01–g06** (there is no g00). Per-shell shipped pattern counts:
  g01 = 32, g02 = 6, g03 = 6, g04 = 32, g05 = 4, g06 = 4 → **84 shipped mask files.**
- Pattern formats are **per-file**: `pattern_00` = a 64×64 565 solid base colour (no alpha); design patterns (e.g. `g01_pattern_05`) = **DXT4_5 512×512** full art (verified: green skull/ribs).

Other goalie gear textures follow the same name-hashed convention (all DXT4_5 512×512, replaceable as ordinary textures):

```
pad_g{00..19}.iff           pad_g{NN}_logos.iff
blocker_g{00..17}.iff       blocker_g{NN}_logos_l/_r.iff
catcher_g{00..19}.iff       catcher_g{NN}_logos_l/_r.iff
```

**~175 goalie mask+gear rows** are surfaced in the launcher's IFF Textures catalog. These are **single-primary** IFFs (DRAM descriptor + one `0x0E4837` VRAM blob) — `list_textures()` returns 0 for them (no multi-tree), so tooling uses the **primary path** (`archive_textures.primary_fetch/decode_preview/replace`), not the tree path.

**Loading path:** per-goalie fields → name builders `Asset_GetGoalieHelmetName_{Home,Away}{1,2}{,b} @0x83FD71CC` → `VC_FormatAssetName` → `Res_LoadAsset`. Precache grid `Tex_PrecacheGoalieHelmets @0x83FDDAB0` → `Tex_PrecacheGrid2D(fmt, 0x20, 7)` = 7 shells × 32 patterns.

---

## 2. The Goalie Equipment launcher tab

Two functions: **repaint** a shipped mask slot, and **live-assign** a mask to goalies. It does **not** edit roster/game files for assignment — see §5.

### 2a. Repaint a mask slot (in-place / grow)

UI: Name + Style combobox (g01–g06) + Pattern spinbox (1–31) + image → `archive_textures.replace` on the chosen `helmet_gSS_pattern_PP.iff` slot.

- **Repaint accepts ANY detail/resolution** — relocate/grow renders fine (verified in-game: a 149 KB grown mask on `g01_pattern_10` rendered correctly). Earlier "append won't load" reverts were wrong; that was the g07 no-mesh problem (see §6), not a storage limit.
- **Gotcha:** `archive_textures.ensure_clean` *relocates* the clean bytes to `1B` when they don't fit the current slot, and relocated/appended data does not load under Xenia. The repaint path uses `_restore_mask_clean_inplace(name, game_dir)` instead — it writes the CLEAN bytes back to the clean `0B` offset and repoints the TOC — for both fresh-start and revert.

### 2b. Assign a mask to goalies (live memory patch)

Pick a mask from a combobox (★ custom masks by name first, then all built-ins), select goalies in a multi-select Treeview (name filter + "Assigned (saved)" column), Assign → patches the **running game's memory**. Assignments are saved in launcher config `goalie_masks = {"first|last": [memShell, memPat, identity]}` and **re-applied on every Launch** (`self.after(60000, self._goalie_apply_saved_async)`, polls ~20 s × 18 until the roster is in memory).

---

## 3. Memory map — where the mask fields live (Ghidra + live-verified)

Xenia host address = `0x100000000 + guest_VA`.

```
g_RosterManager  @ guest VA 0x849DE29C          → manager base pointer
manager + 0x08   → chunk table, entries {hash u32, count u32, ptr u32}
  PLAYER chunk hash 0x1E159C31 → count ~2715, ptr = player-record array
player record stride 0x1A4 (big-endian fields):
  +0x00  last-name ptr    (UTF-16BE name pool)
  +0x04  first-name ptr   (UTF-16BE)
  +0x40  position dword   → goalie iff (v>>3)&7 == 0
  +0xB4  mask SHELL  dword
  +0xB8  mask PATTERN dword
  +0x158 / +0x15C / +0x160  three recolor colours (see §4)
```

The two mask fields:

| Field | Offset | Decode | Ghidra getter |
|---|---|---|---|
| Mask **shell** (model) | `+0xB4` | `(dword >> 23) & 0xF` (4 bits) | `Goalie_GetMaskModelIndex @0x840A8888` |
| Mask **pattern** | `+0xB8` | `dword & 0x1F` (5 bits) | `Goalie_GetMaskPatternIndex @0x840A9010` |

**Both dwords carry other goalie attributes** in their remaining bits → writes are **read-modify-write**, touching only the mask bits (verified other bits preserved live).

**Filename ↔ memory ↔ in-game editor mapping** (`Asset_GetGoalieHelmetName_* @0x83FD71CC`, fmt `helmet_g%02d_pattern_%02d`):

| | value |
|---|---|
| filename shell `SS` | `memShell + 1` |
| filename pattern `PP` | `memPat` (no offset) |
| in-game editor pattern shown | `memPat + 1` |

Ground truth (live): Roberto Luongo memShell=0, memPat=17 → file `g01_pattern_17` → editor shows 18.
So to make a goalie wear `helmet_gSS_pattern_PP.iff`: write **memShell = SS − 1**, **memPat = PP**.

**Live validation:** 299 goalie-position players enumerated with correct names (Giguere, Tim Thomas g04, Kiprusoff, Osgood, Quick, Luongo…). Stars appear 2–3× (active club + free-agent / all-star / Olympic pools); `apply_masks` keys by `first|last` and writes **every** record matching the name so all copies are set (a fix for an earlier bug where a duplicate-collapsing dict wrote the pool record, not the active one → "no change in-game"). The mask resolves at goalie **load** — start/reload a game to see it; if stale, restart Xenia (archive cache).

---

## 4. Mask QUALITY (crisp) + team-tint RECOLOR bypass

**Quality — shipped masks are DXT1** → blocky, zig-zag diagonals. The repaint stores **8888** (uncompressed, crisp) via `archive_textures.replace_primary_convert(iff, img, game_dir, "8888")` = `replace_multitex_convert` with `rec_off=0` (single-primary descriptor record sits at offset 0; fetch @0x94; field layout: `0x08/0x0C/0x1C` descriptor, `0x60` dims, `0x6c` vram-off+1, `0x70` mip0, `0x74` tail, `0x98` f1, `0xA0` f3, `0xA8` f5). 8888 grows → relocates, which renders fine. (A **4444** half-size option also exists — block-free, ~4-bit banding; descriptor `0x1828014F`, fetch id 15.) 8888 descriptor = `0x18280186`, fetch id per the 8888 tables.

**Recolor — masks are team-tinted.** `Function_84095150` feeds the mask material **3 per-goalie colours** (`0xFFRRGGBB` dwords at **goalie+0x158 / +0x15C / +0x160**; getters `FUN_840A9F80/9FC8/AA010`; shader params `0xFABD032 / 0x78ACE0A4 / -0x1E5A4EE2`) and the shader does approximately:

```
out = tex.R × colour1  +  tex.G × colour2  +  tex.B × colour3
```

A custom true-colour texture would come out tinted (a red design reads as the team colour, e.g. Vancouver blue). Fix: set the 3 colours to an **RGB identity** — `0xFFFF0000 / 0xFF00FF00 / 0xFF0000FF` — so the substitution becomes a pass-through and the texture renders exactly as painted. `set_mask(..., identity=True)` writes them; ASSIGN sets identity **for ★ custom masks only** (config flag), shipped masks keep their team colours. Both quality and recolor verified in-game (2026-07-06).

> Test gotcha: solid-colour test textures are useless here (they all come out as the tint). Always test with a bold *design* (checkerboard / quadrants) so it shows through any tint.

---

## 5. Why assignment is live-memory only (not a file edit)

`Roster.ROS` stores the mask in a per-record field (`+0x118`) that **re-salts / checksums on every save** — a before/after diff showed the byte moving inside a field that re-salts globally per save (2711 distinct XORs; only ~4 bits stable). So a clean on-disk edit is not possible. **In memory the `+0xB4`/`+0xB8` fields are plain**, so assignment patches the running game and re-applies on each Launch. (Non-elevated Python cannot `OpenProcess` the elevated Xenia — Win32 error 5 — so the launcher runs elevated via `uac_admin`.)

---

## 6. Custom-mask ADD / EXPAND investigation

Goal: add *new* mask slots beyond the shipped 84.

### Mesh mapping (3 physical shapes)

`Goalie_GetMaskMeshId @0x84090770` returns one of **3** mesh IDs; shells sharing an ID share the shape **and its UV layout**. In **filename** terms:

| Mesh ID | Filename shells |
|---|---|
| `0x27` (default) | g01, g04, and g07–g16 |
| `0x28` | g02, g03 |
| `0x29` | g05, g06 |

(In getter/memShell terms: memShell 1,2 → `0x28`; 4,5 → `0x29`; else (0,3,6…) → `0x27`. Same table, different indexing base.) The mesh part is attached at body-part slot **0x0D** in `Goalie_UpdateGearMeshParts @0x84093F88` via `Player_SetMeshPartById(...,0x0D,...,meshId)`.

### The addressing limit

The roster stores mask as a **4-bit shell + 5-bit pattern**, so only **g01–g16 × pattern 00–31 = 512** names are *requestable* — but see the hard walls below. Add mechanism (`customasset.add_custom_asset` / `add_custom_mask`): sorted-insert a new 16-byte TOC entry by name-hash (the engine **binary-searches** the hash-sorted TOC — an end-appended entry is never found), append the texture to `1B` (0x800-aligned), bump the count `@0x10`, grow the `1B` size in the archive table `@0x18`. This part works (verified the entry appears in the live in-memory directory).

### ✅ CORRECTION (doc 13, 2026-07-06): earlier "append won't load" was the g07 problem, not a fundamental block

A previous conclusion claimed appended/relocated archive data never loads under Xenia. **That was wrong** — every failing test had been on shell **g07**, which fails to render for a *separate* reason (no mesh). Verified in-game: a **relocated / grown 149 KB** detailed mask on a *renderable* slot (`g01_pattern_10`) **renders fine**. Consequences:

- **Relocate/grow WORKS on shells g01–g06** → high-quality masks (any detail/resolution/8888) are fully supported, same as any other texture.
- The grid is 6 shells × 32 patterns. g01/g04 are full, but **g02/g03 (patterns 06–31) and g05/g06 (patterns 04–31) have ~108 empty renderable slots** — those should be genuinely **ADDABLE** (sorted-insert TOC + append data + a goalie on that shell). *Mechanism proven; the specific empty-slot add is untested end-to-end.*

### ❌ Two hard walls remain

1. **New shell g07–g16 = no mesh.** Decisive test: identical in-bounds bytes rendered on shell 0 (g01) but produced **nothing** on shell 6 (g07). `Goalie_GetMaskMeshId` returns a valid `0x27` for shell 6 and the set-part is called, yet no mesh attaches — the goalie model/mesh loader only loads mask meshes for the **6 shipped shells**; `0x27` is never resident for a shell-6 goalie, so the set-part is a no-op. (A possible future unlock: patch the mesh loader to make shell 6 resident on `0x27` → up to ~310 extra slots on the g01 shape. Not done.)
2. **New pattern 33+ = 5-bit cap.** Pattern is a 5-bit field (`& 0x1F`, max 32) and **bit 5 is already used** by other goalie data (Luongo `+0xB8 = 0x67C4C271`, bit 5 = 1). Widening to `& 0x3F` reads that existing data and corrupts every goalie's pattern; would require relocating the field.

Dead-end avenues also tested: loose files in the game folder (`VCWIN32FILEDEVICE` not in the mask lookup chain for bare names); override devices (Preloader `@0x84D198B4` only caches *existing* assets; DLC `dcr:` caps override names at ~15 chars vs 25-char mask names). See `docs/13`.

---

## 7. The 3D mask MODEL — geometry reimport is a WALL

- The mask **mesh** is a `BB05A9C1` scene-graph part, body-part slot `0x0D`; mesh handle registry `g_MeshHandleRegistry @0x850C610C`. Source IFF = **global.iff** (43.5 MB; `0A @0x1149800`), section `bb05a9c1` decompresses to ~22.5 MB scene graph, section `411536d5` to ~67 MB packed VB/IB/tex.
- The mesh was **extracted to OBJ** with UVs/normals via a RenderDoc capture of the D3D12 recomp (VS-Output; TEXCOORD2.xyz = world position, UVs packed in interpolator `.w`), and the corresponding vertex buffer was **located in the file** by UV fingerprint (decompressed-`bb05a9c1` offset ~`0x1155BC0`, stride 36, UV@0 i16n BE, position@12 i16 BE, ~713 real verts). A geometry edit was even written back in-place with a working wp=12 `0e4837` encoder.
- **BUT reimporting geometry does not change what's rendered.** The file-locatable stride-36 buffer (where the UVs live) is a **CPU/secondary copy**; the GPU renders from a **separate vertex stream**. A whole-mask ±12000 i16 offset to `pos@12` produced no in-game change (confirmed: the edited i16 positions were found loaded in Xenia RAM, but nothing moved on screen). Writing the geometry the GPU actually renders is **blocked** pending a live render-VB trace (registry `0x850C610C` → mask mesh `0x27/0x28/0x29` → render-mesh struct → its VB pointers → byte-match into the file).
- **Bottom line:** mask/gear **textures** are fully moddable; mask **geometry** (e.g. moving straps) is **not**, without solving the render-VB linkage.

---

## Open questions / caveats

- **Player chunk count** is cited as ~2715 here (goalie-equipment live read) but 2714 in the Roster.ROS editor notes — treat as ≈2714–2715; read the count field, don't hardcode it.
- **Empty-slot ADD (g02/g03/g05/g06)** is proven in mechanism but **not tested end-to-end in-game** — the launcher's practical feature is **repaint an existing shipped slot**, not add.
- **8_8 uncompressed normal** format (for crisp gear normals) is **derived, not in-game-verified**: predicted descriptor `0x1828010A`, fetch id 10, gated by module flag `DXN_TO_8_8`. If a normal renders garbage/flat in-game, set the flag False.
- **Team-tint identity** is applied for custom masks only; a custom mask therefore loses the game's genuine per-team recolour (accepted trade-off).
- **Assignment does not persist to disk** — it lives in launcher config and re-applies each Launch; the game must be running to enumerate/patch goalies. The `+0x118` roster field is unwritable (re-salts per save), so a goalie's saved mask can only be corrected live once the roster reloads.
- **Per-team masks (future):** the duplicate goalie records (club vs international/all-star) are the hook, but the **team field inside the 0x1A4 record is not yet identified** — same unlock as the deferred team filter in the assign list.
- **Extending create to pads/gloves/sticks** needs those name templates + their roster fields mapped (same add-a-slot + live-assign pattern).

### Superseded old-doc claims (corrected here)
- ~~"g01–g06 only" as the mask ceiling~~ → the real limit is the addressing grid + the g07 no-mesh / pattern-33 cap; g01–g06 relocate/grow works.
- ~~"appended/relocated archive data won't load under Xenia"~~ → **false**; that was the g07 no-mesh problem. Relocate/grow renders fine on g01–g06.
- ~~mesh map "shell 1,2→0x28; 3→0x27; 4,5→0x29" (early project_goalie_masks)~~ → superseded by the confirmed 3-shape filename table in §6 (0x27 = g01/g04/g07–g16, 0x28 = g02/g03, 0x29 = g05/g06).
- ~~"add a custom mask, then point roster fields at it (as a file edit)"~~ → roster field re-salts per save; assignment is **live-memory only**.
- ~~shipped masks are fine as DXT4_5~~ → shipped masks are **DXT1** (blocky); repaint stores 8888.
