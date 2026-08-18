"""roster_verify.py — check a Roster.ROS against the real NHL rosters on NHL.com.

Everything this needs is already readable offline: `player_assign.PlayerTable` resolves a player's
name, his club (the self-relative pointer at +0x14 straight at the team record) and the bio bit
fields the live editor mapped out of Ghidra. So the check is a pure file read — no Xenia, no
running game.

Source of truth is the NHL's own public API (the one nhl.com's roster pages call):

    https://api-web.nhle.com/v1/roster/<ABBR>/current      -> forwards / defensemen / goalies

which carries exactly the things worth checking:

    positionCode, heightInInches, weightInPounds, shootsCatches, sweaterNumber, birthDate

RATINGS ARE DELIBERATELY NOT CHECKED. NHL.com publishes no attribute ratings, so there is nothing
to diff a 2K rating against — any "expected" rating would be my invention, not a fact, and a tool
that reports invented mismatches is worse than one that reports none. What IS checked is the
record's internal consistency (a goalie with no goalie ratings, an all-zero attribute block), which
catches the failure that actually happens: a created player who was never given ratings.

Matching is by name, not by index. Names are normalised (accents folded, punctuation and Jr/Sr
suffixes dropped) and a player is matched inside his NHL club first; only if that fails is the whole
league searched, which is what turns "not on this team" into a WRONG_TEAM finding rather than two
useless MISSING/EXTRA lines.

Tolerances exist because the fields are quantised in the save, not because the data is fuzzy:
weight is stored 6-bit as `4*v + 130`, so ±2 lb is unrepresentable and is not a mistake. Height is
exact to the inch. Both decodes were re-derived here against 632 name-matched players rather than
inherited on trust, which is how the old `+133` weight constant turned out to be 3 lb high.

CLI:
    python -m launcher.roster_verify "<Roster.ROS>" [--json out.json] [--team VAN] [--no-cache]
"""
from __future__ import annotations

import json
import re
import struct
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .player_assign import PlayerTable
    from . import career_stats, salary
except ImportError:                                    # standalone run
    from player_assign import PlayerTable
    import career_stats
    import salary

API = "https://api-web.nhle.com/v1/roster/{abbr}/current"
CACHE_TTL = 12 * 3600

# The 32 clubs, keyed by the abbreviation the API wants.
NHL_TEAMS = [
    "ANA", "BOS", "BUF", "CAR", "CBJ", "CGY", "CHI", "COL", "DAL", "DET", "EDM", "FLA",
    "LAK", "MIN", "MTL", "NJD", "NSH", "NYI", "NYR", "OTT", "PHI", "PIT", "SEA", "SJS",
    "STL", "TBL", "TOR", "UTA", "VAN", "VGK", "WPG", "WSH",
]

# The save spells a few clubs the way 2K did in 2009 (LA, SJ, NJ, TB) and the relocated/expansion
# work added its own codes. Map ROS team code -> API abbreviation; anything unmapped is treated as
# not-an-NHL-club (minors, national teams, All-Star sides) and skipped.
ROS_TO_NHL = {
    "ANA": "ANA", "BOS": "BOS", "BUF": "BUF", "CAR": "CAR", "CBJ": "CBJ", "CGY": "CGY",
    "CHI": "CHI", "COL": "COL", "DAL": "DAL", "DET": "DET", "EDM": "EDM", "FLA": "FLA",
    "LA": "LAK", "LAK": "LAK", "MIN": "MIN", "MTL": "MTL", "NJ": "NJD", "NJD": "NJD",
    "NSH": "NSH", "NYI": "NYI", "NYR": "NYR", "OTT": "OTT", "PHI": "PHI", "PIT": "PIT",
    "SEA": "SEA", "SJ": "SJS", "SJS": "SJS", "STL": "STL", "TB": "TBL", "TBL": "TBL",
    "TOR": "TOR", "UTA": "UTA", "UTH": "UTA", "VAN": "VAN", "VGK": "VGK", "WPG": "WPG",
    "WSH": "WSH",
}
# A club record whose nickname says "Minors" is an AHL affiliate no matter what its 3-letter code
# is (CHI = "WPG Minors", TOR = "TOR Minors"), so the code alone cannot decide.
MINOR_MARK = "minors"

POS_MAP = {0: "G", 1: "D", 2: "LW", 3: "RW", 4: "C"}
# NHL.com collapses the wings to L/R; 2K keeps LW/RW. Compare in the API's alphabet.
POS_TO_API = {"G": "G", "D": "D", "LW": "L", "RW": "R", "C": "C"}

WEIGHT_TOL = 2        # 6-bit field, 4 lb per step -> ±2 lb is unrepresentable
HEIGHT_TOL = 0        # stored to the inch

