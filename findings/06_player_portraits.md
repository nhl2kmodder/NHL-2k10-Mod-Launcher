# 06 — Player UI Portraits

**Summary:** The white-shirt shoulder-up headshots shown on player cards and roster lists live in one
big archive blob (`disc_b9610aac.iff`); each player is linked to a portrait by a u16 "photo key" in
their record, and the launcher can re-skin, re-assign, and auto-download real NHL.com headshots into them.

**Status:** Storage, mapping, format, and the import-quality byte-order bug are all fully reversed and
verified in-game. The launcher "Portraits" tab (assign + edit + NHL.com download) is shipped and works
offline; the only outstanding item is a final in-game visual pass on the NHL.com bulk auto-fill flow.
Portraits are engine-locked to **256×256 DXT4_5** — going bigger or adding slots needs an XEX loader
patch (out of scope).

---

## 1. Where the portraits live

Two files work together:

| File | Archive | Magic | Role |
|------|---------|-------|------|
| `disc_b9610aac.iff` | **0B** (TOC crc `0xB9610AAC`) | `0x0E4837` blob stream | **Pixels** — 65.7 MB, one blob per player = **1478 portraits** |
| `portrait.iff` | — | `0xF0985030` | **Index** — ~65 KB, no pixels (`pixels_in_file=False`), loaded at `App_Init` |

- `portrait.iff` is loaded via `Function_83D32438(0xc, "portrait.iff")` → `Res_LoadAssetEx`, name-hash
  `0x5373a8b7`, `entry_count = 1478`.
- **Each portrait blob** decompresses to exactly `0x20000` bytes:
  - `[0x00000 : 0x10000]` = **256×256 DXT4_5** mip0 (the portrait).
  - `[0x10000 : 0x20000]` = the mip chain (see layout below).
- Blobs are **self-delimiting** (`archive_textures._walk_blobs`): header = MAGIC, `dec_sz@+4`,
  `tot@+8`, `codec@+12`, `wp@+16`, `comp@+20`; the next blob starts at `off + tot`. So a portrait is a
  discrete unit — replace one by re-encoding + recompressing in place.

### Native mip-tail layout (inside the `0x10000` tail)
Every level below 128² sits in its **own `0x4000` tile**, padded to 32×32 DXT blocks, tiled with the
**128-wide GTO map, content in the top-left corner**:

```
+0x10000  mip1  128²  (fills the tile)          0x4000
+0x14000  mip2   64²  top-left of a 128 canvas  0x4000
+0x18000  mip3   32²  top-left                   0x4000
+0x1C000  mip4   16²  top-left + packed 8² at (16,0) + 4² at (24,0)  0x4000
                                                = 0x20000 total
```

This TL-corner-per-tile layout is load-bearing: the GPU reads the tail with the 128-wide map, so a
naive contiguous mip pack renders scrambled garbage as mip2/mip3 (this was the origin of years of
"chunky fringe" reports — see §3).

### Why every earlier search missed the portraits (two bugs)
1. `archive_textures._walk_blobs` returned `[]` for `F0985030` packs → it skipped `portrait.iff` (and
   all 11 `F0985030` index entries in the TOC).
2. A **60 MB size cap** in the search scripts skipped the 66 MB pixel store. (`_read_asset` itself has
   no cap.)

---

## 2. Portrait ↔ player mapping (CRACKED)

Reversed via Ghidra + a live Cheat Engine read-breakpoint on Luongo's record.

1. **`player_record + 0x1C` (u16, BIG-ENDIAN) = the portrait KEY.** Reader is literally
   `*(u16*)(player + 0x1C)` (`FUN_840a69e0`). Luongo = key 507. Keys are sparse (observed 3…9442; most
   are > 1477). Confirmed live: 507 → 200 yields the grey silhouette (no photo for that key).
2. The game formats an asset name **`"%04d_image" % key`** (Luongo → `"0507_image"`). The .NET format
   string `"{0:d4}_image"` is at guest `0x83b1dd3c` (UTF-16). Resolver = `Function_83D32188`, entry
   `Function_83D32560`.
3. **`Str_Hash` = standard zlib CRC32** (init `0xFFFFFFFF`, `Crc32_Update @0x84113740`). So the key's
   hash is `crc32("0507_image") = 0xF911EE12` (ASCII, no trailing null).
