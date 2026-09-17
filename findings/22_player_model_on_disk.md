# 22 — The player model on disk: `global.iff`

**2026-08-01.** The runtime vertex and index streams captured in
`jersey_model_capture.rdc` were traced back to their archive asset. They come from
**`global.iff`**, and they are stored there in *exactly* the form the GPU fetches
them — the loader does a straight copy, no repacking.

That closes the question doc 15 left open. Model replacement is an edit to
`global.iff`, not a reverse-engineering problem.

## Locating the asset

| | |
|---|---|
| Name | `global.iff` |
| TOC crc | `0xDB5E3E48` = `crc32("GLOBAL.IFF")` |
| TOC index | 2060 |
| Archive | `1B`, file offset `0x59DD0800` |
| Packed size | 43,537,244 bytes |
| Header | `FF3BEF94` (scene asset) |

Two `0E4837C3` blobs, both codec 7:

| Blob | Packed at | Packed | Decompressed | Window |
|---|---|---|---|---|
| 0 (DRAM) | `+0x3E10` | 13,774,340 | **22,519,256** | wp 12 |
| 1 (VRAM) | `+0xD26C14` | 29,413,227 | 67,006,464 | wp 10 |

Blob 0 holds the geometry. It also holds the shader set, whose parameter names
identify the asset beyond doubt: `GloveColorMap`, `HelmetBrandColorMap`,
`HelmetLogoUVScaleAndOffset`, `StickColorMap`, `StickNormalMap`,
`PerPlayerOcclusionSampler`, `ModelPosScaleAndOffset`.

## Blob 0 loads verbatim at a fixed guest address

```
guest_address = blob0_file_offset + 0x5C59380
```

Verified on 27 independent buffers — every index buffer of every captured draw,
plus the shared vertex streams — each one byte-identical for its **full length**
at that single delta. Nothing is reordered, re-endianed or re-strided on load.
An edit to blob 0 is an edit to what the GPU reads.

## Geometry region (file offsets inside blob 0)

| Region | Range | Size | Layout |
|---|---|---|---|
| Submesh table | `0x1323AF4` – `0x1324304` | 2,064 B | 43 × 48-byte records |
| Index block | `0x1324304` – `0x133D6E8` | 103,396 B | BE `u16`, tri-strips, `0xFFFF` restart |
| Vertex stream B | `0x133D6E8` – `0x136A830` | 184,648 B | stride 8 |
| Vertex stream A | `0x136A830` – ~`0x141EE71` | ~738,496 B | stride 32 |

**23,081 vertex slots / 51,698 index slots**; the submesh table uses 23,077 and
50,986 of them. The two vertex streams are parallel arrays over the same vertex
index — the vertex shader fetches A and B separately (Xenos vfetch slot 47).

## Submesh table — 48 bytes per record, big-endian

| Offset | Field |
|---|---|
| `+0x00` | primitive type (always 6 = triangle strip) |
| `+0x04` | first index |
| `+0x08` | index count |
| `+0x0C` | triangle count (always `index_count − 2`) |
| `+0x10` | 0 |
| `+0x14` | first vertex |
| `+0x18` | vertex count |
| `+0x1C` | signed, `−239 − 48·record` (a stride-linked back-pointer) |
| `+0x20` | material / shader index |
| `+0x24` | LOD-or-visibility bitmask (`1,2,4,8,10,20,40,…`) |
| `+0x28` | id (`record << 16`) |
| `+0x2C` | 1 or 2 |

The table is self-consistent and matched **9 of 9** captured draws on `first index`
with no misses:

| Rec | Part (from doc 15) | Draw | Indices | Verts | Mat |
|---|---|---|---|---|---|
| 0 | jersey_arms | 1137 | 2407 | 968 | 1 |
| 1 | jersey_front | 1147 | 1100 | 472 | 0 |
| 2 | jersey_back | 1152 | 1179 | 510 | 2 |
| 15 | undershirt | 1162 | 499 | 214 | 3 |
| 18 | collar | 1167 | 287 | 168 | 0 |
| 21 | pants | 1177 | 2339 | 890 | 4 |
| 23 | socks | 1189 | 2153 | 840 | 5 |
| 24 | skates | 1199 | 3681 | 1812 | 6 |
| 37 | skate_blades | 1209 | 176 | 116 | 20 |

