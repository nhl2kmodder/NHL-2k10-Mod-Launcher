"""team_colors.py — read/edit NHL 2k10 TEAM colors in a Roster.ROS save.

The team PRIMARY / SECONDARY colors (the ones shown on the scoreclock and the
team-select screen) live in chunk **0x8489FAF3** (stride 412) — the same 412-byte
record table team_order.py works on, just framed 0x63 bytes earlier:
  - the league-0 records = the NHL teams' colors, in the roster's own team order
  - each team's AHL affiliate record holds that team's arena-LED colors (``led=True``)
Within a record: PRIMARY  @ +0x14C (3 bytes RGB), SECONDARY @ +0x14F (3 bytes RGB).

⚠ Nothing here may assume **30**. An expansion team is an ordinary league-0 record
(Seattle and Vegas make it 32), so the team list, the record indices and the
affiliate lookup are all derived from the roster. The old code required exactly 30
teams and hard-coded "affiliate = record + 30"; with 32 league-0 records that
silently fell back to a stock alphabetical code list, which after a reorder maps
codes onto the *wrong* records. The affiliate is now found the way affiliates.py
defines it: the league-1 record wearing the same ASSET key.

Every record carries its own index at +0x12B and its team id at +0x12C. On a stock
save those are equal (0..29) for the team records, while the LED block runs +0x12B
30..59 against +0x12C 0..29. The table is NOT found via the chunk directory: that
chunk's data_offset lands 7 records (2884 B) *into* the table — record 0 (ANA) sits
BEFORE ``chunk.foff``, so anchoring on foff silently loses the 7 teams that sort
before Colorado (ANA, ATL, BOS, BUF, CGY, CAR, CHI), which is why they could never
be mapped. See _team_base().

⚠ Record index is NOT a fixed alphabetical position. team_order.py physically
permutes these records to change the in-game team list order, and it deliberately
does not renumber +0x12B/+0x12C — the ids travel with their team. So
``team_map(ros_path)`` reads each record's display code out of the file, and
_team_base() anchors on the PRO/FARM roster pointers (team_order.find_table),
which is the one signature a reorder leaves intact. The NHL_CODES fallback only
applies when no path is supplied, and is right for a stock-ordered save only.

Uniform colors (helmet / jersey / fonts) are a *different* chunk (0x1AEB24EC,
stride 284, RGBA) and are not edited here — those are per-uniform, not per-team.

Edits are strictly IN PLACE: the file size never changes (so every fixed offset
the game holds stays valid), and a one-time ``<roster>.ROS.colorbak`` backup is
written before the first change. Colors are cached by the game at load, so a
change only shows after a full game restart.
"""
from __future__ import annotations
import shutil
from pathlib import Path

import ros_file as RF                                    # reuse the chunk-directory parser
import roster_editor as RE                               # team order == team id

TEAM_COLOR_CHUNK = 0x8489FAF3
PRIMARY_OFF   = 0x14C
SECONDARY_OFF = 0x14F
REC_IDX_OFF   = 0x12B       # this record's own index (0..59+)
TEAM_ID_OFF   = 0x12C       # this record's team id (0..29; wraps for the LED block)
NTEAMS        = 30          # the SHIPPING league-0 count — only used by the stock fallback
FRAME_SHIFT   = 0x63        # this module's record 0 starts 0x63 bytes before team_order's


def _color_chunk(ros: "RF.RosFile"):
    # the team-color table is the 412-byte-stride chunk
    return next(c for c in ros.chunks if c.hash == TEAM_COLOR_CHUNK and c.stride >= 200)


def _team_base(ros: "RF.RosFile", c) -> int:
    """File offset of team record 0.

    Primary anchor is team_order.find_table(), which keys off the PRO/FARM roster pointers
    every record carries. That signature survives a **team reorder**; the old one below did
    not, because it required record k to hold the id k and a reorder moves records without
    renumbering them. team_order frames a record 0x63 later than this module does (its +0xC8
    id byte is this module's +0x12B), hence FRAME_SHIFT.

    The legacy id-signature probe is kept as a fallback: ``c.foff`` is not the table start
    (it lands 7 records in), so it walks record-sized shifts around it looking for a run of
    records with +0x12B == +0x12C == k. It scores every shift and takes the best rather than
    demanding a perfect run of 30 — an expansion team or a reorder breaks the run (Seattle
    carries index 30 at record 23), and demanding 30 made this raise on a modded save.
    Raises rather than guess — a wrong base would write colours into neighbouring records.
    """
    try:
        import team_order as TO
    except ImportError:
        from . import team_order as TO
    try:
        return TO.find_table(ros.data)[0] - FRAME_SHIFT
    except Exception:
        pass
    d, S = ros.data, c.stride
    best, score = None, 0
    for shift in range(-16, 17):
        b = c.foff + shift * S
        if b < 0 or b + (NTEAMS - 1) * S + TEAM_ID_OFF >= len(d):
            continue
        n = sum(1 for k in range(NTEAMS)
                if d[b + k * S + REC_IDX_OFF] == k and d[b + k * S + TEAM_ID_OFF] == k)
        if n > score:
            best, score = b, n
    if best is not None and score >= NTEAMS - 4:      # 30 records, a few may have been reordered
        return best
    raise RuntimeError("team colour table not found: no run of records with "
                       "+0x12B == +0x12C == k near chunk 0x%08X" % TEAM_COLOR_CHUNK)


