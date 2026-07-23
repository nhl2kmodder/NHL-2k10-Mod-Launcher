# 03 — Texture Modding

**Summary:** How NHL 2K10 loads a texture (team → asset name → TOC resolve → `0x0E4837` blob → Xenos-tiled DXT in VRAM), and how the launcher edits it back — the two safe replace paths (in-place vs relocate-grow), supported pixel formats, mip-chain rebuild, the Xenos 8-in-16 endian gotcha, per-texture premultiply detection, lossless DXT→8888 conversion, multi-texture packs, and the two proven dead-ends (live in-memory patching; runtime-generated uniform normals).

**Status:** Extract + replace works end-to-end and is verified in-game (menu logos, team assets, uniforms, overlays, ice, goalie gear). Same-resolution / recolor edits are always reliable. Growable packs can go heavier-than-slot (relocate-grow, auto-8888 lossless). A few packs are in-place-only. Live in-memory texture patching is a **confirmed dead end** — offline apply + game restart only. Uniform embroidery/stitching is a **runtime-generated normal**, not moddable by texture replace.

Driven by `NHL2K10 Mod Launcher/launcher/archive_textures.py` + `encode_dxt5.py` + `nhl2k10_trace_dump.py` (decoder), surfaced in the launcher's **IFF Textures** tab.

---

## 1. The load pipeline (archive → screen)

The game resolves an asset by *name*, not by stored index. Names are built from templates and hashed to a TOC offset.

1. **Identify (team/player → filename).** Asset names are built from sprintf-style templates (`VC_FormatAssetName` @`0x84172CA8`). The token is the team's asset ID — a **string** read from runtime team data (roster / `team2k.iff`), which in practice **equals the 3-letter team code** (`buf`, `van`, `tor`, … Phoenix = `pho`).

   | Asset | Template | Notes |
   |---|---|---|
   | Team logo | `logo_{code}.iff` | uncompressed raw IFF (no `0x0E4837`) |
   | Jersey base | `uniform_base_{code}_{home/away/alt}.iff` | 565 base + 565 detail + **DXN base-normal** (3 contiguous tex) |
   | Jersey overlay | `uniform_{code}_{home/away/alt}.iff` | 6 contiguous tex (stamps/normal/helmet/letters/letters_normal/crowd) |
   | Ice (playoffs/finals) | `ice_{code}_{playoffs/finals}.iff` | |
   | Ice (regular) | `rink_{code}.iff` | large package, 50–65 sub-textures |
   | Arena LED | `led_{code}.iff` | |
   | Arena presentation | `arena_presentation_{code}.iff` | **scatter-packed** |
   | Zamboni | `zamboni_{code}.iff`, `zamboni_team_{code}.iff` | |
   | Overlay / HUD | `overlay_static.iff` etc. | stored-offset multi-tex |
   | Global UI | `global.iff` | loader-repacked, ~67 MB blob |

   Catalog: `team_iff_catalog.csv` (30 teams × ~15 categories). **`arena_{code}.iff` is AUDIO** (goal horn), not a texture — do not decode it as one.

2. **Request → resolve.** `Tex_LoadByName` @`0x83FDB9B8` → `Asset_LoadTyped` @`0x84153290` (type token `g_TextureManager` @`0x84AF232C`) → `Asset_ResolveHandler` @`0x84152210`. Name is lowercased, then hashed with **standard CRC32** (`Str_Hash` @`0x84113740`, `hash = zlib.crc32(name.lower())`); the CRC (TOC field `f2`) indexes the 0A archive TOC → `(archive, offset, size)`. See `01_archives_and_toc.md` / `iff_toc_format`.

3. **Stream read.** `Asset_StreamLoadWorker` @`0x83D32DF8` reads the asset from the mounted 0A/0B/1A/1B archives (VCFILEDEVICE) in 1 MB chunks under a crit-section.

4. **Decompress.** Each IFF payload is wrapped in the custom **`0x0E4837C3`** flag-byte LZ77 (codec 7/8). See `02_e4837_compression.md`. It is **lossless**.

