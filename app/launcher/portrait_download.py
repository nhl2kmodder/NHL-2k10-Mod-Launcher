"""portrait_download.py — fetch official NHL headshots and reframe them to the game's portrait framing.

Lets the launcher replace a player's UI portrait (the shoulders-up headshot) with that player's real
current NHL mug, one player at a time or for the whole loaded roster at once. Motivation: after you
rename 2009 roster slots to current (e.g. Kesler -> Rossi), the faces are still the 2009 players; this
pulls the right face from NHL.com and drops it into the slot the player already points at.

Data source (public NHL endpoints, same ones the user's test.py used):
  search : https://search.d3.nhle.com/api/v1/search/player   (q, active, limit, culture)
  landing: https://api-web.nhle.com/v1/player/{id}/landing    (headshot URL, birthDate, seasonTotals)
  mug CDN: https://assets.nhle.com/mugs/nhl/{season}/{TEAM}/{playerId}.png   (season + team embedded)

The `active` search param is a hard FILTER, not a toggle (active=true returns only current players,
active=false only retired) — so we search active first, then retired, then fall back to a silhouette.

A missing mug is NOT a 404: the CDN serves a generic gray silhouette PNG (a fixed ~11.8 KB file). We
detect it by content hash and treat it as "no photo".

Reframe: NHL mugs are rigidly framed (transparent background, top-of-head ~16% down the frame, lots of
headroom + shoulders). The game's portraits are a tighter head crop over a gray studio backdrop. We
anchor on the mug's alpha silhouette (top-of-head row + head centre column) and apply a fixed
scale/offset so the head lands where the game expects it. Because both sources are rigidly framed this
aligns top-of-head / eyes / chin across players with no face-detection dependency (validated visually).

Everything here is pure logic (no tkinter). The GUI in nhl2k10_launcher.py drives it; the reframed PNG
is written to a temp file, handed to archive_textures.replace_portraits (which does the DXT4_5 encode
+ studio-backdrop composite), then deleted — no images are kept on disk.
"""
import hashlib
import io
import re
import struct
import unicodedata

import numpy as np
import requests
from PIL import Image, ImageFilter

SEARCH_URL = "https://search.d3.nhle.com/api/v1/search/player"
LANDING_URL = "https://api-web.nhle.com/v1/player/{}/landing"
MUG_URL = "https://assets.nhle.com/mugs/nhl/{season}/{team}/{pid}.png"

_TIMEOUT = 12
_UA = {"User-Agent": "Mozilla/5.0 (NHL2k10-ModLauncher portrait fetch)"}

OUT = 256                        # game portrait is 256x256

# ── reframe calibration (fractions of the frame) ────────────────────────────
# Measured from native NHL 2k10 portraits (head-top ~6% down, head fills ~2/3 of the frame) and the
# rigid NHL mug framing (head-top ~16% down, head spans ~46% of the mug). Validated across players.
_TARGET_HEAD_TOP = 0.055         # place the top of the hair this far down the 256 frame
_TARGET_HEAD_FRAC = 0.57         # top-of-hair -> chin should span this fraction of the 256 frame.
                                 # Lower value = smaller head, more neck/shoulder in frame (top-of-head
                                 # stays put since we anchor on it). 0.66 put the chin at the bottom edge
                                 # with no neck/shoulder; 0.57 shows the collar + shoulders like a card.
_MUG_HEAD_TOP_FRAC = 0.161       # NHL mug: top of hair this far down the mug
_MUG_CHIN_FRAC = 0.62            # NHL mug: chin roughly here (rigid framing)

