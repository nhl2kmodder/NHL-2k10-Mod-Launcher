"""salary.py — pull current NHL contracts from PuckPedia and write them into Roster.ROS.

The save stores a contract in two bit fields of the player record, both already mapped by the live
editor (`ros_live_editor._FIELDS`):

    Salary ($)      (u32 @ 0x38 >> 3) & 0x1FFFFFFF   dollars, exact — Boeser reads 2,633,000
    Contract (yrs)   u32 @ 0x39       & 0x7          years REMAINING, so 0..7

The salary field is **29 bits**, ceiling $536,870,911 — not the 24 the live editor assumed. Bits
31..27 are zero on every record of every roster only because nobody earned $16.8M in 2009, and the
XEX contains no 24-bit extract for the word (a plain `srwi 3` is what 29 bits compiles to).
CONFIRMED IN GAME: Carlsson written at $18,000,000 and Kaprizov at $17,000,000 both display
correctly, where a 24-bit read would have shown $1.22M and $0.22M.

The term really is narrow: 3 bits caps it at 7 seasons, one short of the 8-year maximum a club can
give its own player. A longer deal is clamped to 7 rather than wrapped to 0.

ONE page per club is enough — `puckpedia.com/team/<slug>`, 32 requests for the whole league. Each
roster row carries everything the save needs:

  * `data-extract_ch="7,250,000"` on each season cell — the EXACT cap hit in dollars, first cell =
    the current season. No rounding, unlike the "$7.25M" text beside it.
  * The grid is five seasons wide, so **the number of non-zero cells is the term remaining**, up to
    five. A deal that fills all five is longer than the grid can show, and only then does the row's
    last cell spell it out: `<span class="pp-ufa pp-contract-expiry-wide">UFA</span><div>2032</div>`
    — end year minus the current season start. Boeser reads 5 cells + 2032, i.e. 6 years left,
    which is the "year 2 of 7" the site shows on his own page.
  * The `/player/<slug>` link, the printed name, and his age — the age is what tells two men with
    the same name apart (Vancouver dresses two Elias Petterssons, 26 and 21).

Rows are matched to the save by club, so a namesake on another team can never be picked up.

PuckPedia's robots.txt explicitly allows real-time assistants fetching on a user's behalf; requests
are cached on disk for a week so a re-sync is free, and paced so a full league sweep is polite.
"""
from __future__ import annotations

import html as html_mod
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "https://puckpedia.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) nhl2k10-launcher"
CACHE_TTL = 7 * 24 * 3600
THROTTLE = 1.5                        # seconds between requests — a faster sweep earns 403s

SALARY_OFF, SALARY_LO, SALARY_W = 0x38, 3, 29
TERM_OFF, TERM_LO, TERM_W = 0x39, 0, 3
MAX_SALARY = (1 << SALARY_W) - 1
MAX_TERM = (1 << TERM_W) - 1

TEAM_SLUG = {
    "ANA": "anaheim-ducks", "BOS": "boston-bruins", "BUF": "buffalo-sabres",
    "CGY": "calgary-flames", "CAR": "carolina-hurricanes", "CHI": "chicago-blackhawks",
    "COL": "colorado-avalanche", "CBJ": "columbus-blue-jackets", "DAL": "dallas-stars",
    "DET": "detroit-red-wings", "EDM": "edmonton-oilers", "FLA": "florida-panthers",
    "LAK": "los-angeles-kings", "MIN": "minnesota-wild", "MTL": "montreal-canadiens",
    "NSH": "nashville-predators", "NJD": "new-jersey-devils", "NYI": "new-york-islanders",
    "NYR": "new-york-rangers", "OTT": "ottawa-senators", "PHI": "philadelphia-flyers",
    "PIT": "pittsburgh-penguins", "SJS": "san-jose-sharks", "SEA": "seattle-kraken",
    "STL": "st-louis-blues", "TBL": "tampa-bay-lightning", "TOR": "toronto-maple-leafs",
    "UTA": "utah-mammoth", "VAN": "vancouver-canucks", "VGK": "vegas-golden-knights",
    "WSH": "washington-capitals", "WPG": "winnipeg-jets",
}


class SalaryError(RuntimeError):
    pass


# ── fetching ────────────────────────────────────────────────────────────────
def _cache_dir() -> Path:
    d = Path.home() / ".nhl2k10_launcher" / "puckpedia_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


_last = [0.0]