# Weight decode. The constant was `4*v + 133`, fit from two players (Kane 178, Chara 256) back when
# the field was first found; against 632 name-matched NHL players it is 3 lb high. Refit here: the
# MEDIAN real weight of every raw bucket v=8..31 sits on `4*v + 130` to within a pound (the two
# buckets that don't hold one and two players). The field itself is confirmed, not just the scale —
# a bit-by-bit sweep of all 420 bytes finds no better-correlated region (r=0.922, and widths 7..10
# score identically, i.e. the bits below the 6 are dead).
WEIGHT_MUL, WEIGHT_ADD = 4, 130
# Height needs no such correction: the same sweep puts +0x0C bits15..22 at r=0.984 and `v*0.2 + 50`
# reproduces the NHL's listed inches exactly for 89% of players, the rest differing by 1".
HEIGHT_MUL, HEIGHT_ADD = 0.2, 50
# Handedness likewise survives its own sweep: +0x34 bit15 agrees with NHL.com for 71% of players and
# is the single best-agreeing bit in the entire record (runner-up 66% is the position field, which
# only correlates because of the D/RW skew). So the 29% are roster data, not a decode fault.

# Bio bit fields, offsets identical to the in-memory record (see project_ros_disk_memory_delta).
OFF_HEIGHT, OFF_WEIGHT, OFF_SHOOTS, OFF_JERSEY, OFF_POS, OFF_OVR = 0x0C, 0x11, 0x34, 0x20, 0x40, 0x34
# Birth date: ONE 21-bit field at bit 11 of the u32 at +0x1E, `year(12) | month(4) | day(5)`, month
# 1-based. Verified exact on 31/31 known 2009-10 players in the stock save. Being contiguous is what
# lets it ride the same single-bit-field machinery as height and weight below.
#
# ⚠ NOT the same encoding as the game's own calendar, which packs year-2000 / ZERO-based month into
# a different u32 in the XEX — see project_game_date_clock. Two date formats, one game.
OFF_BIRTH, BIRTH_LO, BIRTH_W = 0x1E, 11, 21
# Attribute block: 2-byte slots, 10-bit value at bit 4, displayed = stored * 0.16.
SKATER_SLOTS = (0x52, 0x54, 0x56, 0x58, 0x5A, 0x5C, 0x66, 0x68, 0x70, 0x72, 0x80, 0x82)
GOALIE_SLOTS = (0x62, 0x64, 0x76, 0x78, 0x7A, 0x7C, 0x7E, 0x92, 0x94, 0x96)


# ── name normalisation ───────────────────────────────────────────────────────
_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv)\b")


def norm_name(s: str) -> str:
    """Fold to a comparable key: accents stripped, punctuation dropped, suffixes removed.

    Alexander Nikishin / Nikišin and Marc-Edouard Vlasic / Marc-Édouard both have to land on one
    key or every European on the roster reads as a mismatch."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("'", "").replace("`", "").replace(".", "")
    s = s.replace("-", " ").replace("  ", " ")
    s = _SUFFIX.sub("", s)
    return " ".join(s.split())


# ── birth dates ──────────────────────────────────────────────────────────────
def pack_birth(iso: str) -> int:
    """'1997-01-13' -> the 21-bit field. Raises ValueError on anything unparseable, which is how
    plan_fixes() drops a player the API has no usable date for instead of writing a zero."""
    y, m, d = (int(x) for x in str(iso)[:10].split("-"))
    if not (1900 <= y <= 4095 and 1 <= m <= 12 and 1 <= d <= 31):
        raise ValueError(f"birth date out of range: {iso}")
    return (y << 9) | (m << 5) | d


def unpack_birth(v: int) -> str:
    """The 21-bit field -> 'YYYY-MM-DD', or '' when the record has no date at all."""
    y, m, d = v >> 9, (v >> 5) & 0xF, v & 0x1F
    return f"{y:04d}-{m:02d}-{d:02d}" if y and m and d else ""


def name_key(first: str, last: str) -> str:
    return f"{norm_name(last)}|{norm_name(first)}"


def loose_key(first: str, last: str) -> str:
    """Surname + first initial — catches Mitch/Mitchell Marner and Alex/Alexander Ovechkin."""
    f = norm_name(first)
    return f"{norm_name(last)}|{f[:1]}"


# ── NHL.com ──────────────────────────────────────────────────────────────────
def _cache_dir() -> Path:
    d = Path.home() / ".nhl2k10_launcher" / "nhl_roster_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_team(abbr: str, use_cache=True, timeout=25) -> list[dict]:
    """One club's current roster as flat player dicts. Cached for 12 h so a re-run is instant."""
    cf = _cache_dir() / f"{abbr}.json"
    if use_cache and cf.exists() and time.time() - cf.stat().st_mtime < CACHE_TTL:
        raw = json.loads(cf.read_text(encoding="utf-8"))
    else:
        req = urllib.request.Request(API.format(abbr=abbr), headers={"User-Agent": "nhl2k10-launcher"})
        with urllib.request.urlopen(req, timeout=timeout) as r:      # follows the 307 to /v1/...
            raw = json.loads(r.read().decode("utf-8"))
        cf.write_text(json.dumps(raw), encoding="utf-8")
    out = []
    for group in ("forwards", "defensemen", "goalies"):
        for p in raw.get(group, []):
            first = (p.get("firstName") or {}).get("default", "")
            last = (p.get("lastName") or {}).get("default", "")
            out.append({
                "team": abbr,
                "first": first,
                "last": last,
                "pos": p.get("positionCode", ""),
                "height": p.get("heightInInches"),
                "weight": p.get("weightInPounds"),
                "shoots": (p.get("shootsCatches") or "").upper()[:1],
                "jersey": p.get("sweaterNumber"),
                "birth": (p.get("birthDate") or "")[:10],
                "id": p.get("id"),
            })
    return out


