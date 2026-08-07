"""extra_teams.py — surface EXPANSION teams' assets in the launcher like any shipping team.

`data/team_iff_catalog.csv` is a frozen 30-team list, so a team added to Roster.ROS after it was
built (Seattle, Vegas — see `project_expansion_teams`) owns a full family of `.iff` assets that the
IFF tab simply never lists. This module closes that gap by deriving the extra teams from the roster
itself instead of from the shipped CSV: every league-0 team record whose **asset key** isn't already
catalogued gets its 15-row family synthesised and resolved against the LIVE archive TOC.

Keying on the asset key (record `+0xB8`, `team_order` frame) rather than the display code is what
makes relocations behave: Winnipeg's code is `WPG` but its asset key is still `ATL`, and Utah's is
still `PHO`, so both are already catalogued and correctly produce no extra rows. Only a genuinely
novel key — one with its own `logo_<key>.iff` and uniforms — shows up here.

## The alias hazard, and why we de-alias EAGERLY

A new asset key is made loadable by adding TOC entries that may point at the DONOR team's bytes
(see `project_asset_key_aliasing` — "a whole family costs zero copied bytes"). That is fine until
somebody edits one: a same-size, in-place replace writes at the shared offset, so **editing Vegas's
rink would edit Anaheim's rink**. `_relocate` only de-aliases as a side effect of a *grow*, which
is not the path a same-size edit takes.

Rather than guard all thirteen `replace*` entry points — one missed path silently corrupts a
shipping team — `dealias()` gives every shared member its own physical copy in the 1B tail up
front, once. After that the asset is ordinary and every existing code path is already correct.
Seattle was built this way by hand and behaves exactly like a stock team, which is the proof.
"""
from __future__ import annotations

import csv
import hashlib
import struct
import zlib
from pathlib import Path

import archive_textures as AT
import team_order as TO

# The 15 assets a team owns, as `team_iff_catalog.csv` spells them. `_alt` never resolves for a
# clone (Anaheim has no alternate) but is listed so the family matches a stock team's row set.
FAMILY = [
    ("logo",                 "logo_%s.iff"),
    ("uniform_base_home",    "uniform_base_%s_home.iff"),
    ("uniform_base_away",    "uniform_base_%s_away.iff"),
    ("uniform_base_alt",     "uniform_base_%s_alt.iff"),
    ("uniform_overlay_home", "uniform_%s_home.iff"),
    ("uniform_overlay_away", "uniform_%s_away.iff"),
    ("uniform_overlay_alt",  "uniform_%s_alt.iff"),
    ("ice",                  "ice_%s_playoffs.iff"),
    ("ice",                  "ice_%s_finals.iff"),
    ("rink",                 "rink_%s.iff"),
    ("led",                  "led_%s.iff"),
    ("arena_presentation",   "arena_presentation_%s.iff"),
    ("arena",                "arena_%s.iff"),
    ("zamboni",              "zamboni_%s.iff"),
    ("zamboni_team",         "zamboni_team_%s.iff"),
    ("uniform_frontend_home", "uniform_frontend_%s_home.iff"),
    ("uniform_frontend_away", "uniform_frontend_%s_away.iff"),
    ("uniform_frontend_alt",  "uniform_frontend_%s_alt.iff"),
]

# The front-end (team-select) jersey is its OWN asset, not a re-render of the in-game uniform.
# `Function_83C94298` composes it as `uniform_frontend_<uniform asset key><slot>.iff` — the same
# name-keyed lookup the logo uses — so a novel asset key with no such entry draws nothing at all,
# which is exactly the blank jersey panel expansion teams showed. Slot suffixes come from
# `Uniform_GetSlot`: 0 `_home`, 1 `_away`, 2 `_alt`, else `_mii`.
FRONTEND_SLOTS = ("home", "away", "alt")

OFF_LEAGUE = 0x110          # team_order frame; league = high nibble, division = next


# ── roster side ──────────────────────────────────────────────────────────────────

