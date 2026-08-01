"""team_colors.py — read/edit NHL 2k10 TEAM colors in a Roster.ROS save.

The team PRIMARY / SECONDARY colors (the ones shown on the scoreclock and the
team-select screen) live in chunk **0x8489FAF3** (stride 412) as two blocks of 30:
  - team records 0..29  = the 30 NHL teams' colors, in the roster's own team order
  - LED  records 30..59 = the same teams' arena-LED colors (+30)
Within a record: PRIMARY  @ +0x14C (3 bytes RGB), SECONDARY @ +0x14F (3 bytes RGB).

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
LED_OFFSET    = 30          # record N+30 holds the same team's arena-LED colors
NTEAMS        = 30
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
    (it lands 7 records in), so it walks record-sized shifts around it looking for 30
    consecutive records with +0x12B == +0x12C == k. Raises rather than guess — a wrong base
    would write colours into neighbouring records.
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
    for shift in range(-16, 17):
        b = c.foff + shift * S
        if b < 0 or b + (NTEAMS - 1) * S + TEAM_ID_OFF >= len(d):
            continue
        if all(d[b + k * S + REC_IDX_OFF] == k and d[b + k * S + TEAM_ID_OFF] == k
               for k in range(NTEAMS)):
            return b
    raise RuntimeError("team colour table not found: no run of 30 records with "
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
            try:
                import team_order as TO
            except ImportError:
                from . import team_order as TO
            m = {t["code"].upper(): t["slot"] for t in TO.read_order(ros_path) if t["code"]}
            if len(m) == NTEAMS:
                return m
        except Exception:
            pass
    if len(RE.NHL_CODES) != NTEAMS:
        raise RuntimeError(f"expected {NTEAMS} team codes, got {len(RE.NHL_CODES)}")
    return {code.upper(): i for i, code in enumerate(RE.NHL_CODES)}


def _rgb(d, off):
    return (d[off], d[off + 1], d[off + 2])


def load(ros_path) -> dict:
    """Return {code: {'rec', 'primary'(r,g,b), 'secondary'(r,g,b)}} for every team."""
    ros = RF.RosFile(str(ros_path)); d = ros.data; c = _color_chunk(ros)
    base = _team_base(ros, c)
    out = {}
    for code, rec in team_map(ros_path).items():
        if not (0 <= rec < NTEAMS):
            continue
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
        rec += LED_OFFSET
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
