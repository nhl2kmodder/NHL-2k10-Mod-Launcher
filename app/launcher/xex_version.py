"""xex_version.py — tell stock v1.0 from Title Update #1 (v1.1), and translate guest VAs
between them.

## Why this exists

Every XEX patch in this launcher is written against the *stock v1.0* disc executable: a VA plus
the stock word expected there. TU #1 is not an overlay — it is a FULL RELINK (255 changed
functions, 72 added, 64 removed), so essentially every code VA moved. Applying a v1.0 patch
address to a v1.1 image would either land in the wrong instruction or, thanks to `expect=`,
refuse outright.

Rather than fork 20 modules, this module makes the *v1.0 VA the canonical identifier* and
translates at the one chokepoint every patcher already goes through, `xex_patch.va_to_offset`.
A module keeps saying "patch 0x84095558" and it lands correctly on either image.

## How the map was built (data/xex_tu11_map.json)

Both executables are flattened (XexTool -c u -e u) and, for each VA a launcher module actually
touches, the stock bytes are located in v1.1 by:

  1. exact context signature — a symmetric window around the site must occur exactly once;
  2. masked signature — same, but branch displacements and the hi/lo halves of address literals
     (b/bc/bl, lis/addi/ori/oris) are wildcarded, since the relink legitimately rewrites those;
  3. enclosing-function match — walk back to the `mflr r12` prologue, relocate the function by
     masked signature, then apply the identical in-function offset. This is what resolves the ten
     cloned `li r5,7` helmet-precache sites, which are byte-identical to each other;
  4. anchor delta — for a leaf function with no prologue, take the delta of an already-resolved
     neighbour and verify the instruction word matches in both images.

Injected-code stubs are a separate case: they live in .text tail padding, and TU 1.1's code grew
into the v1.0 stub addresses. They are re-homed into 1.1's own tail (code ends 0x84265683,
zeros run to 0x84267FF0), and the destination is re-checked for zero before every write.

## What does NOT port

`gameplay_tuning` — its 18 tuners are fields of a tuning struct that the TU re-laid out
(inserts and deletes, not just value changes), so no VA translation is meaningful. That tab is
disabled on v1.1, which costs nothing: its entire purpose is to write 2K's v1.1 values into a
v1.0 exe, and a real v1.1 exe already has them.
"""
from __future__ import annotations

import functools
import json
import os
import struct
from pathlib import Path

V10 = "1.0"
V11 = "1.1"

# blk16's data_size is the cheapest reliable discriminator: it is the only block whose size the
# TU changed (884,736 -> 917,504), it lives in the already-parsed header, and patching bytes
# never alters it.
_BLK16_SIZE = {884736: V10, 917504: V11}
_BLK16_INDEX = 16

# Corroborating witness, read from the image itself, in case a future rebuild shifts the blocks.
_WITNESS_VA = 0x83BE6018
_WITNESS = {0x81630004: V10, 0x39200001: V11}

_MAP_FILE = Path(__file__).resolve().parent / "data" / "xex_tu11_map.json"