def fetch_league(teams=None, use_cache=True, log=print) -> dict[str, list[dict]]:
    teams = teams or NHL_TEAMS
    out, failed = {}, []
    for a in teams:
        try:
            out[a] = fetch_team(a, use_cache=use_cache)
        except (urllib.error.URLError, OSError, ValueError) as e:
            failed.append(f"{a}: {e}")
            out[a] = []
    if failed and log:
        log("NHL.com fetch failed for: " + "; ".join(failed))
    return out


# ── Roster.ROS ───────────────────────────────────────────────────────────────
def _bits(t: PlayerTable, row, off, lo, w):
    return (t._u32(row, off) >> lo) & ((1 << w) - 1)


def _rating(t: PlayerTable, row, off):
    return round(_bits(t, row, off, 4, 10) * 0.16)


def read_ros(path, log=print) -> list[dict]:
    """Every player row with the fields this check needs.

    `path` may also be an already-open PlayerTable, which is what lets apply_fixes() read back a
    SIMULATED save (writes applied in memory, nothing on disk) instead of re-reading the file.

    `team_id` is the FILE OFFSET of the club record, not its name — two distinct records can carry
    identical names (the save here has two 'Utah Mammoth' and a second 'New York Rangers'), and
    collapsing them by name is what hides a duplicate club instead of reporting it."""
    t = path if isinstance(path, PlayerTable) else PlayerTable(path)
    d = t.ros.data
    rows = []
    for r in range(t.nrec):
        first, last = t.name(r)
        city, nick, code = t.team(r)
        po = t.base + r * 420 + 0x14
        v = struct.unpack_from(">I", d, po)[0]
        team_id = (po + v - 1) if v else None
        pos = POS_MAP.get(_bits(t, r, OFF_POS, 3, 3), "?")
        slots = GOALIE_SLOTS if pos == "G" else SKATER_SLOTS
        rows.append({
            "row": r,
            "first": first, "last": last,
            "city": city, "nick": nick, "code": code, "team_id": team_id,
            "pos": pos,
            "height": round(_bits(t, r, OFF_HEIGHT, 15, 8) * HEIGHT_MUL + HEIGHT_ADD),
            "weight": round(_bits(t, r, OFF_WEIGHT, 26, 6) * WEIGHT_MUL + WEIGHT_ADD),
            "shoots": "R" if _bits(t, r, OFF_SHOOTS, 15, 1) else "L",
            "jersey": _bits(t, r, OFF_JERSEY, 12, 7),
            "birth": unpack_birth(_bits(t, r, OFF_BIRTH, BIRTH_LO, BIRTH_W)),
            "overall": _bits(t, r, OFF_OVR, 0, 7),
            "ratings": [_rating(t, r, o) for o in slots],
        })
    if log:
        log(f"{path}: {t.nrec} player records")
    return rows


def is_nhl_club(p) -> bool:
    """True for a row sitting on one of the 32 NHL clubs (not a farm team or a national side)."""
    if not p["team_id"] or MINOR_MARK in (p["nick"] or "").lower():
        return False
    return p["code"] in ROS_TO_NHL


def primary_clubs(active) -> dict:
    """{api_abbr: team_record_offset} — which club record is the real roster.

    A club can own more than one record: this save carries a second Anaheim ('ANAHEIM'/'DUCKS'), a
    second Rangers, Utah under both UTA and UTH, and a second Winnipeg. Each holds ~23 rows from the
    2336+ block, most with EMPTY names and the rest copies of stars already on the real club — the
    created/all-star duplicate pool, not a roster. Checking those rows would invent a duplicate for
    every star in the league, so the record with the most NAMED players wins and the rest are pool.
    """
    tally = {}
    for p in active:
        abbr = ROS_TO_NHL[p["code"]]
        k = (abbr, p["team_id"])
        named, lo = tally.get(k, (0, p["row"]))
        tally[k] = (named + (1 if (p["last"] or "").strip() else 0), min(lo, p["row"]))
    best = {}
    for (abbr, tid), (named, lo) in tally.items():
        cur = best.get(abbr)
        if cur is None or (named, -lo) > (cur[1], -cur[2]):
            best[abbr] = (tid, named, lo)
    return {a: v[0] for a, v in best.items()}


