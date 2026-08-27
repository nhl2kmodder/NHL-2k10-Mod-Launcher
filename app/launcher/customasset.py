"""Add BRAND-NEW assets to the NHL 2K10 archives (not just replace existing ones).

A new asset = its bytes appended to the LAST archive named by 0A's header (spilling to a fresh
archive at the 2 GB ceiling, exactly like a relocating replace) + one new 16-byte TOC entry
inserted IN HASH-SORTED ORDER (the engine binary-searches the table — an end-appended entry is
never found; verified in-game) + the entry count at 0x10 bumped. The entry costs 16 bytes of the
zero slack between the table end and the first blob (~100 free on a stock 0A), so 0A's length —
and therefore every asset offset inside it — never changes. `remove_custom_asset` is the exact
inverse, so an add is fully revertible.

Everything is header-driven (archive count @0x08, entry base = 0x18 + narc*16): this module makes
NO assumptions about how many archives exist. (Its previous incarnation hardcoded 4 archives +
grow-1B, which silently corrupts a game dir that has spilled to 1C — that world is gone.)

The mask-specific half: a goalie mask .iff carries crc32 of its OWN section name
("helmet_gSS_pattern_PP_c", plain crc32, no case fold) in TWO places — the IFF header (@+0x64 on
shipped masks) and dword 0 of the small decompressed DRAM descriptor blob. The engine registers
the mask texture under that baked-in hash and the renderer looks it up by the name it builds from
the roster fields; a clone that keeps the template's hash registers under the WRONG name and
draws blank (the root cause of every historical "added mask is invisible" failure).
`build_rebranded_clone` patches both copies.

Use `add_custom_mask()` end-to-end: clone a template into an UNUSED helmet name, rebrand the
hash, register it, then push the user's art through the normal repaint path. Slots g07-g16 render
as the STANDARD (g01/g04) shape once the mask_shells XEX patch is in (they are invisible without
it — see mask_shells.py).
"""
import struct, zlib
from pathlib import Path

try:
    from . import archive_textures as AT
except ImportError:
    import archive_textures as AT

ALIGN = 0x800

# filename shell -> shape family (mesh 0x27/0x28/0x29 via Goalie_GetMaskMeshId; shells >= 7 all
# fall back to 0x27 = the standard g01/g04 shape, which is what makes them usable as extra slots)
# g06 counts as STANDARD since the g06->standard XEX patch (mask_shells.py): its patterns 04..31
# are the shipping "extra standard slots" lane, so new g06 clones must use the 512x512 standard
# template. (Shells 7-16 kept for legacy configs; that lane never rendered in-game.)
STANDARD_SHELLS = (1, 4, 6) + tuple(range(7, 17))

# Clone template per family: a 512x512 DXT1 member, NOT pattern_00 — every family's pattern_00 is
# a 64x64 placeholder, and the repaint path keeps the asset's own stored dimensions, so cloning it
# would lock the new slot at 64x64 forever. (g01_pattern_05 is also the container whose two hash
# sites were byte-verified — see the module docstring.)
SHAPE_TEMPLATE = {s: ("helmet_g01_pattern_05.iff" if s in STANDARD_SHELLS
                      else "helmet_g02_pattern_02.iff" if s in (2, 3)
                      else "helmet_g05_pattern_02.iff") for s in range(1, 17)}


def _read_header_slack(a0: Path, slack: int):
    """Like AT._read_header but with `slack` bytes of tail — an archive-row insert (spill) and a
    TOC-entry insert EACH consume 16 zero bytes, so a worst-case add needs 32."""
    head = open(a0, "rb").read(0x18)
    narc = AT._BE(head, 0x08)
    cnt = AT._BE(head, 0x10)
    need = 0x18 + narc * 16 + cnt * 16 + slack
    buf = bytearray(open(a0, "rb").read(need))
    if len(buf) != need:
        raise RuntimeError(f"0A is shorter ({len(buf)}) than its own header claims ({need})")
    tbl = AT.arc_table(buf)
    return buf, [n for n, _ in tbl], [s for _, s in tbl], 0x18 + narc * 16, cnt


