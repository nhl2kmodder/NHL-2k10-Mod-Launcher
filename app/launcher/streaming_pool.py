"""Keep every asset class's STREAMING-POOL slot big enough for the biggest asset in it.

Why this exists
---------------
There is no size constant to patch.  Each asset class has a size callback (uniform_base's is
`Tex_PrecacheTeamUniforms` @0x83FDC438) that walks the roster, builds every member of that class's
family name, stats each one through the TOC (`sub_83FDB9B8` -> Asset_LoadTyped) and keeps the
MAXIMUM.  That max becomes the per-slot capacity of the streaming pool (`Function_84017878` ->
`Function_83D33F60`; slot stride 0x488, capacity at slot+0x10).

At load time `Asset_StreamLoadWorker` @0x83D32DF8 does:

    if ((ulonglong)*(int *)(slot + 0x10) < file_size) {   // doesn't fit
        *(undefined4 *)(req + 0x20) = 1;                  // mark "finished"...
        LeaveCriticalSection(...); return;                // ...but never signal completion
    }

so an oversized asset is SILENTLY DECLINED and the game sits on "Loading..." forever.  Nothing is
corrupt — the game read the size correctly and said no.  Note the test is `capacity < size`, so a
member that exactly equals the ceiling still fits.

An ADDED team's own asset does not raise that maximum (padding Vegas's own pack never moved the
threshold), so the cure is to pad a DIFFERENT, shipped member of the same family.  Zeros appended
past the container's declared total_size are invisible to the IFF parser — only the TOC size grows,
and the TOC size is the one number the sizing pass reads.

The pad is self-describing: the container declares its real content length as a big-endian u32 at
byte 8, so `ballast = toc_size - header_size` and stripping the pad is exact.  No backup files.

`ensure_headroom()` is called automatically after every asset relocate (see archive_textures.
_relocate), which is what makes this preventative rather than a thing you discover in-game.
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

try:
    from . import archive_textures as AT
except ImportError:                                  # run as a plain module (sys.path has launcher/)
    import archive_textures as AT

# Asset keys the SHIPPED game enumerates. Relocated clubs keep their donor's key (WPG -> ATL,
# UTA -> PHO), so this list is stable; expansion clubs are deliberately absent, because their own
# assets are exactly what the sizing pass does NOT count.
STOCK_CODES = (
    "ana atl bos buf cgy car chi col cbj dal det edm fla lak min mtl nsh njd nyi nyr ott phi "
    "pho pit sjs stl tbl tor van wsh"
).split()

# Codes that exist in a modded install but are not shipped. Their members count toward the size we
# must FIT, never toward the ceiling the game computes.
ADDED_CODES = ("sea", "vgk", "uta", "wpg")

ALL_CODES = tuple(STOCK_CODES) + ADDED_CODES

# One entry per streaming asset class we know how to measure. Members are discovered rather than
# hardcoded (ice alone ships plain / _playoffs / _finals variants), but the family prefix must be
# listed here so `arena_presentation_*` is never mistaken for an `arena_*` member.
FAMILY_PREFIXES = ("uniform_base", "ice", "arena", "rink", "led", "zamboni")

_MEMBER_RE = {
    fam: re.compile(rf"^{re.escape(fam)}_({'|'.join(ALL_CODES)})(_[a-z0-9]+)?\.iff$")
    for fam in FAMILY_PREFIXES
}

_ALIGN = 1 << 16                     # 64 KiB granularity
_CUSHION = 8                         # pad to need + need/8, so ordinary re-edits don't immediately
                                     # blow the ceiling again and force another ballast relocate
_reentrant = False                   # ensure_headroom pads by relocating — don't recurse


def _target_size(need: int) -> int:
    """How big the ballast has to get to clear `need`, with a little slack for the next edit."""
    return (need + max(need // _CUSHION, _ALIGN) + _ALIGN - 1) & ~(_ALIGN - 1)


def _candidate_names():
    """Every asset name the launcher knows about (catalog + discovered)."""
    names = set()
    try:
        for row in AT.load_catalog():
            iff = row.get("iff") if isinstance(row, dict) else None
            if iff:
                names.add(iff)
    except Exception:
        pass
    # Families are dense and predictable, so also synthesise the obvious members. resolve() drops
    # whatever doesn't exist, and this covers installs whose catalog predates an added club.
    for fam in FAMILY_PREFIXES:
        for code in ALL_CODES:
            for suf in ("", "_home", "_away", "_alt", "_mii", "_playoffs", "_finals"):
                names.add(f"{fam}_{code}{suf}.iff")
    return names


def family_of(name: str):
    """The streaming family `name` belongs to, or None. Longest prefix wins so `uniform_base_*`
    is never read as some shorter family."""
    for fam in sorted(FAMILY_PREFIXES, key=len, reverse=True):
        if _MEMBER_RE[fam].match(name):
            return fam
    return None


def _read(name, game_dir):
    loc = AT.resolve(name, game_dir)
    if not loc:
        return None
    arc, off, size, idx, _f3 = loc
    with open(AT._arc_file(game_dir, arc, clean=False), "rb") as f:
        f.seek(off)
        return {"name": name, "arc": arc, "off": off, "size": size, "idx": idx,
                "data": f.read(size)}


def _content_len(data: bytes) -> int:
    """The container's own declared length — anything past it is ballast we appended."""
    if len(data) < 12:
        return len(data)
    tot = struct.unpack_from(">I", data, 8)[0]
    return tot if 0 < tot <= len(data) else len(data)


