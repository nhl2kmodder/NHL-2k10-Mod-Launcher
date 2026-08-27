"""ice_fetch.py — auto-grab the newest full-rink sheet per team from thefaceoff.net.

https://www.thefaceoff.net/ice-nhl is a Wix gallery, newest sheets first, paginated
(?comp-mb1jb5iy_page=N, ~8 rink records per page). Each record's ``"1Rink"`` field is a
wix:image URI whose media id serves the ORIGINAL upload — the 5000x2236 full-rink view
ice_convert expects — at https://static.wixstatic.com/media/<id>. The record's ``title``
names it: exactly ``"<Team> <year>"`` for the regular-season sheet, or with a suffix
("... Playoffs", "... Final", "... Preseason", "... Practice Rink", "... (2nd)") for the
variants we do NOT want.

So the grab is: walk pages, keep the highest-year REGULAR record per wanted team, and
stop once every wanted team has one and a couple of pages go by without a better year
(ordering is only roughly newest-first — practice rinks and specials interleave). A team
missing from the newest year is picked up from the year before automatically, which is
exactly the "2027, else 2026, else..." fallback.

Team names on the site are TODAY'S league — the 2k10 archives still file relocated
teams under their donor codes, so Winnipeg maps to atl and Utah to pho.

Pure scraping, no UI, no game-file access: the tab (ice_convert_gui) owns those.
"""
from __future__ import annotations

import re
import urllib.request
from io import BytesIO

from PIL import Image

PAGE_URL = "https://www.thefaceoff.net/ice-nhl?comp-mb1jb5iy_page={n}"
MEDIA_URL = "https://static.wixstatic.com/media/{mid}"
_UA = {"User-Agent": "Mozilla/5.0"}

# 2k10 ice asset code -> the team's name on thefaceoff.net (today's league).
SITE_TEAM = {
    "ana": "Anaheim Ducks",          "atl": "Winnipeg Jets",       # ATL -> WPG relocation
    "bos": "Boston Bruins",          "buf": "Buffalo Sabres",
    "car": "Carolina Hurricanes",    "cbj": "Columbus Blue Jackets",
    "cgy": "Calgary Flames",         "chi": "Chicago Blackhawks",
    "col": "Colorado Avalanche",     "dal": "Dallas Stars",
    "det": "Detroit Red Wings",      "edm": "Edmonton Oilers",
    "fla": "Florida Panthers",       "lak": "Los Angeles Kings",
    "min": "Minnesota Wild",         "mtl": "Montreal Canadiens",
    "njd": "New Jersey Devils",      "nsh": "Nashville Predators",
    "nyi": "New York Islanders",     "nyr": "New York Rangers",
    "ott": "Ottawa Senators",        "phi": "Philadelphia Flyers",
    "pho": "Utah Mammoth",           # PHO -> UTA relocation
    "pit": "Pittsburgh Penguins",    "sea": "Seattle Kraken",
    "sjs": "San Jose Sharks",        "stl": "St. Louis Blues",
    "tbl": "Tampa Bay Lightning",    "tor": "Toronto Maple Leafs",
    "van": "Vancouver Canucks",      "vgk": "Vegas Golden Knights",
    "wsh": "Washington Capitals",
}

# a record: "1Rink":"wix:image:\/\/v1\/<mediaid>\/<file>#..." ... "title":"<Team> <year>"
_REC = re.compile(
    r'"1Rink":"wix:image://v1/([0-9a-f_]+~mv2\.\w+)/[^"#]*#[^"]*"'
    r'.{0,2500}?"title":"([^"]+)"', re.S)
_REGULAR = re.compile(r'^(.+) (\d{4})$')     # exact "<Team> <year>": the regular sheet


def _fetch_page(n: int, timeout: float = 30) -> str:
    req = urllib.request.Request(PAGE_URL.format(n=n), headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_page(html: str) -> tuple[list[tuple[str, int, str]], int]:
    """([(team site-name, year, media url)] for REGULAR-season records, total record
    count). The total tells an all-playoffs page apart from the end of the gallery."""
    out, total = [], 0
    for mid, title in _REC.findall(html.replace("\\/", "/")):
        total += 1
        m = _REGULAR.match(title)
        if m:
            out.append((m.group(1), int(m.group(2)), MEDIA_URL.format(mid=mid)))
    return out, total


def find_sheets(codes, log=lambda s: None, max_pages: int = 60) -> dict[str, dict]:
    """{code: {"year": int, "team": site name, "url": str}} — newest regular sheet per
    wanted team. Walks pages until every team is found and 2 pages bring no better year
    (the gallery is only roughly newest-first)."""
    by_name = {SITE_TEAM[c]: c for c in codes if c in SITE_TEAM}
    best: dict[str, dict] = {}
    stale = 0
    for n in range(1, max_pages + 1):
        try:
            recs, total = parse_page(_fetch_page(n))
        except Exception as e:
            log(f"[ice] page {n}: {e} — stopping the walk")
            break
        improved = False
        for name, year, url in recs:
            code = by_name.get(name)
            if code and year > best.get(code, {}).get("year", 0):
                best[code] = {"year": year, "team": name, "url": url}
                improved = True
        log(f"[ice] page {n}: {len(recs)}/{total} regular sheets, "
            f"{len(best)}/{len(by_name)} teams found")
        if total == 0:
            break                        # ran off the end of the gallery
        stale = 0 if improved else stale + 1
        if len(best) == len(by_name) and stale >= 2:
            break
    return best


def fetch_image(url: str, timeout: float = 60) -> Image.Image:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return Image.open(BytesIO(r.read())).convert("RGB")