4. Each `disc_b9610aac.iff` portrait blob's 224-byte header chunk **starts with this same crc32**. The
   lookup matches `crc32(name)` against `portrait.iff`'s `0x5c369069` section records
   (`Function_8410D6C8`).

### Invertible map
For every blob *i*: decode its header chunk, read the first u32 (the hash), then invert
`{ crc32("%04d_image" % N) : N for N in 0..9999 }` to recover the key. **All 1478 blobs resolve.**
Verified: blob 1436 → key 507 (Luongo, user-confirmed); the whole Vancouver roster matched (the Sedin
twins decoded byte-identical).

> **Gotcha:** `portrait.iff`'s hash-record table (`0x5c369069`, base `0x1778`, 1478 recs, layout
> `[V1][V2][hash][marker][2]`) is **sorted by hash for binary search**, so *record index ≠ file blob
> index*. Use the **blob header hash (file order)** for blob→key, never the record order.

### Runtime read path
The game does **not** walk the 66 MB pack per portrait. `portrait.iff` carries a blob-order offset
table `@0xA210`: 1478 × `[smallOff, smallSize, bigOff, bigSize]` (the "small"/"big" = header chunk /
pixel blob). Stale entries → truncated/corrupt reads. `_sync_portrait_index` in `archive_textures`
rewrites this table on every `replace_portraits`.

Roster addressing (live memory): the flat player array is at guest `0xA48490A0`, 2715 records, stride
`0x1A4`, `+0x1C` = u16 key. `player_record + 0x14` → team roster array (ptr list, stride `0x1A4`,
~20/team).

---

## 3. Import-quality root cause + fix (Xenos 8-in-16 fetch endian)

**Symptom:** imported portraits looked blocky with a speckled black/gray fringe and a jittery face,
while native ones looked smooth. Six rounds of "fix the mips / feather the alpha / sharpen" all backfired
because the previews *decoded with the same wrong convention as the encoder*, so every preview lied.

**Root cause (proven via Cheat Engine live-memory dump vs. our decode):** the portrait texture fetch
constant has `endian = 1` (**Xenos 8-in-16 byte order**) on the *whole* texture, not just the mips.
Relative to our tools' byte order, a stored BC3 (DXT4_5) block has:

- **alpha endpoint bytes 0–1 swapped** — this flips the BC3 alpha mode between the 8-level and the
  6-level+0/255 interpretation (that flip *was* the speckled edge ring),
- **alpha index bytes 2–7 pair-swapped** (the jittery face rows),
- **565 colour words unchanged** (we already stored these big-endian),
- **colour index bytes 12–15 pair-swapped**.

Proof: native mip1 alpha MSE vs. reference went **1409 → 0.9** once the full transform was applied. The
game's `0x0E4837` decompressor and fetch-constant handling are otherwise byte-identical to ours (live
dump == our file blob), so offline decode == the in-game look.

**Fix:** `_bc3_8in16` (an involution) applied on **both encode and decode** in `replace_portraits` /
`decode_portrait` — all levels including mip0.

### This was a global texture bug, not portrait-specific
Generalized to `_dxt_endian(enc, fmt)` + `_DXT_ENDIAN_PAIRS`, wired into `_encode_tiled` (all block
encodes) and all 10 decode sites:

| Format | Bytes swapped (per 16-byte block) |
|--------|-----------------------------------|
| DXT1   | index bytes 4–7 only |
| BC3 (DXT4_5) | 0–7 and 12–15 |
| DXT5A  | 0–7 |
| DXN    | all 16 |

(565 colour words stay big-endian in our convention.) Validated on unrelated assets: `logo_bos` DXT4_5
row-pair jitter ratio 0.32 → 0.89, helmet DXT1 0.36 → 0.84 (balanced = correct). Earlier `4444`/`8888`
conversions "felt better" only because linear encoders happen to write BE words that match the 8-in-16
fetch; only *block* formats carried the bug.

> **Migration note:** textures imported into the game files **before** this fix still carry the old byte
> order in-game. Re-import or run **Apply All** to regenerate them correctly. Launcher previews of
> old-modded files now truthfully show their jitter.

