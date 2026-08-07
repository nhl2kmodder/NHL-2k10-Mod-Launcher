"""logos_atlas.py — author the `0xF0985030` logo-atlas INDEX files.

`logos_large.iff` / `logos_medium.iff` / `logos_small.iff` are the index files for the three
front-end branding bundles (`disc_b6b4e9c8` / `disc_a300d85f` / `disc_a38365c6`, see
`archive_textures.BUNDLE_PACKS`). Each index NAMES every tile in its bundle, and the game looks a
team's logo up **by name hash** — this module is what lets a team that did not ship with the game
(Seattle, Vegas, …) get a real logo instead of nothing.

## Why this file exists (root cause, 2026-08-02)

`Function_840C8D60` builds the front-end team logo like this:

    key  = Team_GetAssetKey(team)          // "SEA"  (Roster.ROS team record +0xB0)
    Str_ToLowerInplace(key)                // "sea"
    hash = Str_Hash(key, 0x7fffffff)       // = crc32 over the LOWERCASE ASCII key
    Iff_RegisterSectionHandler(0x850D220C, ..., hash, 0x5C369069, ...)

`0x850D220C` is the logos container. If no record in the atlas carries that hash the handler comes
back **0**, the null propagates, and you get **no logo followed by a hang** on entering gameplay.
`logo_<code>.iff` is a red herring — it is referenced by `Tex_PrecacheTeamLogos` and nothing draws
from it, which is why jerseys (fetched by name from the archive) work for a novel key while the
logo does not.

## FORMAT (fully reversed; `build()` reproduces all five shipping files byte-exactly)

All big-endian. `n` = entry count. Layout is five fixed regions, no padding:

    0x00  header, 0x68 bytes
          0x00 u32  magic 0xF0985030
          0x04 u32  file_size
          0x08 u32  file_size (duplicate)
          0x10 u32  section_count = 2
          0x14 u32  21          (constant across all five files)
          0x18 u32  n           <- entry count
          0x1C u32  77          (constant)
          0x20 u32  24*n + 73   <- derived; see below
          0x24 u32  44*n + 69   <- derived
          0x28  section table, 2 x 0x20: [type, type, align, alloc, 0, dataoff, compsize, 0]
                types 0xBB05A9C1 (DRAM) + 0x411536D5 (VRAM). `dataoff` is a VIRTUAL continuation
                past EOF: DRAM at file_size, VRAM at file_size+0xE0 (tile 0's pair, a template).
    0x68  arrayA    n x u32,  value = (4n+1) + 0x10*i  (DRAM offsets, stride 0x10, RANK order)
          records   n x 20  (see below), sorted STRICTLY ASCENDING by hash
          arrayB    n x u32,  value = (4n+1) + 0x0C*i  (DRAM offsets, stride 0x0C, RANK order)
          readtable n x 16, [hdrOff, hdrSize, texOff, texSize] into the companion .cdf
          tail      UTF-16BE "<name>.cdf" + u16 NUL

    record:  +0x00 u32 hash  = crc32(lowercase ascii name)
             +0x04 u32 tag   = 0x5C369069   (the section type Function_840C8D60 asks for)
             +0x08 u32 flags = 2
             +0x0C u32 rank * dram_stride  (0xE0, the descriptor size)
             +0x10 u32 rank * vram_stride  (the per-tile VRAM allocation: 0x60000 large,
                                            0x20000 medium, 0x10000 small)

i.e. `+0x0C`/`+0x10` are simply the tile's DRAM and VRAM byte offsets inside the loader's
rank-ordered image. `iconnav.iff` mixes tile sizes so its VRAM stride is not uniform; `parse`
detects that and `build` then writes its `+0x0C`/`+0x10` back verbatim.

`0x20`/`0x24` are pure functions of `n` — solved from three independent files and exact on all of
them (logos_* n=120 -> 2953/5349, portrait n=1478 -> 35545/65101, iconnav n=58 -> 1465/2621).
They are the record-table and read-table END offsets in the runtime DRAM image, whose header is 73
bytes where the file's is 104: `0x20 = 104 + 24n - 31`, `0x24 = 104 + 44n - 35`.

## The two orderings — do not confuse them

* **tile index** = record index = position in the .cdf = **hash ascending**. This is the one that
  matters: record `j` describes bundle tile `j`, and `readtable[j]` locates it. Verified: every
  tile's 0xE0 descriptor begins with its own name hash, and those hashes match `records[j].hash`
  for all 120 entries in all three atlases.
* **rank** (`record+0x0C / 0xE0`) is a *separate* permutation — the entries in **alphabetical name
  order** — and it is what `arrayA`/`arrayB` are indexed by. Inserting a name therefore shifts the
  ranks of every name that sorts after it, but leaves tile indices governed purely by hash.

Because rank is alphabetical, a new name's rank can be derived without knowing the spelling of the
unnamed entries: take the highest rank among KNOWN names that sort before it, add one, and shift.

## Editing the art

Adding an entry here only NAMES a tile. The pixels live in the bundle and are replaced through the
existing `archive_textures.replace_bundles()` path (which rebuilds mips, re-encodes at the blob's
native window/codec, re-syncs this read table and cascades large -> medium -> small). A team added
by `add_team()` starts out cloned from a donor team so it is never blank, and is then editable in
the launcher exactly like any of the 30 shipping teams.
"""
from __future__ import annotations
import struct, zlib