# ── the check ────────────────────────────────────────────────────────────────
def verify(path, teams=None, use_cache=True, log=print) -> dict:
    ros = read_ros(path, log=log)
    nhl = fetch_league(teams, use_cache=use_cache, log=log)

    # NHL side: exact key -> player, plus a loose index for first-name variants.
    nhl_by_key, nhl_by_loose = {}, {}
    for club in nhl.values():
        for p in club:
            nhl_by_key.setdefault(name_key(p["first"], p["last"]), []).append(p)
            nhl_by_loose.setdefault(loose_key(p["first"], p["last"]), []).append(p)

    # ROS side: only rows sitting on an NHL club are candidates. Rows on farm/national teams are
    # legitimately not in an NHL roster feed, so they can never be a finding.
    on_club = [p for p in ros if is_nhl_club(p)]
    if teams:
        on_club = [p for p in on_club if ROS_TO_NHL[p["code"]] in teams]
    primary = primary_clubs(on_club)
    pool = [p for p in on_club if p["team_id"] != primary[ROS_TO_NHL[p["code"]]]]
    active = [p for p in on_club if p["team_id"] == primary[ROS_TO_NHL[p["code"]]]
              and (p["last"] or "").strip()]
    ros_by_key = {}
    for p in active:
        ros_by_key.setdefault(name_key(p["first"], p["last"]), []).append(p)

    findings, matched_nhl, claimed, nhl_ids = [], set(), {}, {}

    def add(kind, p, detail, **extra):
        f = {"kind": kind, "row": p["row"],
             "player": f"{p['first']} {p['last']}".strip(),
             "team": p["code"], "detail": detail}
        f.update(extra)
        findings.append(f)

    for p in active:
        want_abbr = ROS_TO_NHL[p["code"]]
        k, lk = name_key(p["first"], p["last"]), loose_key(p["first"], p["last"])
        cands = nhl_by_key.get(k) or nhl_by_loose.get(lk) or []
        if not cands:
            add("NOT_IN_NHL", p, f"no {p['code']} (or any) NHL player by this name")
            continue
        # Prefer the candidate on the club the save puts him on; that's what makes a genuine
        # transfer read as WRONG_TEAM instead of a pile of attribute mismatches.
        same = [c for c in cands if c["team"] == want_abbr]
        # Namesakes are real: Vancouver dresses TWO Elias Petterssons. Pick the one whose sweater
        # matches, else the closest build, so the pair maps one-to-one instead of both rows
        # collapsing onto the same man and reading as a duplicate.
        pool_c = same or cands
        real = min(pool_c, key=lambda c: (c["jersey"] != p["jersey"],
                                          abs((c["height"] or 0) - p["height"])))
        matched_nhl.add((real["team"], name_key(real["first"], real["last"]), real["id"]))
        claimed.setdefault(real["id"], []).append(p)
        # The career-stats pass needs the identity this match established; recomputing it there
        # would be a second, silently different matcher.
        nhl_ids[p["row"]] = real["id"]

        if real["team"] != want_abbr:
            add("WRONG_TEAM", p, f"is on {real['team']}, save has him on {p['code']}",
                expected=real["team"], actual=p["code"])
        if real["height"] and abs(p["height"] - real["height"]) > HEIGHT_TOL:
            add("HEIGHT", p, f"{p['height']}\" vs NHL {real['height']}\"",
                expected=real["height"], actual=p["height"])
        if real["weight"] and abs(p["weight"] - real["weight"]) > WEIGHT_TOL:
            add("WEIGHT", p, f"{p['weight']} lb vs NHL {real['weight']} lb",
                expected=real["weight"], actual=p["weight"])
        if real["shoots"] and p["shoots"] != real["shoots"]:
            add("SHOOTS", p, f"{p['shoots']} vs NHL {real['shoots']}",
                expected=real["shoots"], actual=p["shoots"])
        if real["pos"] and POS_TO_API.get(p["pos"]) != real["pos"]:
            add("POSITION", p, f"{p['pos']} vs NHL {real['pos']}",
                expected=real["pos"], actual=p["pos"])
        if real["jersey"] and p["jersey"] != real["jersey"]:
            add("JERSEY", p, f"#{p['jersey']} vs NHL #{real['jersey']}",
                expected=real["jersey"], actual=p["jersey"])
        # Birth date travels with the man and the game computes his displayed age from it. Most
        # synced rows carry whoever's date the record used to hold, because nothing ever wrote it.
        if real["birth"] and p["birth"] != real["birth"]:
            add("BIRTHDATE", p, f"{p['birth'] or 'unset'} vs NHL {real['birth']}",
                expected=real["birth"], actual=p["birth"])
        # Ratings have no external truth — only internal coherence is checkable.
        if p["ratings"] and max(p["ratings"]) == 0:
            add("NO_RATINGS", p, f"every {'goalie' if p['pos'] == 'G' else 'skater'} rating is 0")
        elif p["overall"] == 0:
            add("NO_OVERALL", p, "overall is 0 (never computed for this record)")

    # Duplicates: two rows resolving to the SAME NHL player. Keyed on his NHL id, not on his name,
    # because a shared name is not a duplicate — see the Pettersson note above.
    for pid, ps in claimed.items():
        if len(ps) > 1:
            where = ", ".join(f"{q['code']}(row {q['row']})" for q in ps)
            add("DUPLICATE", ps[0], f"appears {len(ps)}x on NHL clubs: {where}")

    # Team-level view.
    team_report = {}
    for abbr in (teams or NHL_TEAMS):
        club = [p for p in active if ROS_TO_NHL[p["code"]] == abbr]
        real = nhl.get(abbr, [])
        missing = [f"{q['first']} {q['last']} ({q['pos']})" for q in real
                   if q["id"] not in claimed]
        bad = [f for f in findings if f["team"] in ROS_TO_NHL and ROS_TO_NHL[f["team"]] == abbr]
        extra = [p for p in pool if ROS_TO_NHL[p["code"]] == abbr]
        team_report[abbr] = {
            "ros_count": len(club),
            "nhl_count": len(real),
            "findings": len(bad),
            "missing": missing,
            "codes": sorted({p["code"] for p in club}),
            "pool_rows": len(extra),
            "pool_codes": sorted({p["code"] for p in extra}),
            "goalies": sum(1 for p in club if p["pos"] == "G"),
        }

    return {"ros_path": str(path), "players_checked": len(active),
            "pool_rows": len(pool), "findings": findings, "teams": team_report,
            # Carried for the career-stats pass in apply_fixes(): who each row actually IS, and
            # which team record each club's roster lives on.
            # Computed over EVERY club, not the --team subset: a career line can sit on any of the
            # 32, so a filtered map would silently drop most of a checked player's seasons.
            "nhl_ids": nhl_ids, "primary_clubs": primary_clubs([p for p in ros if is_nhl_club(p)])}