5. **Section / format.** An IFF holds a **DRAM** section (type `0xBB05A9C1` — a serialized C++ resource, pointer-relocated at load) and a **VRAM** section (type `0x411536D5` — the tiled texture bytes). Section record = 0x20 bytes: `type@+0, dup@+4, align@+8, runtime@+0xC (decompressed/alloc size), flags@+0x10, payload_off@+0x14, payload_size@+0x18, extra@+0x1C`.

6. **GPU upload.** `Tex_CreateFromIffSections` @`0x8410C330` pairs DRAM+VRAM; `Tex_BuildFromDramVram` @`0x8417CEB0` relocates internal pointers; `Gpu_SetTextureHeader` @`0x84212A58` builds the Xenos fetch constant. **The GPU decompresses DXT and detiles in hardware** — there is no CPU detile in the game and **no special high-quality path**. Native quality = the cooker's offline DXT + the full mip chain + GPU trilinear/aniso filtering + the original authored resolution. Replacements can **match** native but not exceed the authored resolution.

---

## 2. The Xenos texture fetch constant

The 6-dword GPU fetch constant is what tells the GPU the format/dims/tiling/base. Reading a baked one:

```
d0 @+0x94 : tiled = d0>>31 ; pitch = (d0>>22) & 0x1FF   (pitch*32 == aligned width)
d1 @+0x98 : fmt  = d1 & 0x3F ; ENDIAN = (d1>>6) & 0x3F ; base = d1 & 0xFFFFF000
d2 @+0x9C : w = (d2 & 0x1FFF)+1 ; h = ((d2>>13) & 0x1FFF)+1
d3 @+0xA0 : filter/swizzle bits
d4 @+0xA4 : mip_max_level in bits 6-9 ( (log2(max(w,h))<<6) | 0x03 )
d5 @+0xA8 : mip address = (mip0_size & ~0xFFF) | packed/dim flags (0xA00 = packed 0x800 | k2D 0x200)
```

**Validity test used everywhere:** `(d0 & 3) == 2` (Xenos type bits = texture). This one check killed the "false fetch" bug that made uniforms decode as noise.

In small-descriptor assets the constant sits at record `+0x94`. In package assets there is a **texture-record array**: `count @0x20`, `ptr @0x24`, **0xE0-byte records**: `w@+0x60, h@+0x62, flags@+0x5C, vram-offset@+0x6C, mip0-size@+0x70, mip-tail-size@+0x74`, embedded fetch @`+0x94`. Footprint = `+0x70` + `+0x74`.

**Untile** (`gto` in `nhl2k10_trace_dump.py`, byte-for-byte verified against Xenia `Tiled2D`):

```
x,y in BLOCKS; pitch in BLOCKS; bpb_log2 = log2(bytes/block)
DXT1/DXT5A=3, DXT4_5/DXT2_3/DXN=4, 565/1555/4444/8_8=1, 8888=2, "8"=0
pitch = align(pitch,32)
macro = ((x>>5)+(y>>5)*(pitch>>5)) << (bpb_log2+7)
micro = ((x&7)+((y&0xE)<<2)) << bpb_log2
off   = macro + ((micro&~0xF)<<1) + (micro&0xF) + ((y&1)<<4)
return ((off&~0x1FF)<<3) + ((y&16)<<7) + ((off&0x1C0)<<2)
       + (((((y&8)>>2)+(x>>3))&3)<<6) + (off&0x3F)
```

Returns a **byte** offset already (do not ×8). Bijection verified over many sizes.

---

## 3. Supported formats

`REPLACE_FORMATS = ("DXT1","DXT4_5","DXT5","DXN","DXT5A","8888","8_8","4444","565","1555","8")`

| Format | Xenos code | bytes/block or /px | Notes |
|---|---|---|---|
| DXT1 (BC1) | 18 | 8 /block | 1-bit-alpha or opaque color |
| DXT2_3 (BC2) | 19 | 16 /block | explicit 4-bit alpha (decode only) |
| DXT4_5 (BC3) | 20 | 16 /block | **the dominant format**; DXT4=premult, DXT5=straight (see §5) |
| DXT3A / DXT5A | 58 / 59 | 8 /block | single-channel (glyphs/masks), rendered gray |
| DXN / BC5 (ATI2) | **49 (0x31)** | 16 /block | 2-channel tangent normal (X in bytes 0-7, Y in 8-15, Z reconstructed). Was originally missing from the format table → normals decoded as garbage; **fixed**. |
| 8888 (A8R8G8B8) | — | 4 /px | uncompressed, ARGB, lossless |
| 8_8 | — | 2 /px | uncompressed 2-channel (normal target) |
| 4444 | — | 2 /px | uniform stamps, overlays |
| 565 | — | 2 /px | jersey base color |
| 1555 | — | 2 /px | 1-bit alpha |
| "8" | — | 1 /px | single-channel grayscale |

