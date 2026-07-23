# 01 — Archive (0A/0B/1A/1B), TOC, and IFF container format

One-line summary: NHL 2K10's data ships as four archive files that form one concatenated address space indexed by a hash-keyed TOC in `0A`; each resource is an IFF container of typed sections (DRAM/VRAM), most of them 0x0E4837-compressed.

Status: **verified.** The TOC layout, the name→hash resolver, the IFF section table, and the in-place / append-to-1B modding paths have all been confirmed against real data and tested in Xenia. A few sub-points (the roster string pointer encoding) remain open and are flagged below.

---

## The four archives

The game ships four data files with no extension: `0A`, `0B`, `1A`, `1B`. The engine treats them as **one logical concatenated address space** in that exact order. Concat offsets referenced by the TOC map into this virtual stream:

```
0A: concat [0x000000000, 0x035C00000)   ~1.8 GB   master TOC + textures/descriptors
0B: concat [0x035C00000, 0x059E8A800)   ~1.2 GB   more textures / assets
1A: concat [0x059E8A800, 0x08FA8A800)   ~1.8 GB   audio
1B: concat [0x08FA8A800, 0x0B5C38000)   ~1.28 GB  audio (last archive — the only safe grow target)
```

The exact bounds are **not hardcoded** — they are derived at runtime from the size table embedded in `0A` (see below). Always compute them from the file rather than trusting a copied constant.

---

## 0A header + master TOC

The index lives at the start of `0A`. All fields are big-endian (this is an Xbox 360 title).

```
0x00  magic         = AA 00 B3 BF                 (BE)
      leading longs = [magic][ALIGN=0x800][ARCHIVES=4][0][FILES=0x967=2407][0]
0x18  archive size table: 4 × 16-byte records, one per 0A/0B/1A/1B:
        [ size_in_0x800_sectors ][ 0 ][ name_utf16BE ("0A"/"0B"/"1A"/"1B") ][ 0 ]
0x58  TOC: <FILES> × 16-byte entries, each:
        [ flags(0) ][ size ][ f2 ][ f3 ]          (all BE u32)
```

`FILES` = `0x967` = 2407 entries. Read the count from `0x10`; do not assume it.

Per TOC entry:

- **`f3 × 0x800` = byte offset into the concatenated stream.** Map that offset through the `0x18` size-table bounds to get `(archive file, local offset)`.
- **`size`** = the resource (IFF) byte length to read.
- **`f2`** = the resource **name hash** (see next section). Entries are sorted ascending by `f2` so the game can binary-search them.

The `ALIGN` field (`0x800`) is the sector size that `f3` and the size table are expressed in.

---

## The name hash `f2` — name→offset is SOLVED

`f2` is a standard CRC32 of the **uppercased** ASCII asset name:

```python
f2 == zlib.crc32(name.upper().encode('ascii')) & 0xFFFFFFFF
```

This was verified against 12 named catalog entries (`bootup_audio.iff`, `roster.iff`, `loc.iff`, …) plus 283 team-asset names.

> **Correction vs. the earliest notes.** The original memory header described static name→offset matching as a "DEAD END (f2 != CRC32(name))". That conclusion was wrong — it came from hashing the **lowercase** name. The runtime uppercases the VFS path before hashing, so hashing lowercase never matched. Static resolution is fully solved; there is no need to fall back to live tracing just to resolve a name.

Runtime confirmation: the on-disk `f2` is produced by `Str_Hash` @0x84113740 driving `Crc32_Update` @0x84113438 (table @0x83ACD550). `Str_Hash` inits `0xFFFFFFFF`, walks each UTF-16 char low-byte-then-high-byte, its inner `do/while(c!=0)` **skips zero bytes** (so ASCII-in-UTF16 hashes only the ASCII bytes), and returns `~crc` — i.e. a textbook CRC32 over the uppercased ASCII name.

### Name → offset resolver (works for any known name)

```
h          = crc32(name.upper()) & 0xffffffff
entry      = toc[h]                          # find the entry whose f2 == h
concat_off = entry.f3 * 0x800
(archive, local_off) = map concat_off through the 0x18 size-table bounds
data       = read(archive, local_off, entry.size)
```

Reference implementation: `NHL2K10 Mod Launcher/launcher/archive_textures.py` (`load_toc()`, `resolve()`). Known-name lists: `data/iff_asset_map.csv`, `data/team_iff_catalog.csv`, `team_assets.csv`.

### Recovering names (the TOC only stores hashes)

Hashes can't be reversed, so names were harvested from the executable: grep `default_unpacked.exe` for UTF-16 `.iff` strings (both LE and BE forms are present), expand the format templates found there, and **verify each candidate by CRC against the TOC** before trusting it (so unpack quirks can't inject a wrong entry). Recovered so far:

- ~45 literal global names (frontend/loc/roster/global/gamedata/`logos_large|medium|small`/portrait/overlay_*/bootup*/…).
- `player_head_id_%d.iff` brute-forced → 282 player-face textures.
- Templates like `tr%06d.iff` / `ts%06d.iff` exist in the exe but are **not** present in this archive's TOC.

Coverage is roughly 773 / 2407 entries named (~32%); the remainder are mostly non-texture assets (audio/data) or scene-style assets that need fixed-offset traces.