# ── report ───────────────────────────────────────────────────────────────────
ORDER = ["WRONG_TEAM", "DUPLICATE", "NOT_IN_NHL", "POSITION", "SHOOTS", "HEIGHT",
         "WEIGHT", "BIRTHDATE", "JERSEY", "NO_RATINGS", "NO_OVERALL"]


def format_report(res, max_per_kind=40) -> str:
    L = [f"Roster check — {res['ros_path']}",
         f"{res['players_checked']} players on NHL clubs, {len(res['findings'])} findings", ""]
    by_kind = {}
    for f in res["findings"]:
        by_kind.setdefault(f["kind"], []).append(f)
    L.append("SUMMARY")
    for k in ORDER:
        if by_kind.get(k):
            L.append(f"  {k:<12} {len(by_kind[k])}")
    L.append("")
    for k in ORDER:
        fs = by_kind.get(k)
        if not fs:
            continue
        L.append(f"── {k} ({len(fs)}) " + "─" * 30)
        for f in sorted(fs, key=lambda x: (x["team"], x["player"]))[:max_per_kind]:
            L.append(f"  [{f['team']:<3}] row {f['row']:<5} {f['player']:<26} {f['detail']}")
        if len(fs) > max_per_kind:
            L.append(f"  … {len(fs) - max_per_kind} more")
        L.append("")
    L.append("── TEAMS " + "─" * 38)
    L.append(f"  {'':<5}{'ROS':>4}{'NHL':>5}{'G':>3}{'bad':>5}{'pool':>6}  missing from save")
    for abbr, t in sorted(res["teams"].items()):
        miss = ", ".join(t["missing"][:6]) + (f" +{len(t['missing']) - 6}" if len(t["missing"]) > 6 else "")
        flag = "!" if (t["pool_rows"] or t["ros_count"] > 30 or t["goalies"] not in (2, 3)) else " "
        L.append(f" {flag}{abbr:<4}{t['ros_count']:>4}{t['nhl_count']:>5}{t['goalies']:>3}"
                 f"{t['findings']:>5}{t['pool_rows']:>6}  {miss}")
    L.append("")
    L.append(f"  ROS = named players on the club's main record; pool = rows on a SECOND record for "
             f"the same club\n  (created/all-star duplicates, {res['pool_rows']} in total, not "
             f"checked); bad = findings; G = goalies.")
    return "\n".join(L)


# ── writing the mechanical fields back ───────────────────────────────────────
#
# Only the fields that have ONE right answer and no gameplay judgement in them: height, weight,
# handedness, sweater number and birth date. POSITION is deliberately not fixable here — 2K's LW/RW/C
# carries line-assignment meaning the API's L/R/C does not, and WRONG_TEAM is a roster decision, not
# a typo.
#
# Each entry is (offset, lo bit, width, encode, decode). The encoders are the inverse of read_ros().
# BIRTHDATE fits this shape only because its three components are contiguous bits — it is one 21-bit
# number whose "value" happens to print as a date, so it needs no special case anywhere below.
FIXABLE = {
    "HEIGHT": (OFF_HEIGHT, 15, 8,
               lambda inches: int(round((inches - HEIGHT_ADD) / HEIGHT_MUL)),
               lambda v: round(v * HEIGHT_MUL + HEIGHT_ADD)),
    "WEIGHT": (OFF_WEIGHT, 26, 6,
               lambda lb: int(round((lb - WEIGHT_ADD) / WEIGHT_MUL)),
               lambda v: round(v * WEIGHT_MUL + WEIGHT_ADD)),
    "SHOOTS": (OFF_SHOOTS, 15, 1,
               lambda hand: 1 if hand == "R" else 0,
               lambda v: "R" if v else "L"),
    "JERSEY": (OFF_JERSEY, 12, 7,
               lambda n: int(n),
               lambda v: v),
    "BIRTHDATE": (OFF_BIRTH, BIRTH_LO, BIRTH_W, pack_birth, unpack_birth),
}


def _set_bits(t: PlayerTable, row, off, lo, w, val):
    """Replace one bit field inside the u32 at record+off, leaving every other bit alone."""
    mask = ((1 << w) - 1) << lo
    cur = t._u32(row, off)
    t._set_u32(row, off, (cur & ~mask) | ((val << lo) & mask))


