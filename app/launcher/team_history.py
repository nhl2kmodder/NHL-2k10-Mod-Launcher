"""Team history / record book  —  Roster.ROS team record, bytes 0x160..0x171.

SOLVED 2026-08-02.  Every league team carries an 18-byte franchise record book.  It is what
the game shows as a team's "records"; there is **no** current- or last-season standings field
anywhere in Roster.ROS (see the note at the bottom), so this is the only place records live.

Layout, relative to the start of a team record (chunk 0x8489FAF3, stride 412):

    0x160  u32   founded(12) | winning_seasons(8) | playoff_appearances(8) | 0(4)
    0x164  u8    division titles
    0x165  u8    Stanley Cups
    0x166  u16   best_year(12) | 0(4)          <- best regular season, by winning %
    0x168  u8 u8 u8   W  L  T/OTL   (+0x16B always 0)
    0x16C  u16   deep_year(12) | round(4)      <- furthest playoff run
    0x16E  u8 u8 u8   W  L  T/OTL   of that season
    0x170..0x171 -> the round nibble's season W/L/T ends at 0x170; 0x171 begins a 12-bit
                    counter (~8 per season played) whose meaning is not established — left alone.

`round` is how far that season went: 5 = won the Stanley Cup, 4 = lost the Final, 3 = lost the
conference/semi final, 2 = lost round two, 1 = lost round one, 0 = missed.

The decode was confirmed against shipped data, not assumed:
    founded   ANA 1993 · OTT/MTL 1917 · BOS 1924 · CHI/NYR 1926 · SJS 1991 · CBJ/MIN 2000
    cups      DET 11 · MTL 23 · TOR 11 · BOS 5 · NYR 4 · CHI 3   (2009 values)
    best      BOS 1929-30 38-5-1 · DET 1995-96 62-13-7 · SJS 2008-09 53-18-11
    deep      MTL 1976-77 60-8-12 round 5 · VAN 1993-94 round 4 · CBJ 2008-09 round 1

Years are the season's START year: 1993 means 1993-94.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

from . import team_order as TO

BACKUP_SUFFIX = ".histbak"

OFF_HIST = 0x160
OFF_DIV = 0x164
OFF_CUPS = 0x165
OFF_BEST = 0x166
OFF_DEEP = 0x16C

ROUNDS = {0: "missed", 1: "round 1", 2: "round 2", 3: "conf. final",
          4: "lost the Final", 5: "won the Cup"}

# Fields this module owns, in display order.  (key, label, kind)
FIELDS = [
    ("founded", "Founded", "year"),
    ("winning", "Winning seasons", "u8"),
    ("playoffs", "Playoff apps", "u8"),
    ("division", "Division titles", "u8"),
    ("cups", "Stanley Cups", "u8"),
    ("best_year", "Best season", "year"),
    ("best_w", "W", "u8"), ("best_l", "L", "u8"), ("best_t", "T/OTL", "u8"),
    ("deep_year", "Best playoff run", "year"),
    ("deep_round", "Result", "round"),
    ("deep_w", "W", "u8"), ("deep_l", "L", "u8"), ("deep_t", "T/OTL", "u8"),
]


class HistoryError(Exception):
    pass


# ---------------------------------------------------------------- codec

def _read(d: bytes, o: int) -> dict:
    packed = struct.unpack_from(">I", d, o + OFF_HIST)[0]
    best = struct.unpack_from(">H", d, o + OFF_BEST)[0]
    deep = struct.unpack_from(">H", d, o + OFF_DEEP)[0]
    return {
        "founded": packed >> 20,
        "winning": (packed >> 12) & 0xFF,
        "playoffs": (packed >> 4) & 0xFF,
        "division": d[o + OFF_DIV],
        "cups": d[o + OFF_CUPS],
        "best_year": best >> 4,
        "best_w": d[o + 0x168], "best_l": d[o + 0x169], "best_t": d[o + 0x16A],
        "deep_year": deep >> 4,
        "deep_round": deep & 0xF,
        "deep_w": d[o + 0x16E], "deep_l": d[o + 0x16F], "deep_t": d[o + 0x170],
    }


def _write(buf: bytearray, o: int, h: dict) -> None:
    for k in ("founded", "best_year", "deep_year"):
        if not 0 <= h[k] <= 0xFFF:
            raise HistoryError(f"{k} {h[k]} does not fit the 12-bit year field")
    if not 0 <= h["deep_round"] <= 5:
        raise HistoryError(f"playoff round {h['deep_round']} is out of range 0..5")
    for k in ("winning", "playoffs", "division", "cups",
              "best_w", "best_l", "best_t", "deep_w", "deep_l", "deep_t"):
        if not 0 <= h[k] <= 0xFF:
            raise HistoryError(f"{k} {h[k]} does not fit a byte")
    # the low nibble of the 0x160 word is zero in every shipping record — keep it that way
    struct.pack_into(">I", buf, o + OFF_HIST,
                     (h["founded"] << 20) | (h["winning"] << 12) | (h["playoffs"] << 4))
    buf[o + OFF_DIV] = h["division"]
    buf[o + OFF_CUPS] = h["cups"]
    struct.pack_into(">H", buf, o + OFF_BEST, h["best_year"] << 4)
    buf[o + 0x168], buf[o + 0x169], buf[o + 0x16A] = h["best_w"], h["best_l"], h["best_t"]
    struct.pack_into(">H", buf, o + OFF_DEEP, (h["deep_year"] << 4) | h["deep_round"])
    buf[o + 0x16E], buf[o + 0x16F], buf[o + 0x170] = h["deep_w"], h["deep_l"], h["deep_t"]


# ---------------------------------------------------------------- read

def read(ros_path) -> list:
    """[{record, code, akey, city, name, ...history fields...}] for every league team."""
    d = Path(ros_path).read_bytes()
    base, count = TO.find_table(d)
    out = []
    for i in TO.league0_slots(d, base, count):
        o = base + i * TO.STRIDE
        row = {"record": i,
               "code": TO._text(d, o + TO.OFF_CODE),
               "akey": TO._text(d, o + TO.OFF_AKEY).upper(),
               "city": TO._text(d, o + TO.OFF_CITY),
               "name": TO._text(d, o + TO.OFF_NICK)}
        row.update(_read(d, o))
        out.append(row)
    return out


# ---------------------------------------------------------------- the 2026 update
#
# Keyed on the ASSET key so a relocated team resolves without special-casing (Utah carries
# PHO, Winnipeg carries ATL) — same convention as launcher/affiliates.py.
#
# Only fields that can be stated without judgement are listed.  Stanley Cups and the furthest
# playoff run are a matter of record; "best regular season" is stored by winning PERCENTAGE
# (Boston's 1929-30 .875 still stands over 2022-23's 65 wins) and the three season counters
# need a season-by-season audit, so those are left for the editor rather than guessed at here.
#
# Seattle and Vegas are authored in full: both were cloned from Anaheim and still read as a
# 1993 expansion team with Anaheim's 2007 Cup run.

MODERN_2026 = {
    # ---- Cups won since the game shipped, and the run that earned them
    "BOS": {"cups": 6, "deep_year": 2010, "deep_round": 5, "deep_w": 46, "deep_l": 25, "deep_t": 11},
    "CHI": {"cups": 6, "deep_year": 2014, "deep_round": 5, "deep_w": 48, "deep_l": 28, "deep_t": 6},
    "LAK": {"cups": 2, "deep_year": 2013, "deep_round": 5, "deep_w": 46, "deep_l": 28, "deep_t": 8},
    "PIT": {"cups": 5, "deep_year": 2016, "deep_round": 5, "deep_w": 50, "deep_l": 21, "deep_t": 11},
    "WSH": {"cups": 1, "deep_year": 2017, "deep_round": 5, "deep_w": 49, "deep_l": 26, "deep_t": 7},
    "STL": {"cups": 1, "deep_year": 2018, "deep_round": 5, "deep_w": 45, "deep_l": 28, "deep_t": 9},
    "TBL": {"cups": 3, "deep_year": 2020, "deep_round": 5, "deep_w": 36, "deep_l": 17, "deep_t": 3},
    "COL": {"cups": 3, "deep_year": 2021, "deep_round": 5, "deep_w": 56, "deep_l": 19, "deep_t": 7},
    "FLA": {"cups": 2, "deep_year": 2024, "deep_round": 5, "deep_w": 47, "deep_l": 31, "deep_t": 4},
    "CAR": {"cups": 2, "deep_year": 2025, "deep_round": 5, "deep_w": 53, "deep_l": 22, "deep_t": 7},
    # ---- deeper runs than the shipped entry, but no Cup
    "VAN": {"deep_year": 2010, "deep_round": 4, "deep_w": 54, "deep_l": 19, "deep_t": 9},
    "SJS": {"deep_year": 2015, "deep_round": 4, "deep_w": 46, "deep_l": 30, "deep_t": 6},
    "NSH": {"deep_year": 2016, "deep_round": 4, "deep_w": 41, "deep_l": 29, "deep_t": 12},
    # ---- the two expansion teams, authored from scratch
    "SEA": {"founded": 2021, "winning": 1, "playoffs": 1, "division": 0, "cups": 0,
            "best_year": 2022, "best_w": 46, "best_l": 28, "best_t": 8,
            "deep_year": 2022, "deep_round": 2, "deep_w": 46, "deep_l": 28, "deep_t": 8},
    "VGK": {"founded": 2017, "winning": 8, "playoffs": 8, "division": 5, "cups": 1,
            "best_year": 2022, "best_w": 51, "best_l": 22, "best_t": 9,
            "deep_year": 2022, "deep_round": 5, "deep_w": 51, "deep_l": 22, "deep_t": 9},
}


def _season(y: int) -> str:
    return f"{y}-{(y + 1) % 100:02d}"


def plan(ros_path, updates: dict | None = None) -> list:
    """[(code, field, old, new)] — everything `updates` would change, and nothing else."""
    updates = MODERN_2026 if updates is None else updates
    out = []
    for row in read(ros_path):
        want = updates.get(row["akey"]) or updates.get(row["code"].upper())
        if not want:
            continue
        for k, v in want.items():
            if row.get(k) != v:
                out.append((row["code"], k, row.get(k), v))
    return out


def apply(ros_path, updates: dict | None = None, backup: bool = True, log=print) -> int:
    """Write `updates` (default: the shipped 2026 set) into the record book.

    Only the 18-byte history block of the teams named in `updates` is touched; a verify pass
    re-reads the file and fails if any other team's block moved.  Run with the game closed.
    """
    updates = MODERN_2026 if updates is None else updates
    path = Path(ros_path)
    old = path.read_bytes()
    buf = bytearray(old)
    base, count = TO.find_table(old)

    changed, touched = 0, set()
    for i in TO.league0_slots(old, base, count):
        o = base + i * TO.STRIDE
        akey = TO._text(old, o + TO.OFF_AKEY).upper()
        code = TO._text(old, o + TO.OFF_CODE)
        want = updates.get(akey) or updates.get(code.upper())
        if not want:
            continue
        h = _read(old, o)
        unknown = set(want) - {k for k, _, _ in FIELDS}
        if unknown:
            raise HistoryError(f"{code}: unknown history field(s) {sorted(unknown)}")
        new = dict(h, **want)
        if new == h:
            continue
        _write(buf, o, new)
        touched.add(i)
        changed += 1
        bits = []
        if new["cups"] != h["cups"]:
            bits.append(f"{h['cups']} -> {new['cups']} Cups")
        if (new["deep_year"], new["deep_round"]) != (h["deep_year"], h["deep_round"]):
            bits.append(f"best run {_season(h['deep_year'])} {ROUNDS[h['deep_round']]}"
                        f" -> {_season(new['deep_year'])} {ROUNDS[new['deep_round']]}")
        if new["founded"] != h["founded"]:
            bits.append(f"founded {h['founded']} -> {new['founded']}")
        if new["best_year"] != h["best_year"]:
            bits.append(f"best season {_season(h['best_year'])} -> {_season(new['best_year'])}"
                        f" {new['best_w']}-{new['best_l']}-{new['best_t']}")
        log(f"  {code}: " + "; ".join(bits or ["record book updated"]))

    if not changed:
        log("record book already up to date")
        return 0

    # verify: nobody else's history block may have moved, and nothing outside it may have.
    new_bytes = bytes(buf)
    for i in TO.league0_slots(new_bytes, base, count):
        o = base + i * TO.STRIDE
        if i in touched:
            lo, hi = o + OFF_HIST, o + 0x171
            if new_bytes[o:lo] != old[o:lo] or new_bytes[hi:o + TO.STRIDE] != old[hi:o + TO.STRIDE]:
                raise HistoryError(f"verify failed: record {i} changed outside its history block")
        elif _read(new_bytes, o) != _read(old, o):
            raise HistoryError(f"verify failed: untouched record {i} history changed")
    if len(new_bytes) != len(old):
        raise HistoryError("verify failed: file size changed")

    if backup:
        bak = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copyfile(path, bak)
        log(f"backup -> {bak.name}")
    path.write_bytes(new_bytes)
    log(f"{changed} team record book(s) updated")
    return changed


def revert(ros_path, log=print) -> bool:
    path = Path(ros_path)
    bak = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if not bak.is_file():
        return False
    shutil.copyfile(bak, path)
    log(f"restored from {bak.name}")
    return True


# ---------------------------------------------------------------------------
# WHY THERE IS NO SEASON STANDINGS FIELD
#
# Every byte of the 412-byte team record that varies across the league was checked for a
# W/L/OTL triple (any three consecutive bytes summing to an 82-game season): the only hits are
# 0x168 and 0x16E, the two record-book seasons above.  The remaining per-team tables were
# identified too — 0xD8 points into chunk 0x33DEDA9C, which is the 88-entry HEAD COACH table
# (surname, first name, ordinal, career totals), and chunk 0xE35B988E is the 40 arenas.
#
# So a team's current record is not shipped data; the game keeps standings in whatever season
# or franchise it is playing and starts everyone at 0-0-0.  Updating a league's standings to a
# real season is therefore not something this file can express.
# ---------------------------------------------------------------------------