### Related quality facts
- **Card screens render mip1/mip2, not mip0** (LOD ≈ 1–2, even while the card magnifies). Native cards
  are equally 64²-soft — that *is* the original quality. mip0 sharpness barely matters on the card.
- The card draws a wide, soft **alpha-driven glow/aura** that samples the small mips — garbage small
  mips produced the chunky aura.
- Portraits are **straight alpha** (transparent RGB = the ~147-gray studio backdrop); encode with
  `premultiply = False`.

---

## 4. The Portraits launcher tab

### 4a. Assign (re-point a player to any existing portrait)
`launcher/portrait_assign.py` (mirrors the goalie-equipment live-memory model — it writes the *loaded
player array*, not the on-disk `.ROS`, because the disk `Roster.ROS` player chunk `0x1E159C31` is a
fixed 2714-slot table where ~2410 slots are empty (`0xFF`) and `+0x1C` there is **not** the key):

- `OFF_KEY = 0x1C` — u16 **big-endian** portrait key.
- `enumerate_players(h)` — name + key.
- `set_portrait_key(h, addr, key)` — writes the u16 at `+0x1C` (BE).
- `apply_portraits(assignments={player_id: key})` — live-write + persisted to `cfg["player_portraits"]`
  and re-applied on Launch.
- Blob thumbnails come from `archive_textures.decode_portrait`; `portrait_key_blob_map()` → `{key:
  blob}` is cached to bundled `portrait_key_map.json` (building it decodes the 66 MB pack ~12 s; the
  JSON loads in ~9 ms).

Validated live: setting Kesler's key to 507 makes Kesler show Luongo's face.

### 4b. Edit (extract / import your own image)
`archive_textures.replace_portraits(name, edits, game_dir)` re-encodes mip0 DXT4_5, regenerates the
native-layout mip tail, applies the 8-in-16 transform, recompresses at the blob's native `wp`/`codec`,
verifies, and splices in place (relocating into region **1B** if needed). Import pipeline for cut-out
PNGs: `_unmatte` → `_premult_resize` → `_alpha_bleed` → `_feather_alpha`. VRAM proof confirms the game
renders the injected bytes verbatim, so all quality iteration is offline.

### 4c. Download real NHL.com headshots
`launcher/portrait_download.py` (pure logic, no tkinter). Fetches a player's official mug by name and
composites it over the slot's native backdrop, then runs the normal DXT4_5 8-in-16 encode.

**Endpoints:**
- search — `https://search.d3.nhle.com/api/v1/search/player` (`culture, q, limit, active`)
- landing — `https://api-web.nhle.com/v1/player/{id}/landing`
- mug CDN — `https://assets.nhle.com/mugs/nhl/{season}/{TEAM}/{playerId}.png` (season + team in the path)

**API behavior (non-obvious):**
- `active` is a **hard filter**, not a toggle: `active=true` → only current players, `active=false` (or
  omitted) → only retired. `match()` searches active first, then retired, then falls back to a
  silhouette.
- A missing mug is **not a 404** — the CDN 200s with a generic gray **silhouette** PNG (~11.8 KB, known
  md5). Detect via `md5 == silhouette` or `size < 14 KB` → treat as "no photo".
- Search records lack birthDate; **landing** carries `birthDate`, `currentTeamAbbrev`, `seasonTotals`.
  Duplicate names are disambiguated by position/team/birthCity (search) or DOB (landing). The game
  roster does not cheaply expose DOB or team, so bulk auto-picks the active candidate and logs the name;
  single mode shows a chooser.
- **Historical / alternate-jersey pulls** work by swapping season+team in the mug URL; a wrong
  team-for-season simply returns the silhouette (graceful fallback).

**Reframe (align mug to the game's tighter head crop), no face-detection dependency:** NHL mugs are
rigidly framed (transparent bg, top-of-head ~16.1% down, head ~46% of frame); native 2k10 portraits are
a tighter crop over a 147-gray backdrop. `reframe()` anchors on the mug's **alpha silhouette** (top row
+ head-centre column) and applies a fixed scale/offset. Calibration constants: `_TARGET_HEAD_TOP =
0.055`, `_TARGET_HEAD_FRAC = 0.57` (default; a per-player slider spans 0.44–0.74), `_MUG_HEAD_TOP_FRAC =
0.161`, `_MUG_CHIN_FRAC = 0.62`, `OUT = 256`. Output is 256×256 RGBA transparent-bg;
`replace_portraits` composites it over the slot's real native backdrop.