def league0_teams(ros_path) -> list:
    """Every NHL-league team record: [{rec, code, akey, city, nick, division}], in record order."""
    d = bytearray(Path(ros_path).read_bytes())
    base, cnt = TO.find_table(d)
    out = []
    for i in range(cnt):
        r = base + i * TO.STRIDE
        lg = struct.unpack_from(">I", d, r + OFF_LEAGUE)[0]
        if (lg >> 28) != 0:
            continue
        akey = TO._text(d, r + TO.OFF_AKEY).strip()
        if not akey:
            continue
        out.append({"rec": i, "code": TO._text(d, r + TO.OFF_CODE).strip(),
                    "akey": akey, "city": TO._text(d, r + TO.OFF_CITY).strip(),
                    "nick": TO._text(d, r + TO.OFF_NICK).strip(),
                    "division": (lg >> 24) & 0xF})
    return out


def _catalogued_keys() -> set:
    """Lowercase asset keys the SHIPPED catalog already covers (from its `logo_<key>.iff` rows).

    Reads the CSV directly rather than `AT.load_catalog()` on purpose: load_catalog now splices in
    whatever this module last discovered, so going through it would make a second discovery pass
    see its own output as "already known" and return nothing.
    """
    keys = set()
    try:
        with open(AT.CATALOG_CSV, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                iff = (r.get("iff") or "")
                if r.get("category") == "logo" and iff.startswith("logo_") and iff.endswith(".iff"):
                    keys.add(iff[5:-4].lower())
    except OSError:
        pass
    return keys


def extra_teams(ros_path) -> list:
    """League-0 teams whose asset key the shipped catalog doesn't know — i.e. expansion teams.

    Relocated teams are correctly EXCLUDED: WPG still keys on `ATL` and UTA on `PHO`, both of
    which are catalogued, so they keep using the rows they already have.
    """
    known = _catalogued_keys()
    return [t for t in league0_teams(ros_path) if t["akey"].lower() not in known]


# ── catalog side ─────────────────────────────────────────────────────────────────

def catalog_rows(game_dir, ros_path) -> list:
    """Synthesised `team_iff_catalog.csv`-shaped rows for every extra team, resolved against the
    live TOC. Unresolvable members are dropped rather than listed as broken rows.

    `load_catalog` derives its friendly labels from `team` + `category`, so these rows get the same
    "SEA rink" / "SEA arena (bowl, seats, signage)" treatment as a stock team with no extra work.
    """
    game_dir = Path(game_dir)
    rows = []
    for t in extra_teams(ros_path):
        code = t["akey"].lower()
        for cat, pat in FAMILY:
            name = pat % code
            try:
                loc = AT.resolve(name, game_dir)
            except Exception:
                loc = None
            if not loc:
                continue
            arc, off, size, _idx, _f3 = loc
            rows.append({"team": code, "category": cat, "iff": name, "archive": arc,
                         "offset": "0x%X" % off, "size": "0x%X" % size, "resolved": "1"})
    return rows


def frontend_pairs(rows) -> list:
    """[(in-game uniform, front-end sheet)] for the rows `catalog_rows` produced.

    `data/jersey_map.json` was built by hashing the shipped art, so it knows nothing about a
    team added afterwards — which is why Seattle and Vegas showed their team-select sheet as a
    separate row while every shipping team shows one. The pairing needs no hashing here: an
    expansion team's assets are named, so `uniform_<key>_<slot>` pairs with
    `uniform_frontend_<key>_<slot>` by name. Feed the result to `jerseys.set_extra_jerseys`.
    """
    by = {}
    for r in rows:
        cat = r.get("category") or ""
        if cat.startswith("uniform_overlay_") or cat.startswith("uniform_frontend_"):
            slot = cat.rsplit("_", 1)[1]
            by.setdefault((r["team"], slot), {})[
                "sheet" if cat.startswith("uniform_frontend_") else "uni"] = r["iff"]
    return [(v["uni"], v["sheet"]) for v in by.values() if "uni" in v and "sheet" in v]


def relocated_aliases(game_dir, ros_path) -> list:
    """[(alias uniform, the mapped uniform it is a second name for)] for RELOCATED teams.

    A relocation keeps the old asset key: Winnipeg plays as `WPG` but its uniform is still keyed
    `ATL`, and the launcher gives the new code its own TOC entries pointing at the SAME blob. So
    `uniform_wpg_home.iff` and `uniform_atl_home.iff` are two names for one set of bytes, and
    editing either one changes the in-game jersey.

    The front-end sheet is a separate asset, and `jersey_map.json` — built by hashing the shipped
    art, long before the relocation — maps it to the `atl` name only. Edit the jersey under the
    `wpg` row and the mirror finds no twin, so the sheet keeps the Thrashers crest while the ice
    shows the Jets: the "old team's logo on the team-select render" bug. Pairing the alias with the
    real name's sheets fixes it for every relocation, without naming any team here.

    Only names resolving to the SAME archive and offset are paired — a code that has grown its own
    physical copy is a genuinely separate jersey and is left alone.
    """
    game_dir, out = Path(game_dir), []
    for t in league0_teams(ros_path):
        code, akey = (t["code"] or "").lower(), (t["akey"] or "").lower()
        if not code or not akey or code == akey:
            continue
        for slot in FRONTEND_SLOTS:
            alias, real = f"uniform_{code}_{slot}.iff", f"uniform_{akey}_{slot}.iff"
            la, lr = AT.resolve(alias, game_dir), AT.resolve(real, game_dir)
            if la and lr and la[0] == lr[0] and la[1] == lr[1]:
                out.append((alias, real))
    return out


# ── de-aliasing ──────────────────────────────────────────────────────────────────

def _shared_f3(game_dir) -> dict:
    """f3 (offset / 0x800) -> how many TOC entries point at it. >1 means the blob is shared."""
    d = (Path(game_dir) / "0A").read_bytes()
    count = struct.unpack_from(">I", d, 0x10)[0]
    seen: dict = {}
    for i in range(count):
        f3 = struct.unpack_from(">I", d, 0x58 + i * 16 + 12)[0]
        seen[f3] = seen.get(f3, 0) + 1
    return seen


def aliased_members(game_dir, ros_path) -> list:
    """Extra-team assets that still share their bytes with another TOC entry (usually the donor
    team's). Editing one of these in place would edit the donor too. -> [(team, iff)]."""
    game_dir = Path(game_dir)
    shared = _shared_f3(game_dir)
    out = []
    for r in catalog_rows(game_dir, ros_path):
        loc = AT.resolve(r["iff"], game_dir)
        if loc and shared.get(loc[4], 0) > 1:
            out.append((r["team"], r["iff"]))
    return out


# ── adding TOC entries (front-end jerseys) ───────────────────────────────────────

def _sha(game_dir, name):
    """sha1 of an asset's raw bytes, or None if it doesn't resolve."""
    loc = AT.resolve(name, Path(game_dir))
    if not loc:
        return None
    arc, off, size, _idx, _f3 = loc
    with (Path(game_dir) / arc).open("rb") as f:
        f.seek(off)
        return hashlib.sha1(f.read(size)).hexdigest()


def donor_key(game_dir, akey: str, slot: str):
    """Which shipping team an expansion team's uniform was cloned from, by exact content match.

    An expansion team is built by copying a real team's assets, so its `uniform_<key>_<slot>.iff`
    is byte-identical to the donor's until the user edits it. Matching on content rather than
    remembering a donor id means this still works on a roster built by someone else, and it fails
    closed (returns None) once the jersey has actually been repainted — at which point aliasing the
    donor's front-end sheet would be the wrong thing to do anyway.
    """
    mine = _sha(game_dir, f"uniform_{akey.lower()}_{slot}.iff")
    if not mine:
        return None
    for cand in sorted(_catalogued_keys()):
        if cand == akey.lower():
            continue
        if _sha(game_dir, f"uniform_{cand}_{slot}.iff") == mine:
            return cand
    return None


def add_toc_alias(game_dir, new_name: str, donor_name: str, log=print) -> str:
    """Add a TOC entry for `new_name` pointing at `donor_name`'s existing blob — a new loadable
    asset for zero copied bytes.

    Entries are `(flags, size, crc32(NAME.UPPER), offset/0x800)` and the table is sorted ascending
    by crc, so the new entry is spliced into its sorted position and the count at `0x10` bumped.
    Entry INDEX is safe to shift: every lookup path keys on the name hash, never on position.
    The table grows toward the first blob, so the free-slot check is against the lowest live
    offset rather than a fixed bound.
    """
    game_dir = Path(game_dir)
    a0 = game_dir / "0A"
    AT._backup_once(a0, log)
    d = bytearray(a0.read_bytes()[:0x10000])
    cnt = struct.unpack_from(">I", d, 0x10)[0]
    tbl = 0x58
    end = tbl + cnt * 16

    crc = zlib.crc32(new_name.upper().encode("ascii")) & 0xFFFFFFFF
    entries = [struct.unpack_from(">4I", d, tbl + i * 16) for i in range(cnt)]
    if any(e[2] == crc for e in entries):
        return f"{new_name}: already in the TOC — nothing to do"

    dloc = AT.resolve(donor_name, game_dir)
    if not dloc:
        raise ValueError(f"donor asset {donor_name} does not resolve")
    _arc, _off, size, didx, f3 = dloc
    flags = entries[didx][0]

    lowest = min(e[3] for e in entries) * 0x800
    if end + 16 > lowest:
        raise RuntimeError(f"TOC is full ({(lowest - end) // 16} free slots) — relocate the first "
                           f"blob to the archive tail to make room")

    pos = next((i for i, e in enumerate(entries) if e[2] > crc), cnt)
    entries.insert(pos, (flags, size, crc, f3))
    for i, e in enumerate(entries):
        struct.pack_into(">4I", d, tbl + i * 16, *e)
    struct.pack_into(">I", d, 0x10, cnt + 1)
    with a0.open("r+b") as f:
        f.seek(0)
        f.write(d[:tbl + (cnt + 1) * 16])
    return (f"{new_name}: TOC entry added at #{pos} (crc {crc:08X}) -> {donor_name}'s blob "
            f"(0x{size:X} bytes @ f3 0x{f3:X})")


def missing_frontend(game_dir, ros_path) -> list:
    """Front-end jersey assets an expansion team should have but doesn't -> [(team, iff)].

    Only slots whose in-game uniform exists are counted: Anaheim ships no alternate, so a clone of
    Anaheim is not missing an `_alt` sheet — it has no alternate jersey to show.
    """
    game_dir = Path(game_dir)
    out = []
    for t in extra_teams(ros_path):
        akey = t["akey"].lower()
        for slot in FRONTEND_SLOTS:
            if AT.resolve(f"uniform_frontend_{akey}_{slot}.iff", game_dir):
                continue
            if AT.resolve(f"uniform_{akey}_{slot}.iff", game_dir):
                out.append((akey, f"uniform_frontend_{akey}_{slot}.iff"))
    return out


def ensure_frontend_uniforms(game_dir, ros_path, log=print) -> str:
    """Give every expansion team the `uniform_frontend_<key>_<slot>.iff` assets the team-select
    screen looks up, so its jersey panel renders instead of staying blank.

    Aliases the donor team's sheet, which is the correct image for a not-yet-repainted clone: the
    front-end sheet's `decals` texture is a byte-identical copy of the in-game uniform's `stamps`
    (see `jerseys.py`), so a cloned uniform and the donor's sheet already agree. Only slots the
    donor actually has are added — Anaheim ships no alternate, so no `_alt` is invented.

    Narrower than `ensure_team_assets`, which covers the whole family (this is the three front-end
    sheets only) and can be handed an explicit donor. Kept for the case where the donor must be
    inferred per slot from uniform content.
    """
    game_dir = Path(game_dir)
    added, msgs = 0, []
    for t in extra_teams(ros_path):
        akey = t["akey"].lower()
        for slot in FRONTEND_SLOTS:
            name = f"uniform_frontend_{akey}_{slot}.iff"
            if AT.resolve(name, game_dir):
                continue
            donor = donor_key(game_dir, akey, slot)
            if not donor:
                msgs.append(f"  {name}: no donor found (uniform_{akey}_{slot}.iff is missing or "
                            f"already repainted) — skipped")
                continue
            src = f"uniform_frontend_{donor}_{slot}.iff"
            if not AT.resolve(src, game_dir):
                msgs.append(f"  {name}: donor {donor.upper()} has no {slot} front-end sheet — skipped")
                continue
            msgs.append("  " + add_toc_alias(game_dir, name, src, log))
            added += 1
    for m in msgs:
        log(m)
    return f"added {added} front-end jersey asset(s)"


# ── adding TOC entries for ONE extra jersey slot ─────────────────────────────────

# The three assets a single uniform record makes the game ask for. `%s` pairs are (key, slot);
# the middle one keys off the record's BASE key, the other two off its ASSET key.
UNIFORM_SLOT_ASSETS = (("asset", "uniform_%s_%s.iff"),
                       ("base",  "uniform_base_%s_%s.iff"),
                       ("asset", "uniform_frontend_%s_%s.iff"))


def uniform_slot_assets(asset_key: str, base_key: str, slot: str) -> list:
    """The three .iff names a `uniform_colors` row resolves to, in FAMILY spelling (lowercase)."""
    keys = {"asset": asset_key.lower(), "base": base_key.lower()}
    return [pat % (keys[which], slot) for which, pat in UNIFORM_SLOT_ASSETS]


def missing_uniform_slot(game_dir, asset_key: str, base_key: str, slot: str) -> list:
    """Which of those three the live TOC has no entry for."""
    game_dir = Path(game_dir)
    return [n for n in uniform_slot_assets(asset_key, base_key, slot)
            if not AT.resolve(n, game_dir)]


def ensure_uniform_slot(game_dir, asset_key: str, base_key: str, slot: str,
                        donor_asset_key: str | None = None, donor_base_key: str | None = None,
                        donor_slot: str = "home", log=print) -> str:
    """Create the three assets an added jersey needs, cloned from the donor kit.

    This is the asset half of `uniform_colors.add_uniform`. Giving Anaheim an Alternate writes a
    roster row that asks for `uniform_ANA_alt.iff`, `uniform_base_ANA_alt.iff` and
    `uniform_frontend_ANA_alt.iff` — none of which a team with no alternate has ever had, so
    without this the jersey appears in the carousel and renders nothing.

    The donor keys default to the new jersey's own, which is the ordinary case (Anaheim's alternate
    keys on `ANA` like the rest of its kits). They are separate parameters because a jersey may be
    given a key of its own instead — that is how the shipped throwbacks work (`DET_WC09`,
    `BUF_CL93`) — and then the bytes still have to come from a kit that exists.

    Each name is spliced into the TOC aliasing the donor's blob and then immediately given its own
    copy in the 1B tail, so repainting the new jersey can never write over the kit it came from.
    Idempotent: a name that already resolves is left alone, so re-running after a partial failure
    finishes the job. Run with the game closed.
    """
    game_dir = Path(game_dir)
    made = []
    for want, src in zip(uniform_slot_assets(asset_key, base_key, slot),
                         uniform_slot_assets(donor_asset_key or asset_key,
                                             donor_base_key or base_key, donor_slot)):
        if AT.resolve(want, game_dir):
            continue
        if not AT.resolve(src, game_dir):
            log(f"  {want}: donor {src} does not resolve — skipped")
            continue
        log("  " + add_toc_alias(game_dir, want, src, log))
        loc = AT.resolve(want, game_dir)
        if not loc:
            log(f"  {want}: added but still does not resolve — skipped the de-alias")
            continue
        arc, off, size, idx, _f3 = loc
        with (game_dir / arc).open("rb") as fh:
            fh.seek(off)
            data = fh.read(size)
        if len(data) != size:
            log(f"  {want}: short read ({len(data)}/{size}) — left sharing {src}'s bytes")
            continue
        log(f"  {want}: own copy at " + _append_copy(game_dir, data, idx, log))
        made.append(want)
    if not made:
        return f"the {slot} assets already exist — nothing to do"
    return f"created {len(made)} asset(s) from the {donor_slot} kit: " + ", ".join(made)


# ── adding TOC entries (the whole family) ────────────────────────────────────────

def family_names(akey: str) -> list:
    """[(category, iff)] — the full asset family an asset key owns, in FAMILY order."""
    k = akey.lower()
    return [(cat, pat % k) for cat, pat in FAMILY]


def missing_members(game_dir, ros_path, akey: str | None = None) -> list:
    """Family members an expansion team has no TOC entry for at all -> [(team, iff)].

    A superset of what `ensure_team_assets` can actually fill: a member the DONOR hasn't got
    either (Anaheim ships no alternate jersey) is listed here as missing but is correctly left
    alone, because there is nothing to clone and the team genuinely has no such kit.
    """
    game_dir = Path(game_dir)
    want = akey.lower() if akey else None
    out = []
    for t in extra_teams(ros_path):
        k = t["akey"].lower()
        if want and k != want:
            continue
        out.extend((k, n) for _cat, n in family_names(k) if not AT.resolve(n, game_dir))
    return out


def infer_donor(game_dir, akey: str):
    """Which shipping team this key was cloned from, inferred from whichever uniform still matches.

    `donor_key` answers that per slot and fails closed once the jersey is repainted, so this tries
    every slot and takes the first hit — a team whose home kit has been repainted but whose away
    kit hasn't is still identifiable. Returns None for a brand-new key that owns no assets yet;
    that case has no evidence to work from and the donor must be passed in explicitly.
    """
    for slot in FRONTEND_SLOTS:
        d = donor_key(game_dir, akey, slot)
        if d:
            return d
    return None


def plan_team_assets(game_dir, ros_path, akey: str | None = None, donor: str | None = None,
                     log=None) -> list:
    """What `ensure_team_assets` WOULD add -> [{team, donor, iff, src}], writing nothing.

    Split out from the doing so the UI can size the job before asking the user to confirm it, and
    so "already prepared" stays an honest answer: a member the donor hasn't got is left out of the
    plan entirely rather than counted as work that will never happen.
    """
    game_dir = Path(game_dir)
    want = akey.lower() if akey else None
    plan = []
    for t in extra_teams(ros_path):
        k = t["akey"].lower()
        if want and k != want:
            continue
        # Work out what's missing BEFORE asking who the donor is: a team that already owns its
        # whole family needs no donor, and demanding one would make every re-run of an
        # already-finished team report a spurious "no donor" problem.
        gaps = [pat for _cat, pat in FAMILY if not AT.resolve(pat % k, game_dir)]
        if not gaps:
            continue
        src_key = (donor or infer_donor(game_dir, k) or "").lower()
        if not src_key or src_key == k:
            if log:
                log(f"  {k.upper()}: {len(gaps)} asset(s) have no TOC entry, but " +
                    ("the donor is the team itself" if src_key else
                     "no donor was given and none can be inferred (its uniforms no longer match "
                     "any shipping team's, which is normal once they've been repainted)") +
                    " — left alone. Pass donor= if these should be filled in.")
            continue
        # A gap the donor hasn't got either is not a gap at all — an Anaheim clone is not "missing"
        # an alternate jersey, it has none, exactly like Anaheim.
        for pat in gaps:                          # from the PATTERN, never by substituting the key
            src = pat % src_key                    # into the name ("ice" would eat "ice_%s_...")
            if AT.resolve(src, game_dir):
                plan.append({"team": k, "donor": src_key, "iff": pat % k, "src": src})
    return plan


def ensure_team_assets(game_dir, ros_path, akey: str | None = None, donor: str | None = None,
                       logo=True, dealias_after=True, log=print) -> str:
    """Give an expansion team the COMPLETE asset family a shipping team has, cloned from `donor`.

    `create_expansion_team` writes a team into Roster.ROS with a novel asset key; at that moment the
    club exists but owns no art whatsoever, and every name-keyed lookup the game makes
    (`logo_<key>.iff`, `uniform_<key>_<slot>.iff`, `rink_<key>.iff`, the arena, the zambonis, the
    LED ribbon, the ice, the front-end sheet) misses and draws nothing. This is the other half of
    the job: one TOC entry per family member, each aliasing the donor's blob, then a de-alias so the
    new team owns its bytes and can be edited without touching the team it was cloned from.

    Only members the donor actually has are added. Anaheim ships no alternate jersey, so an
    Anaheim clone gets 15 assets and no `_alt` — it has no alternate kit, exactly like its donor.

    `donor` is required for a brand-new key: `infer_donor` matches uniform CONTENT, and a key with
    no uniforms yet has nothing to match. Passing it explicitly is also what a caller coming from
    `create_expansion_team` already knows (that function clones a specific donor record).

    Idempotent at every step — `add_toc_alias` returns early on an existing crc, `dealias` skips
    anything already unshared, and `logos_atlas.add_team` skips a key already in the atlas — so
    re-running after a partial failure completes the job rather than duplicating it. Run with the
    game closed.
    """
    game_dir = Path(game_dir)
    want = akey.lower() if akey else None
    teams = [t for t in extra_teams(ros_path) if not want or t["akey"].lower() == want]
    if not teams:
        return (f"no expansion team with asset key {akey!r} in this roster" if want else
                "no expansion teams in this roster — every league team's asset key is catalogued")

    plan = plan_team_assets(game_dir, ros_path, akey, donor, log)
    added, last = 0, None
    for step in plan:
        if step["team"] != last:
            last = step["team"]
            n = sum(1 for s in plan if s["team"] == last)
            log(f"  {last.upper()}: cloning {n} missing asset(s) from {step['donor'].upper()}")
        log("    " + add_toc_alias(game_dir, step["iff"], step["src"], log))
        added += 1

    out = [f"added {added} TOC entr{'y' if added == 1 else 'ies'}"]

    if logo:
        out.append(_ensure_logo_tiles(game_dir, teams, donor, log))
    if dealias_after:
        out.append(dealias(game_dir, ros_path, log))
    return "; ".join(p for p in out if p)


def _logos_atlas():
    """The atlas editor, however this module happens to have been imported."""
    try:
        from launcher import logos_atlas as LA           # normal: run as part of the package
    except ImportError:
        import logos_atlas as LA                         # running from inside launcher/
    return LA


def missing_logo_tiles(game_dir, ros_path, akey: str | None = None) -> list:
    """Expansion teams with no front-end logo tile -> [asset key]. See `_ensure_logo_tiles`."""
    tiles = _logos_atlas().team_tiles(Path(game_dir))
    want = akey.lower() if akey else None
    return [t["akey"].lower() for t in extra_teams(ros_path)
            if (not want or t["akey"].lower() == want) and t["akey"].lower() not in tiles]


def _ensure_logo_tiles(game_dir, teams, donor, log) -> str:
    """Give each team a front-end logo TILE, which is a different thing from `logo_<key>.iff`.

    `logo_<key>.iff` is the in-game (rink/HUD) logo and is an ordinary named asset the loop above
    covers. The menus instead read a TILE out of the `0xF0985030` branding atlases, keyed by
    `crc32(lowercase asset key)`, and a key with no tile shows a blank box no matter how many .iff
    entries exist (see `project_logos_atlas_gate`). `logos_atlas.add_team` splices one in.
    """
    LA = _logos_atlas()
    done, msgs = 0, []
    for t in teams:
        k = t["akey"].lower()
        src_key = (donor or infer_donor(game_dir, k) or "").lower()
        if not src_key or src_key == k:
            continue
        if k in LA.team_tiles(game_dir):
            continue
        msgs.append(LA.add_team(k, src_key, game_dir, log))
        done += 1
    for m in msgs:
        log("  " + m.replace("\n", "\n  "))
    return f"added {done} logo atlas tile(s)" if done else "logo atlas already has every team"


def _append_copy(game_dir, data: bytes, toc_idx: int, log) -> str:
    """Append `data` to the 1B tail and repoint TOC entry `toc_idx` at it.

    Deliberately NOT `AT._relocate`: that function's auto-reuse shortcut overwrites the asset's
    CURRENT slot whenever the slot is already in the appended region and big enough — which for an
    aliased entry is the donor's slot, so it would write the donor's own bytes back over the donor
    and leave the alias intact. De-aliasing must always take a fresh slot.
    """
    ALIGN = 0x800
    game_dir = Path(game_dir)
    a0, a1b = game_dir / "0A", game_dir / "1B"
    AT._backup_once(a0, log); AT._backup_once(a1b, log)
    cnt = struct.unpack_from(">I", a0.read_bytes()[:0x14], 0x10)[0]
    d0a = bytearray(a0.open("rb").read(0x58 + cnt * 16))
    sizes = [struct.unpack_from(">I", d0a, 0x18 + i * 16)[0] * ALIGN for i in range(4)]
    base1b = sizes[0] + sizes[1] + sizes[2]

    old = a1b.stat().st_size
    new_local = (old + ALIGN - 1) & ~(ALIGN - 1)
    with a1b.open("r+b") as f:
        if new_local > old:
            f.seek(old); f.write(b"\x00" * (new_local - old))
        f.seek(new_local); f.write(data)
    total = new_local + len(data)

    eo = 0x58 + toc_idx * 16
    struct.pack_into(">I", d0a, 0x18 + 3 * 16, (total + ALIGN - 1) // ALIGN)   # 1B size bound
    struct.pack_into(">I", d0a, eo + 4, len(data))                            # size
    struct.pack_into(">I", d0a, eo + 12, (base1b + new_local) // ALIGN)        # f3
    with a0.open("r+b") as f:
        f.seek(0); f.write(d0a)
    return f"1B:0x{new_local:X} ({len(data)} bytes), TOC#{toc_idx} repointed"


def dealias(game_dir, ros_path, log=print) -> str:
    """Give every shared extra-team asset its own physical copy in the 1B tail, so an edit to an
    expansion team can never write into the team it was cloned from.

    Copies the asset's exact bytes and repoints ONLY its own TOC entry — the donor entry is left
    pointing at the original blob, so the donor team is byte-for-byte unaffected. Idempotent: an
    asset that is already unshared is skipped. Costs disk (a full family is ~40 MB); `AT.compact_1b`
    reclaims anything later orphaned.
    """
    game_dir = Path(game_dir)
    todo = aliased_members(game_dir, ros_path)
    if not todo:
        return "every expansion-team asset already owns its bytes — nothing to do"
    done = 0
    for team, iff in todo:
        loc = AT.resolve(iff, game_dir)
        if not loc:
            continue
        arc, off, size, idx, _f3 = loc
        blob = (game_dir / arc).open("rb")
        try:
            blob.seek(off)
            data = blob.read(size)
        finally:
            blob.close()
        if len(data) != size:
            log(f"  {iff}: short read ({len(data)}/{size}) — skipped")
            continue
        log(f"  {iff}: own copy at " + _append_copy(game_dir, data, idx, log))
        done += 1
    return f"de-aliased {done} of {len(todo)} shared expansion-team asset(s)"