# ── team name -> mug abbreviation (only needed for HISTORICAL season pulls; current pulls take the
# abbrev straight from landing.currentTeamAbbrev / the headshot URL). Covers the modern era the game
# is used for; anything unmapped falls back to a silhouette, which the caller handles gracefully. ──
_TEAM_ABBREV = {
    "anaheim ducks": "ANA", "mighty ducks of anaheim": "ANA",
    "arizona coyotes": "ARI", "phoenix coyotes": "PHX",
    "utah hockey club": "UTA", "utah mammoth": "UTA",
    "atlanta thrashers": "ATL",
    "boston bruins": "BOS", "buffalo sabres": "BUF", "calgary flames": "CGY",
    "carolina hurricanes": "CAR", "chicago blackhawks": "CHI", "colorado avalanche": "COL",
    "columbus blue jackets": "CBJ", "dallas stars": "DAL", "detroit red wings": "DET",
    "edmonton oilers": "EDM", "florida panthers": "FLA", "los angeles kings": "LAK",
    "minnesota wild": "MIN", "montreal canadiens": "MTL", "nashville predators": "NSH",
    "new jersey devils": "NJD", "new york islanders": "NYI", "new york rangers": "NYR",
    "ottawa senators": "OTT", "philadelphia flyers": "PHI", "pittsburgh penguins": "PIT",
    "san jose sharks": "SJS", "seattle kraken": "SEA", "st. louis blues": "STL",
    "st louis blues": "STL", "tampa bay lightning": "TBL", "toronto maple leafs": "TOR",
    "vancouver canucks": "VAN", "vegas golden knights": "VGK", "washington capitals": "WSH",
    "winnipeg jets": "WPG",
}

# The 32 current teams for the single-fetch "Jersey / team" picker: (mug abbrev, display name), by
# abbrev. A player can only be pulled in a jersey they actually wore (the CDN has no mug otherwise) —
# the picker still lists everyone so you can try, and an invalid combo comes back as a silhouette.
CURRENT_TEAMS = [
    ("ANA", "Anaheim Ducks"), ("BOS", "Boston Bruins"), ("BUF", "Buffalo Sabres"),
    ("CGY", "Calgary Flames"), ("CAR", "Carolina Hurricanes"), ("CHI", "Chicago Blackhawks"),
    ("COL", "Colorado Avalanche"), ("CBJ", "Columbus Blue Jackets"), ("DAL", "Dallas Stars"),
    ("DET", "Detroit Red Wings"), ("EDM", "Edmonton Oilers"), ("FLA", "Florida Panthers"),
    ("LAK", "Los Angeles Kings"), ("MIN", "Minnesota Wild"), ("MTL", "Montreal Canadiens"),
    ("NSH", "Nashville Predators"), ("NJD", "New Jersey Devils"), ("NYI", "New York Islanders"),
    ("NYR", "New York Rangers"), ("OTT", "Ottawa Senators"), ("PHI", "Philadelphia Flyers"),
    ("PIT", "Pittsburgh Penguins"), ("SJS", "San Jose Sharks"), ("SEA", "Seattle Kraken"),
    ("STL", "St. Louis Blues"), ("TBL", "Tampa Bay Lightning"), ("TOR", "Toronto Maple Leafs"),
    ("UTA", "Utah Mammoth"), ("VAN", "Vancouver Canucks"), ("VGK", "Vegas Golden Knights"),
    ("WSH", "Washington Capitals"), ("WPG", "Winnipeg Jets"),
]


# Season dropdown values (season_id, label), newest first. "current" is handled specially (uses the
# authoritative landing.headshot URL rather than guessing a season).
def season_list():
    out = [("current", "Current (latest)")]
    for y in range(2026, 2004, -1):
        out.append((f"{y}{y + 1}", f"{y}-{str(y + 1)[2:]}"))
    return out


_SESSION = None


def _session():
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update(_UA)
        try:
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            retry = Retry(total=2, backoff_factor=0.4,
                          status_forcelist=(429, 500, 502, 503, 504))
            s.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        except Exception:
            pass
        _SESSION = s
    return _SESSION


