"""
default_matchup.py — set the two teams the game pre-selects on boot (Play Now / Exhibition).

Found 2026-08-01.  The front-end seeds its matchup in Function_83BCEF68, which asks the roster
for two teams *by id* and stores the resulting record pointers at 0x84B9D938+8/+0xC (mirrored
at +0x10/+0x14, next door to g_GameSettings@0x84B9DC68):

    83bcef74  li   r3,0xa            <- HOME team id   (10 = Detroit)
    83bcef78  bl   0x83d4f808        <- Roster_FindTeamById
    83bcef7c  or   r29,r3,r3
    83bcef80  cmplwi cr6,r3,0x0
    83bcef84  bne  cr6,0x83bcef90
    83bcef88  bl   0x83d4b5b0        <- fallback: GetTeamByIndex
    ...
    83bcef90  li   r3,0x17           <- AWAY team id   (23 = Pittsburgh)
    83bcef94  bl   0x83d4f808
    ...
    83bcefcc  stw  r29,0x8(r31)      <- home  -> 0x84B9D940
    83bcefd0  stw  r30,0xc(r31)      <- away  -> 0x84B9D944

Stock 10 v 23 = Detroit v Pittsburgh, the 2009 Stanley Cup Final.  So the whole feature is two
`li r3,imm` instructions and the patch is 4 bytes each.

IMPORTANT — this is an ID, not a row number.  Roster_FindTeamById (0x83d4f808) walks all 96 team
records calling Team_GetId (0x840ad2d8 = `lbz r3,0xc0(rec)`, disk offset +0xC8) and returns the
first whose id byte matches.  In a stock roster id == row, but they can diverge, so team_choices()
reads the ids out of Roster.ROS rather than assuming.  If the id has no match the game falls back
to a plain by-index lookup, so a bad value degrades instead of crashing.

Discovered because permuting the team records' id fields left the menu list order alone but moved
the default matchup to Edmonton v St. Louis — see project_team_display_order.

Version safety: Title Update #1 is a full relink, so these v1.0 VAs are meaningless on a TU build.
read()/write() therefore verify both `li r3,*` slots AND the two `bl Roster_FindTeamById` that
follow them, and refuse the write if any is off.
"""
from __future__ import annotations
import struct
from pathlib import Path

from . import xex_patch

HOME_VA = 0x83BCEF74           # li r3,<home id>
AWAY_VA = 0x83BCEF90           # li r3,<away id>

# the `bl 0x83d4f808` immediately after each `li` — our version fingerprint
_GUARD = {HOME_VA + 4: 0x48180891, AWAY_VA + 4: 0x48180875}

STOCK_HOME, STOCK_AWAY = 10, 23        # Detroit, Pittsburgh

_LI_R3 = 0x38600000                    # addi r3,0,imm
_LI_MASK = 0xFFFF0000

# team table in Roster.ROS: chunk 0x8489FAF3, 96 records x 412 bytes
_TEAM_CHUNK = 0x8489FAF3
_STRIDE = 0x19C
_OFF_CITY, _OFF_NICK, _OFF_CODE, _OFF_ID = 0xA8, 0xAC, 0xB0, 0xC8


class MatchupError(RuntimeError):
    pass


# ── xex side ────────────────────────────────────────────────────────────────────

def _word(data: bytes, xex_path, va: int) -> int:
    off = xex_patch.va_to_offset(xex_path, va)
    if off is None or off + 4 > len(data):
        raise MatchupError(f"VA {va:#x} is not present in this XEX")
    return struct.unpack_from(">I", data, off)[0]


def _check_version(data: bytes, xex_path) -> None:
    for va, want in _GUARD.items():
        got = _word(data, xex_path, va)
        if got != want:
            raise MatchupError(
                f"this XEX doesn't look like NHL 2K10 v1.0 (at {va:#x} expected {want:08x}, "
                f"found {got:08x}). Title Update #1 relinks the whole image, so these "
                f"addresses only apply to the launch executable.")


def read(xex_path) -> tuple[int, int]:
    """Return (home_id, away_id) currently baked into the XEX."""
    xex_path = str(xex_path)
    data = Path(xex_path).read_bytes()
    _check_version(data, xex_path)
    out = []
    for va in (HOME_VA, AWAY_VA):
        w = _word(data, xex_path, va)
        if (w & _LI_MASK) != _LI_R3:
            raise MatchupError(f"{va:#x} is not `li r3,imm` (found {w:08x})")
        out.append(w & 0xFFFF)
    return out[0], out[1]


def write(xex_path, home_id: int, away_id: int, log=print) -> None:
    """Patch the two ids. Verifies the version fingerprint and that both slots are still `li r3,*`."""
    xex_path = str(xex_path)
    for nm, v in (("home", home_id), ("away", away_id)):
        if not 0 <= int(v) <= 0x7FFF:
            raise MatchupError(f"{nm} id {v} out of range for `li r3,imm`")
    data = Path(xex_path).read_bytes()
    _check_version(data, xex_path)
    for va, new_id in ((HOME_VA, int(home_id)), (AWAY_VA, int(away_id))):
        cur = _word(data, xex_path, va)
        if (cur & _LI_MASK) != _LI_R3:
            raise MatchupError(f"{va:#x} is not `li r3,imm` (found {cur:08x}) — refusing to write")
        xex_patch.patch_va(xex_path, va,
                           struct.pack(">I", _LI_R3 | new_id),
                           expect=struct.pack(">I", cur), log=log)


