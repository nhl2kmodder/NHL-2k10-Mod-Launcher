"""
career_stats.py — read and rewrite the per-player career stat sheet in Roster.ROS.

Cracked 2026-08-17.  The layout is worth stating up front because it is the reason every pointer
sweep of this file came up empty: **nothing in Roster.ROS points at the stat table.**  The link runs
the other way, as an index array living inside the player's own record.

    player record +0x184   15 x u16, 0xFFFF = empty

        slot 0 is ALWAYS 0xFFFF on a stock save — it is the *in-progress* season, filled in once
        games are played.  Entries are packed from slot 1, MOST RECENT FIRST, so a career is
        capped at 14 stored seasons.  105 players ship full.

        Each value is a plain record index into chunk 0x2A02A5E6.  Verified across all 2,715 rows:
        no out-of-range index, no gaps, no line shared by two players, seasons never increase.

    chunk 0x2A02A5E6       9,862 x 48 bytes — a flat POOL of season lines

        +0x00  s32   self-relative pointer to the TEAM record (target = field_off + v - 1).
                     Stock data uses only 30 distinct targets, 412 apart from 0x11d7d5.
        +0x04  u16   season << 4.  0x7d80 = 2008 = the 2008-09 season.  Low nibble always 0.
        +0x06  u16   total ice time in SECONDS, truncated to 16 bits.  Goalies only; skater lines
                     leave it 0.  (Luongo 2006-07, 4490 min -> 269,400 s -> 7,256 = 0x1c58.)
        +0x08  u8    games played
        +0x0A  u8    goals
        +0x0B  u8    assists
        +0x0C  u8    short-handed goals
        +0x0D  u8    power-play goals
        +0x0E  s8    plus/minus
        +0x10  u16   shots
        +0x14  u16   penalty minutes
        +0x16..      26 bytes, always zero

Records 7362..9861 ship as a contiguous free block (season 0), and 766 of the live lines are
orphans left by earlier edits — so there is real headroom, but it is finite and this module
manages it explicitly rather than trusting it.

Goalies reuse the very same record.  Their GP/G/A/PIM are genuine — Brodeur's lone regular-season
goal is sitting at +0x0A of his 1999-2000 line.  Wins, losses and shutouts have no room in a
48-byte line, so a goalie's *season* row really is GP / GA / ice time and nothing more; his career
W-L-SO-SA does exist, but only as one lump in the totals block below.

    player record +0x174   16 bytes, bit-packed — the career TOTAL (see TOTALS_BITS)

The card's total line is READ FROM THERE, not added up from the seasons on screen.  Miss it and a
synced player shows three seasons of 70/76/55 games under a career total of 51.

See project_career_stats_table.
"""
from __future__ import annotations

import json
import struct
import time
import urllib.error
import urllib.request
from pathlib import Path

from .ros_file import RosFile

STATS_HASH = 0x2A02A5E6
PLAYER_HASH = 0x1E159C31
TEAM_HASH = 0x8489FAF3

LINE_SIZE = 48
SLOT_OFF = 0x184        # first career slot in the player record
N_SLOTS = 15            # slot 0 reserved for the in-progress season
MAX_SEASONS = N_SLOTS - 1
EMPTY = 0xFFFF

# Field widths the game will actually hold.  Anything wider is clamped on write rather than
# silently wrapping — a 260-shot season must not come back as 4.
_U8, _U16, _S8 = 0xFF, 0xFFFF, 127


class CareerError(RuntimeError):
    pass


# ── the career TOTAL — a packed block at +0x174, right before the slot array ──
#
# The "Total" line on a player's card is NOT summed from the season rows on screen: it is stored,
# in sixteen bit-packed bytes immediately ahead of the index array, and the game prints it as-is.
# That is why a synced player showed three seasons of 70/76/55 games under a total of 51 — 51 was
# the career of the 2009 man whose record he replaced.
#
# Only a bit-aligned sweep could find it (byte-aligned scans miss every field but GP), the same
# lesson the salary field taught.  Names are given skater-first / goalie-second: the block is one
# layout with two readings, exactly like the 48-byte season line.
TOTALS_OFF = 0x174
TOTALS_SIZE = 16
TOTALS_BITS = {          # name: (first bit within the block, width)
    "gp": (0, 11),
    "g": (11, 12),        # goalie: goals AGAINST
    "ppg": (23, 9),       # goalie: ties + OT losses
    "a": (32, 12),        # goalie: losses
    "pim": (44, 12),
    "pm": (56, 8),        # SIGNED for skaters; goalie: shutouts
    "shots": (64, 15),    # goalie: shots against
    "w": (79, 10),        # goalie wins — zero on all 1,344 skaters
    "shg": (89, 7),
}
TOTALS_MIN_OFF = 12       # +0x180: minutes played, f32, goalies only (skaters leave it 0.0)