def toc_slack_entries(game_dir) -> int:
    """How many NEW assets fit before the entry table would touch the first blob."""
    a0 = Path(game_dir) / "0A"
    d = open(a0, "rb").read(0x20000)
    cnt = AT._BE(d, 0x10)
    ebase = AT.entry_base(d)
    lowest = min(AT._BE(d, ebase + i * 16 + 12) for i in range(cnt)) * ALIGN
    return max(0, (lowest - (ebase + cnt * 16)) // 16)


def add_custom_asset(game_dir, new_name: str, payload: bytes, log=print, flags_from=None) -> str:
    """Register `new_name` -> `payload` as a brand-new TOC entry. Raises if the name already
    resolves (use replace, not add) or the TOC slack is exhausted. `flags_from` names an existing
    asset whose entry flags the new entry copies (default 0, which is what shipped masks carry)."""
    game_dir = Path(game_dir)
    a0 = game_dir / "0A"
    if AT.resolve(new_name, game_dir) is not None:
        raise ValueError(f"{new_name} already exists in the TOC (use replace, not add)")
    h = zlib.crc32(new_name.upper().encode("ascii")) & 0xffffffff

    AT._backup_once(a0, log)
    buf, names, sizes, ebase, cnt = _read_header_slack(a0, 32)
    bases, acc = [], 0
    for s in sizes:
        bases.append(acc); acc += s

    flags = 0
    if flags_from:
        floc = AT.resolve(flags_from, game_dir)
        if floc:
            flags = AT._BE(buf, ebase + floc[3] * 16)

    # append to the LAST archive, spilling to a fresh one at the ceiling (same invariant as
    # _relocate_raw: only the last archive may ever change size)
    spill = names[-1]
    spill_path = game_dir / spill
    spill_size = spill_path.stat().st_size if spill_path.exists() else 0
    new_local = (spill_size + ALIGN - 1) & ~(ALIGN - 1)
    if spill_size and new_local + len(payload) > AT.SPILL_CEILING:
        log(f"  {spill} would reach {new_local + len(payload):,} B, past the "
            f"{AT.SPILL_CEILING:,} B ceiling — spilling to a new archive")
        spill = AT._add_archive(game_dir, buf, names, log)     # consumes 16 B of buf's slack
        names.append(spill); sizes.append(0); bases.append(acc)
        spill_path = game_dir / spill
        spill_size = 0; new_local = 0
        ebase += 16                                            # the entry table just moved
    eend = ebase + cnt * 16
    if any(buf[eend:eend + 16]):
        raise RuntimeError(f"0A's TOC slack is exhausted at {cnt} entries — no room for a new "
                           f"asset (remove added assets or compact first)")

    AT._backup_if_shipped(spill_path, log)
    with open(spill_path, "r+b" if spill_path.exists() else "wb") as f:
        if new_local > spill_size:
            f.seek(spill_size); f.write(b"\x00" * (new_local - spill_size))
        f.seek(new_local); f.write(payload)
    total = new_local + len(payload)
    si = len(names) - 1
    struct.pack_into(">I", buf, 0x18 + si * 16, (total + ALIGN - 1) // ALIGN)  # last archive size

    # hash-sorted insert (the engine binary-searches on the name hash)
    lo, hi = 0, cnt
    while lo < hi:
        mid = (lo + hi) // 2
        if AT._BE(buf, ebase + mid * 16 + 8) < h:
            lo = mid + 1
        else:
            hi = mid
    pos = lo
    row = struct.pack(">IIII", flags, len(payload), h, (bases[si] + new_local) // ALIGN)
    buf[ebase + pos * 16:ebase + pos * 16] = row
    del buf[-16:]                                              # the insert consumed 16 B of slack
    struct.pack_into(">I", buf, 0x10, cnt + 1)
    AT._write_header(a0, buf)
    _tree_sync(game_dir)                 # a brand-new entry: the tree picks it up by TOC diff
    return (f"ADDED {new_name} -> {spill}:0x{new_local:X} ({len(payload)} bytes; TOC insert at "
            f"#{pos}/{cnt}, hash {h:08X}); count {cnt} -> {cnt + 1}")


def remove_custom_asset(game_dir, name: str, log=print) -> str:
    """The exact inverse of add_custom_asset: delete `name`'s TOC entry and give its 16 bytes back
    to the slack. Refuses to touch anything the shipped game had (present in the pristine 0A.orig
    TOC). The appended bytes stay as dead space in their archive — harmless, and compaction
    reclaims them — so removal never moves another asset."""
    game_dir = Path(game_dir)
    a0 = game_dir / "0A"
    if AT.resolve(name, game_dir, clean=True) is not None:
        raise ValueError(f"{name} is a SHIPPED asset — remove refuses; use restore/replace instead")
    loc = AT.resolve(name, game_dir)
    if loc is None:
        return f"{name}: not in the TOC — nothing to remove"
    idx = loc[3]
    buf, names, sizes, ebase, cnt = _read_header_slack(a0, 0)
    h = zlib.crc32(name.upper().encode("ascii")) & 0xffffffff
    eo = ebase + idx * 16
    if AT._BE(buf, eo + 8) != h:
        raise RuntimeError(f"{name}: TOC entry #{idx} hash mismatch — refusing to remove blind")
    del buf[eo:eo + 16]
    buf += b"\x00" * 16                                        # the freed row becomes slack again
    struct.pack_into(">I", buf, 0x10, cnt - 1)
    AT._write_header(a0, buf)
    return (f"REMOVED {name} (TOC #{idx}); count {cnt} -> {cnt - 1}. Its {loc[2]} bytes in "
            f"{loc[0]} are dead space until the next compaction.")


# ── mask cloning (the name-hash rebrand) ─────────────────────────────────────

def _section_name(iff_name: str) -> str:
    """helmet_g07_pattern_00.iff -> helmet_g07_pattern_00_c (the baked-in section name; hashed
    with plain crc32, no case fold — unlike TOC name hashes, which upper-case first)."""
    stem = iff_name[:-4] if iff_name.lower().endswith(".iff") else iff_name
    return stem + "_c"


def rebrand_hash(res: bytes, old_iff: str, new_iff: str, log=print) -> bytes:
    """Patch every copy of `old_iff`'s section name-hash in a cloned container to `new_iff`'s.

    The hash lives in the (uncompressed) IFF header and again at dword 0 of the decompressed DRAM
    descriptor blob; the engine registers the mask texture under the descriptor's copy, so a clone
    that skips this renders BLANK. The descriptor is re-encoded, and if its compressed size
    changes, the section table's blob offsets/sizes and the total size are refit."""
    old_h = zlib.crc32(_section_name(old_iff).encode("ascii")) & 0xffffffff
    new_h = zlib.crc32(_section_name(new_iff).encode("ascii")) & 0xffffffff
    old_be, new_be = struct.pack(">I", old_h), struct.pack(">I", new_h)
    res = bytearray(res)
    hdr = AT._BE(res, 4)

    hits = [o for o in range(0, hdr - 3, 4) if bytes(res[o:o + 4]) == old_be]
    if len(hits) != 1:
        raise ValueError(f"{old_iff}: expected exactly 1 header copy of hash {old_h:08X}, "
                         f"found {len(hits)} — template layout not understood, refusing")
    struct.pack_into(">I", res, hits[0], new_h)

    # descriptor blob(s): patch inside the decompressed bytes, re-encode, splice (reverse offset
    # order so earlier splices don't invalidate later blob offsets)
    blobs = AT._walk_blobs(res, len(res))
    patched_desc = 0
    for b in reversed(blobs):
        dec = b["dec"]
        if dec is None or len(dec) > 0x2000:
            continue
        docs = [o for o in range(0, len(dec) - 3, 4) if dec[o:o + 4] == old_be]
        if not docs:
            continue
        new_dec = bytearray(dec)
        for o in docs:
            new_dec[o:o + 4] = new_be
        new_blob = AT.EE.encode_payload(bytes(new_dec))
        err = AT._verify_blob(new_blob, bytes(new_dec))
        if err:
            raise ValueError(f"descriptor re-encode failed: {err}")
        off, tot = b["off"], b["tot"]
        delta = len(new_blob) - tot
        if delta == 0:
            res[off:off + tot] = new_blob
        else:
            res = bytearray(res[:off] + new_blob + res[off + tot:])
            p = 0x20
            while p + 0x20 <= hdr:
                if AT._BE(res, p) == 0:
                    break
                bo = AT._BE(res, p + 0x14)
                if bo == off:
                    struct.pack_into(">I", res, p + 0x18, len(new_blob))
                elif bo > off:
                    struct.pack_into(">I", res, p + 0x14, bo + delta)
                p += 0x20
        patched_desc += len(docs)
    if patched_desc == 0:
        raise ValueError(f"{old_iff}: no descriptor blob carries hash {old_h:08X} — "
                         f"template layout not understood, refusing")
    struct.pack_into(">I", res, 8, len(res))

    # end-to-end verify: the finished container must decode with the NEW hash everywhere
    chk = AT._walk_blobs(res, len(res))
    ok = any(b["dec"] and len(b["dec"]) <= 0x2000 and b["dec"][:4] == new_be for b in chk)
    if not ok or any(b["dec"] is None for b in chk):
        raise ValueError("rebranded container failed verification (descriptor hash / blob decode)")
    log(f"  rebranded {old_iff} -> {new_iff}: hash {old_h:08X} -> {new_h:08X} "
        f"(1 header copy + {patched_desc} descriptor copy/copies)")
    return bytes(res)


def build_rebranded_clone(template_name: str, new_name: str, game_dir, log=print) -> bytes:
    """The PRISTINE template bytes with every baked-in name-hash rebranded to `new_name` — ready
    for add_custom_asset(). Texture content still the template's; push the user's art through the
    normal repaint path afterwards."""
    loc, cl = AT.resolve_clean(template_name, game_dir)
    if not loc:
        raise ValueError(f"template {template_name} not found")
    arc, off, size, idx, f3 = loc
    with open(AT._arc_file(game_dir, arc, clean=cl), "rb") as f:
        f.seek(off); res = f.read(size)
    return rebrand_hash(res, template_name, new_name, log)


def add_custom_mask(game_dir, model: int, pattern: int, edited_image=None,
                    template=None, fmt="8888", log=print) -> str:
    """Create helmet_g{model:02d}_pattern_{pattern:02d}.iff as a NEW slot: rebranded clone of a
    same-shape template, TOC-registered, then (if `edited_image`) repainted via the normal convert
    path. model 6, patterns 04-31 = the extra STANDARD-shape slots (the mask_shells g06 XEX patch
    renders them on the g01/g04 mesh). Assign to a goalie as roster shell=model-1, pattern=pattern."""
    if not (1 <= model <= 16):
        raise ValueError(f"model g{model:02d} out of range g01-g16")
    if not (0 <= pattern <= 63):
        # patterns 32-63 need the mask_pattern6 XEX patch (6-bit lane: +0xB8 low 5
        # bits + the +0xB4 bit-26 donor); only style g01 (model 1) supports them.
        raise ValueError(f"pattern {pattern} out of range 0-63")
    if pattern > 31 and model != 1:
        raise ValueError(f"patterns 32-63 are only available on the standard style g01")
    name = f"helmet_g{model:02d}_pattern_{pattern:02d}.iff"
    template = template or SHAPE_TEMPLATE[model]
    clone = build_rebranded_clone(template, name, game_dir, log)
    st = add_custom_asset(game_dir, name, clone, log, flags_from=template)
    log("  " + st)
    if edited_image:
        # Masks always go in at 2x native (1024x1024) 8888 -- see replace_primary_hires. The helmet
        # streaming pool sizes on a runtime MAX over the whole helmet family, so this raises the
        # ceiling once rather than per-slot, and there is no forever-Loading trap.
        st2 = AT.replace_primary_hires(name, edited_image, game_dir, fmt, 2, log=log)
        log("  " + st2)
    return st


# -- extracted-tree write-through ----------------------------------------------
# archive_textures wraps its own replace_* entry points so every texture write lands in
# ROOT/NHL2k10_Extracted as well as the archives. The writes in THIS module go straight to the
# archive files, so they have to say so themselves or the tree quietly falls behind.
def _tree_sync(game_dir, names=()):
    try:
        try:
            from . import volume_store as _vs
        except ImportError:
            import volume_store as _vs
        _vs.sync_after_op(game_dir, names)
    except Exception:
        pass          # the archives are already correct; a stale tree is not a failure