def census(fam: str, game_dir) -> list:
    """[{name, code, size, content, pad, stock}] for every resolvable member, biggest first."""
    game_dir = Path(game_dir)
    rows = []
    for name in _candidate_names():
        m = _MEMBER_RE[fam].match(name)
        if not m:
            continue
        loc = AT.resolve(name, game_dir)
        if not loc:
            continue
        code = m.group(1)
        blob = _read(name, game_dir)
        content = _content_len(blob["data"]) if blob else loc[2]
        rows.append({"name": name, "code": code, "size": loc[2], "content": content,
                     "pad": loc[2] - content, "stock": code in STOCK_CODES})
    rows.sort(key=lambda r: -r["size"])
    return rows


def ceiling(rows) -> int:
    """The capacity the game will actually compute: the max over members it counts (shipped ones)."""
    return max((r["size"] for r in rows if r["stock"]), default=0)


def needed(rows) -> int:
    """The biggest member that has to FIT — counted or not."""
    return max((r["size"] for r in rows), default=0)


def status(game_dir) -> list:
    """[{family, ceiling, needed, short, ballast}] — `short` > 0 means this family will hang."""
    out = []
    for fam in FAMILY_PREFIXES:
        rows = census(fam, game_dir)
        if not rows:
            continue
        cap, need = ceiling(rows), needed(rows)
        ball = next((r for r in rows if r["stock"]), None)
        out.append({"family": fam, "ceiling": cap, "needed": need,
                    "short": max(0, need - cap), "rows": rows,
                    "ballast": ball["name"] if ball else None})
    return out


def ensure_headroom(name_or_family: str, game_dir, log=print) -> str | None:
    """Make sure the family that `name_or_family` belongs to can load its biggest member.

    Pads the largest SHIPPED member (the ballast) with zeros until its TOC size reaches the biggest
    member in the family. No-op — and no write at all — when there's already headroom, which is the
    normal case. Returns a status line when it padded, else None.
    """
    global _reentrant
    if _reentrant:
        return None
    fam = name_or_family if name_or_family in FAMILY_PREFIXES else family_of(name_or_family)
    if not fam:
        return None
    game_dir = Path(game_dir)
    rows = census(fam, game_dir)
    if not rows:
        return None
    cap, need = ceiling(rows), needed(rows)
    if need <= cap:
        return None                                     # already fits — the overwhelmingly common path

    ball = next((r for r in rows if r["stock"]), None)
    if ball is None:
        log(f"  WARNING {fam}: nothing shipped to use as ballast — cannot raise the ceiling")
        return None
    target = _target_size(need)
    blob = _read(ball["name"], game_dir)
    if blob is None:
        return None
    base = blob["data"][:_content_len(blob["data"])]     # strip any pad we added before
    if target <= len(base):
        return None
    out = base + b"\0" * (target - len(base))

    _reentrant = True
    try:
        AT._relocate(ball["name"], out, blob["idx"], game_dir, 0, 0, "DXT4_5", lambda *a: None)
    finally:
        _reentrant = False
    msg = (f"  {fam}: streaming ceiling was {cap:,} but the family needs {need:,} — padded "
           f"{ball['name']} to {target:,} (would otherwise hang on Loading)")
    log(msg)
    return msg


def strip_ballast(fam: str, game_dir, log=print) -> str | None:
    """Undo the pad on a family's ballast (restore the container's declared length)."""
    game_dir = Path(game_dir)
    for r in census(fam, game_dir):
        if r["pad"] <= 0:
            continue
        blob = _read(r["name"], game_dir)
        base = blob["data"][:_content_len(blob["data"])]
        AT._relocate(r["name"], base, blob["idx"], game_dir, 0, 0, "DXT4_5", lambda *a: None)
        log(f"  {fam}: stripped {r['pad']:,} bytes of ballast from {r['name']}")
        return r["name"]
    return None
