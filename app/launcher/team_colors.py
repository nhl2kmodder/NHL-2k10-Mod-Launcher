"""team_colors.py — read/edit NHL 2k10 TEAM colors in a Roster.ROS save.

The team PRIMARY / SECONDARY colors (the ones shown on the scoreclock and the
team-select screen) live in chunk **0x8489FAF3** (stride 412) as two blocks of 30:
  - team records 0..29  = the 30 NHL teams' colors, in the roster's own team order
  - LED  records 30..59 = the same teams' arena-LED colors (+30)
Within a record: PRIMARY  @ +0x14C (3 bytes RGB), SECONDARY @ +0x14F (3 bytes RGB).

The table is SELF-DESCRIBING: every record carries its own index at +0x12B and its
team id at +0x12C. For the 30 team records those are equal (0..29); for the LED
block +0x12B runs 30..59 while +0x12C wraps back to 0..29. We anchor on that
signature rather than on the chunk directory, because the directory's data_offset
for this chunk lands 7 records (2884 B) *into* the table — record 0 (ANA) sits
BEFORE ``chunk.foff``. Anchoring on foff silently loses the 7 teams that sort
before Colorado (ANA, ATL, BOS, BUF, CGY, CAR, CHI), which is why they could never
be mapped. See _team_base().

Team id (+0x12C) == roster_editor.NHL_CODES order, i.e. plain NHL-alphabetical:
0=ANA … 7=COL … 23=PIT … 29=WSH. So the code->record map is just that list; no
hand-built map file is needed (team_color_map.json is dead, and its CAR=23 was
wrong — record 23 in the old foff-relative numbering is ANA's arena LED).

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


def _color_chunk(ros: "RF.RosFile"):
    # the team-color table is the 412-byte-stride chunk
    return next(c for c in ros.chunks if c.hash == TEAM_COLOR_CHUNK and c.stride >= 200)


def _team_base(ros: "RF.RosFile", c) -> int:
    """File offset of team record 0, found by the table's own id signature.

    ``c.foff`` is not the table start (it lands 7 records in), so probe a window of
    record-sized shifts around it for the run of 30 records with +0x12B == +0x12C == k.
    Raises RuntimeError rather than guess — a wrong base would write colors into
    neighbouring records.
    """
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
    """{TEAMCODE: team record index} — the table's +0x12C team id.

    Keyed off RE.NHL_CODES (a fixed 30-entry list), NOT RosterEditor(...).teams: that
    property is built as [teams[c] for c in NHL_CODES if c in teams], so a code its string
    parser failed to find would silently shorten the list and shift every later team's
    colour onto the wrong record. NHL_CODES order == team id order (verified against the
    +0x12C field and the stock colours for all 30).
    """
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
    for code, rec in team_map().items():
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
    rec = team_map()[code.upper()]
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
