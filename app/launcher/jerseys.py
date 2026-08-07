"""jerseys.py — one source of truth per jersey.

A jersey exists TWICE in the game files: as the in-game uniform (6 textures — stamps, normal,
helmet, letters, letters_normal, crowd) and as the front-end team-select sheet (2 textures —
decals, decals_normal). The sheet's `decals` is a **byte-identical copy** of the uniform's
`stamps` (both 2048x512 4444) — verified across all 232 jerseys by hashing decoded pixels. That
duplication is why editing a jersey used to take two passes, once per copy.

`data/jersey_map.json` pairs them (built by content, not by name — the synthetic disc_<crc>
names carry no hint, and hand-guessed pairings were wrong: two "Latvia" sheets are Austria, and
Dallas alt/away were swapped). Creation-zone slots whose default art is identical across slots
couldn't be paired by content and were paired by OFFSET RANK — the Nth uniform by offset pairs
with the Nth sheet by offset, a rule validated 145/145 against the content-matched pairs.

Used for two things:
  hidden_members()  -> the twins to hide from the asset list, so one jersey = one row
  twin_edits(iff, idx) -> the (asset, texture index) copies an edit must also be written to
"""
from __future__ import annotations
import json
from functools import lru_cache

import resources as R

MAP_NAME = "jersey_map.json"

# The sheet mirrors only the uniform's first two textures:
#   uniform idx0 stamps <-> sheet idx0 decals          (VERIFIED identical for every jersey)
#   uniform idx1 normal <-> sheet idx1 decals_normal   (usually identical; NOT always — e.g. ANA
#                                                       stock differs while BOS matches)
# idx2..5 (helmet/letters/letters_normal/crowd) exist only on the uniform, so they never fan out.
MIRRORED_INDICES = (0, 1)


@lru_cache(maxsize=1)
def _doc() -> dict:
    try:
        return json.loads(R.data_path(MAP_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {"jerseys": []}


# Jerseys that exist on this install but not in the shipped map — an expansion team's, which is
# every jersey whose asset key post-dates the map. Registered at runtime by the IFF tab (see
# `extra_teams.frontend_pairs`) rather than baked in, because the set depends on the roster.
_EXTRA: dict = {}


def set_extra_jerseys(pairs) -> int:
    """Register (uniform asset, front-end sheet) pairs discovered on this install.

    Same contract as the shipped map: the in-game uniform is the canonical row and the sheet is
    hidden behind it, so Seattle's jersey is one row that fans its edit out to the team-select
    art — exactly how the 30 shipping teams already behave. Replaces any previous registration.
    """
    global _EXTRA
    m = {}
    for uni, sheet in pairs:
        m.setdefault(uni, []).append(sheet)
    if m == _EXTRA:
        return len(m)
    _EXTRA = m
    for f in (_by_member, hidden_members):
        f.cache_clear()
    return len(m)


def _entries() -> list:
    """The shipped map plus anything registered at runtime, in one shape."""
    out = list(_doc().get("jerseys", []))
    known = {m for e in out for m in e.get("members", [])}
    for uni, sheets in _EXTRA.items():
        if uni in known:
            continue
        out.append({"name": uni, "members": [uni] + list(sheets),
                    "uniforms": [uni], "frontend_sheets": list(sheets),
                    "paired_by": "expansion team, by asset key"})
    return out


@lru_cache(maxsize=1)
def _by_member() -> dict:
    """asset name -> its jersey entry."""
    out = {}
    for e in _entries():
        for m in e.get("members", []):
            out[m] = e
    return out


def entry(iff: str):
    return _by_member().get(iff)


def canonical(iff: str) -> str:
    """The row this asset is edited under (the in-game uniform), or itself if unmapped."""
    e = entry(iff)
    return e["name"] if e else iff


@lru_cache(maxsize=1)
def hidden_members() -> frozenset:
    """Every mapped asset that is NOT its jersey's canonical row — hide these so the editor shows
    one row per jersey instead of the same shirt two or four times."""
    out = set()
    for e in _entries():
        for m in e.get("members", []):
            if m != e["name"]:
                out.add(m)
    return frozenset(out)


def twins(iff: str) -> list:
    """Other assets holding this same jersey image (the front-end sheet, and for teams whose home
    and away art is identical, the other uniform too)."""
    e = entry(iff)
    if not e:
        return []
    return [m for m in e["members"] if m != iff]


def twin_edits(iff: str, idx: int) -> list:
    """[(asset, texture_index)] an edit to `iff` texture `idx` must ALSO be written to.

    Empty unless `idx` is one of the mirrored textures — editing a uniform's helmet/letters/crowd
    has no front-end counterpart to update.
    """
    if idx not in MIRRORED_INDICES:
        return []
    return [(t, idx) for t in twins(iff)]


def stats() -> str:
    d = _entries()
    return f"{len(d)} jerseys, {sum(len(e.get('members', [])) for e in d)} assets"
