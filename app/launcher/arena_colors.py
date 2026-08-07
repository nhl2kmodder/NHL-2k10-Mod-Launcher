"""arena_colors.py — the arena record in Roster.ROS: dasher-board colours and seating capacity.

A table nothing in the launcher touched before. Chunk **0xE35B988E**, 40 records x 40 (0x28) bytes,
one per arena — the 30 NHL buildings first, then the 2K-invented rinks, the Pond, the outdoor
Winter Classic venues, the All-Star arena and the Skill Competition rink.

    +0x00 .. +0x0C   4 self-relative string pointers (name, and three more)
    +0x10   u32      0
    +0x14   u32      flags — 0xC0000000 or 0x80000000
    +0x18   i16      unidentified
    +0x1A   u16      SEATING CAPACITY   (Bell Centre 21273, Wrigley Field 41118, Pond 20)
    +0x1C   u16      unidentified
    +0x1E   u8       arena id
    +0x20   ARGB     DASHER BOARD colour
    +0x24   ARGB     a second dasher/kickplate colour — white in 34 of 40 arenas

`Arena_GetByIndex @0x83D4B3B0` confirms the stride (40) and reads the table through
`g_RosterManager+0x3c` (count) and `+0x40` (pointer).

## Which record belongs to which team

The team record's `+0xCC` (team_order frame) is a self-relative pointer straight at its arena
record, and that is what `arena_record()` follows — it stays right after a team reorder, a
relocation or an expansion team. On a stock roster it also happens that **arena index == team id**
(ANA 0, WPG 1, BOS 2, UTA 22, PIT 23, WSH 29), which is the fallback. AHL affiliates share their
parent club's arena, so writing a colour through an affiliate would repaint the NHL building.

## What +0x20 is

The dasher boards. The data only takes six values league-wide — #A60000 red for 18 of 30 teams,
then #004D8C blue, #4D4D4D grey, #00632E green, #CCCC00 yellow and #FFFFFF — and the outliers line
up exactly with what the boards look like in game: Edmonton blue, San Jose yellow, Boston grey,
Dallas green, everyone else red. `Arena_GetByIndex`'s fourteen xrefs were decompiled looking for
the consumer and none of them reads +0x20, so the field is identified from behaviour rather than
from code; +0x24's role is the weaker claim of the two (a second board tier or the kickplate),
since it is #FFFFFF everywhere except Winnipeg, Carolina, Philadelphia and St. Louis, where it
simply repeats +0x20.

Edits are in place — the file size never changes — and a one-time `<roster>.ROS.arenabak` is
written before the first one. The game caches the roster at load, so a change shows after a
restart.
"""
from __future__ import annotations
import shutil
import struct
from pathlib import Path

try:
    from . import ros_file as RF
    from . import team_order as TO
except ImportError:                     # run as a script, or with launcher/ itself on sys.path
    import ros_file as RF
    import team_order as TO

ARENA_CHUNK = 0xE35B988E
STRIDE = 40

NAME_OFF = 0x00
FLAGS_OFF = 0x14
CAPACITY_OFF = 0x1A         # u16
ARENA_ID_OFF = 0x1E         # u8
DASHER_OFF = 0x20           # ARGB
DASHER2_OFF = 0x24          # ARGB

BACKUP_SUFFIX = ".arenabak"

# The six values the stock roster uses, so the editor can offer them as presets.
STOCK_DASHERS = {
    "#A60000": "Red (league default — 18 of 30)",
    "#004D8C": "Blue (Edmonton, Toronto, St. Louis, NY Rangers, …)",
    "#4D4D4D": "Grey/black (Boston)",
    "#00632E": "Green (Dallas)",
    "#CCCC00": "Yellow (San Jose)",
    "#FFFFFF": "White (Los Angeles, and the non-NHL rinks)",
}


class ArenaError(RuntimeError):
    pass


def chunk(ros):
    for c in ros.chunks:
        if c.hash == ARENA_CHUNK and c.stride == STRIDE:
            return c
    raise ArenaError("no 40x40 arena chunk (0xE35B988E) in this Roster.ROS")


def count(ros) -> int:
    return chunk(ros).nrec


def _off(ros, i) -> int:
    return ros.rec_off(chunk(ros), i)


def name(ros, i) -> str:
    return TO._text(ros.data, _off(ros, i) + NAME_OFF)


def _hex(d, o) -> str:
    return "#%02X%02X%02X" % (d[o + 1], d[o + 2], d[o + 3])


def read(ros, i) -> dict:
    d, o = ros.data, _off(ros, i)
    return {"index": i, "name": name(ros, i),
            "capacity": struct.unpack_from(">H", d, o + CAPACITY_OFF)[0],
            "arena_id": d[o + ARENA_ID_OFF],
            "dasher": _hex(d, o + DASHER_OFF),
            "dasher2": _hex(d, o + DASHER2_OFF),
            "flags": struct.unpack_from(">I", d, o + FLAGS_OFF)[0]}


