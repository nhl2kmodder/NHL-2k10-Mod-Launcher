# 24 — Goalie and arena models on disk

Follow-on to doc 22 (skater model). Two more model classes are now readable
straight out of the archive: the **goalie** (`global.iff`) and the **arena +
rink** (`arena_<TEAM>.iff` / `rink_<TEAM>.iff`).

Captures used: `goalie.rdc` (colour pass at eid 1093) and `arena.rdc`
(colour pass from eid 14878, Vancouver).

---

## 1. Method (reusable for any model)

1. In the capture, dump each draw's index buffer and **vertex fetch slots 94 and
   95** — the other ~46 fetch slots hold stale garbage. Slot 95 is the geometry.
2. Byte-match the index buffer into the decompressed archive. That names the
   owning asset and pins the index block.
3. Walk back from the index block to the **48-byte submesh table** (record format
   in doc 22 §submesh record).
4. Recover the source→post-VS vertex permutation (`src_ib[i] ↔ vsout_ib[i]`,
   element-parallel — note `vsout_ib_stride` can be **2**, not 4) and brute-force
   which field of the source record reproduces each post-VS float channel.

Two search anchors that proved decisive:

* **Skin weights** — for character models, 4 consecutive bytes summing to exactly
  255 on *every* vertex. Essentially false-positive free; snap the record phase
  with `start = weight_abs − (stride − 8)`.
* **High-entropy mid-buffer probes** — for the archive sweep, take ≥128 bytes
  from 25/50/75 % into the buffer. 16-byte probes taken from the *head* of an
  index buffer match ascending runs all over the archive (191 false-positive
  assets in the first attempt).

---

## 2. Goalie — `global.iff`, table `0x6456F0`

Predicted in doc 22 and now confirmed: all four captured goalie draws byte-match
its index block (1096→`0x646020`, 1101→`0x64730E`, 1106→`0x647BD4`,
1111→`0x64B180`).

| | |
|---|---|
| table | `0x6456F0`, **49 records** |
| index block | `0x6456F0 + 49*48 = 0x646020`, 120,199 × u16 BE |
| vertex stream | **one interleaved stream** at `0x680D58`, 49,685 × **40 B** = 1,987,400 B |
| fetch slot | 95 (guest `0x62DA0D8`) |

Record layout (stride 40, all big-endian) — the skater's two streams
concatenated:

| offset | field |
|---|---|
| +0 | pos.xyz SNORM16 (+6 = ±32767 sentinel) |
| +8 | normal.xyz + **U** (`snorm × 2`) in .w |
| +16 | tangent.xyz + **V** (`snorm × 2`) in .w |
| +24 | U2, V2, extra (× `c39.x` = 16), unused |
| +32 | 4 × UNORM8 skin weights (sum exactly 255) |
| +36 | 4 × bone slots (pre-multiplied by 3) |

`pos = snorm(s0..s2) * c30.w + c30.xyz`, with
`c30 = [-0.0124140922, -20.1607113, 3.95913363, 107.180046]`
(VS float constant 30, *ModelPosScaleAndOffset*).

Verification: UV vs post-VS max error **3.07e-08**; `|n·t|` **0.00001**; weights
sum to 255 on **49,685 / 49,685**; bone bytes 100 % divisible by 3 (62 distinct
values → 61 bones). Model bbox 214.36 × 178.83 × 51.93 (1 unit ≈ 1 cm),
consistent with the skater's 177.6 height.

Table structure: records 0–2 = jersey arms/front/back at LOD `0x1` (968/472/510
verts, identical to the skater); 3–10 = the LOD chain; 11–39 = one distinct
material + LOD bit each (pads / blocker / catcher / mask variants); 40–48 =
materials 65568+.

Fetch slot 94 (guest `0xC7800F4`, 83,904 B) is **not** in blob 0 — it is
generated at runtime.

Exported to `UniformSubstance/mesh/goalie/`:
`goalie.obj` (16 drawn records, 14,142 verts, 22,445 tris), `goalie_all.obj`
(all 49 records), `submeshes.json`, `goalie_preview.png`.

---

## 3. Arena — `arena_<TEAM>.iff` and `rink_<TEAM>.iff`

The arena is **not** in `global.iff`. Sweeping the archive with mid-buffer probes
named the two owners for the captured (Vancouver) frame:

| asset | crc | container | packed |
|---|---|---|---|
| `arena_VAN.iff` | `B8543FA7` | 0A | 6,810,207 B → blob0 5,428,744 B (codec 7 wp 12) + blob1 9,043,968 B (codec 8 wp 11) |
| `rink_VAN.iff` | `33DAEF3D` | 1B | 21,539,261 B → blob0 2,062,864 B + blob1 44,815,680 B |

All geometry is in **blob 0**, verbatim: 33 of the 35 deduplicated colour-pass
draws matched a table there (the two that did not are `0x11b…` buffers, a
different asset class). The naming is regular — `arena_<TEAM>.iff` /
`rink_<TEAM>.iff` for all 30 teams; many rink props are byte-identical across
teams, which is why one probe hits dozens of assets.

⚠ The frame draws the same geometry ~5 times (shadow / reflection / main passes).
Dedupe draws by index count before doing anything else.

### 3.1 Submesh table

Same 48-byte record as doc 22, but two fields differ from the character models —
`+0x2C` is not restricted to 1|2 (values up to 12 seen), so do not filter on it.
Example (arena_VAN table `0x40D30`, rec 19):

```
prim=6  first_idx=29866  n_idx=12736  tris=12734  0  first_vtx=22752
n_vtx=10155  0  mat=28  lod=1024  0  12
```

### 3.2 Vertex format

Arena vertices are **not** SNORM16 — position is float. Two variants:

**Format A (strides 32 / 36 / 40, world space):**

| offset | field |
|---|---|
| +0 | pos.xyz **float32 BE** — world coordinates, 1 unit ≈ 1 cm |
| +12 | 3 × float16 BE — baked vertex term (→ interp0.xyz) |
| +18 | float16 BE (→ interp0.w) |
| +20, +22 | **UV** float16 BE (→ interp1.xy) |
| +24… | per-shader extras; the last dword is a constant `0x0007FC00` |

**Format B (stride 28, object space):** pos = **float16 ×4 @ +0**, UV =
float16 @ +16 / +18. Used by small props; the positions need that object's model
matrix to be placed in the world.

There is **no per-vertex normal** in the arena streams — the bowl lighting is
baked, and interp2 is a constant `(0,0,0,1)`. Recompute normals from geometry
when exporting.

### 3.3 Models recovered (Vancouver)

| asset | table | recs | verts | stride | fmt | tris | vertex stream | index block |
|---|---|---|---|---|---|---|---|---|
| arena_VAN | `0x40D30` | 32 | 46,030 | 40 | f32 | 25,622 | `0x5E368` | `0x41330` |
| arena_VAN | `0x21FD70` | 22 | 13,401 | 40 | f32 | 7,642 | `0x228F28` | `0x220190` |
| arena_VAN | `0x2D8410` | 3 | 4,408 | 32 | f32 | 1,824 | `0x2DB2E8` | `0x2D84A0` |
| arena_VAN | `0x2FFD0C` | 2 | 30,796 | 32 | f32 | 14,888 | `0x312DD8` | `0x2FFD6C` |
| arena_VAN | `0x406D30` | 5 | 16,068 | 32 | f32 | 26,242 | `0x419CC8` | `0x406E20` |
| arena_VAN | `0x4A16C0` | 6 | 4,656 | 32 | f32 | 2,474 | `0x4A4788` | `0x4A17E0` |
| arena_VAN | `0x4C8F00` | 3 | 1,128 | 32 | f32 | 484 | `0x4C9D38` | `0x4C8F90` |
| arena_VAN | `0x4D2BE0` | 7 | 4,320 | 32 | f32 | 2,290 | `0x4D5958` | `0x4D2D30` |
| rink_VAN | `0x993C0` | 29 | 13,685 | 40 | f32 | 10,785 | `0xA42A8` | `0x99930` |
| rink_VAN | `0x13DE20` | 9 | 1,975 | 32 | f32 | 1,926 | `0x13FAC8` | `0x13DFD0` |
| rink_VAN | `0x1607D0` | 9 | 2,001 | 32 | f32 | 1,926 | `0x162498` | `0x160980` |
| rink_VAN | `0x186C80` | 5 | 1,466 | 40 | f32 | 2,070 | `0x188978` | `0x186D70` |
| rink_VAN | `0x1C9270` | 7 | 2,203 | 28 | f16 | 3,455 | `0x1CBE98` | `0x1C93C0` |

