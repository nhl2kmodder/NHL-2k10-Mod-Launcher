"""audio_names.py — bank attribution + authored names for the Audio tab.

Data (launcher/data/, see resources.data_path):
  audio_bank_refs.json      {"1A"|"1B": {"0x<offset>": ["arena_van", "gamedata", ...]}}
                            = which sound banks reference each catalogued 1A/1B stream
                            (parsed from the 30 arena_*.iff + gamedata.iff directories).
  audio_authored_names.json bank-centric inventory of the hash-name directories found in
                            crowdloops/pasfx/paintro/sfx_arena*.bnk/... (crc32 name hashes,
                            552/1385 cracked to authored names as of 2026-07-18).

The Audio tab uses bank_tags()/bank_search() to show a "Bank / Team" column and to let the
Team dropdown + search box match team arena audio (e.g. every 1B stream Vancouver's bank
references shows "VAN"; streams only VAN references show "VAN only" = goal song / horn /
team-specific PA candidates).
"""
from __future__ import annotations
import json

try:
    from . import resources
except ImportError:                                     # flat import when run un-packaged
    import resources

CODE_TEAM = {
    "ana": "Anaheim Ducks", "atl": "Atlanta Thrashers", "bos": "Boston Bruins",
    "buf": "Buffalo Sabres", "car": "Carolina Hurricanes", "cbj": "Columbus Blue Jackets",
    "cgy": "Calgary Flames", "chi": "Chicago Blackhawks", "col": "Colorado Avalanche",
    "dal": "Dallas Stars", "det": "Detroit Red Wings", "edm": "Edmonton Oilers",
    "fla": "Florida Panthers", "lak": "Los Angeles Kings", "min": "Minnesota Wild",
    "mtl": "Montreal Canadiens", "njd": "New Jersey Devils", "nsh": "Nashville Predators",
    "nyi": "NY Islanders", "nyr": "NY Rangers", "ott": "Ottawa Senators",
    "phi": "Philadelphia Flyers", "pho": "Phoenix Coyotes", "pit": "Pittsburgh Penguins",
    "sjs": "San Jose Sharks", "stl": "St. Louis Blues", "tbl": "Tampa Bay Lightning",
    "tor": "Toronto Maple Leafs", "van": "Vancouver Canucks", "wsh": "Washington Capitals",
}

_refs: dict | None = None


def _load() -> dict:
    global _refs
    if _refs is None:
        try:
            raw = json.loads(resources.data_path("audio_bank_refs.json").read_text(encoding="utf-8"))
            _refs = {fid: {int(k, 16): v for k, v in m.items()} for fid, m in raw.items()}
        except Exception:
            _refs = {}
    return _refs


def bank_info(fid: str, offset) -> tuple[str, str]:
    """(display, search_haystack) for one catalogued stream.

    display: "VAN only" (single arena bank = team-specific), "VAN +3", "All arenas",
             "SFX (gamedata)", "" when unreferenced.
    search:  lowercase codes + bank names + full team names, so search/Team-filter hit.
    """
    m = _load().get(fid)
    if not m or offset is None:
        return "", ""
    banks = m.get(offset)
    if not banks:
        return "", ""
    arena = [b[6:] for b in banks if b.startswith("arena_")]
    other = [b for b in banks if not b.startswith("arena_")]
    parts = []
    if arena:
        if len(arena) == 1:
            parts.append(f"{arena[0].upper()} only")
        elif len(arena) >= 28:
            parts.append("All arenas")
        else:
            codes = sorted(c.upper() for c in arena)
            parts.append(codes[0] + (f" +{len(codes) - 1}" if len(codes) > 1 else ""))
    if "gamedata" in other:
        parts.append("SFX")
    display = ", ".join(parts)
    hay = " ".join(banks).lower()
    hay += " " + " ".join(CODE_TEAM.get(c, "").lower() for c in arena)
    return display, hay
