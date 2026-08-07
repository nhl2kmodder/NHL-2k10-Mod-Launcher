"""f0985030.py — parse the NHL 2k10 `0xF0985030` texture-atlas container.

A handful of assets use a different container magic than the standard IFF (`0xff3bef94`):
`logos_large/medium/small.iff` (team-logo atlases), `portrait.iff` (player portraits),
`iconnav.iff` (menu icons). This format is **loader-repacked**: the file is a small INDEX — its
DRAM/VRAM section data offsets point PAST end-of-file, the file's entropy is low (~5.1 vs ~7.9 for
DXT), and it contains no `0x0E4837` blob. The actual texture pixels live in VRAM, filled by the
loader at runtime (same situation as `global.iff`). So:

  * The FILE gives per-texture descriptors + the VRAM address/size of each texture.
  * The PIXELS must be read from the running game (live capture at those VRAM addresses).

Header (big-endian), verified across logos_large / portrait / iconnav:
    0x00  u32  magic          0xF0985030
    0x04  u32  file_size
    0x08  u32  file_size      (duplicate / decompressed size)
    0x10  u32  section_count  (2 -> DRAM + VRAM)
    0x18  u32  entry_count    (# textures: iconnav 58, logos_large 120, portrait 1478)
    0x20  u32  desc_off       -> in-file per-texture descriptor array
    0x24  u32  index_off      -> in-file (block_size, vram_addr) array
    0x28  section table, stride 0x20: [type@+0, alloc@+0xC, dataoff@+0x14, compsize@+0x18]
          types 0xBB05A9C1 (DRAM) + 0x411536D5 (VRAM); dataoff is PAST-EOF (repacked).

SUPERSEDED for the logo atlases — use `logos_atlas.py`, which fully reverses this format
(parse/build round-trip byte-exactly on all five files) and can ADD entries. Two fields this module
mislabels: `0x20` and `0x24` are not `desc_off`/`index_off` but derived sizes (`24n+73` / `44n+69`);
the real regions are laid out contiguously from `0x68`. This module is kept only for the
header sniff + live-VRAM-capture path.

Do NOT edit team logos via the per-team `logo_<code>.iff` files — nothing draws from them
(`Tex_PrecacheTeamLogos` is their only reference). The front end looks a logo up by
`crc32(lowercase asset key)` inside these atlases; that is what `logos_atlas.py` edits.
"""
import struct

MAGIC = 0xF0985030
_BE = lambda b, o: struct.unpack_from(">I", b, o)[0]


def is_f0985030(data: bytes) -> bool:
    return len(data) >= 4 and _BE(data, 0) == MAGIC


def parse_header(data: bytes) -> dict:
    """Parse the container header + section table. Returns {} if not this format."""
    if not is_f0985030(data):
        return {}
    h = {
        "magic": MAGIC,
        "file_size": _BE(data, 0x04),
        "section_count": _BE(data, 0x10),
        "entry_count": _BE(data, 0x18),
        "desc_off": _BE(data, 0x20),
        "index_off": _BE(data, 0x24),
        "sections": [],
    }
    p = 0x28
    for _ in range(min(h["section_count"], 8)):
        if p + 0x20 > len(data):
            break
        t = _BE(data, p)
        if t == 0:
            break
        h["sections"].append({
            "type": t,
            "alloc": _BE(data, p + 0x0C),      # VRAM/DRAM allocation size (runtime)
            "dataoff": _BE(data, p + 0x14),    # PAST-EOF -> repacked, not in file
            "compsize": _BE(data, p + 0x18),
        })
        p += 0x20
    # is the pixel data actually in the file? (dataoff within bounds for any section)
    h["pixels_in_file"] = any(s["dataoff"] + s["compsize"] <= h["file_size"]
                              for s in h["sections"] if s["type"] == 0x411536D5)
    return h


def vram_index(data: bytes):
    """Best-effort (block_size, vram_addr) pairs from the index array — the guest-physical VRAM
    addresses a live capture reads each texture's pixels from (host = phys_base + vram_addr).
    Partially reversed; returns [] if the array can't be walked within the file."""
    h = parse_header(data)
    if not h:
        return []
    off = h["index_off"]
    out = []
    p = off
    while p + 8 <= len(data) and len(out) < h["entry_count"]:
        block_size = _BE(data, p)
        vram_addr = _BE(data, p + 4)
        if vram_addr == 0:
            break
        out.append((block_size, vram_addr))
        p += 8
    return out


def describe(name: str, data: bytes) -> str:
    """One-line human summary for logging / the launcher status line."""
    h = parse_header(data)
    if not h:
        return f"{name}: not an F0985030 container"
    return (f"{name}: F0985030 atlas, {h['entry_count']} textures, "
            f"pixels {'in-file' if h['pixels_in_file'] else 'REPACKED in VRAM (needs live capture)'}")