Totals: arena bowl 120,807 verts / 81,466 tris; rink props 21,330 verts /
20,162 tris. Bowl extent ±6,184 × 6,667 tall × ±8,099 (cm).

These are only the models the capture actually drew; the assets contain more
tables (the record scanner finds ~30 runs in `rink_VAN` alone). To add one, find
its table with the scanner and its vertex stream by size (`stream size = n_vtx ×
stride`).

Exported to `UniformSubstance/mesh/arena/`: `arena_VAN.obj`, `rink_VAN.obj`,
`models.json`, `arena_preview.png`.

---

## 4. Editing

Both classes are edited exactly like the skater (doc 22 §roundtrip): decompress
blob 0, overwrite vertices **in place** (never change sizes), re-encode at the
blob's native window power with its original codec, write back. The skater
roundtrip (pants stretched ×1.7) was verified in game and reverted byte-exact,
so the same path is expected to work here — but a goalie/arena write has **not**
been round-tripped in game yet.

Scripts: `scripts/export_goalie.py`, `scripts/export_arena.py`.

---

## 5. Arena lighting (baked) — how to edit it

There is no per-vertex normal and no dynamic light in the arena shader: the light
is **pre-baked per vertex**, in the Format-A record:

| offset | field | evidence |
|---|---|---|
| +12 / +14 / +16 | **baked light colour, RGB, float16 BE** | 5–7 k distinct values per model; R=G=B on grey materials, R>G>B on warm ones (table `0x406D30`); correlates with world Y at −0.73 … −0.82 on the concourse/roof models |
| +18 | second scalar (ambient / AO / shadow term), float16 BE | separate distribution, corr(Y) −0.53 … −0.78 |
| +20 / +22 | UV (unchanged) | |

Rendering the mesh with *only* +12/+14/+16 as vertex colour reproduces a
recognisable arena bake — bright ice and lower bowl, dark roof underside
(`arena_bake.png`, left panel = RGB, right = the +18 scalar).

The stored scale is **per material**, not normalised — mean bake is 0.0019 on one
model and 0.32 on another — so relighting must be **multiplicative**, never a
fixed value.

`scripts/arena_relight.py` does exactly that: decompress blob 0, multiply the
triple (and optionally +18) by a gain/tint, re-encode at the native codec+window,
pad back to the original packed `total`, and patch the archive in place (TOC
untouched). Dry run by default:

```
python arena_relight.py --gain 1.6                 # 60 % brighter, dry run
python arena_relight.py --tint 1.0,0.85,0.7 --write
```

Measured headroom for a ×1.6 gain on all 13 recovered models:

| asset | blob0 | re-encoded | original packed | headroom |
|---|---|---|---|---|
| `arena_van.iff` | 5,428,744 | 2,570,376 | 2,587,672 | 17,296 |
| `rink_van.iff` | 2,062,864 | 1,099,580 | 1,099,822 | **242** |

⚠ `rink_*.iff` has almost no slack — a big edit will not fit and the script
refuses to write rather than corrupt the chain. `decode(encode(x)) == x` is
asserted before anything is written.

Limits: with no normals you can only **rescale/retint what is already baked** —
you cannot move a light or re-bake from scratch without recomputing the values
offline from the geometry. The complementary lever is the texture side (lightmap
/ ambient sheets, and the emissive lamp/glow textures — `arena_van.iff` #75–78
are the light bloom sprites), which goes through the normal `archive_textures.py`
in-place replace path.

---

## 6. Where the arena TEXTURES live