# ── name matching ───────────────────────────────────────────────────────────
def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _norm(s):
    """Loose comparison key: accent-free, lowercase, punctuation collapsed to spaces."""
    s = _strip_accents(s or "").lower()
    s = re.sub(r"[.\-'`]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _cand_name(p):
    if p.get("name"):
        return p["name"]
    fn, ln = p.get("firstName") or "", p.get("lastName") or ""
    if isinstance(fn, dict):
        fn = fn.get("default", "")
    if isinstance(ln, dict):
        ln = ln.get("default", "")
    return f"{fn} {ln}".strip()


def search(name, active, limit=40):
    """Raw NHL search. active is 'true'/'false'. Returns the JSON list (possibly empty)."""
    r = _session().get(SEARCH_URL, params={"culture": "en-us", "q": name,
                                           "limit": limit, "active": active}, timeout=_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    return j if isinstance(j, list) else []


def match(name):
    """Find exact full-name matches. Tries ACTIVE players first, then RETIRED. Returns
    {'candidates': [...], 'source': 'active'|'inactive'|None}. `candidates` are the raw search
    records whose full name equals `name` (accent/punctuation-insensitive); >1 means a duplicate
    name that needs disambiguation."""
    want = _norm(name)
    for active, tag in (("true", "active"), ("false", "inactive")):
        try:
            res = search(name, active)
        except Exception:
            res = []
        exact = [p for p in res if _norm(_cand_name(p)) == want]
        if exact:
            return {"candidates": exact, "source": tag}
    return {"candidates": [], "source": None}


_LANDING_CACHE = {}


def landing(pid):
    pid = str(pid)
    if pid not in _LANDING_CACHE:
        r = _session().get(LANDING_URL.format(pid), timeout=_TIMEOUT)
        r.raise_for_status()
        _LANDING_CACHE[pid] = r.json()
    return _LANDING_CACHE[pid]


def _dflt(v):
    return v.get("default", "") if isinstance(v, dict) else (v or "")


def team_abbrev(team_name):
    return _TEAM_ABBREV.get(_norm(team_name))


def player_id(cand):
    return str(cand.get("playerId") or cand.get("id") or "")


def describe(cand):
    """Short one-line disambiguation label for a search candidate."""
    nm = _cand_name(cand)
    bits = []
    pos = cand.get("positionCode")
    if pos:
        bits.append(pos)
    tm = cand.get("teamAbbrev") or cand.get("lastTeamAbbrev")
    if tm:
        bits.append(tm)
    city = cand.get("birthCity")
    if city:
        cc = cand.get("birthCountry") or ""
        bits.append(f"{city}{', ' + cc if cc else ''}")
    if cand.get("active") is False:
        bits.append("retired")
    return f"{nm}  ·  " + "  ·  ".join(str(b) for b in bits) if bits else nm


def team_history(land):
    """Distinct NHL teams the player wore, most-recent first: [(abbrev, teamName, season_id)].
    Drives the single-mode 'jersey' dropdown. Skips teams we can't map to a mug abbrev."""
    rows = [r for r in land.get("seasonTotals", [])
            if r.get("leagueAbbrev") == "NHL" and r.get("gameTypeId") == 2]
    rows.sort(key=lambda r: str(r.get("season", "")), reverse=True)
    seen, out = set(), []
    for r in rows:
        tn = _dflt(r.get("teamName"))
        ab = team_abbrev(tn)
        if not ab or ab in seen:
            continue
        seen.add(ab)
        out.append((ab, tn, str(r.get("season"))))
    return out


def season_team(land, season):
    """The player's team abbrev in a given season_id (e.g. '20132014'), or None if they didn't play
    an NHL regular season that year / the team can't be mapped."""
    for r in land.get("seasonTotals", []):
        if (r.get("leagueAbbrev") == "NHL" and r.get("gameTypeId") == 2
                and str(r.get("season")) == str(season)):
            return team_abbrev(_dflt(r.get("teamName")))
    return None


def most_recent_season(land, cand=None):
    """Most recent NHL regular-season id the player has (fallbacks to the search record / a default)."""
    seasons = [str(r.get("season")) for r in (land or {}).get("seasonTotals", [])
               if r.get("leagueAbbrev") == "NHL" and r.get("gameTypeId") == 2 and r.get("season")]
    if seasons:
        return max(seasons)
    return str((cand or {}).get("lastSeasonId") or "20262027")


# ── image download + silhouette detection ───────────────────────────────────
_SILHOUETTE_MD5 = None            # md5 of the CDN's generic "no photo" mug
_SILHOUETTE_RAW = False           # cached raw silhouette mug (False = not fetched yet, None = failed)


def _silhouette_md5():
    global _SILHOUETTE_MD5
    if _SILHOUETTE_MD5 is None:
        try:
            b = _session().get(MUG_URL.format(season="20262027", team="VAN", pid="9999999"),
                               timeout=_TIMEOUT).content
            _SILHOUETTE_MD5 = hashlib.md5(b).hexdigest()
        except Exception:
            _SILHOUETTE_MD5 = ""
    return _SILHOUETTE_MD5


def fetch_mug(url):
    """Download a mug URL. Returns (PIL RGBA image | None, is_silhouette: bool). is_silhouette True
    means the CDN returned its generic no-photo placeholder (treat as no real headshot)."""
    try:
        r = _session().get(url, timeout=_TIMEOUT)
    except Exception:
        return None, False
    if r.status_code != 200 or "image" not in r.headers.get("content-type", ""):
        return None, False
    data = r.content
    sil = (hashlib.md5(data).hexdigest() == _silhouette_md5()) or len(data) < 14000
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None, False
    return img, sil


def silhouette_placeholder(head_frac=None):
    """The reframed generic silhouette, used when a player has no NHL headshot. Raw mug cached;
    reframed on demand so it honours a per-fetch head_frac."""
    global _SILHOUETTE_RAW
    if _SILHOUETTE_RAW is False:
        img, _ = fetch_mug(MUG_URL.format(season="20262027", team="VAN", pid="9999999"))
        _SILHOUETTE_RAW = img            # PIL image, or None if the fetch failed
    if _SILHOUETTE_RAW is None:
        return Image.new("RGBA", (OUT, OUT), (0, 0, 0, 0))
    return reframe(_SILHOUETTE_RAW, head_frac)


# ── reframe (mug -> game framing) ───────────────────────────────────────────
def _head_metrics(im, thr=32):
    """(top_row, head_centre_x) from the alpha silhouette. Falls back to a background-colour diff
    for the rare fully-opaque source."""
    arr = np.array(im.convert("RGBA"))
    a = arr[:, :, 3]
    ys, xs = np.where(a > thr)
    if len(ys) == 0:                                  # opaque source: detect subject vs corner bg
        rgb = arr[:, :, :3].astype(int)
        bg = rgb[0:8, 0:8].reshape(-1, 3).mean(0)
        dist = np.sqrt(((rgb - bg) ** 2).sum(2))
        ys, xs = np.where(dist > 40)
        if len(ys) == 0:
            h, w = a.shape
            return 0, w / 2
    top = ys.min()
    band = top + int(0.35 * (ys.max() - top))
    hx = xs[ys <= band]
    cx = (hx.min() + hx.max()) / 2 if len(hx) else im.size[0] / 2
    return int(top), float(cx)


def reframe(mug, head_frac=None):
    """Reframe an NHL mug (any size, transparent bg) to a 256x256 RGBA aligned to the game's head
    box (top-of-head / eyes / chin land where native portraits have them). Transparent background
    is preserved; replace_portraits composites it over the slot's real studio backdrop.

    head_frac overrides `_TARGET_HEAD_FRAC` for this call (top-of-hair..chin as a fraction of the
    frame): smaller = smaller head + more neck/shoulder, top-of-head stays anchored. Lets the fetch
    dialog fine-tune head size per player."""
    mug = mug.convert("RGBA")
    W, H = mug.size
    frac = _TARGET_HEAD_FRAC if head_frac is None else float(head_frac)
    top, cx = _head_metrics(mug)
    head_h = max(1.0, (_MUG_CHIN_FRAC - _MUG_HEAD_TOP_FRAC) * H)
    scale = (frac * OUT) / head_h
    nW, nH = max(1, int(round(W * scale))), max(1, int(round(H * scale)))
    r = mug.resize((nW, nH), Image.LANCZOS)
    px = int(round(OUT / 2 - cx * scale))
    py = int(round(_TARGET_HEAD_TOP * OUT - top * scale))
    canvas = Image.new("RGBA", (OUT, OUT), (0, 0, 0, 0))
    canvas.alpha_composite(r, (px, py))
    return canvas


# ── jersey compositing: put a player's head/neck onto another team's jersey ──
# The NHL CDN only has a mug for team-seasons a player actually played. To show a player in a jersey
# they never wore, graft their head+neck onto a JERSEY TEMPLATE isolated from a reference player of the
# target team (fetched live from the team roster). Skin-keying leaves the neck-hole open so the target's
# own neck shows and the collar wraps around it. Quality < a real mug, so this is a FALLBACK only —
# used when no real mug exists for the wanted team. Cleaner would be a hand-built per-team PNG template
# with a transparent neck-hole; drop one in via set_team_template(team, img).
def _skin_mask(rgb):
    R = rgb[:, :, 0].astype(float); G = rgb[:, :, 1].astype(float); B = rgb[:, :, 2].astype(float)
    mx = rgb[:, :, :3].max(2); mn = rgb[:, :, :3].min(2)
    # ratio terms exclude saturated jersey colours (e.g. red G/R~0.2) that a plain RGB test calls skin
    return ((R > 60) & (G > 28) & (B > 15) & (R >= G - 4) & (G >= B - 4) & ((R - B) > 8)
            & ((mx - mn) > 6) & (G > 0.40 * R) & (B > 0.22 * R))


def _mask_img(boolmask, erode=0, dilate=0, blur=1.2):
    m = Image.fromarray((boolmask * 255).astype(np.uint8), "L")
    for _ in range(erode):
        m = m.filter(ImageFilter.MinFilter(3))
    for _ in range(dilate):
        m = m.filter(ImageFilter.MaxFilter(3))
    if blur:
        m = m.filter(ImageFilter.GaussianBlur(blur))
    return m


# ── ML face parser (optional) — clean head/neck segmentation ────────────────
# A BiSeNet/CelebAMask-HQ face-parsing model (launcher/data/face_parsing_resnet18.onnx, ~53MB) segments
# skin/hair/neck/clothing/etc. separately, so we can keep the head+neck and drop the jersey robustly —
# where the colour/geometry heuristic failed (short necks + high collars like McDavid, beards eating the
# neck like Matthews). Optional: if onnxruntime or the model is absent, _clean_head_neck falls back to
# the heuristic. Classes: 0 bg,1 skin,2-3 brow,4-5 eye,6 glasses,7-8 ear,9 earring,10 nose,11 mouth,
# 12-13 lip,14 neck,15 necklace,16 cloth,17 hair,18 hat.
_FACE_SESS = False                       # False = not yet tried, None = unavailable, else an ORT session
_FACE_MEAN = np.array([0.485, 0.456, 0.406], np.float32)
_FACE_STD = np.array([0.229, 0.224, 0.225], np.float32)
_HEADNECK_CLASSES = (1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 17, 18)   # drop bg/earring/necklace/cloth
_NECK_CLASS = 14


def _face_model_path():
    try:
        from . import resources
        return resources.data_path("face_parsing_resnet18.onnx")
    except Exception:
        import os
        return os.path.join(os.path.dirname(__file__), "data", "face_parsing_resnet18.onnx")


def _face_session():
    """The face-parsing ORT session, or None if onnxruntime / the model isn't available (cached)."""
    global _FACE_SESS
    if _FACE_SESS is False:
        _FACE_SESS = None
        try:
            import os
            import onnxruntime as ort
            p = str(_face_model_path())
            if os.path.exists(p):
                _FACE_SESS = ort.InferenceSession(p, providers=["CPUExecutionProvider"])
        except Exception:
            _FACE_SESS = None
    return _FACE_SESS


def _face_parse(mug, size=512):
    """Per-pixel face-part label map at the mug's resolution, or None if the parser is unavailable."""
    sess = _face_session()
    if sess is None:
        return None
    try:
        inp = sess.get_inputs()[0].name
        rgb = mug.convert("RGB").resize((size, size), Image.BILINEAR)
        x = ((np.asarray(rgb, np.float32) / 255.0 - _FACE_MEAN) / _FACE_STD).transpose(2, 0, 1)[None]
        lab = sess.run(None, {inp: x})[0][0].argmax(0).astype(np.uint8)
        return np.array(Image.fromarray(lab).resize(mug.size, Image.NEAREST))
    except Exception:
        return None


def _isolate_jersey(templ, y_neck=0.50):
    """Jersey fabric only (collar + shoulders), neck-hole/background/head removed → RGBA with an open
    neck so a grafted head's neck shows through."""
    arr = np.array(templ.convert("RGBA")); rgb = arr[:, :, :3]; a = arr[:, :, 3]
    H = a.shape[0]; yy = np.arange(H)[:, None]
    jersey = (a > 140) & (~_skin_mask(rgb)) & (yy >= y_neck * H)
    m = _mask_img(jersey, erode=1, dilate=2, blur=1.4)
    out = arr.copy(); out[:, :, 3] = np.minimum(a, np.array(m))
    return Image.fromarray(out, "RGBA")


def _row_widths(a, thr=32):
    """Silhouette width per row (max-min of opaque pixels)."""
    return np.array([(np.where(a[y] > thr)[0].max() - np.where(a[y] > thr)[0].min())
                     if (a[y] > thr).any() else 0 for y in range(a.shape[0])])


def _clean_head_neck(mug, margin=14):
    """Isolate the player's FULL head + FULL neck (jersey removed). Uses the ML face parser when it's
    available (robust across short necks / high collars / beards); otherwise falls back to a geometric
    heuristic. Returns (rgba, {nb, ...})."""
    lab = _face_parse(mug)
    if lab is not None:
        arr = np.array(mug.convert("RGBA"))
        mask = np.isin(lab, _HEADNECK_CLASSES) & (arr[:, :, 3] > 0)
        m = (Image.fromarray((mask * 255).astype(np.uint8), "L")
             .filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
             .filter(ImageFilter.GaussianBlur(1.0)))          # close specks, soften the edge
        out = arr.copy(); out[:, :, 3] = np.minimum(arr[:, :, 3], np.array(m))
        ny = np.where((lab == _NECK_CLASS).any(1))[0]
        nb = int(ny.max()) if len(ny) else int(0.75 * mug.size[1])
        return Image.fromarray(out, "RGBA"), {"nb": nb, "ml": True}
    # ── heuristic fallback (no onnxruntime / no model) ──────────────────────────
    arr = np.array(mug.convert("RGBA")); a = arr[:, :, 3]; rgb = arr[:, :, :3]
    H, W = a.shape
    prof = _row_widths(a)
    nz = np.where(prof > 0)[0]
    if len(nz) == 0:
        return mug.convert("RGBA"), {"cx": W / 2, "nb": int(0.75 * H), "neck_w": W, "neck_row": int(0.58 * H)}
    top = nz.min()
    lo, hi = int(0.50 * H), int(0.64 * H)
    neck_row = lo + int(np.argmin(prof[lo:hi])); neck_w = prof[neck_row]
    xs = np.where(a[neck_row] > 32)[0]; cx = (xs.min() + xs.max()) / 2
    skin = _skin_mask(rgb)
    head_half = prof[top:neck_row + 1].max() / 2 + margin
    half = neck_w / 2 + margin
    nb = neck_row                                       # neck bottom: descend while the column is skin
    for y in range(neck_row, int(0.86 * H)):
        seg = skin[y, max(0, int(cx - half)):int(cx + half)]
        if seg.size and seg.mean() > 0.4:
            nb = y
        else:
            break
    # jersey-coloured pixels: saturated + non-skin. Drop these in the lower head band so a high collar
    # (e.g. McDavid's orange) that rises above the neck row doesn't get kept as "head" — while dark hair
    # / beards (low saturation) stay.
    mx = rgb.max(2).astype(float); mn = rgb.min(2).astype(float)
    sat = (mx - mn) / (mx + 1e-3)
    jersey_like = (sat > 0.42) & (~skin)
    # also cap the head width to the FACE/hair (upper band) so a short-necked player's wide collar
    # (which sits at/above the detected neck row) isn't kept as "head".
    face_hi = int(0.28 * H); face_lo = int(0.46 * H)
    face_w = prof[face_hi:face_lo].max() if face_lo > face_hi else prof[top:neck_row + 1].max()
    head_half = min(head_half, face_w / 2 + margin)
    yy = np.arange(H)[:, None]; xx = np.arange(W)[None, :]
    head = (yy <= neck_row) & (np.abs(xx - cx) <= head_half) & ~(jersey_like & (yy > 0.45 * H))
    neck = (yy > neck_row) & (yy <= nb) & (np.abs(xx - cx) <= half) & skin    # skin only → no collar
    keep = (a > 0) & (head | neck)
    m = Image.fromarray((keep * 255).astype(np.uint8), "L")
    m = (m.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
          .filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(1.2)))   # close gaps, despeckle
    out = arr.copy(); out[:, :, 3] = np.minimum(a, np.array(m))
    return Image.fromarray(out, "RGBA"), {"cx": cx, "nb": nb, "neck_w": neck_w, "neck_row": neck_row}


_CLOTH_CLASS = 16                        # CelebAMask-HQ 'cloth' = the jersey


def _isolate_jersey_ml(mug):
    """Jersey = the ML 'cloth' class (clean, no skin-keying) with an open neck-hole. None if the parser
    isn't available."""
    lab = _face_parse(mug)
    if lab is None:
        return None
    arr = np.array(mug.convert("RGBA"))
    mask = (lab == _CLOTH_CLASS) & (arr[:, :, 3] > 0)
    m = (Image.fromarray((mask * 255).astype(np.uint8), "L")
         .filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.0)))
    out = arr.copy(); out[:, :, 3] = np.minimum(arr[:, :, 3], np.array(m))
    return Image.fromarray(out, "RGBA")