MAGIC = 0xF0985030
TAG = 0x5C369069            # section type Function_840C8D60 requests
HDR = 0x68                  # header size
REC = 20                    # record stride
CONST_14, CONST_1C = 21, 77
FLAGS = 2

# Names recovered by brute-forcing the 120 hashes (all 30 teams + IIHF nations + marks).
# Used only to place a new entry at the right ALPHABETICAL rank; unnamed entries keep their
# relative rank order, so gaps in this table are harmless.
KNOWN_NAMES = [
    "2k", "ana", "atl", "bos", "buf", "can", "car", "cbj", "cgy", "chi", "col", "cze", "dal",
    "det", "edm", "fin", "fla", "fra", "ger", "kaz", "lak", "lat", "min", "mtl", "nhl", "njd",
    "nsh", "nyi", "nyr", "ott", "phi", "pho", "pit", "rus", "sjs", "stl", "sui", "swe", "tbl",
    "tor", "ukr", "usa", "van", "wsh",
]


def name_hash(name: str) -> int:
    """Str_Hash as the game computes it for a logo section: crc32 over the LOWERCASE ascii key."""
    return zlib.crc32(name.lower().encode("ascii"))


_BRUTE = None


def _brute_map() -> dict:
    """{hash -> name} for every plausible asset key, so a tile names ITSELF from its hash.

    Team asset keys are short ([a-z0-9], 2-3 chars: `ana`, `pho`, `2k`), and crc32 is cheap, so we
    just invert the whole space once. This is why a team added after ship (`sea`, `vgk`) shows up
    with its real name in the launcher with no table to maintain — there IS no name in the file,
    only the hash. Ambiguous hashes are dropped rather than guessed."""
    global _BRUTE
    if _BRUTE is None:
        import itertools
        alpha = "abcdefghijklmnopqrstuvwxyz0123456789"
        seen, out = set(), {}
        for ln in (2, 3):
            for t in itertools.product(alpha, repeat=ln):
                s = "".join(t)
                h = zlib.crc32(s.encode("ascii"))
                if h in seen:
                    out.pop(h, None)          # collision -> no name is better than a wrong one
                else:
                    seen.add(h); out[h] = s
        _BRUTE = out
    return _BRUTE