def arena_record(ros, team_rec: int):
    """Arena record index for team record `team_rec`, via the team's own +0xCC pointer.

    Falls back to "index == team id" only if the pointer doesn't land inside the arena table,
    because the pointer is the thing that survives a reorder and the id coincidence is not.
    """
    d = ros.data
    base, n = TO.find_table(d)
    if not 0 <= team_rec < n:
        return None
    c = chunk(ros)
    t = TO._target(d, base + team_rec * TO.STRIDE + TO.OFF_ARENA)
    if t is not None and c.foff <= t < c.foff + c.size and (t - c.foff) % STRIDE == 0:
        return (t - c.foff) // STRIDE
    tid = struct.unpack_from(">H", d, base + team_rec * TO.STRIDE + 0xF0)[0]
    return tid if 0 <= tid < c.nrec else None


def _team_index(ros_path):
    """{TEAMCODE: (team record, arena record)} for every league-0 team."""
    ros = RF.RosFile(str(ros_path))
    d = ros.data
    base, n = TO.find_table(d)
    out = {}
    for i in range(n):
        r = base + i * TO.STRIDE
        if (TO._u32(d, r + TO.OFF_LEAGUE) >> 28) != 0:
            continue
        code = TO._text(d, r + TO.OFF_CODE).strip().upper()
        if code:
            out[code] = (i, arena_record(ros, i))
    return ros, out


def load(ros_path) -> dict:
    """{TEAMCODE: {arena, name, capacity, dasher, dasher2}} for every NHL team."""
    ros, idx = _team_index(ros_path)
    out = {}
    for code, (_rec, ar) in idx.items():
        if ar is None:
            continue
        v = read(ros, ar)
        v["arena"] = ar
        out[code] = v
    return out


def load_all(ros_path) -> list:
    """Every arena record, including the ones no team plays in (Pond, Wrigley, All-Star…)."""
    ros = RF.RosFile(str(ros_path))
    return [read(ros, i) for i in range(count(ros))]


def set_arena(ros_path, code: str, dasher=None, dasher2=None, capacity=None,
              backup=True, log=print) -> int:
    """Write a team's arena fields IN PLACE. Returns the arena record index.

    `dasher`/`dasher2` are '#RRGGBB' or (r,g,b); the alpha byte is forced to 0xFF, as every stock
    record has. `capacity` is a u16, so 0..65535 — Wrigley's 41118 is the largest shipped.
    """
    ros_path = Path(ros_path)
    ros, idx = _team_index(ros_path)
    if code.upper() not in idx:
        raise KeyError(f"'{code.upper()}' is not a league-0 team in this roster")
    ar = idx[code.upper()][1]
    if ar is None:
        raise KeyError(f"'{code.upper()}' has no arena record")
    if backup:
        bak = ros_path.with_suffix(ros_path.suffix + BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copy2(ros_path, bak)
            log(f"  backup -> {bak.name}")
    d, o = ros.data, _off(ros, ar)
    if dasher is not None:
        d[o + DASHER_OFF:o + DASHER_OFF + 4] = b"\xFF" + bytes(_as_rgb(dasher))
    if dasher2 is not None:
        d[o + DASHER2_OFF:o + DASHER2_OFF + 4] = b"\xFF" + bytes(_as_rgb(dasher2))
    if capacity is not None:
        cap = int(capacity)
        if not 0 <= cap <= 0xFFFF:
            raise ValueError(f"capacity {cap} does not fit in a u16 (0..65535)")
        struct.pack_into(">H", d, o + CAPACITY_OFF, cap)
    if len(d) != ros.orig_size:
        raise RuntimeError("refusing to write: roster size changed (in-place invariant)")
    ros_path.write_bytes(d)
    bits = [f"dasher={_fmt(dasher)}" if dasher is not None else "",
            f"dasher2={_fmt(dasher2)}" if dasher2 is not None else "",
            f"capacity={capacity}" if capacity is not None else ""]
    log(f"  {code.upper()} arena {ar} ({name(ros, ar)}): " + " ".join(b for b in bits if b))
    return ar


def revert(ros_path, log=print) -> bool:
    """Restore the whole roster from its .arenabak. True if there was one."""
    ros_path = Path(ros_path)
    bak = ros_path.with_suffix(ros_path.suffix + BACKUP_SUFFIX)
    if not bak.exists():
        return False
    shutil.copy2(bak, ros_path)
    log(f"  restored {ros_path.name} from {bak.name}")
    return True


def _as_rgb(v):
    if isinstance(v, str):
        s = v.lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    return tuple(int(x) & 0xFF for x in v[:3])


def _fmt(v):
    r, g, b = _as_rgb(v)
    return f"#{r:02X}{g:02X}{b:02X}"


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else (
        r"C:\Users\cloug\Documents\xenia_master\Xenia Stable"
        r"\content\54540853\00000001\Roster.ROS\Roster.ROS")
    for code, v in sorted(load(p).items()):
        print(f"{code:4s} arena {v['arena']:2d} {v['name'][:30]:30s} "
              f"cap={v['capacity']:6d} dasher={v['dasher']} / {v['dasher2']}")