def decode_totals(buf: bytes, rec_off: int) -> dict:
    blk = int.from_bytes(buf[rec_off + TOTALS_OFF:rec_off + TOTALS_OFF + TOTALS_SIZE], "big")
    out = {k: (blk >> (TOTALS_SIZE * 8 - b - w)) & ((1 << w) - 1) for k, (b, w) in TOTALS_BITS.items()}
    if out["pm"] > 127:
        out["pm"] -= 256                                   # skater plus/minus is two's complement
    out["minutes"] = struct.unpack_from(">f", buf, rec_off + TOTALS_OFF + TOTALS_MIN_OFF)[0]
    return out


def encode_totals(buf: bytearray, rec_off: int, tot: dict) -> None:
    """Write the career total in place, clamping every field to the width the game gives it."""
    blk = 0
    for k, (b, w) in TOTALS_BITS.items():
        v = int(round(tot.get(k, 0)))
        if k == "pm":
            v = max(-128, min(v, 127)) & 0xFF
        else:
            v = max(0, min(v, (1 << w) - 1))
        blk |= v << (TOTALS_SIZE * 8 - b - w)
    buf[rec_off + TOTALS_OFF:rec_off + TOTALS_OFF + TOTALS_SIZE] = blk.to_bytes(TOTALS_SIZE, "big")
    struct.pack_into(">f", buf, rec_off + TOTALS_OFF + TOTALS_MIN_OFF, float(tot.get("minutes", 0.0)))


# ── one 48-byte line ─────────────────────────────────────────────────────────
def decode_line(buf: bytes, base: int, team_of=None) -> dict:
    """One stat line -> a plain dict.  `team_of(file_offset)` resolves the team pointer to whatever
    the caller wants (a record index, a name); left out, the raw target offset is reported."""
    ptr = struct.unpack_from(">i", buf, base)[0]
    target = base + ptr - 1
    return {
        "team": team_of(target) if team_of else target,
        "season": struct.unpack_from(">H", buf, base + 0x04)[0] >> 4,
        "toi": struct.unpack_from(">H", buf, base + 0x06)[0],
        "gp": buf[base + 0x08],
        "g": buf[base + 0x0A],
        "a": buf[base + 0x0B],
        "shg": buf[base + 0x0C],
        "ppg": buf[base + 0x0D],
        "pm": struct.unpack_from(">b", buf, base + 0x0E)[0],
        "shots": struct.unpack_from(">H", buf, base + 0x10)[0],
        "pim": struct.unpack_from(">H", buf, base + 0x14)[0],
    }


def encode_line(buf: bytearray, base: int, line: dict, team_foff: int) -> None:
    """Write a stat line in place.  `team_foff` is the file offset of the team RECORD the line
    should credit; the stored pointer is made self-relative to +0x00, which is the file's
    convention everywhere else."""
    season = int(line["season"])
    if not 1900 <= season <= 4094:
        raise CareerError(f"season {season} outside the 12-bit field")
    struct.pack_into(">i", buf, base, team_foff - base + 1)
    struct.pack_into(">H", buf, base + 0x04, (season << 4) & 0xFFFF)
    struct.pack_into(">H", buf, base + 0x06, min(int(line.get("toi", 0)), _U16) & _U16)
    buf[base + 0x08] = min(int(line.get("gp", 0)), _U8)
    buf[base + 0x09] = 0
    buf[base + 0x0A] = min(int(line.get("g", 0)), _U8)
    buf[base + 0x0B] = min(int(line.get("a", 0)), _U8)
    buf[base + 0x0C] = min(int(line.get("shg", 0)), _U8)
    buf[base + 0x0D] = min(int(line.get("ppg", 0)), _U8)
    struct.pack_into(">b", buf, base + 0x0E, max(-128, min(int(line.get("pm", 0)), _S8)))
    buf[base + 0x0F] = 0
    struct.pack_into(">H", buf, base + 0x10, min(int(line.get("shots", 0)), _U16))
    struct.pack_into(">H", buf, base + 0x12, 0)
    struct.pack_into(">H", buf, base + 0x14, min(int(line.get("pim", 0)), _U16))
    buf[base + 0x16:base + LINE_SIZE] = b"\x00" * (LINE_SIZE - 0x16)