def plan_fixes(res, kinds=None, jersey_on_wrong_team=False) -> list[dict]:
    """Turn verify() findings into concrete writes, dropping any that the field cannot represent.

    A 6-bit weight only lands on multiples of 4 above 130, so `stored` is what the save will actually
    read back afterwards — not necessarily the NHL's listed pound. A row whose stored value already
    equals the target is dropped rather than written.

    Height, weight, handedness and birth date travel with the MAN, so they are safe to write wherever
    the save has him. A sweater number travels with the CLUB, and a row already flagged WRONG_TEAM is by
    definition on the wrong club, so writing his new number there just collides with the team-mate
    who legitimately wears it — measured on this save, doing it anyway turns 52 collisions into 32
    (14 of them new), whereas holding those 48 back gives 25 (3 new) and still fixes 73 numbers. The
    held-back ones become correct for free once the team move is made, so pass True only if the
    team assignments are already settled."""
    kinds = [k for k in (kinds or FIXABLE) if k in FIXABLE]
    wrong_team = {f["row"] for f in res["findings"] if f["kind"] == "WRONG_TEAM"}
    out = []
    for f in res["findings"]:
        if f["kind"] not in kinds or "expected" not in f:
            continue
        if f["kind"] == "JERSEY" and not jersey_on_wrong_team and f["row"] in wrong_team:
            out.append({**f, "skip": "on the wrong club — fix the team move first"})
            continue
        off, lo, w, enc, dec = FIXABLE[f["kind"]]
        try:
            raw = enc(f["expected"])
        except (TypeError, ValueError):
            continue
        hi = (1 << w) - 1
        if not 0 <= raw <= hi:                       # unrepresentable — never clamp silently
            out.append({**f, "skip": f"{f['expected']} does not fit {w} bits"})
            continue
        stored = dec(raw)
        if stored == f["actual"]:                    # rounding lands back on what's already there
            continue
        out.append({**f, "raw": raw, "stored": stored})
    return out


def apply_fixes(path, res, kinds=None, dry_run=True, log=print,
                jersey_on_wrong_team=False) -> dict:
    """Write the planned fixes into Roster.ROS. Returns the plan plus any sweater collisions.

    A .fixbak is made on the first write. Sweater numbers are checked AFTER the pass: pulling a
    number from NHL.com can collide with a team-mate the save still has on the old number, and that
    is a visible in-game fault, so it is reported rather than resolved by guesswork."""
    plan = plan_fixes(res, kinds, jersey_on_wrong_team=jersey_on_wrong_team)
    writes = [p for p in plan if "raw" in p]
    skipped = [p for p in plan if "skip" in p]

    t = PlayerTable(path)
    before = _sweater_clashes(t)                  # the save as it stands, for comparison
    for p in writes:                              # applied in memory either way, so a dry run can
        off, lo, w, _enc, _dec = FIXABLE[p["kind"]]   # report the state the pass WOULD produce
        _set_bits(t, p["row"], off, lo, w, p["raw"])
    after = _sweater_clashes(t)

    # Career stats ride along with every sync, deliberately without a switch. A roster is the men
    # AND what they have done; leaving 2009 careers under 2026 names is the same class of staleness
    # as leaving the old birth dates, and a separate button would just mean it never gets pressed.
    careers = sync_careers(t, res, log=log)
    # Contracts likewise: the money is part of who the man is now, and PuckPedia has it a club at a
    # time, so the whole league costs 32 cached page fetches.
    salaries = sync_salaries(t, res, log=log)

    if not dry_run and (writes or careers["seasons"] or salaries["written"]):
        bak = Path(str(path) + ".fixbak")
        if not bak.exists():
            bak.write_bytes(Path(path).read_bytes())
        t.save(backup=False)
        if log:
            log(f"wrote {len(writes)} field(s), {careers['seasons']} season line(s) for "
                f"{careers['players']} player(s) and {salaries['written']} contract(s) to {path} "
                f"(backup {bak.name})")

    return {"writes": writes, "skipped": skipped,
            "collisions": after, "collisions_before": before,
            "collisions_new": {k: v for k, v in after.items() if k not in before},
            "by_kind": {k: sum(1 for p in writes if p["kind"] == k) for k in FIXABLE},
            "careers": careers, "salaries": salaries}