def _build_jersey_template(mug):
    """Isolated jersey (open neck-hole) to composite UNDER... on top of a head/neck: ML cloth class when
    the parser is available, else the skin-keyed heuristic."""
    return _isolate_jersey_ml(mug) or _isolate_jersey(mug)


def _fill_opening(head_rgba, jersey_rgba, iters=200, darken=0.9985):
    """Fill the jersey's WHOLE collar opening with the head/neck's own skin by iterative inpaint —
    spreads the neck down AND sideways into the flared shoulder area, so nothing gray shows through the
    collar once the jersey goes on top. Darkens with depth for a natural under-collar shadow."""
    arr = np.array(head_rgba.convert("RGBA")).astype(np.float32); H, W = arr.shape[:2]
    jop = np.array(jersey_rgba.convert("RGBA"))[:, :, 3] > 40
    open_mask = np.zeros((H, W), bool)
    for y in range(H):
        xs = np.where(jop[y])[0]
        if len(xs) < 2:
            continue
        l, r = xs.min(), xs.max(); open_mask[y, l:r] = ~jop[y, l:r]       # collar opening = hole in the cloth
    rgb = arr[:, :, :3].copy(); filled = arr[:, :, 3] > 150
    for _ in range(iters):
        empty = open_mask & ~filled
        if not empty.any():
            break
        acc = np.zeros_like(rgb); cnt = np.zeros((H, W), np.float32)
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            sf = np.roll(filled, (dy, dx), (0, 1)); sc = np.roll(rgb, (dy, dx), (0, 1))
            m = sf & empty; acc[m] += sc[m]; cnt[m] += 1
        nf = empty & (cnt > 0)
        rgb[nf] = (acc[nf] / cnt[nf][:, None]) * darken; filled[nf] = True
    synth = open_mask & (arr[:, :, 3] < 40)
    out = np.dstack([rgb, np.where(filled, 255, arr[:, :, 3]).astype(np.float32)])
    blur = np.array(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")
                    .filter(ImageFilter.GaussianBlur(1.4)), np.float32)
    out = np.where(synth[:, :, None], blur, out)             # soften only the synthesised skin
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