# ── the table ────────────────────────────────────────────────────────────────
class CareerTable:
    """The stat pool plus the per-player index arrays, with an allocator over the free lines.

    Everything is done in place: the pool is a fixed 9,862 slots and RosFile.save() refuses any
    size change, so `set_career` frees a player's old lines back to the pool before taking new
    ones.  That keeps a re-sync idempotent instead of leaking a career's worth of slots per run."""

    def __init__(self, path_or_ros):
        self.ros = path_or_ros if isinstance(path_or_ros, RosFile) else RosFile(path_or_ros)
        self.stats = self._chunk(STATS_HASH)
        self.players = self._chunk(PLAYER_HASH)
        self.teams = self._chunk(TEAM_HASH)
        self.n_lines = self.stats.size // LINE_SIZE
        self.n_players = self.players.nrec
        self._reindex()

    def _chunk(self, h):
        c = next((c for c in self.ros.chunks if c.hash == h), None)
        if c is None:
            raise CareerError(f"chunk 0x{h:08X} not present — is this a Roster.ROS?")
        return c

    # -- addressing --
    def line_off(self, i: int) -> int:
        if not 0 <= i < self.n_lines:
            raise CareerError(f"line index {i} out of range (0..{self.n_lines - 1})")
        return self.stats.foff + i * LINE_SIZE

    def player_off(self, row: int) -> int:
        if not 0 <= row < self.n_players:
            raise CareerError(f"player row {row} out of range")
        return self.players.foff + row * self.players.stride

    def team_off(self, idx: int) -> int:
        if not 0 <= idx < self.teams.nrec:
            raise CareerError(f"team index {idx} out of range")
        return self.teams.foff + idx * self.teams.stride

    def team_index(self, file_offset: int):
        """Team record index for a resolved pointer, or None if it does not land on a boundary."""
        rel = file_offset - self.teams.foff
        if rel < 0 or rel % self.teams.stride or rel // self.teams.stride >= self.teams.nrec:
            return None
        return rel // self.teams.stride

    # -- the index array --
    def slots(self, row: int) -> list[int]:
        base = self.player_off(row) + SLOT_OFF
        return [struct.unpack_from(">H", self.ros.data, base + 2 * k)[0] for k in range(N_SLOTS)]

    def _write_slots(self, row: int, values: list[int]) -> None:
        if len(values) != N_SLOTS:
            raise CareerError("slot array must be exactly 15 entries")
        base = self.player_off(row) + SLOT_OFF
        for k, v in enumerate(values):
            struct.pack_into(">H", self.ros.data, base + 2 * k, v & 0xFFFF)

    # -- the stored career total --
    def is_goalie(self, row: int) -> bool:
        v = struct.unpack_from(">I", self.ros.data, self.player_off(row) + 0x40)[0]
        return ((v >> 3) & 7) == 0

    def totals(self, row: int) -> dict:
        return decode_totals(self.ros.data, self.player_off(row))

    def set_totals(self, row: int, career: list[dict]) -> dict:
        """Sum a WHOLE career — every season fetched, not just the ones the 14 slots could hold —
        and store it.  The stock file does the same: Brodeur's block reads 999 GP against 14 stored
        seasons, so a veteran's total has always been the honest number even when the sheet above
        it is short."""
        goalie = self.is_goalie(row)
        tot = {"minutes": 0.0}
        if goalie:
            tot.update(gp=sum(s.get("gp", 0) for s in career),
                       g=sum(s.get("ga", 0) for s in career),
                       ppg=sum(s.get("ties", 0) + s.get("otl", 0) for s in career),
                       a=sum(s.get("losses", 0) for s in career),
                       pim=sum(s.get("pim", 0) for s in career),
                       pm=sum(s.get("so", 0) for s in career),
                       shots=sum(s.get("sa", 0) for s in career),
                       w=sum(s.get("wins", 0) for s in career), shg=0,
                       minutes=sum(s.get("secs", 0) for s in career) / 60.0)
        else:
            for k, src in (("gp", "gp"), ("g", "g"), ("ppg", "ppg"), ("a", "a"),
                           ("pim", "pim"), ("pm", "pm"), ("shots", "shots"), ("shg", "shg")):
                tot[k] = sum(s.get(src, 0) for s in career)
            tot["w"] = 0
        encode_totals(self.ros.data, self.player_off(row), tot)
        return tot

    def career(self, row: int) -> list[dict]:
        """A player's seasons, most recent first, decoded."""
        out = []
        for i in self.slots(row):
            if i == EMPTY:
                continue
            d = decode_line(self.ros.data, self.line_off(i), team_of=self.team_index)
            d["line"] = i
            out.append(d)
        return out

    # -- free-list --
    def _reindex(self) -> None:
        """Referenced lines vs everything else.  A line nobody's array names is free, whether it
        was never used or was orphaned by an earlier edit — the stock file has 766 of the latter."""
        # Reference COUNTS, not a set: an edited save can have two players naming the same line
        # (this collection's roster has 205 such pairs). Freeing one man's sheet must not hand a
        # line still named by somebody else back to the allocator — that would rewrite a stranger's
        # season under his name.
        self.refs = {}
        for row in range(self.n_players):
            for i in self.slots(row):
                if i != EMPTY:
                    self.refs[i] = self.refs.get(i, 0) + 1
        self.used = set(self.refs)
        self.free = [i for i in range(self.n_lines) if i not in self.refs]

    def capacity(self) -> dict:
        return {"lines": self.n_lines, "used": len(self.used), "free": len(self.free)}

    def set_career(self, row: int, lines: list[dict], team_index_of,
                   max_seasons: int = MAX_SEASONS) -> int:
        """Replace a player's whole sheet.  `lines` is most-recent-first; only the first 14 are
        kept, because that is all the array holds.  `team_index_of(line)` returns the team record
        index to credit, or None to skip the line entirely (an unmappable club is better dropped
        than credited to the wrong team).  `max_seasons` lowers that ceiling when the pool is too
        small to give everyone 14 — the oldest seasons go first, which is the cheapest thing to
        lose.  Returns the number of seasons written."""
        max_seasons = max(0, min(max_seasons, MAX_SEASONS))
        for i in (i for i in self.slots(row) if i != EMPTY):    # recycle first, so a re-sync
            n = self.refs.get(i, 1) - 1                         # never grows the pool
            if n > 0:
                self.refs[i] = n
                continue
            self.refs.pop(i, None)
            self.used.discard(i)
            self.free.append(i)
        self.free.sort()

        keep = []
        for ln in lines:
            ti = team_index_of(ln)
            if ti is None:
                continue
            keep.append((ln, ti))
            if len(keep) == max_seasons:
                break

        if len(keep) > len(self.free):
            raise CareerError(f"stat pool exhausted: need {len(keep)} lines, {len(self.free)} free")

        idx = []
        for ln, ti in keep:
            i = self.free.pop(0)
            encode_line(self.ros.data, self.line_off(i), ln, self.team_off(ti))
            self.used.add(i)
            self.refs[i] = 1
            idx.append(i)

        self._write_slots(row, [EMPTY] + idx + [EMPTY] * (N_SLOTS - 1 - len(idx)))
        return len(idx)

    def trim(self, row: int, keep: int) -> int:
        """Drop a row's OLDEST seasons down to `keep`, returning the lines freed.

        Used to harvest headroom from players nobody is syncing — a 2009 farm-team sheet is stale
        whatever happens to it, and losing its oldest years is cheaper than denying a current NHL
        player his career outright."""
        idx = [i for i in self.slots(row) if i != EMPTY]
        if len(idx) <= keep:
            return 0
        drop = idx[keep:]                          # slots are most-recent-first
        for i in drop:
            n = self.refs.get(i, 1) - 1
            if n > 0:
                self.refs[i] = n
                continue
            self.refs.pop(i, None)
            self.used.discard(i)
            self.free.append(i)
        self.free.sort()
        kept = idx[:keep]
        self._write_slots(row, [EMPTY] + kept + [EMPTY] * (N_SLOTS - 1 - len(kept)))
        return len(drop)

    def clear_career(self, row: int) -> int:
        return self.set_career(row, [], lambda _ln: None)

    def save(self, out_path=None, backup=True):
        self.ros.save(out_path, backup=backup)

    # -- sanity --
    def audit(self) -> dict:
        """The invariants the stock file satisfies exactly; a write that breaks one is a bug."""
        bad_index = shared = not_packed = ascending = slot0 = 0
        seen = {}
        for row in range(self.n_players):
            sl = self.slots(row)
            if sl[0] != EMPTY:
                slot0 += 1
            idx = [i for i in sl[1:] if i != EMPTY]
            if sl[1:1 + len(idx)] != idx:
                not_packed += 1
            seasons = []
            for i in idx:
                if i >= self.n_lines:
                    bad_index += 1
                    continue
                if i in seen:
                    shared += 1
                seen[i] = row
                seasons.append(struct.unpack_from(">H", self.ros.data, self.line_off(i) + 4)[0] >> 4)
            if any(seasons[k] > seasons[k - 1] for k in range(1, len(seasons))):
                ascending += 1
        return {"bad_index": bad_index, "shared_lines": shared, "not_packed": not_packed,
                "seasons_ascending": ascending, "slot0_not_empty": slot0,
                "players_with_stats": sum(1 for r in range(self.n_players)
                                          if any(i != EMPTY for i in self.slots(r)))}