def sync_careers(t: PlayerTable, res, log=print) -> dict:
    """Rewrite every matched player's career sheet from NHL.com, in place, on `t`'s buffer.

    Runs off the identities verify() already established, so a man is never matched twice by two
    slightly different matchers. Failures are per-player and never abort the pass: a network error
    or an exhausted stat pool costs that one sheet and is reported, because the alternative is
    losing an otherwise good roster fix to an unrelated timeout.

    Everything is fetched before anything is written, because the stat pool is a fixed 9,862 lines
    for the whole file and the league wants more than that. Knowing the full demand up front lets
    the pass trim every career by the same number of oldest seasons instead of handing 14 apiece to
    whoever happens to sort first and nothing at all to the men at the end of the alphabet."""
    ids = res.get("nhl_ids") or {}
    clubs = res.get("primary_clubs") or {}
    out = {"players": 0, "seasons": 0, "failed": [], "dropped": [], "truncated": 0,
           "cap": career_stats.MAX_SEASONS, "capacity": None}
    if not ids:
        return out

    table = career_stats.CareerTable(t.ros)       # same buffer, so this rides the one save

    # primary_clubs() speaks file OFFSETS (that is what the player's +0x14 pointer resolves to);
    # the stat table wants record indices. A club whose offset is not on a record boundary is
    # dropped from the map rather than guessed at, which turns into a dropped season, not a bad one.
    clubs = {abbr: idx for abbr, off in clubs.items()
             if (idx := table.team_index(off)) is not None}
    if log:
        missing = sorted(set(res.get("primary_clubs") or {}) - set(clubs))
        if missing:
            log(f"  careers: no team record for {', '.join(missing)} — those seasons drop")
    # -- pass 1: fetch, and reclaim the old sheet of everyone we are about to rewrite.
    feeds, done = {}, 0
    for row, pid in sorted(ids.items()):
        try:
            feeds[row] = career_stats.fetch_career(pid)
        except (urllib.error.URLError, OSError, ValueError) as e:
            out["failed"].append({"row": row, "id": pid, "error": str(e)})
            continue
        table.clear_career(row)                   # frees the lines before the budget is measured
        done += 1
        if log and done % 100 == 0:
            log(f"  careers … fetched {done}/{len(ids)}")

    # -- how many seasons each man actually wants, once unmappable clubs are struck out.
    want = {row: sum(1 for ln in c
                     if career_stats.TEAM_BY_NAME.get(ln["team"]) in clubs)
            for row, c in feeds.items()}
    need = sum(min(n, career_stats.MAX_SEASONS) for n in want.values())

    # -- harvest before capping. Roughly 4,400 lines sit on farm/free-agent/legacy rows that no
    # live NHL roster points at and that nobody is syncing; those sheets are stale whatever we do.
    # Trimming their oldest seasons is a far cheaper loss than docking every current player two
    # years off the career total the game prints.
    if need > len(table.free):
        donors = {r: len([i for i in table.slots(r) if i != career_stats.EMPTY])
                  for r in range(table.n_players) if r not in feeds}
        donors = {r: n for r, n in donors.items() if n}
        held = sum(donors.values())
        dcap = career_stats.fit_cap(donors.values(), max(0, held - (need - len(table.free))))
        freed = sum(table.trim(r, dcap) for r in donors)
        if log:
            log(f"  careers: {need} season(s) wanted — harvested {freed} slot(s) from "
                f"{len(donors)} unsynced row(s) (trimmed to {dcap} season(s) each)")

    cap = career_stats.fit_cap(want.values(), len(table.free))
    out["cap"] = cap
    if log and cap < career_stats.MAX_SEASONS:
        log(f"  careers: {sum(want.values())} season(s) wanted, {len(table.free)} pool slot(s) "
            f"free — capping every career at {cap} season(s)")

    # -- pass 2: write.
    for row, career in feeds.items():
        try:
            r = career_stats.sync_player(table, row, ids[row], clubs,
                                         career=career, max_seasons=cap)
        except (career_stats.CareerError, ValueError) as e:
            out["failed"].append({"row": row, "id": ids[row], "error": str(e)})
            continue
        out["players"] += 1
        out["seasons"] += r["seasons"]
        out["truncated"] += r["truncated"]
        out["dropped"].extend(r["dropped"])
    out["capacity"] = table.capacity()
    out["audit"] = table.audit()
    return out


def sync_salaries(t: PlayerTable, res, use_cache=True, log=print) -> dict:
    """Write every rostered player's CURRENT contract into the save, in place, on `t`'s buffer.

    Same rule as the career pass: it rides the roster fix with no switch of its own. A 2009 cap hit
    under a 2026 name is the same staleness as a 2009 birth date, and both fields already exist in
    the record — `(u32 @ 0x38 >> 3) & 0xFFFFFF` dollars and `u32 @ 0x39 & 7` years remaining.

    Matching is per CLUB, off PuckPedia's own roster page, so a namesake elsewhere in the league can
    never be picked up. Within a club a repeated name is settled by age — Vancouver's two Elias
    Petterssons are 27 and 22 — and if that still ties, the row is left alone rather than guessed at.
    A club whose page cannot be fetched costs that club only; the rest of the pass still lands."""
    out = {"players": 0, "written": 0, "unmatched": [], "failed": [], "season": None,
           "clamped": 0}
    rows = read_ros(t, log=None)
    active = [p for p in rows if is_nhl_club(p) and (p["last"] or "").strip()]
    primary = primary_clubs(active)
    active = [p for p in active if p["team_id"] == primary.get(ROS_TO_NHL[p["code"]])]

    by_club = {}
    for p in active:
        by_club.setdefault(ROS_TO_NHL[p["code"]], []).append(p)

    for abbr in sorted(by_club):
        try:
            feed = salary.fetch_team(abbr, use_cache=use_cache)
        except (urllib.error.URLError, OSError, salary.SalaryError, ValueError) as e:
            out["failed"].append({"team": abbr, "error": str(e)})
            continue
        out["season"] = out["season"] or feed["season"]
        idx = {}
        for slug, e in feed["players"].items():
            idx.setdefault(name_key(e["first"], e["last"]), []).append(e)
            idx.setdefault(loose_key(e["first"], e["last"]), []).append(e)

        for p in by_club[abbr]:
            cands = idx.get(name_key(p["first"], p["last"])) or \
                idx.get(loose_key(p["first"], p["last"])) or []
            if not cands:
                # Either the man is genuinely unsigned (an RFA with an all-$0 row, which parse_team
                # drops on purpose) or the name did not fold to the same key. Both leave the save's
                # existing contract alone — inventing $0 would read as a player playing for free.
                out["unmatched"].append({"row": p["row"], "team": abbr,
                                         "player": f"{p['first']} {p['last']}".strip()})
                continue
            if len(cands) > 1:
                born = int((p["birth"] or "0000")[:4])
                if not born or not feed["season"]:
                    continue                          # a tie with nothing to break it: leave it be
                cands = sorted(cands, key=lambda c: abs((c["age"] or 0)
                                                        - (feed["season"] - born)))
            e = cands[0]
            out["players"] += 1
            if e["cap_hit"] > salary.MAX_SALARY:      # 29 bits — $536M, so this never trips
                out["clamped"] += 1
            had = (_bits(t, p["row"], salary.SALARY_OFF, salary.SALARY_LO, salary.SALARY_W),
                   _bits(t, p["row"], salary.TERM_OFF, salary.TERM_LO, salary.TERM_W))
            want = (min(e["cap_hit"], salary.MAX_SALARY), min(e["years"], salary.MAX_TERM))
            if had == want:
                continue
            _set_bits(t, p["row"], salary.SALARY_OFF, salary.SALARY_LO, salary.SALARY_W, want[0])
            _set_bits(t, p["row"], salary.TERM_OFF, salary.TERM_LO, salary.TERM_W, want[1])
            out["written"] += 1
        if log:
            log(f"  salaries … {abbr} {len(by_club[abbr])} row(s)")
    return out