def revert(xex_path, log=print) -> None:
    write(xex_path, STOCK_HOME, STOCK_AWAY, log=log)


# ── roster side: id -> team name ────────────────────────────────────────────────

def _team_table(d: bytes) -> int:
    """File offset of team record 0, or raise. The container's data base is NOT the 0xB28
    ros_file.py assumes (measured 0x47 on a stock save), so the candidates are probed and
    validated instead of trusted."""
    n = struct.unpack_from(">I", d, 8)[0]
    dir_off = None
    for i in range(min(n, 64)):                       # the real directory is short; the tail is junk
        h, cnt, off = struct.unpack_from(">III", d, 0x0C + i * 12)
        if h == _TEAM_CHUNK and cnt >= 30:
            dir_off = off
            break
    if dir_off is None:
        raise MatchupError("no team chunk (0x8489FAF3) in this Roster.ROS")
    for base_delta in (0x47, 0xB28, 0x0C + n * 12):
        base = dir_off + base_delta
        if base + _STRIDE > len(d):
            continue
        if d[base + _OFF_ID] == 0 and _string(d, base + _OFF_CITY):
            return base
    raise MatchupError("found the team chunk but could not locate record 0")


def _string(d: bytes, field_off: int) -> str:
    """Self-relative UTF-16BE string pointer: target = field offset + value - 1."""
    v = struct.unpack_from(">I", d, field_off)[0]
    if not v:
        return ""
    p = field_off + v - 1
    if not 0 <= p < len(d) - 1:
        return ""
    e = d.find(b"\x00\x00", p)
    if e < 0:
        return ""
    if (e - p) % 2:
        e += 1
    try:
        s = d[p:e].decode("utf-16-be")
    except UnicodeDecodeError:
        return ""
    return s if s.isprintable() else ""


def team_choices(ros_path) -> list[tuple[int, str]]:
    """[(id, "Detroit Red Wings (DET)"), ...] for the 30 NHL teams, read from the save so the
    list reflects any renames. Ordered by id, which is the order the ids are quoted in."""
    d = Path(ros_path).read_bytes()
    base = _team_table(d)
    out = []
    for row in range(30):
        r = base + row * _STRIDE
        tid = d[r + _OFF_ID]
        city, nick, code = (_string(d, r + o) for o in (_OFF_CITY, _OFF_NICK, _OFF_CODE))
        label = " ".join(x for x in (city, nick) if x) or f"team {tid}"
        out.append((tid, f"{label} ({code})" if code else label))
    out.sort(key=lambda t: t[0])
    return out


STOCK_CHOICES = [
    (0, "Anaheim Ducks (ANA)"), (1, "Atlanta Thrashers (ATL)"), (2, "Boston Bruins (BOS)"),
    (3, "Buffalo Sabres (BUF)"), (4, "Calgary Flames (CGY)"), (5, "Carolina Hurricanes (CAR)"),
    (6, "Chicago Blackhawks (CHI)"), (7, "Colorado Avalanche (COL)"), (8, "Columbus Blue Jackets (CBJ)"),
    (9, "Dallas Stars (DAL)"), (10, "Detroit Red Wings (DET)"), (11, "Edmonton Oilers (EDM)"),
    (12, "Florida Panthers (FLA)"), (13, "Los Angeles Kings (LA)"), (14, "Minnesota Wild (MIN)"),
    (15, "Montreal Canadiens (MTL)"), (16, "Nashville Predators (NSH)"), (17, "New Jersey Devils (NJ)"),
    (18, "New York Islanders (NYI)"), (19, "New York Rangers (NYR)"), (20, "Ottawa Senators (OTT)"),
    (21, "Philadelphia Flyers (PHI)"), (22, "Phoenix Coyotes (PHO)"), (23, "Pittsburgh Penguins (PIT)"),
    (24, "San Jose Sharks (SJ)"), (25, "St. Louis Blues (STL)"), (26, "Tampa Bay Lightning (TB)"),
    (27, "Toronto Maple Leafs (TOR)"), (28, "Vancouver Canucks (VAN)"), (29, "Washington Capitals (WSH)"),
]


def choices(ros_path=None) -> list[tuple[int, str]]:
    """team_choices() when a readable save is available, else the stock 30."""
    if ros_path and Path(ros_path).is_file():
        try:
            return team_choices(ros_path)
        except Exception:
            pass
    return list(STOCK_CHOICES)


if __name__ == "__main__":
    import sys
    xex = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    h, a = read(xex)
    names = dict(choices(sys.argv[2] if len(sys.argv) > 2 else None))
    print(f"home id {h} = {names.get(h, '?')}")
    print(f"away id {a} = {names.get(a, '?')}")