Naming conventions worth knowing: team codes are the standard 3-letter NHL codes **except Phoenix = `pho`** (not `phx`). Per-team logos are individual `logo_<code>.iff` entries **and** there is a separate packed menu-logo resource (TOC #1534). Ice is `ice_<code>_playoffs.iff` / `ice_<code>_finals.iff`; regular-season ice is `rink_<code>.iff`. Arena audio (incl. goal horn) is `arena_<code>.iff`.

---

## IFF container format

A resolved resource is normally an **IFF** container (magic `FF 3B EF 94`, BE). It holds a section table; each section's payload is usually 0x0E4837-compressed (see `02_compression_0E4837.md`).

```
0x00  magic   = FF 3B EF 94   (BE)
0x10  section count
0x20  section table: 0x20-byte records, each:
        +0x00 type            (BB05A9C1 = DRAM, 411536D5 = VRAM, others exist)
        +0x04 type (dup)
        +0x08 align
        +0x0C runtime / decompressed size
        +0x10 flags / compression
        +0x14 payload offset (absolute, from IFF start)
        +0x18 payload (compressed) size
        +0x1C vram pointer (runtime-filled)
```

Payloads follow the header (DRAM then VRAM per section), align-padded.

Section types:

- **`BB05A9C1` (DRAM)** — serialized C++ resource: texture descriptors, geometry, scene graphs, or packed game-data records. Pointer-relocated on load. For a texture, the DRAM section holds the descriptor (width/height/format/offset) that points into the paired VRAM.
- **`411536D5` (VRAM)** — GPU data: Xenos-tiled DXT (BC1/BC3) textures or other buffers.

### Packed multi-blob resources

Some resources (e.g. the menu-logo pack, TOC #1534) are **not** a single texture but many 0x0E4837 blobs concatenated back-to-back — small DRAM-descriptor blobs interleaved with big VRAM blobs. The pack is walked sequentially: each blob's own header `total_size` tells the loader where the next blob begins. Example: the Sabres menu logo is blob #59 of 240 inside TOC #1534 (`size=0x274C50`, `f2=0xA300D85F`, `f3=0x1565B3`).

---

## Modifying an asset: in place vs. growing

**In place (preferred).** If your re-encoded payload **fits within the original byte size**, splice it over the original bytes in the working archive and zero-pad to the original length. No structure changes, nothing shifts. This is the only mode used for packed multi-blob / stored-offset resources — see the compression doc's replace-safety notes.

**Relocate / grow (only into 1B).** The single safe way to grow a resource is to **append the bigger resource to the end of `1B`** (the last archive), then:

- bump `1B`'s size field in the 0A header at **byte `0x48`** (= new size in `0x800` sectors), and
- repoint that TOC entry (`0x58 + idx*16`): **`size` at +4**, **`f3` at +12** = `newConcatOff / 0x800`.

Because 1B is last, nothing else shifts. **This was verified working in Xenia** (green-tint test): the game honors the edited TOC `f3`, and it is universal across recomp / Xenia / JTAG because it is data-only with the originals untouched. **Never grow a middle archive** (0A/0B/1A) — that would shift 1A/1B's concat offsets and break every entry after it.

**Decompressed-size limit inside packed resources.** For a texture inside a packed pack, the engine sizes its decompress/VRAM buffer from the *original* resource's descriptor, so a bigger **decompressed** payload overflows. Keep decompressed size == original (a bigger *compressed* blob is fine — the pack re-walks by each blob's `total_size`, so downstream blobs shift transparently; just confirm in-game that other textures in the pack still render, since any DRAM descriptor holding an absolute offset could need fixing).

**Known 0B tail quirk.** A working `0B` may be a few MB larger than clean with nonzero data past the declared size that the TOC does not reference (launcher-replacer leftover). The 0A header still declares the clean `0B` size, so that tail is currently unaddressable and harmless. Don't rely on it.

---

## Team database and display names (adjacent, for context)

Team metadata is loaded from **`team2k.iff`** via `IffDb_OpenFile` on a content channel — **not** through the 0A TOC. In memory each team record is `0x11C` bytes, array at `[g_RosterManager+0xAC]`, count at `+0xA8`. Asset filenames are derived from templates using fields in that record (`logo_{token}.iff`, `uniform_base_{baseid}{_home/_away/_alt}.iff`, `arena_{code}.iff`, etc.), so "remap a team's asset" = replace the target file, not edit a table.

Team **display names** (city / nickname / arena) do **not** live in the archives — they are UTF-16BE strings in the player save `Roster.ROS` (a flat, tightly-packed, null-terminated UTF-16BE string pool around offset `~0x24B000`). They are editable **in place only**: the save resolves each string from a **fixed byte offset** read to its `00 00` terminator, so shorter/equal edits are safe but **shifting the pool corrupts the save** (renamed teams lose names and jerseys). Growing a name would require relocating it and repointing its stored offset — and the reference encoding has not yet been located.

---

## Open questions / caveats

- **Roster.ROS name growth is unsolved.** No per-string pointer could be found by exhaustive u16/u32 BE+LE, absolute/pool-relative/char-scaled, or pointer-table searches; yet the game clearly stores variable roster layouts, so some position encoding exists. Until it is traced (best path: the team-name load code in Ghidra/recomp), team names can only be edited to **≤ the original slot length**. The launcher's roster editor is deliberately in-place-only and refuses longer names.
- **~68% of TOC entries remain unnamed.** They are mostly audio/data/scene assets; naming more requires more template harvesting or fixed-offset traces.
- **Section types beyond DRAM/VRAM** exist (e.g. `76CBC6E7`) and are not fully catalogued here.
- **`.ROS` per-resource hashes** (the 12-byte `[offset,hash,size]` directory records at `0x2C`) are not recomputed by our tools; in-place edits have worked without validation, but a stricter build could reject them.