**Key design choice:** the download **overwrites the pixels of the blob the player already points at**
(their `+0x1C` key → blob), so no live key reassignment is needed for renamed slots. Players with no
current photo slot are skipped (bulk) or told to assign one first (single); a no-match recycles a free
slot or shares one painted silhouette.

**GUI:** a "Get real portraits from NHL.com (online)" LabelFrame on the Portraits tab — jersey-season
combobox, **Fetch → selected player(s)**, and **Auto-fill ALL roster portraits…** (ThreadPoolExecutor,
8 workers, progress bar + cancel, one batched `replace_portraits`, temp files deleted). No permanent
images are written to disk (download → reframe → temp png → apply → delete). `.spec` hiddenimports add
`portrait_assign, portrait_download, requests, urllib3`.

There is also an optional **jersey-compositing** path (put a player in a jersey they never wore) using a
bundled BiSeNet/CelebAMask-HQ ONNX face-parser (`launcher/data/face_parsing_resnet18.onnx`, ~53 MB) with
a graceful geometric fallback if `onnxruntime`/the model is absent. Composite quality is below a real
mug and only covers the current jersey design.

---

## 5. Engine limits (why bigger / more isn't possible without an XEX patch)

- **256×256 DXT4_5 only, enforced by the loader** (not the descriptor). The portrait descriptor is a
  standard `0xE0` texture record and its dims/format *are* editable, but the streaming path hardcodes a
  fixed **`0x20000` VRAM slot** (256² DXT4_5 mip0 `0x10000` + mips `0x10000`):
  - 512×512 DXT4_5 = `0x40000` mip0 alone → overflows the slot → only the top loads, rest garbage.
  - 256×256 4444/8888 fits `0x20000` but the loader **mis-tiles** uncompressed data for a loader-placed
    (`+0x6c = 1`) portrait → tiling garble.
- **All 1478 must be one consistent size.** The portrait manager clones **one** portrait's descriptor as
  the template for all 128 slot records, so sizes cannot be mixed; any resolution change must convert all
  1478 at once. An all-512 run produced a perfect pack but **crashed at roster load** (streaming path has
  a boot-fixed size constant).
- **Slot count / >1478 portraits never directly tested** — only the 512² *size* test crashed.
- **2 GB container crash:** every apply re-appends the 66 MB pack to region **1B** and orphans the old
  copy; unchecked, 1B exceeds 2 GB → Xenia "Disc Read Error" (signed-32-bit file IO). Portrait applies
  now call `compact_1b()` (single, manual import, and once after a bulk batch).

---

## Open questions / caveats

- **NHL.com bulk auto-fill needs a final in-game visual pass.** The full offline path (reframe → encode
  → decode) is validated, but the mass live re-point of `+0x1C` keys and the shared-silhouette slot
  recycling have not been visually confirmed in-game.
- **Filtering the portrait search by a player's assigned in-game team is not done** — the flat live
  player record (`0x1A4`) has no cheap team field; team assignment lives in a roster-manager TEAM chunk
  whose live layout is not yet identified (the on-disk `0x8489FAF3` chunk / name pool `@0x24A000` are
  different structures).
- **Historical-jersey real mugs can be wrong** (CDN quirk — e.g. Luongo `20132014/VAN` returns his
  post-trade red FLA mug). The current code only trusts a real mug when `want_team == currentTeamAbbrev`;
  otherwise it uses the ML jersey composite.
- **Going past 256×256 or 1478 slots requires an XEX loader patch** to the fixed `0x20000` slot size /
  slot arena — out of scope for asset-only modding.
- The `V1`/`V2` fields in `portrait.iff`'s hash-record table are of **unknown meaning** (not simple
  offsets); they may be part of the boot-fixed streaming constant that blocks resizing. Boot-time exec
  tracing (Xenia gdb stub / DBVM watch) would be needed to pin it — parked.