Records 0–14 are the **same three jersey pieces at five LODs** — flags
`0x01/0x02/0x04/0x08/0x10`, index counts stepping 2407→2360→2360→2383→2204. The
capture only ever drew the `0x01` set, which is why doc 15 saw three jersey draws
and not fifteen.

## What this makes possible — and what is still missing

Replacement is now a bounded editing job:

1. Rewrite indices (BE `u16` strips) and the two vertex streams in place.
2. Update the affected 48-byte submesh records.
3. Re-encode blob 0 with `encode_e4837_lazy` at its native window (wp 12) and
   write it back — the same in-place-replace rule as textures: **stay at or under
   the original size**, because growing shifts everything after it and the load
   address is fixed.

Three draws (arms 1137, socks 1189, skates 1199) additionally fetch a stream at
`0xEBE498C` / `0xEBF6374`, far outside the `global.iff` load region. That one is
**generated at runtime** (per-player data) and is not in the archive.

## Vertex format — SOLVED

Both streams are **big-endian `SNORM16`**, fetched in groups of four shorts. The
Xenia translation makes this explicit: it reads two dwords, `BitFieldSExtract`s
each into a pair of shorts, then swizzles `.yxwz` — the net effect is that the
four big-endian shorts in memory order land in components `x, y, z, w`. Decode is

```python
f = max(s16 / 32767.0, -1.0)      # the -1 clamp is the Xenos SNORM16 rule
```

### Stream B — 8 bytes, the position

| Shorts | Field |
|---|---|
| `0–2` | position `x, y, z`, SNORM16 |
| `3` | unused (only `+32767 / 0 / −32767` occur) |

```
position = clamp(s16/32767, min=-1) * c30.w + c30.xyz
```

`c30` is VS `float_constants[30]`, the constant the shader strings call
**`ModelPosScaleAndOffset`**. It is **identical for all nine player draws**:

```
offset = (0.00193885, -24.6611, 1.872679)      scale = 127.064194
```

Axes, read off the per-submesh bounding boxes: **X** = right (symmetric ±), **Y**
= up (skate blades −108.5 → collar +69.1), **+Z** = front (jersey_front sits at
z −1…+21.6, jersey_back at −21…−1). The player is 177.6 units tall, so **1 unit
≈ 1 cm**.

### Stream A — 32 bytes, everything else

| Bytes | Shorts | Field |
|---|---|---|
| `0–7` | `0–2` | **normal** `x, y, z` (unit length, mean ‖·‖ = 0.9973) |
| | `3` | **U** — `snorm * 2` |
| `8–15` | `4–6` | **tangent** `x, y, z` (unit, mean ‖·‖ = 0.9998) |
| | `7` | **V** — `snorm * 2` |
| `16–23` | `8` | **U2** — `snorm * 2` → `interp3.y` |
| | `9` | **V2** — `snorm * 2` → `interp3.z` |
| | `10` | scalar → `interp3.w`, scaled by `float_constants[39].x` (= 16) |
| | `11` | unused (three distinct values, almost always `0x7FFF`) |
| `24–27` | — | **4 × UNORM8 blend weights** (`×1/255`; they sum to exactly 255 for 23,077 of 23,081 vertices) |
| `28–31` | — | **4 × UINT8 bone slots** |

Normal and tangent are orthogonal: mean `|dot|` = 0.0001 over 23,019 vertices —
they are a genuine tangent basis, not two arbitrary vectors.

UV is exact. Decoded `U`/`V` match the post-VS interpolator to `3.1e-08` — pure
float32 round-off — on all nine draws.

### Skinning

The bone byte is **already pre-multiplied by 3**: slot value `b` selects the
matrix rows at `float_constants[40+b]`, `[41+b]`, `[42+b]`, so the real bone
index is `b/3` and **the matrix palette starts at c40**. 51 distinct slots are in
use across the player. Weight byte `k` pairs with bone byte `k`.