**Decode byte order (Xbox / Xenos):** DXT color endpoints `c0/c1` are **big-endian** 565 words (`c0 = b[1] | b[0]<<8`), the 2-bit color indices are little-endian; 16-bit linear texels are big-endian words; `8888` reads as `A R G B` big-endian; DXT4_5/2_3 store the alpha block at bytes 0–7 and the color block at 8–15.

Format field tables (in `archive_textures.py`, for format-change writes):
```
_FMT_DESCRIPTOR = {8888:0x18280186, 4444:0x1828014F, 8_8:0x1828010A,
                   DXT4_5:0x1A200154, DXT5:0x1A200154, DXT1:0x1A200152}   # set @+0x08,+0x0C,+0x1C
_FMT_F1_LOW     = {8888:0x086, 4444:0x04F, 8_8:0x00A, DXT4_5:0x054, DXT5:0x054, DXT1:0x052}  # low 12 bits of d1@+0x98
```

---

## 4. Xenos 8-in-16 endian issue (the "rough alpha edges" root cause)

Most block-format textures carry **GPU endian = 1 (8-in-16 byte swap)** in `d1` bits 6–11. A decoder/encoder that ignores this **byte-swaps within each 16-bit unit**, which on BC3 corrupts the **edge alpha** and pair-swaps pixel rows on any clean-source import. This was invisible in symmetric extract→edit→reimport (the swap cancels itself) but showed up on any texture built from clean external art.

**Fix:** an explicit `_dxt_endian` / 8-in-16 handling applied to **all block-format encode + decode paths and to DDS passthrough**. Measured on portraits: native-alpha MSE **1409 → 0.9**. The correct 8888 endian is `0x086` (8-in-32); DXT4_5/DXT5 = `0x054` (8-in-16); DXT1 = `0x052`. When converting a texture to 8888 you **must** rewrite the whole low 12 bits of `d1` — leaving the DXT endian on 8888 data reads green as alpha (the "magenta/green corrupted title screen" bug).

> Note: this endian correction is newer than the original texture memory's remark that "8-in-16 index swap made no visible difference" — that early observation was on symmetric round-trips where the error cancels. On clean-source imports it matters; the fix is in the shipping code.

---

## 5. Premultiplied vs straight alpha (per-texture, auto-detected)

Alpha mode is **per-texture**, not global. Verified by raw-decoding originals:
- **Premultiplied (DXT4 semantics):** transparent texels store RGB ≈ 0; e.g. team logos (`logo_van/buf`), the 2K27 title logo.
- **Straight (DXT5 semantics):** transparent texels keep their color; e.g. `overlay_static`, most UI/HUD, titlepage cover/EULA/2k_logo/stats.

Wrongly premultiplying straight art multiplies RGB by alpha and **darkens every partial-alpha pixel** — this is the classic "replaced texture looks washed-out / low-res." A faithful straight round-trip is ~32–42 dB vs ~6–24 dB when wrongly premultiplied.

**Detector:** `_orig_is_premult` decodes the **CLEAN original raw** (not through the un-premult path) and applies the `_rgba_is_premult` invariant — premult art has `maxRGB ≤ alpha` for ~all pixels; straight art shows ≥7% violations. Encode premultiplies **only** genuinely-premult textures; mips are downscaled in the matching space with a **BOX** (2×2 average) filter to match the game's own mip filter and compressibility.

**Best save format for an edit:** **PNG, full RGBA, STRAIGHT (normal) alpha, highest available resolution.** The encoder premultiplies + DXT-compresses internally in one pass. Do **not** pre-premultiply and do **not** hand it a compressed DXT DDS unless it's a deliberate pro-tool passthrough (see §7).