Four per-team asset families, not one (this is why art searched for in
`rink_<TEAM>.iff` isn't there):

| asset | textures | contents |
|---|---|---|
| `arena_<TEAM>.iff` | 86 | the **bowl**: seats/chairs (#70, #71), concrete, stairs, doors, roof trusses, house lights + bloom sprites, sponsor signage (#58 GM Place / NHL.com / 2K / Oakley / Mission), **`GM` logo #80**, "GENERAL MOTORS PLACE" + Canucks sign #79, championship banners #84, gondola #60, flags #68 |
| `rink_<TEAM>.iff` | 81 | the **rink**: ice sheet, boards + dasher ads (Reebok/2K/NHL.com), glass, nets, tape, benches, penalty box, goal light, bench-crew faces/hands |
| `arena_presentation_<TEAM>.iff` | 2 | small presentation pack |
| `led_<TEAM>.iff` | 11 | LED ribbon-board frames ("MAKE SOME NOISE!", "GO CANUCKS GO", "POWER PLAY", team mark) |

Contact sheets: `UniformSubstance/mesh/arena/tex_arena_van.png`,
`tex_rink_van.png`, `tex_led_van.png` (script `scripts/arena_sheet.py`).

Both geometry *and* textures are in the same file — geometry in **blob 0** (DRAM),
textures in **blob 1** (VRAM). `list_textures()` already enumerates both assets, so
they are editable through the existing texture pipeline today.

### 6.1 The five unmatched draws = the crowd

The `0x11b…` slot-95 draws near the jumbotron (eids 17217, 17475, 18213, 18463,
18959; ~320 verts, 1,024×1,024 **565** + 1,024×1,024 BC3) are **not** in
`arena_van`, `rink_van`, `arena_presentation_van` or `led_van`. `crowdanim.iff`'s
primary texture is 1,024×1,024 **565** — these are the crowd billboards, and their
vertex buffers are built at runtime (which is why no file contains them).

## 7. Finding every mesh WITHOUT a capture

A capture was only ever needed to *discover* the layout. Once known, the whole model
table is findable by signature, so all 30 teams work offline
(`launcher/arena_model.py: scan_models`):

1. Scan blob 0 for `u32 6, 0, …` with `+0x14 == 0` — a candidate submesh record.
2. `recs = word[+0x2C] + 1`; accept only if `+0x2C == recs-1-r` counts down across the
   whole table, `tris == n_idx-2`, `first_idx[r+1] == first_idx[r]+n_idx[r]+1` and
   `first_vtx[r+1] == first_vtx[r]+n_vtx[r]`.
3. The index block follows the table. After it comes the vertex declaration and then a
   VB header containing `[stride u16][n_verts*stride u32]` — search the next 0x2000
   bytes for that 6-byte pattern at each plausible stride (24…64).
4. **`vertex buffer = VB header + 0x22`**, constant, verified on 13 models.

Counts: `arena_van` 22 models / 180,433 tris, `rink_van` 37 / 61,219,
`arena_bos` 23 / 210,623. `led_*` and `arena_presentation_*` contain **no** meshes —
they are texture-only.

## 8. Material → texture

There is **no** material→texture table in blob 0 (searched exhaustively at every
stride; the near-miss candidates were running counters inside index buffers). The
binding lives in shader/effect state, so it was recovered from a capture and now
**ships as data**: `launcher/data/arena_materials.json`.

How it was recovered — a two-key join over the arena colour pass:

* draw → submesh: byte-search the draw's first 96 index bytes in blob 0, reject any
  match that is not unique, convert the hit to `first_idx`, look that up in the
  submesh table → material id. 227 of 340 swept draws pinned.
* fetch constant → texture record: Xenos texture fetch constants give
  `(d1>>12)<<12` = base VRAM address and `(d2&0x1FFF)+1 × ((d2>>13)&0x1FFF)+1` =
  dimensions. The asset's runtime base is recovered by voting on
  `base − record.vram_off` over (w,h,fmt)-matching pairs. Raw vote count ties, so the
  top 30 candidates are scored by how many DISTINCT texture sets they explain — the
  real base is close to injective, a coincidental one collapses to a single texture.

Result for `arena_van.iff`: base `0x0C8DA000`, 49 distinct texture sets,
**53 of 88 materials bound**. `rink_*`, `led_*` and `arena_presentation_*` have
several VRAM sub-packages whose group bases the loader assigns at runtime, so their
offline `vram_off` values are wrong and the same vote degenerates (1 texture set) —
those need a live `arena_trace.py` capture before their materials can be mapped.

## 9. Strip parity restarts at the cut

`0xFFFF` in these index buffers is a **primitive restart**, and it resets the triangle
strip's winding parity — parity must be counted from the start of each strip, not from
the start of the run. This is easy to miss and matters: submesh 14 of `arena_van`
model 1 is 580 consecutive 4-index quad strips (2,899 indices, 1,160 real triangles),
so decoding with run-global parity turns every second quad inside-out.

## 10. Replacing one part in place

`arena_model.replace_part` overwrites a single submesh's geometry **without touching
the 48-byte record** — renumbering `first_idx`/`n_idx`/`first_vtx`/`n_vtx` would shift
every later submesh in the model. The new mesh is written inside the existing ranges
and the leftover indices are filled with `0xFFFF`, which draw nothing. Budget:

* vertices ≤ `n_vtx`;
* the re-stripified index stream ≤ `n_idx`. One 4-index strip per triangle costs 4
  indices/tri and an *unedited round trip would not fit* (the artist's strips pack to
  ~2.5), so the importer runs a greedy stripifier (`_stripify`) with single-index
  restarts. Round trip on the part above: 580 strips, **2,899 / 2,899 indices**, UVs
  and baked light bit-identical, zero bytes changed outside the part's own ranges.

New vertices inherit their baked light/ambient (and any unknown tail bytes of the
stride) from the nearest original vertex of that part — there are no normals or lights
in the file to relight from.

Whole-blob write-back is size-preserving (`write_dram`): re-encode with the blob's own
codec/window, pad the packed bytes back to the original length so blob 1 never moves
and the TOC needs no edit; if it doesn't fit, nothing is written. Slack is tight —
`arena_van.iff` has **14,794 spare bytes** of 2,587,672, and `rink_*` has almost none.

## 11. In the launcher — the Arena tab

`launcher/arena_gui.py` (+ `arena_model.py`, `arena_preview.py`): team + asset picker,
model/part tree, a numpy software rasterizer showing the arena with its own textures
and baked lighting (flat-shaded, real z-buffer, ~1 s at 900×600 for 85k triangles),
lighting sliders (gain / ambient / RGB tint, multiplicative — the bake is stored at a
different scale per material), texture export/replace, per-part OBJ export/replace, and
"Export whole arena for Blender" which writes one OBJ + MTL with the game's UVs, the
decoded textures as PNGs and the bake as vertex colours, so nothing has to be hooked up
by hand.

The preview is orthographic, so it has a **cutaway** slider that drops the nearest slice
of geometry — otherwise the roof and near stands hide the bowl.

### 11.1 Deferred shading — why the sliders are fast

`render()` is split into `raster()` + `shade()`. `raster()` writes a G-buffer of
`tid` (triangle index) + two barycentric weights per pixel; it depends only on camera,
viewport size, cutaway and isolate. Everything the lighting sliders touch is in
`shade()`, which interpolates vertex colour + UV, samples the texture bilinearly and
applies exposure/tint. Two G-buffers are kept per camera — the best one and a
≤300k-pixel one — so a slider drag re-shades the small buffer (~40 ms) and a full,
2× supersampled refine follows 320 ms after the drag stops.

Shading must be done **material-sorted**: a boolean mask per material is a full pass
over every shaded pixel, and 88 of them cost more than one `argsort` plus
`searchsorted` into contiguous slices (671 ms → 130 ms at 900×600).

### 11.2 An alternate arena vertex declaration (and a corruption trap)

`arena_van` models 13/14/15 are stride 32 but are **not** the usual layout:

| offset | field |
|---|---|
| 0x00 | position, 3 × f32 |
| 0x0C | packed colour (constant) |
| 0x10 | UV, 2 × **UNORM16** (`0x7fff` = 1.0) |
| 0x14 | 3 × f32 |

Reading the normal f16 "baked light" field there returns values around 1762, which the
preview drew as fluorescent green. `arena_model.is_lit()` gates on the 99th percentile
being ≤ `LIGHT_MAX = 16`; unlit models are drawn flat, and — importantly —
**`relight()` and `light_stats()` skip them**, because writing multiplied f16 into that
field would corrupt geometry data.

Two other practical notes: the per-material bake scale used to normalise light for
display must be cached per asset and re-fed on reload, or a global brightness Apply is
renormalised straight back out of the preview; and `headroom()` re-compresses the whole
2.5 MB blob in pure Python (~21 s, holding the GIL), so it is on-demand behind a button
rather than part of the load.

Related: doc 22 (skater model, vertex format, roundtrip), doc 15 (RenderDoc mesh
editing), doc 01 (archive/IFF format), doc 02 (0E4837 compression).