Verified end to end rather than by inspection. Taking the decoded positions,
weights and bone slots as given, there must exist per-bone 3×4 matrices with
`eye_pos(v) = Σ wᵢ · T[bᵢ] · [p,1]`. That is linear in the unknown matrices, so
it can be solved by least squares straight from the capture:

| | rms residual | % of model size |
|---|---|---|
| decoded fields | 0.298 | **0.153 %** |
| same solve, bone indices shuffled | 10.036 | 5.151 % |

A 34× gap. The residual that remains is the expected cost of 8-bit weight
quantisation. Position, weights **and** bone assignment are all confirmed at once.

## Authoring recipe

```python
import numpy as np
NV = 23081
A = np.frombuffer(blob0[0x136A830:0x136A830 + NV*32], ">i2").reshape(NV, 16)
B = np.frombuffer(blob0[0x133D6E8:0x133D6E8 + NV*8],  ">i2").reshape(NV, 4)
Ab = np.frombuffer(blob0[0x136A830:0x136A830 + NV*32], np.uint8).reshape(NV, 32)

sn  = lambda x: np.maximum(x / 32767.0, -1.0)
pos = sn(B[:, :3]) * 127.064194 + (0.00193885, -24.6611, 1.872679)
nrm, tan = sn(A[:, 0:3]), sn(A[:, 4:7])
uv  = sn(A[:, [3, 7]]) * 2.0
uv2 = sn(A[:, [8, 9]]) * 2.0
wgt, bone = Ab[:, 24:28] / 255.0, Ab[:, 28:32]      # bone index = bone // 3
```

To write back, invert with `round(clamp(v, -1, 1) * 32767)` and store big-endian.
Keep the position inside the fixed `c30` box — that scale/offset is a shader
constant supplied by the engine, not something the mesh carries, so a vertex
pushed past ±127 units of the origin will simply clamp.

## Roundtrip PROVEN — the pants patch

**Verified in game 2026-08-01, then reverted** (archive byte-compared back to
pristine, sha1 `ec6e2821c738125347662b0c9a1dd82a44ea17a3`). Editing player
geometry on disk works end to end.

Note what this does *not* prove: pants (eid 1177) read stream B straight from the
archive, while arms/socks/skates fetch it from the runtime address. Whether disk
edits reach *those* parts is still untested.

2026-08-01. Stretched the two pants submeshes (material 4, records 21+22, 1,780
vertices) ×1.7 downward about the waistline, Y −39.4 → −82.2, and wrote it back
into archive `1B`.

- Re-encode at the native window (wp 12, codec 7): 22,519,256 → **13,698,269 B**
  against an original packed size of 13,774,340 — **76,071 B of headroom**, and
  `decode(encode(x)) == x` byte-exact.
- The new payload is **padded back up to the original `total`** before writing.
  The blob chain is walked with `o += total`, so keeping `total` fixed leaves
  blob 1 exactly where it was and leaves the asset size unchanged — **no TOC edit
  needed at all**. Trailing padding is never read; the decoder stops at `dec_size`.
- Verified by re-reading the asset from the archive and decompressing.
- Backup of the original 43,537,244-byte asset: `1B.global_iff_prepants`
  (16-byte `<QQ` header of offset+size, then the raw bytes).

Scripts: `scratchpad/pants_build.py`, `pants_patch.py`, `pants_restore.py`.

The recipe at the top of this section is therefore confirmed end to end, with one
addition: **pad to the original `total` rather than shrinking the blob.**

## Collars — five records, picked by the ROS (2026-08-26)

**verified** (roster survey + preview render; the mapping is what the getters read).
Records **16..20** of the skater table are five collar variants, variant bits
`0x40..0x400`; only one is drawn per player. Which one is the uniform's
**collar style**, ROS uniform chunk `0x1AEB24EC` (stride 284) word `+0x18`
**bits 24–26** (getters `0x840B63B8`/`0x840B63E0`: `lwz r11,0x18(r3); rlwinm
r3,r11,8,29,31`, inlined everywhere — no `bl` callers; the create-a-jersey
inc/dec at `0x840A9138`/`0x840A9160` caps it at 4). `record = 16 + style`:

| style | rec | verts / tris | look | shipped on |
|---|---|---|---|---|
| 0 | 16 | — | plain V | NJD home/away, 313 rows |
| 1 | 17 | 372 / 621 | **laced — the "strings"** | NYI home/away, 46 rows, all real laced kits |
| 2 | 18 | 168 / 287 | V with Reebok Edge tab (the capture's collar) | ANA/BUF/COL/NSH … |
| 3 | 19 | — | plain V | ATL alt |
| 4 | 20 | — | plain V | EDM home |

The launcher's Jersey Editor › Model tab has a **Collar strings** toggle
(0 ↔ 1) that writes the field on Apply, and the preview swaps the collar
record and its decal atlas accordingly (`player_model.COLLAR_RECS`,
`stamp_shader.select_collar`, `uniform_colors.set_collar_style`).

Bit 27 of the same word is set on exactly five alternates (CHI, COL, FLA, MIN,
NSH) and is **unidentified** — open question.

### TODO — a custom (2026 Fanatics-style) collar

Wanted, not started. Everything needed already exists:

1. Players tab › Skater › `collar_r17` › **Export part** (the largest collar slot).
2. Model inside that slot's budget — `replace_part` is size-preserving (fewer
   vertices/indices is fine, more is refused). Positions inside the fixed
   scale/offset box; UVs on the shirt's base unwrap.
3. **Replace part** writes blob 0 back padded to its slot; weights, bones,
   tangent **and the decal channel** are inherited from the nearest original
   vertex.
4. Give kits the style with `set_collar_style(ros, row, style)`.

Two things to build first: a **style combobox 0..4** in the Model tab (only the
laced checkbox exists), and a **"zero the decal channel"** option in
`replace_part` — the NHL-shield decal quad rides on the collar's UV2/slot stream,
so nearest-vertex inheritance will smear the shield across a new collar unless
the decal channel is cleared or the shield quad is modelled in.

## global.iff holds 38 models, but only one in this format

Scanning blob 0 for the record signature (`prim == 6`, `tris == count − 2`) finds
**38 submesh tables**. All 38 are real: their vertex ranges tile exactly. The
large ones separate submeshes with a **1-index gap** (a strip-restart slot), where
the player's table tiles with no gap — so test tiling with a tolerance of 0 or 1.

| table | recs | verts | note |
|---|---|---|---|
| `0xFA2C70` | 76 | 53,187 | |
| `0xC7EB10` | 66 | 57,154 | largest |
| `0x6456F0` | 49 | 49,685 | first three records are 968/472/510 verts — **the same as the player's arms/front/back**, so a player-class variant |
| `0x355850` / `0x9D4358` | 36 | 43,283 | identical counts — two builds of the same model |
| `0xF0AC60` / `0x12AC510` | 42 | 7,081 | another matched pair |
| `0x1323AF4` | 43 | 23,077 | **the skater, decoded above** |

~~⚠ Only the skater uses the stride-32/8 format.~~ **CORRECTED — see doc 24.**
That claim came from a sweep that only ever tried stride 32. Re-sweeping with a
**skin-weight anchor** (4 consecutive bytes summing to exactly 255 on every
vertex — essentially false-positive free) locates **19 of the 38** streams, at
strides 40, 36, 44, 28, 24 and 20 as well as 32. The *field layout* is shared
across character models; only the packing differs. The goalie (`0x6456F0`) is
decoded in full in doc 24: one interleaved stride-40 stream at `0x680D58`.

The route that worked, and that works for any model: capture a frame with the
target on screen, dump the VS plus its buffers, and match the buffers back into
the decompressed archive. Nearby ASCII strings do **not** name the models (only
shader parameters like `DiffuseTint`/`DiffuseColor` sit near the tables), so
identification has to come from a capture or from geometry.

Related: doc 15 (RenderDoc mesh editing), doc 01 (archive/IFF format),
doc 02 (0E4837 compression), doc 10 (IFF asset inventory).