> Supersedes the old "all DXT4_5 is premultiplied" claim (it was corrected to per-texture) and the "always premultiply" encoder default (now `premultiply=False` + auto-detect).

---

## 6. Replace: in-place vs relocate-grow, and the safety rules

The general flow: edit the exported image → re-encode to game-order tiled bytes (`encode_dxt5.py`, the exact inverse of the decoder — PCA + least-squares endpoint fit) → rebuild mip0 (keep the tail) → `0x0E4837`-compress → `_verify_blob` (aborts, files untouched, if the re-encoded blob doesn't round-trip) → splice into the **working** archive.

Every apply first calls `ensure_clean(iff, game_dir)` (reset to pristine CLEAN bytes) and re-splices the **full current mod-set** of that IFF in one pass, so edits never *stack* on an already-mutated pack. `compact_1b` runs after to drop orphaned relocate dead space.

**Path A — in-place (default, always safe).** New compressed blob **≤ original slot** → overwrite in place; **keep the decompressed size == original** (the engine sizes its VRAM/decompress buffer from the original resource; a bigger *decompressed* payload overflows → a bogus ~500 MB cache-flush → Xenia crash). Recolor / same-resolution edits always fit.

**Path B — relocate-grow (`replace_multitex_grow` / `_grow_many`).** For heavier-than-slot or hi-res edits: **append** the new mip chain to the **end of the texture blob**, **redirect** that texture's record `+0x6C` at it, patch `+0x70`/`+0x74`, re-encode both blobs **at their native LZ window** (`wp`), relocate the resource to the end of **1B**, and repoint the TOC. Old slots become dead space; unedited textures are untouched.

**Preconditions for grow (else fall back to in-place/posterize):**
- the texture blob is the **last** blob in the IFF,
- the record is **findable** — its `+0x6C` resolves to the texture's VRAM offset **and** its `+0x60/+0x62` dims match the edit (dims check added to stop coincidental placeholder records, e.g. a `1x0` stub at offset 0, from poisoning the whole batch),
- when growing a whole IFF section you must also set the section's **decompressed/alloc size @+0xC** (`_patch_iff_section(..., dec_size=)`) — updating only `+0x14/+0x18` under-allocates and the decompress overflows adjacent fetch constants → crash.

Holds for **rink / led / uniform-overlay / scene** packs. Does **not** hold for:
- **`global.iff`** — loader-repacked (see §8); grow precondition fails → in-place / posterize (or the special sequential-splice convert).
- **`overlay_static`** — its big VRAM blob isn't last → in-place / posterize.
- **`arena_presentation_*`** — scatter-packed; never grow/redirect (corrupts the layout) → in-place, or the dedicated `_repack_scatter`.
- **`uniform_base_*`** — offsets are load-assigned (`+0x6C=1`), forced in-place.

**The naive relocate is banned.** Moving the whole resource *without* redirecting the records leaves the stored sub-offsets pointing at the wrong bytes → the game **freezes** booting gameplay. `replace_at` now refuses it with a clear "in-place only, make it smaller" error; the only relocation ever performed is append+redirect.

**Posterize-to-fit:** when an in-place-only edit is a hair too big, `_posterize(img, levels)` quantizes RGB (alpha untouched) so BC3 endpoints repeat and the `0x0E4837` blob shrinks; it loops 64/48/32/24/16 and keeps the highest quality that fits, else raises.

**Scatter-DXT footprint caveat:** small-height tiled block textures (e.g. `overlay_static` t18/t19, 256×64 DXT4_5) gto-tile to a *padded* footprint (30720 B) that doesn't equal their stored mip0 slot (16384 B). `replace_many` pre-filters these — if neither DDS-passthrough nor `_encode_tiled` yields exactly `_mip0_size(fmt,w,h)`, the texture is **skipped** (view/extract only) with a warning so the rest of the batch still applies. These are effectively not replaceable via the in-place path.

**Revert a relocated asset:** restore the CLEAN resource bytes into the working archive at the same offset and repoint the TOC entry (`size@+4`, `f3@+12`) back to CLEAN. The relocated copy at the 1B tail becomes an unreferenced orphan (compaction reclaims it).

---

## 7. Mip-chain rebuild + the small-logo fix

`_rebuild_with_mips` regenerates the mip tail after a mip0 edit, bounded by the texture footprint (`mip_end`) so it never corrupts a neighbor.

**Small-logo pixelation on the scorebug / in-game overlays — FIXED (2026-07-21).** For DXT levels **< 128 px** the walk was writing the *bare* `_encode_tiled` size (64 → 0x3800) instead of the native **padded min-tile** (0x4000), so the running offset desynced ~64 px and the 32/16/8/4 mips kept **stale old-logo bytes**. Menus sample the top mips (looked fine) but the scorebug/overlays sample the tiny mips (pixelated).

Native square-block tail (verified on `logo_bos/van/tor`, 512² DXT4_5, blob 0x60000):
```
mip0 0x40000 | [256:0x10000][128:0x4000][64 pad:0x4000][32 pad:0x4000][packed{16@0,8@16,4@24}:0x4000]
```
**Fix:** for `block and w==h`, build the tail explicitly — own tiles ≥128 at full size, <128 padded into a 128-px top-left canvas down to 32, then one packed 16/8/4 tile — bounded by `cap`/`mip_end` (same layout as the portrait tail). Non-square (uniforms/overlay) and non-block formats keep the old MSE-gated walk (no regression).

**Separate issue — "blow up bigger" softness:** `logo_*` mip0 is DXT4_5 (BC3) block compression, and the menu samples the mip chain, so `logo_*` is pinned to native DXT (skips 8888). Making it truly crisp means storing lossless 8888 or hi-res — which touches the menu mip chain + the `logos_small/medium/large` atlas cascade → needs an in-game verification pass before enabling. **Not done.** A power user can already supply a high-quality pre-compressed DDS via passthrough.

**UI textures don't minify:** all title-page/menu UI textures have `mip_max_level = 0` (no usable mips) and are authored at their exact on-screen size. Upscaling one and letting the GPU shrink it back **aliases** → blockier than native. Replace UI/menu textures at **native size**; hi-res only pays off for textures drawn large/up-close (ice, jerseys, faces).

**DXT→8888 lossless conversion (§ below) and DDS passthrough:** a user-supplied already-compressed DXT DDS is embedded **directly** (only the 2 color-endpoint 565 words per block are byte-swapped LE→BE, then gto-retiled — no re-decode/re-encode) so a pro tool's (NVTT/Compressonator) mip0 is never double-compressed. PNG / uncompressed A8R8G8B8 DDS take the normal single-encode path.

---

## 8. Multi-texture .iff packs

Many assets pack **several textures in one VRAM blob** (`count@0x20 == 0` in the formal tree). Two sub-cases:

- **Stored-offset (splittable from the file):** each 0xE0 record stores its real `+0x6C` offset. Cumulative footprint == stored offset (validated). Applies to `overlay_static` (18 sub-tex), `overlay_wipes` (6), `rink` (50–65), `led` (3), and the **`uniform_<code>_<kit>.iff` overlay** (6 tex). `_stored_offset_records` enumerates them (texture blob = smallest non-records blob ≥ max extent, self-validated).

- **Load-assigned (`+0x6C == 1`, contiguous):** the loader packs textures contiguously in a **build-time permutation** of DRAM record order that is **not** derivable from the file. Applies to `uniform_base_*` and player faces. `_MULTI_LAYOUTS` registers the known DRAM-order → VRAM-order signatures; `_contiguous_records` accepts only registered signatures that **exactly fill** the VRAM (double-gated). These are forced in-place.

  - `uniform_<code>_<kit>.iff` (overlay, VRAM 0x806000, identical across all 30 teams): `stamps` 4444 2048×512 @0 · `normal` **DXN** 2048×512 · `helmet` 4444 1024×256 · `letters` DXT4_5 3968×256 · `letters_normal` **DXN** 3968×256 · `crowd` DXT4_5 1024×1024. DRAM order `[normal,stamps,helmet,crowd,letters,letters_normal]`; VRAM order `[1,0,2,4,5,3]`. **The `stamps` slot is byte-identical to the front-end team-select decals** — jerseys had to be edited twice until this was mapped (see `jersey_map`).
  - `uniform_base_<code>_<kit>.iff` (3 tex, records at DRAM ~0x154E0, past the old 0x400 scan window; scan widened to 0x18000): `[0]` 1024² 565 base color · `[1]` 512² 565 detail · `[2]` 1024² **DXN base-normal**. Footprints all differ → VRAM order fully determined by exact-fill (no permutation guess).

- **Scatter-packed (`arena_presentation_*`):** several sub-packages concatenated; each record's `+0x6C` is **group-relative** (a group starts where `+0x6C` resets to 0/1). `_scatter_records` sorts by base, cumulates, and accepts only if footprints fill VRAM exactly **and** every `(cumulative − group_base) == stored relative offset`. Never grown/redirected; `_repack_scatter` rebuilds the whole blob in record order (copies unedited bytes verbatim, encodes edited ones lossless).

- **Loader-repacked (`global.iff`):** ~67 MB single VRAM blob, ~446 records, **all** with `+0x6C = 1` (placeholder → base+0). The runtime VRAM layout is a load-order + content-dedup sequence not recoverable from record structure — historically needed **live capture** (`live_capture.py` via Cheat Engine on Xenia) to map record→VRAM offset. **However**, decoding each DRAM record at its **cumulative footprint** offset (plain record order) yields 436/446 coherent textures / 93% blob coverage → the *file* blob is a sequential concatenation in DRAM record order. So a file edit is a **splice-in-place** (`replace_sequential_convert`): rebuild `blob[:pos] + new + blob[pos+oldfoot:]`, rewrite the record's format/mip0 fields, **leave `+0x6C = 1`** (the loader ignores the stored offset — redirect/append does **not** work here). A CE-captured runtime map (`global_iff_runtime_map.csv`) gives the ground-truth per-record offsets used for reliable browse/extract. Each convert re-encodes the full 67 MB (~1–2 min) and is in-place only.

  > Supersedes the old doc's flat "global.iff can't be grown / live-capture required for everything" — global is convertible offline by sequential splice; the loader-repack only describes the runtime VRAM layout, which is irrelevant to a file edit.

---

## 9. DXT → 8888 lossless conversion (auto, on growable packs)

Storing a **new** replacement as uncompressed **8888** eliminates BC3 block artifacts (a sharp logo round-tripped through DXT4_5 ≈ 28–29 dB with edge bleed; 8888 = 99 dB / lossless). **Converting an already-DXT texture to 8888 gains nothing** (the data is already degraded) — 8888 only helps for fresh clean-source art.

Conversion rewrites the format fields (ground-truthed by a full 0xE0-record diff):
- descriptor `@+0x08/+0x0C/+0x1C` (all three) → `_FMT_DESCRIPTOR[fmt]`,
- `d1 @+0x98` low **12** bits (format id + **endian**) → `_FMT_F1_LOW[fmt]`,
- `d3 @+0xA0` low 12 bits → `_FMT_F3_LOW` (8888 = 0xC14),
- `d5 @+0xA8` = `(mip0_size & ~0xFFF) | 0xA00`,
- `+0x70` mip0 = `w*h*4`, `+0x74` = tail bytes.

Router `_lossless_target(fmt)`: color-DXT → **8888**, **DXN → 8_8**. Applied automatically on Apply/Apply-All for growable, non-scatter, non-loader-repacked packs (`can_lossless_8888` gates it). In-place-only packs (`global`/`gamedata`) and scatter packs stay DXT (8888 is 4× the bytes, won't fit the slot). Single-primary assets (team logos, masks, cover art) auto-upgrade too via `replace_primary_convert` when growable.

**Extract is never lossy** — it's the exact `T.decode` of the shipped bits; any blockiness in an export is the shipped DXT4_5's own compression, not the tool.

---

## 10. Runtime capture (when static decode is ambiguous)

Xenia GPU traces (`.xtr`) carry the runtime fetch constants (correct fmt/dims/base) + the texture memory. `nhl2k10_trace_dump.py` parses a trace and decodes every fetch — this is how all 30 team logos were first extracted cleanly (a 256² DXT4_5 logo atlas at consecutive VRAM addresses). Enable `trace_gpu_stream=true` in the Xenia config, play to the screen, quit, run the dumper. `live_capture.py` (Cheat Engine on the running Xenia) reads each loaded record's **resolved** `+0x6C` VRAM pointer to recover loader-repacked offsets (`global.iff`).

---

## 11. Dead ends (do NOT pursue)

- **Live in-memory texture patching — CONFIRMED DEAD END (tested 2026-07-06).** Writing new bytes into Xenia's guest RAM (via `WriteProcessMemory` + `VirtualProtectEx`) does **not** refresh the display. The GPU draws from a decoded, cached **D3D12** texture that is decoupled from the reachable guest RAM. Decisive test: filling the entire `logo_van` mip0 with `0xFF` (verified 262144/262144 bytes = FF, stable) → the logo still rendered perfectly. Xenia watches guest writes with **VEH page-guards** (GetWriteWatch = 0 hits, AddVectoredExceptionHandler = 1), which an external write and an injected fault-touch both fail to trip. **Offline apply + full game restart is the only supported flow.** The launcher shows a "Restart Required / Relaunch Game" popup at the end of an apply when NHL 2K10 is live in Xenia.

- **Uniform embroidery / stitching normal — NOT moddable by texture replace.** The in-game raised-stitching effect the user saw comes from a **runtime-generated normal**, not a paintable diffuse texture. The DXN normals that *are* in the IFF (`uniform_base_*` base-normal, `uniform_*` overlay normal + letters_normal) are editable in-place, but the visible embroidery is produced at render time. A full paintable uniform (Substance 3D) is **blocked on a live vertex/mesh capture** — no player-body/uniform mesh exists in extractable form (raw geometry in `model_data/` is packed under an un-reversed scheme, parser handler `0x5c369069`); `vertex_capture.py` exports positions only (no UV/normal/index).

---

## Open questions / caveats

- **>256 px on packed menu-logo slots** still needs the exact Xenos **packed-mip-tail** layout to grow safely; only same-decompressed-size edits are proven there. (Standalone whole-file assets grow fine via relocate-grow.)
- **`uniform_base_*` DXN in-place edit:** in-game render after edit is **UNVERIFIED** (the overlay DXN normal edit is proven, so it's expected to work).
- **Scatter `_repack_scatter`** (arena presentation): group-base = cumulative assumption is headless-validated but **needs an in-game render confirm**; if wrong, disable `REPACK_SCATTER`.
- **`global.iff` sequential-splice convert:** validated headless (99 dB target, neighbors intact) but **awaiting in-game confirm**; the CE runtime map is instance-specific — recapture if archives/version change.
- **`logo_*` true crispness (8888/hi-res):** deliberately not enabled pending an in-game pass on the menu mip chain + `logos_small/medium/large` atlas cascade.
- **`player_face`** second (normal/detail) texture: contiguous-packed, needs one live capture with a face on screen (structure generalizes across all players).
- **Ice packed mip tail** (L6–L8, extreme distance): not replaced; needs the Xenos packed-mip-offset solve (same open item as hi-res minification).

### Corrections vs the old doc (`docs/03_texture_modding.md`)
- Added the **Xenos 8-in-16 endian** issue + fix (absent from old doc; the old texture memory even wrongly said the swap "made no visible difference" — true only for symmetric round-trips).
- Premultiply: reframed as **per-texture auto-detect** (old "all DXT4_5 straight" simplification corrected; the real rule is a per-texture detector — some logos are premult, most UI is straight).
- **DXN** added to the supported-format list (was missing → normals decoded as noise, since fixed).
- **DXT→8888 auto-lossless** conversion + DDS passthrough documented (post-dates the old doc).
- **`global.iff`** corrected from "can't be grown, live-capture required" to "convertible offline by sequential splice; live capture only for the runtime VRAM map."
- **Small-logo mip fix**, **`uniform_base_*` 3-texture (incl. base-normal)** layout, and the **scatter-DXT footprint skip** are new since the old doc.
- **Live texture patching** documented as a proven dead end, and **uniform embroidery** clarified as a runtime-generated normal (not texture-moddable).