def team_map(ros_path=None) -> dict:
    """{TEAMCODE: team record index}.

    **Pass ros_path whenever you have it.** The codes are then read out of the records
    themselves, which is the only correct answer once the teams have been reordered (see
    team_order.py) or renamed — record index is no longer alphabetical position.

    Without a path this falls back to RE.NHL_CODES, a fixed 30-entry list that matches a
    stock roster's order. Not RosterEditor(...).teams: that property is built as
    [teams[c] for c in NHL_CODES if c in teams], so a code its string parser failed to find
    would silently shorten the list and shift every later team's colour onto the wrong
    record.
    """
    if ros_path:
        try:
            m = {t["code"].upper(): t["record"] for t in _TO().read_order(ros_path) if t["code"]}
            # >= NTEAMS, not == : an expansion team is an ordinary league-0 record, so a roster
            # with Seattle and Vegas reports 32. Only a SHORT list (a code the file didn't give
            # us) is untrustworthy, because a missing code would shift nothing — every record
            # index here is read straight out of the file.
            if len(m) >= NTEAMS:
                return m
        except Exception:
            pass
    if len(RE.NHL_CODES) != NTEAMS:
        raise RuntimeError(f"expected {NTEAMS} team codes, got {len(RE.NHL_CODES)}")
    return {code.upper(): i for i, code in enumerate(RE.NHL_CODES)}


def _TO():
    try:
        import team_order as TO
    except ImportError:
        from . import team_order as TO
    return TO


def led_record(ros_path, code: str):
    """Record index holding `code`'s arena-LED colours — its AHL affiliate's record.

    Was "team record + 30", which only held on a stock 30-team roster: the affiliate block
    starts right after the league-0 block, so adding Seattle and Vegas pushed it to +32 and
    their own affiliates sit far higher still (records 81/82). The affiliate is identified
    the way affiliates.py defines ownership — **it wears its parent's ASSET key** — which is
    stable no matter where either record ends up. Returns None if the team has no affiliate.
    """
    TO = _TO()
    d = Path(ros_path).read_bytes()
    base, count = TO.find_table(d)
    want = None
    for i in range(count):
        r = base + i * TO.STRIDE
        if (TO._u32(d, r + TO.OFF_LEAGUE) >> 28) == 0 and \
                TO._text(d, r + TO.OFF_CODE).strip().upper() == code.upper():
            want = TO._text(d, r + TO.OFF_AKEY).strip().upper()
            break
    if not want:
        return None
    for i in range(count):
        r = base + i * TO.STRIDE
        if (TO._u32(d, r + TO.OFF_LEAGUE) >> 28) == 1 and \
                TO._text(d, r + TO.OFF_AKEY).strip().upper() == want:
            return i
    return None


def _rgb(d, off):
    return (d[off], d[off + 1], d[off + 2])


def load(ros_path) -> dict:
    """Return {code: {'rec', 'primary'(r,g,b), 'secondary'(r,g,b)}} for every team."""
    ros = RF.RosFile(str(ros_path)); d = ros.data; c = _color_chunk(ros)
    base = _team_base(ros, c)
    out = {}
    nrec = max(0, (len(d) - base) // c.stride)
    for code, rec in team_map(ros_path).items():
        if not (0 <= rec < nrec):        # was `< 30`, which dropped the last two teams once
            continue                     # Seattle and Vegas made the league-0 block 32 long
        b = base + rec * c.stride
        out[code] = {"rec": rec,
                     "primary":   _rgb(d, b + PRIMARY_OFF),
                     "secondary": _rgb(d, b + SECONDARY_OFF)}
    return out


def set_color(ros_path, code, primary=None, secondary=None,
              led=False, backup=True, log=print) -> int:
    """Write primary/secondary (each an (r,g,b) tuple or #hex str) for a team, IN PLACE.
    `led=True` targets that team's arena-LED record (+30) instead. Returns the record index.
    Raises KeyError if the code isn't in the roster, RuntimeError if the file size would change."""
    ros_path = Path(ros_path)
    rec = team_map(ros_path)[code.upper()]
    if led:
        rec = led_record(ros_path, code)
        if rec is None:
            raise KeyError(f"{code.upper()} has no AHL affiliate record, so no LED colours")
    if backup:
        bak = ros_path.with_suffix(ros_path.suffix + ".colorbak")
        if not bak.exists():
            shutil.copy2(ros_path, bak); log(f"  backup -> {bak.name}")
    ros = RF.RosFile(str(ros_path)); d = ros.data; c = _color_chunk(ros)
    b = _team_base(ros, c) + rec * c.stride
    if primary is not None:
        d[b + PRIMARY_OFF:b + PRIMARY_OFF + 3] = bytes(_as_rgb(primary))
    if secondary is not None:
        d[b + SECONDARY_OFF:b + SECONDARY_OFF + 3] = bytes(_as_rgb(secondary))
    if len(d) != ros.orig_size:
        raise RuntimeError("refusing to write: roster size changed (in-place invariant)")
    ros_path.write_bytes(d)
    log(f"  {code.upper()} (record {rec}): "
        + (f"primary={_hex(primary)} " if primary is not None else "")
        + (f"secondary={_hex(secondary)}" if secondary is not None else ""))
    return rec


def revert(ros_path, log=print) -> bool:
    """Restore the whole roster from its .colorbak (undo all colour edits). True if done."""
    ros_path = Path(ros_path)
    bak = ros_path.with_suffix(ros_path.suffix + ".colorbak")
    if not bak.exists():
        return False
    shutil.copy2(bak, ros_path); log(f"  restored {ros_path.name} from {bak.name}")
    return True


# ── helpers ──────────────────────────────────────────────────────────────────
def _as_rgb(v):
    if isinstance(v, str):
        s = v.lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    return tuple(int(x) & 0xFF for x in v[:3])


def _hex(v):
    r, g, b = _as_rgb(v)
    return f"#{r:02X}{g:02X}{b:02X}"