def composite_jersey(head_mug, jersey_templ):
    """Put head_mug's head+neck into a jersey — 3 layers for a seamless join: (1) the head/neck, (2) its
    neck skin inpainted into the jersey's collar opening (so the neck reads as continuing down under the
    collar), (3) the jersey ON TOP so its unaltered collar wraps over the neck — no gap, no distortion.
    Returns an unframed RGBA mug that reframe() crops to the game framing."""
    head, _info = _clean_head_neck(head_mug)
    jersey = jersey_templ.convert("RGBA").resize(head_mug.size)
    filled = _fill_opening(head, jersey)
    canvas = Image.new("RGBA", head_mug.size, (0, 0, 0, 0))
    canvas.alpha_composite(filled)          # head/neck + neck-fill (under)
    canvas.alpha_composite(jersey)          # jersey ON TOP — collar wraps the neck
    return canvas


_TEAM_TEMPLATE = {}                      # team abbrev -> isolated jersey RGBA (cached; None = failed)


def set_team_template(team, img):
    """Override a team's jersey template with a hand-built PNG (RGBA, open neck-hole, rigidly framed
    like an NHL mug). Bypasses the live-roster auto-template."""
    _TEAM_TEMPLATE[team.upper()] = _build_jersey_template(img) if img is not None else None