def tile_labels(index_iff: str = "logos_large.iff", game_dir=None, extra=()) -> dict:
    """{tile index -> name} for an atlas as it stands on disk. Tiles whose key isn't a short
    alphanumeric string (retro/marks with longer names) are simply absent."""
    from . import archive_textures as AT
    loc = AT.resolve(index_iff, game_dir)
    if not loc:
        return {}
    with open(AT._dir(game_dir) / loc[0], "rb") as f:
        f.seek(loc[1]); data = f.read(loc[2])
    names = dict(_brute_map())
    names.update({name_hash(k): k.lower() for k in list(KNOWN_NAMES) + [e.lower() for e in extra]})
    return {i: names[r["hash"]] for i, r in enumerate(parse(data)["records"]) if r["hash"] in names}


def parse(data: bytes) -> dict:
    """Full structural parse. Raises ValueError if `data` is not a well-formed atlas index."""
    if len(data) < HDR or struct.unpack_from(">I", data, 0)[0] != MAGIC:
        raise ValueError("not an F0985030 container")
    g = lambda o: struct.unpack_from(">I", data, o)[0]
    n = g(0x18)
    need = HDR + 44 * n
    if n <= 0 or need > len(data):
        raise ValueError(f"bad entry count {n} for {len(data)} bytes")
    sections = [struct.unpack_from(">8I", data, 0x28 + s * 0x20) for s in range(2)]
    dram_stride = sections[0][3]                        # DRAM alloc per tile (0xE0)
    p = HDR + 4 * n                                     # arrayA
    records = []
    for i in range(n):
        h, tag, flags, f0, f1 = struct.unpack_from(">5I", data, p + i * REC)
        if tag != TAG:
            raise ValueError(f"record {i}: tag {tag:#x} != {TAG:#x}")
        records.append({"hash": h, "flags": flags, "rank": f0 // dram_stride,
                        "f0": f0, "f1": f1})
    # Uniform VRAM stride? (true for logos_*/portrait, false for iconnav's mixed tile sizes.)
    vram_stride = sections[1][3]
    uniform = all(r["f0"] == r["rank"] * dram_stride and r["f1"] == r["rank"] * vram_stride
                  for r in records)
    p += REC * n + 4 * n                                # skip records + arrayB
    read = [struct.unpack_from(">4I", data, p + i * 16) for i in range(n)]
    tail = data[HDR + 44 * n:]
    cdf = tail.decode("utf-16-be").rstrip("\0")
    return {"n": n, "records": records, "readtable": read, "cdf": cdf, "sections": sections,
            "dram_stride": dram_stride, "vram_stride": vram_stride, "uniform": uniform}


def build(info: dict) -> bytes:
    """Serialise a parsed/edited atlas back to bytes. Inverse of `parse`."""
    recs, read, cdf = info["records"], info["readtable"], info["cdf"]
    n = len(recs)
    if len(read) != n:
        raise ValueError(f"{n} records but {len(read)} read-table entries")
    if any(recs[i]["hash"] >= recs[i + 1]["hash"] for i in range(n - 1)):
        raise ValueError("records must be strictly ascending by hash")
    if sorted(r["rank"] for r in recs) != list(range(n)):
        raise ValueError("ranks must be a permutation of 0..n-1")
    tail = (cdf + "\0").encode("utf-16-be")
    size = HDR + 44 * n + len(tail)

    out = bytearray(size)
    struct.pack_into(">3I", out, 0, MAGIC, size, size)
    struct.pack_into(">I", out, 0x10, 2)
    struct.pack_into(">I", out, 0x14, CONST_14)
    struct.pack_into(">I", out, 0x18, n)
    struct.pack_into(">I", out, 0x1C, CONST_1C)
    struct.pack_into(">I", out, 0x20, 24 * n + 73)
    struct.pack_into(">I", out, 0x24, 44 * n + 69)
    # section table: tile 0's pair, as a template, at the virtual past-EOF continuation
    dram, vram = info["sections"]
    struct.pack_into(">8I", out, 0x28, *dram[:5], size, dram[6], dram[7])
    struct.pack_into(">8I", out, 0x48, *vram[:5], size + dram[6], vram[6], vram[7])

    base = 4 * n + 1                                               # arrayA/arrayB first value
    ds, vs = info["dram_stride"], info["vram_stride"]
    p = HDR
    for i in range(n):                                             # arrayA
        struct.pack_into(">I", out, p + i * 4, base + 0x10 * i)
    p += 4 * n
    for i, r in enumerate(recs):                                   # records
        if info.get("uniform", True):
            f0, f1 = r["rank"] * ds, r["rank"] * vs
        else:                                                      # mixed tile sizes (iconnav)
            f0, f1 = r["f0"], r["f1"]
        struct.pack_into(">5I", out, p + i * REC, r["hash"], TAG, r.get("flags", FLAGS), f0, f1)
    p += REC * n
    for i in range(n):                                             # arrayB
        struct.pack_into(">I", out, p + i * 4, base + 0x0C * i)
    p += 4 * n
    for i, e in enumerate(read):                                   # read table
        struct.pack_into(">4I", out, p + i * 16, *e)
    p += 16 * n
    out[p:p + len(tail)] = tail
    return bytes(out)