def _get(url: str, use_cache=True, timeout=30) -> str:
    key = _cache_dir() / (re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_") + ".html")
    if use_cache and key.is_file() and time.time() - key.stat().st_mtime < CACHE_TTL:
        return key.read_text(encoding="utf-8", errors="replace")
    wait = THROTTLE - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(5):                  # a burst earns a 403/429; backing off clears it
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                    import gzip
                    raw = gzip.decompress(raw)
            break
        except urllib.error.HTTPError as e:
            if e.code not in (403, 429, 502, 503) or attempt == 4:
                raise
            time.sleep(5 * 2 ** attempt)      # 5, 10, 20, 40 s — the block is time-based, not fatal
    _last[0] = time.time()
    html = raw.decode("utf-8", errors="replace")
    key.write_text(html, encoding="utf-8")
    return html


_ROW_NAME = re.compile(r'href="/player/([a-z0-9\-]+)"[^>]*>([^<]+)</a>')
_ROW_AGE = re.compile(r">age</span><span[^>]*>(\d+)<")
_ROW_CH = re.compile(r'data-extract_ch="([\d,]+)"')
_ROW_EXP = re.compile(r"pp-contract-expiry[^>]*>\s*([A-Z]{2,4})\s*</span>\s*<div[^>]*>\s*(\d{4})")
_SEASON = re.compile(r"\b(20\d\d)-\d\d\b")


def parse_team(html: str) -> dict:
    """{'season': 2026, 'players': {slug: {name, first, last, age, cap_hit, years, expiry}}}."""
    season = _SEASON.search(html)
    season = int(season.group(1)) if season else 0
    players = {}
    for row in re.split(r"<tr\b", html):
        if "data-extract_ch" not in row:              # tooltips carry /player/ links but no money
            continue
        who = _ROW_NAME.search(row)
        if not who or who.group(1) in players:
            continue
        cells = [int(c.replace(",", "")) for c in _ROW_CH.findall(row)]
        paid = [c for c in cells if c]
        if not paid:
            continue                                  # unsigned pick — not a man on a $0 contract
        years = sum(1 for c in cells if c)
        exp = _ROW_EXP.search(row)
        if years >= len(cells) - 1 and exp and season:
            # The grid ran out before the contract did; the expiry cell is the only honest figure.
            years = max(years, int(exp.group(2)) - season)
        name = html_mod.unescape(who.group(2)).strip()
        last, _, first = name.partition(",")
        age = _ROW_AGE.search(row)
        players[who.group(1)] = {
            "name": name, "last": last.strip(), "first": first.strip(),
            "age": int(age.group(1)) if age else None,
            "cap_hit": paid[0], "years": min(years, MAX_TERM),
            "expiry": int(exp.group(2)) if exp else None,
            "status": exp.group(1) if exp else None,
        }
    return {"season": season, "players": players}


def fetch_team(abbr: str, use_cache=True) -> dict:
    """parse_team() for one club, by NHL abbreviation."""
    slug = TEAM_SLUG.get(abbr.upper())
    if not slug:
        raise SalaryError(f"no PuckPedia slug for {abbr}")
    res = parse_team(_get(f"{BASE}/team/{slug}", use_cache=use_cache))
    if not res["players"]:
        raise SalaryError(f"no contract rows parsed for {abbr} — page layout changed?")
    return res


# ── the record fields ───────────────────────────────────────────────────────
def _get_bits(data, off, lo, w):
    return (int.from_bytes(data[off:off + 4], "big") >> lo) & ((1 << w) - 1)


def _set_bits(data, off, lo, w, val):
    cur = int.from_bytes(data[off:off + 4], "big")
    mask = ((1 << w) - 1) << lo
    data[off:off + 4] = (((cur & ~mask) | ((val << lo) & mask)) & 0xFFFFFFFF).to_bytes(4, "big")


def read_contract(data, rec_off) -> dict:
    return {"salary": _get_bits(data, rec_off + SALARY_OFF, SALARY_LO, SALARY_W),
            "years": _get_bits(data, rec_off + TERM_OFF, TERM_LO, TERM_W)}


def write_contract(data, rec_off, salary: int, years: int) -> None:
    _set_bits(data, rec_off + SALARY_OFF, SALARY_LO, SALARY_W, min(max(salary, 0), MAX_SALARY))
    _set_bits(data, rec_off + TERM_OFF, TERM_LO, TERM_W, min(max(years, 0), MAX_TERM))