def team_jersey_template(team):
    """Isolated jersey (open neck-hole) for a team, from a live reference player's current mug. Cached;
    None if it can't be built."""
    team = team.upper()
    if team in _TEAM_TEMPLATE:
        return _TEAM_TEMPLATE[team]
    tmpl = None
    try:
        r = _session().get(f"https://api-web.nhle.com/v1/roster/{team}/current", timeout=_TIMEOUT)
        r.raise_for_status()
        j = r.json()
        for grp in ("forwards", "defensemen"):            # skip goalies (different framing/pads)
            for p in j.get(grp, []):
                url = p.get("headshot")
                if not url:
                    continue
                img, sil = fetch_mug(url)
                if img is not None and not sil:
                    tmpl = _build_jersey_template(img)
                    break
            if tmpl is not None:
                break
    except Exception:
        tmpl = None
    _TEAM_TEMPLATE[team] = tmpl
    return tmpl


# ── high-level: name (+ options) -> reframed 256x256 RGBA + metadata ────────
def resolve_image(name, season="current", cand=None, want_team=None, head_frac=None, composite=True):
    """Produce the reframed portrait image for a player name.

    Returns a dict:
      status : 'matched' | 'composited' | 'ambiguous' | 'silhouette'
      image  : 256x256 RGBA (the reframed mug, or the silhouette placeholder)
      candidates : the exact search matches (for the caller to disambiguate when >1)
      source : 'active' | 'inactive' | None
      team, season : what jersey/season the mug came from (for the report)
      chosen : the selected candidate record (or None)

    `cand` (a specific search record) skips the search — used after the caller disambiguates a
    duplicate. `want_team` (a mug abbrev) forces that team's jersey: a real mug if the player wore it,
    else (composite=True) their head/neck grafted onto that team's jersey template ('composited').
    `season` other than 'current' forces that season's jersey. `head_frac` overrides the head size.
    """
    src = None
    if cand is None:
        m = match(name)
        cands, src = m["candidates"], m["source"]
        if len(cands) > 1:
            return {"status": "ambiguous", "candidates": cands, "source": src,
                    "image": None, "team": None, "season": None, "chosen": None}
        if not cands:
            return {"status": "silhouette", "candidates": [], "source": None,
                    "image": silhouette_placeholder(head_frac), "team": None, "season": None,
                    "chosen": None}
        cand = cands[0]

    pid = player_id(cand)
    url = team = season_used = None
    try:
        land = landing(pid)
    except Exception:
        land = None

    force_composite = False
    if want_team and land is not None:                # force a specific team's jersey
        cur_team = land.get("currentTeamAbbrev")
        if want_team == cur_team and land.get("headshot"):
            url = land["headshot"]; team = cur_team    # current team = the authoritative real mug (best)
            m = re.search(r"/mugs/nhl/(\d{8})/", url); season_used = m.group(1) if m else "current"
        else:
            # ANY other team → ML composite (skip real-mug paths). A historical "real" mug for a past
            # team is unreliable: a mid-season-traded player's mug for that team-season is often the OTHER
            # team's (e.g. Luongo 2013-14/VAN is his post-trade FLA mug). The ML composite is consistent.
            force_composite = True
    if not force_composite:
        if url is None and season not in ("current", None) and land is not None:
            ab = season_team(land, season)           # force a specific season's jersey
            if ab:
                url = MUG_URL.format(season=season, team=ab, pid=pid)
                team, season_used = ab, season
        if url is None:                              # default: authoritative current mug
            if land is not None and land.get("headshot"):
                url = land["headshot"]
                team = land.get("currentTeamAbbrev")
                m = re.search(r"/mugs/nhl/(\d{8})/", url)
                season_used = m.group(1) if m else "current"
            else:                                    # no landing: build from the search record
                tm = cand.get("teamAbbrev") or cand.get("lastTeamAbbrev")
                ssn = cand.get("lastSeasonId") or "20262027"
                if tm:
                    url = MUG_URL.format(season=ssn, team=tm, pid=pid)
                    team, season_used = tm, ssn

    img, sil = (None, False)
    if url:
        img, sil = fetch_mug(url)

    # Jersey compositing: the wanted team isn't the player's current one (force_composite), or no real
    # mug came back — graft their head/neck onto that team's jersey template.
    if want_team and composite and (force_composite or img is None or sil):
        head_src = None
        if land is not None and land.get("headshot"):
            hs, hsil = fetch_mug(land["headshot"])
            head_src = hs if (hs is not None and not hsil) else None
        tmpl = team_jersey_template(want_team)
        if head_src is not None and tmpl is not None:
            comp = composite_jersey(head_src, tmpl)
            return {"status": "composited", "candidates": [cand], "source": src,
                    "image": reframe(comp, head_frac), "team": want_team, "season": "composite",
                    "chosen": cand}

    if img is None or sil:
        return {"status": "silhouette", "candidates": [cand], "source": src,
                "image": silhouette_placeholder(head_frac), "team": team, "season": season_used,
                "chosen": cand}
    return {"status": "matched", "candidates": [cand], "source": src,
            "image": reframe(img, head_frac), "team": team, "season": season_used, "chosen": cand}