def rank_for(name: str, records: list) -> int:
    """Alphabetical rank a new `name` should take. Ranks order the entries by name, so we anchor
    on the KNOWN names either side and slot in between; every rank >= the result then shifts up."""
    by_rank = {r["rank"]: r["hash"] for r in records}
    known = {name_hash(k): k for k in KNOWN_NAMES}
    lo = -1
    for rank in sorted(by_rank):
        nm = known.get(by_rank[rank])
        if nm is not None and nm < name.lower():
            lo = rank
    return lo + 1


def insert_entry(info: dict, name: str, tile_read_entry, rank: int | None = None) -> int:
    """Insert `name` into a parsed atlas. Returns the TILE INDEX it landed at (= its position in
    the .cdf, which is hash order). `tile_read_entry` is the new tile's
    (hdrOff, hdrSize, texOff, texSize); the caller must have spliced the pair into the bundle at
    that same tile index. Existing read-table entries are the caller's responsibility to refresh
    (they all shift), so in practice callers rebuild the whole table from the new pack."""
    h = name_hash(name)
    recs = info["records"]
    if any(r["hash"] == h for r in recs):
        raise ValueError(f"{name!r} already present in this atlas")
    tile = sum(1 for r in recs if r["hash"] < h)
    if rank is None:
        rank = rank_for(name, recs)
    for r in recs:                                     # everything after it shifts up one
        if r["rank"] >= rank:
            r["rank"] += 1
    recs.insert(tile, {"hash": h, "flags": FLAGS, "rank": rank})
    info["readtable"].insert(tile, tuple(tile_read_entry))
    info["n"] = len(recs)
    return tile


def team_tiles(game_dir=None, extra=()) -> dict:
    """{lowercase name -> tile index} for the large atlas as it currently stands on disk.
    The tile index is what `archive_textures.replace_bundles()` takes, so this is the bridge
    between a team's asset key and the launcher's logo editor.

    Hashes are one-way, but `tile_labels` inverts the short-key space, so post-ship asset keys
    (Seattle, Vegas, …) resolve on their own — `extra` is only needed for unusual keys."""
    return {name: tile for tile, name in tile_labels("logos_large.iff", game_dir, extra).items()}