@functools.lru_cache(maxsize=1)
def _load_map() -> dict:
    """{v1.0 VA -> v1.1 VA}. Missing/!unreadable file degrades to 'no translation' rather than
    breaking the launcher — detect() still reports 1.1 so callers can refuse cleanly."""
    try:
        raw = json.loads(_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {int(k, 16): int(v, 16) for k, v in (raw.get("map") or raw).items()
            if k.startswith("0x")}


@functools.lru_cache(maxsize=16)
def _detect_cached(path: str, _size: int, _mtime_ns: int):
    try:
        from . import xex_patch as XP
    except ImportError:
        import xex_patch as XP
    try:
        _ds, _ib, blocks = XP.parse_basic_blocks(path)
    except Exception:
        return None
    ver = _BLK16_SIZE.get(blocks[_BLK16_INDEX][0]) if len(blocks) > _BLK16_INDEX else None
    if ver is None:                                   # fall back to the in-image witness word
        w = XP.read_u32(path, _WITNESS_VA, _translate=False)
        ver = _WITNESS.get(w)
    return ver


def detect(xex_path):
    """'1.0', '1.1', or None when the image is neither (or unreadable)."""
    try:
        st = os.stat(xex_path)
    except OSError:
        return None
    return _detect_cached(str(xex_path), st.st_size, st.st_mtime_ns)


def is_tu11(xex_path) -> bool:
    return detect(xex_path) == V11


def label(xex_path) -> str:
    v = detect(xex_path)
    return {V10: "stock v1.0", V11: "Title Update #1 (v1.1)"}.get(v, "unrecognised build")


def translate(xex_path, va: int) -> int:
    """A canonical (v1.0) guest VA -> the VA it lives at in `xex_path`. Identity on v1.0 and on
    any VA the TU did not move."""
    if not is_tu11(xex_path):
        return va
    return _load_map().get(va, va)


def translated_pairs(xex_path):
    """[(v1.0, v1.1)] for every VA this image translates — for logs and the About box."""
    if not is_tu11(xex_path):
        return []
    return [(k, v) for k, v in sorted(_load_map().items()) if k != v]


# ── branch fixups ───────────────────────────────────────────────────────────────
#
# A word a module writes may itself be a relative branch built from v1.0 addresses
# (`bl <site> -> <stub>`). Both endpoints move independently, so the displacement has to be
# recomputed against the translated site. Same for an `expect=` word: the stock branch at a
# moved site encodes a different displacement in v1.1, and a naive comparison would refuse a
# perfectly correct patch.

def _sign26(d: int) -> int:
    return d - 0x04000000 if d & 0x02000000 else d


def fix_branch_word(xex_path, word: int, site_v10: int, site_v11: int) -> int:
    """Re-aim one relative b/bl word so it points at the translation of its original target.
    Returns `word` unchanged for anything that is not a relative branch."""
    if (word >> 26) != 18 or (word & 2):          # not `b`/`bl`, or absolute (AA=1)
        return word
    tgt10 = (site_v10 + _sign26(word & 0x03FFFFFC)) & 0xFFFFFFFF
    tgt11 = _load_map().get(tgt10)
    if tgt11 is None:
        # Target not explicitly mapped. That is the ordinary intra-function branch, where site
        # and target moved together and the displacement is already right — correcting it with a
        # guessed address would break a word that was fine. Leave it alone.
        return word
    return (word & 0xFC000003) | ((tgt11 - site_v11) & 0x03FFFFFC)


@functools.lru_cache(maxsize=1)
def _reverse_map() -> dict:
    """{v1.1 VA -> v1.0 VA}. Any VA two v1.0 addresses collide on is dropped rather than
    guessed — an ambiguous reverse lookup must not silently pick one."""
    fwd = _load_map()
    rev, dupes = {}, set()
    for k, v in fwd.items():
        if v in rev and rev[v] != k:
            dupes.add(v)
        rev[v] = k
    for d in dupes:
        rev.pop(d, None)
    return rev


def untranslate(xex_path, va: int) -> int:
    """The inverse of translate(): a VA in this image -> its canonical v1.0 address."""
    if not is_tu11(xex_path):
        return va
    return _reverse_map().get(va, va)


def fix_branch_bytes(xex_path, data, site_v10: int, site_v11: int):
    """Re-aim every relative branch in `data`, treating it as words laid down starting at
    site_v10 (canonically) / site_v11 (actually). Handles injected stubs, not just single
    instructions — a stub that branches back to its caller needs the same correction."""
    if data is None or site_v10 == site_v11 or len(data) < 4 or len(data) % 4:
        return data
    out = bytearray(data)
    for i in range(0, len(out), 4):
        w = struct.unpack_from(">I", out, i)[0]
        struct.pack_into(">I", out, i,
                         fix_branch_word(xex_path, w, site_v10 + i, site_v11 + i))
    return bytes(out)


def unfix_branch_bytes(xex_path, data, site_v10: int, site_v11: int):
    """The read-side inverse: express branches found at the real site in canonical v1.0 terms,
    so a module comparing against a hardcoded stock v1.0 word still recognises them."""
    if data is None or site_v10 == site_v11 or len(data) < 4 or len(data) % 4:
        return data
    out = bytearray(data)
    for i in range(0, len(out), 4):
        w = struct.unpack_from(">I", out, i)[0]
        if (w >> 26) != 18 or (w & 2):
            continue
        s11, s10 = site_v11 + i, site_v10 + i
        tgt10 = _reverse_map().get((s11 + _sign26(w & 0x03FFFFFC)) & 0xFFFFFFFF)
        if tgt10 is None:                       # see fix_branch_word: not ours to touch
            continue
        struct.pack_into(">I", out, i,
                         (w & 0xFC000003) | ((tgt10 - s10) & 0x03FFFFFC))
    return bytes(out)