# ── NHL.com career feed ──────────────────────────────────────────────────────
LANDING = "https://api-web.nhle.com/v1/player/{pid}/landing"
CACHE_TTL = 7 * 24 * 3600      # a finished season never changes; a week is plenty


def _cache_dir() -> Path:
    d = Path.home() / ".nhl2k10_launcher" / "nhl_career_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_career(pid: int, use_cache=True, timeout=25) -> list[dict]:
    """One player's NHL regular-season history, MOST RECENT FIRST, in this module's line shape.

    `seasonTotals` also carries junior, European and playoff rows; only leagueAbbrev == "NHL" with
    gameTypeId == 2 belongs on the career sheet.  A mid-season trade legitimately yields two rows
    for one season and both are kept — the game stores them that way too."""
    cf = _cache_dir() / f"{pid}.json"
    if use_cache and cf.exists() and time.time() - cf.stat().st_mtime < CACHE_TTL:
        raw = json.loads(cf.read_text(encoding="utf-8"))
    else:
        req = urllib.request.Request(LANDING.format(pid=pid),
                                     headers={"User-Agent": "nhl2k10-launcher"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = json.loads(r.read().decode("utf-8"))
        cf.write_text(json.dumps(raw), encoding="utf-8")

    out = []
    for s in raw.get("seasonTotals", []):
        if s.get("leagueAbbrev") != "NHL" or s.get("gameTypeId") != 2:
            continue
        gp = s.get("gamesPlayed") or 0
        season = int(str(s.get("season", "0"))[:4])
        # Only goalie rows carry `timeOnIce`, and only goalie lines carry ice time in the save —
        # every stock skater line leaves +0x06 at zero.  Deriving a skater total from `avgToi`
        # would just write a 16-bit-wrapped number into a field the game never fills.
        toi = s.get("timeOnIce")
        try:
            m, sec = str(toi).split(":")
            secs = int(m) * 60 + int(sec)
        except (ValueError, AttributeError, TypeError):
            secs = 0
        out.append({
            "season": season,
            "team": (s.get("teamName") or {}).get("default", ""),
            "gp": gp,
            "g": s.get("goals") or 0,
            "a": s.get("assists") or 0,
            "ppg": s.get("powerPlayGoals") or 0,
            "shg": s.get("shorthandedGoals") or 0,
            "pm": s.get("plusMinus") or 0,
            "shots": s.get("shots") or 0,
            "pim": s.get("pim") or 0,
            "toi": secs & 0xFFFF,
            # Nothing below fits a 48-byte season line — the save has no per-season goalie record.
            # They exist for the career TOTAL block, which is the one place wins, losses, shutouts
            # and shots against are kept.  `secs` is unwrapped here on purpose: the total is minutes
            # in a float, so it has no 16-bit ceiling to duck under.
            "wins": s.get("wins") or 0,
            "losses": s.get("losses") or 0,
            "otl": s.get("otLosses") or 0,
            "ties": s.get("ties") or 0,
            "so": s.get("shutouts") or 0,
            "ga": s.get("goalsAgainst") or 0,
            "sa": s.get("shotsAgainst") or 0,
            "secs": secs,
        })
    out.sort(key=lambda r: r["season"], reverse=True)
    return out


# ── club names -> the abbreviation the rest of the launcher speaks ───────────
#
# `seasonTotals` names the club in full and never abbreviates it, so the mapping has to be spelled
# out.  Only the last 14 seasons can be stored, which bounds how much history this needs: the one
# franchise that moved inside that window is Phoenix -> Arizona -> Utah, and Atlanta became Winnipeg
# in 2011 — just outside, but it costs nothing to carry.
TEAM_BY_NAME = {
    "Anaheim Ducks": "ANA", "Boston Bruins": "BOS", "Buffalo Sabres": "BUF",
    "Calgary Flames": "CGY", "Carolina Hurricanes": "CAR", "Chicago Blackhawks": "CHI",
    "Colorado Avalanche": "COL", "Columbus Blue Jackets": "CBJ", "Dallas Stars": "DAL",
    "Detroit Red Wings": "DET", "Edmonton Oilers": "EDM", "Florida Panthers": "FLA",
    "Los Angeles Kings": "LAK", "Minnesota Wild": "MIN", "Montréal Canadiens": "MTL",
    "Montreal Canadiens": "MTL", "Nashville Predators": "NSH", "New Jersey Devils": "NJD",
    "New York Islanders": "NYI", "New York Rangers": "NYR", "Ottawa Senators": "OTT",
    "Philadelphia Flyers": "PHI", "Pittsburgh Penguins": "PIT", "San Jose Sharks": "SJS",
    "Seattle Kraken": "SEA", "St. Louis Blues": "STL", "Tampa Bay Lightning": "TBL",
    "Toronto Maple Leafs": "TOR", "Vancouver Canucks": "VAN", "Vegas Golden Knights": "VGK",
    "Washington Capitals": "WSH", "Winnipeg Jets": "WPG",
    # relocations and rebrands the feed still reports under their period name
    "Phoenix Coyotes": "UTA", "Arizona Coyotes": "UTA",
    "Utah Hockey Club": "UTA", "Utah Mammoth": "UTA", "Atlanta Thrashers": "WPG",
}


def fit_cap(demands, budget: int) -> int:
    """Largest per-player season cap K with `sum(min(n, K)) <= budget`.

    The pool is a fixed 9,862 lines shared by the whole file, and a league of 780 men with 14
    seasons each wants 10,920 — so somebody has to give. Trimming everyone's oldest seasons equally
    is the honest way to lose the surplus: the alternative, first-come-first-served, silently leaves
    whoever the loop reaches last with no career at all."""
    demands = list(demands)
    for k in range(MAX_SEASONS, 0, -1):
        if sum(min(n, k) for n in demands) <= budget:
            return k
    return 0


def sync_player(table: "CareerTable", row: int, nhl_id: int, club_rows: dict,
                use_cache=True, career=None, max_seasons: int = MAX_SEASONS) -> dict:
    """Pull one player's NHL career and write it over whatever the save had.

    `club_rows` is {abbr: team_record_index} — the launcher's existing primary-club map. A season
    on a club the save has no record for is dropped rather than credited to the wrong sweater.
    Pass `career` to reuse an already-fetched feed (the batch pass fetches everything first so it
    can size the pool before writing a byte)."""
    if career is None:
        career = fetch_career(nhl_id, use_cache=use_cache)
    dropped = []

    def team_index_of(line):
        abbr = TEAM_BY_NAME.get(line["team"])
        idx = club_rows.get(abbr)
        if idx is None:
            dropped.append(f"{line['season']} {line['team']}")
        return idx

    n = table.set_career(row, career, team_index_of, max_seasons=max_seasons)
    # The card's "Total" is a stored block, not a sum of the rows above it, so it has to be written
    # too — and from the FULL feed, including seasons the 14 slots could not hold.
    table.set_totals(row, career)
    return {"row": row, "id": nhl_id, "seasons": n,
            "available": len(career), "dropped": dropped,
            "truncated": max(0, len(career) - len(dropped) - n)}