def add_team(code: str, donor: str, game_dir, log=print) -> str:
    """Give team asset key `code` (e.g. "SEA") a real front-end logo, cloned from `donor`
    (e.g. "ANA") so it is never blank, and thereafter editable in the launcher exactly like any
    shipping team's tile.

    For each of the three branding bundles this splices a new [0xE0 descriptor + texture] pair
    into the pack at the tile index `code`'s hash sorts to, then rebuilds that bundle's atlas
    index with the new record and a freshly walked read table. Both the pack and the index are
    relocated through the normal TOC path, so nothing is written outside the game archives and a
    one-time .orig backup is taken first.
    """
    from . import archive_textures as AT
    import encode_e4837_lazy as EE

    code, donor = code.lower(), donor.lower()
    game_dir = AT._dir(game_dir)
    out = []
    for pack, (label, w, mip0, dec_sz, index_iff) in AT.BUNDLE_PACKS.items():
        ploc = AT.resolve(pack, game_dir)
        iloc = AT.resolve(index_iff, game_dir)
        if not ploc or not iloc:
            raise ValueError(f"{pack}/{index_iff}: not found in the game TOC")
        with open(game_dir / ploc[0], "rb") as f:
            f.seek(ploc[1]); res = f.read(ploc[2])
        with open(game_dir / iloc[0], "rb") as f:
            f.seek(iloc[1]); idat = f.read(iloc[2])

        info = parse(idat)
        pairs = _pairs(AT, res, len(res), dec_sz)
        if len(pairs) != info["n"]:
            raise ValueError(f"{pack}: {len(pairs)} tiles but {index_iff} says {info['n']}")
        h = name_hash(code)
        if any(r["hash"] == h for r in info["records"]):
            out.append(f"{label}: {code!r} already present — skipped")
            continue
        dh = name_hash(donor)
        src = next((i for i, r in enumerate(info["records"]) if r["hash"] == dh), None)
        if src is None:
            raise ValueError(f"{index_iff}: donor {donor!r} not found")

        # new descriptor = donor's, renamed (the name hash is the first u32 of every descriptor)
        hdr, tex = pairs[src]
        dec = bytearray(hdr["dec"])
        struct.pack_into(">I", dec, 0, h)
        nhdr = EE.encode_payload(bytes(dec), wparam=hdr["wp"], codec=hdr["codec"])
        err = AT._verify_blob(nhdr, bytes(dec))
        if err:
            raise ValueError(f"{pack}: descriptor re-encode failed ({err}) — nothing written")
        ntex = res[tex["off"]:tex["off"] + tex["tot"]]          # donor pixels, verbatim

        tile = sum(1 for r in info["records"] if r["hash"] < h)
        at = pairs[tile][0]["off"] if tile < len(pairs) else len(res)
        new_res = res[:at] + nhdr + ntex + res[at:]

        # rebuild the read table from the spliced pack, then insert the record
        npairs = _pairs(AT, new_res, len(new_res), dec_sz)
        if len(npairs) != info["n"] + 1:
            raise ValueError(f"{pack}: splice produced {len(npairs)} tiles, expected {info['n']+1}")
        got = insert_entry(info, code, (0, 0, 0, 0))
        if got != tile:
            raise ValueError(f"{pack}: tile index disagreement ({got} vs {tile})")
        info["readtable"] = [(a["off"], a["tot"], b["off"], b["tot"]) for a, b in npairs]
        nidx = build(info)

        AT._backup_once(game_dir / ploc[0], log)
        AT._backup_once(game_dir / iloc[0], log)
        log(f"  {label}: {code} -> tile {tile} (cloned from {donor} tile {src}), "
            f"{info['n']-1} -> {info['n']} entries")
        out.append(AT._relocate(pack, new_res, ploc[3], game_dir, w, w, "DXT4_5", log))
        out.append(AT._relocate(index_iff, nidx, iloc[3], game_dir, w, w, "INDEX", log))
        AT._BUNDLE_CACHE.pop((pack, str(game_dir)), None)
    return "\n".join(out)


def _pairs(AT, data: bytes, size: int, dec_sz: int):
    """[(hdr, tex)] blob pairs of a branding bundle — same rule replace_bundles uses."""
    blobs = AT._walk_blobs(data, size)
    pairs, i = [], 0
    while i + 1 < len(blobs):
        a, b = blobs[i], blobs[i + 1]
        if (a["dec"] is not None and len(a["dec"]) == 0xE0
                and b["dec"] is not None and len(b["dec"]) == dec_sz):
            pairs.append((a, b)); i += 2
        else:
            i += 1
    return pairs


def describe(data: bytes) -> str:
    info = parse(data)
    known = {name_hash(k): k for k in KNOWN_NAMES}
    named = sum(1 for r in info["records"] if r["hash"] in known)
    return (f"F0985030 atlas -> {info['cdf']}: {info['n']} entries, {named} named, "
            f"{info['n'] - named} unidentified")