def _sweater_clashes(t: PlayerTable) -> dict:
    """{'BOS #73': [names]} for every sweater worn twice on one club's primary record."""
    rows = read_ros(t, log=None)
    active = [q for q in rows if is_nhl_club(q) and (q["last"] or "").strip()]
    primary = primary_clubs(active)
    clashes = {}
    for q in active:
        if q["team_id"] != primary[ROS_TO_NHL[q["code"]]]:
            continue
        clashes.setdefault((q["code"], q["jersey"]), []).append(f"{q['first']} {q['last']}".strip())
    return {f"{c} #{n}": names for (c, n), names in sorted(clashes.items()) if len(names) > 1}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    try:                                               # names carry accents; cp1252 consoles choke
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if not argv:
        print(__doc__)
        return 2
    path, out_json, only, use_cache = argv[0], None, None, True
    fix_kinds, apply, jersey_anyway = None, False, False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            i += 1; out_json = argv[i]
        elif a == "--team":
            i += 1; only = argv[i].upper().split(",")
        elif a == "--no-cache":
            use_cache = False
        elif a == "--fix":                     # --fix [HEIGHT,WEIGHT,…]; default is all four
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if nxt and not nxt.startswith("--"):
                i += 1; fix_kinds = nxt.upper().split(",")
            else:
                fix_kinds = list(FIXABLE)
        elif a == "--apply":
            apply = True
        elif a == "--jersey-anyway":            # write sweaters onto wrong-club rows too
            jersey_anyway = True
        i += 1
    res = verify(path, teams=only, use_cache=use_cache)
    print(format_report(res))
    if out_json:
        Path(out_json).write_text(json.dumps(res, indent=1), encoding="utf-8")
        print(f"\nwrote {out_json}")
    if fix_kinds:
        r = apply_fixes(path, res, kinds=fix_kinds, dry_run=not apply,
                        jersey_on_wrong_team=jersey_anyway)
        print(f"\n── FIX {'APPLIED' if apply else '(dry run)'} " + "─" * 30)
        for k, n in r["by_kind"].items():
            if k in fix_kinds:
                print(f"  {k:<8} {n}")
        why = {}
        for p in r["skipped"]:
            why.setdefault(p["skip"], []).append(f"[{p['team']}] {p['player']}")
        for reason, who in why.items():
            print(f"  SKIP {len(who):>4}  {reason}")
            for w in who[:5]:
                print(f"          {w}")
            if len(who) > 5:
                print(f"          … and {len(who) - 5} more")
        print(f"  sweater collisions: {len(r['collisions_before'])} before -> "
              f"{len(r['collisions'])} after ({len(r['collisions_new'])} new)")
        for k, names in list(r["collisions_new"].items())[:25]:
            print(f"    NEW  {k}: {', '.join(names)}")
        c = r["careers"]
        print(f"  careers: {c['seasons']} season line(s) for {c['players']} player(s), "
              f"{c['truncated']} truncated at {c['cap']}, {len(c['dropped'])} season(s) dropped "
              f"(club not in the game), {len(c['failed'])} failed, "
              f"{c['capacity']} pool slot(s) free")
        for f in c["failed"][:10]:
            print(f"    FAIL row {f['row']} id {f['id']}: {f['error']}")
        s = r["salaries"]
        print(f"  salaries: {s['written']} contract(s) updated of {s['players']} matched "
              f"(season {s['season']}), {len(s['unmatched'])} unmatched, "
              f"{len(s['failed'])} club(s) failed, {s['clamped']} over the ${salary.MAX_SALARY:,} "
              f"field ceiling")
        for u in s["unmatched"][:10]:
            print(f"    unsigned / no PuckPedia contract  [{u['team']}] {u['player']}")
        if len(s["unmatched"]) > 10:
            print(f"    … and {len(s['unmatched']) - 10} more")
        for f in s["failed"][:10]:
            print(f"    FAIL {f['team']}: {f['error']}")
        if not apply:
            print("  nothing written — re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
