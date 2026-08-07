"""team_order.py — change the order the 30 NHL teams appear in, in every menu list.

SOLVED 2026-08-01, verified in game.  The team-select list is walked in **physical record
order**.  Not by a stored id, not through Roster_GetTeamByIndex, not from any table in the
XEX — all three were patched and tested and none of them moved the list.  The decisive
test was swapping team records 1 and 29 in the *running game's* memory: the list swapped
with them.  So reordering the menu means physically permuting the records in Roster.ROS.

## The table

Chunk 0x8489FAF3, 96 records x 412 (0x19C) bytes.
    0..29   the NHL teams, in display order
    30..59  their AHL affiliates — record 30+i is the same organisation as record i
    60..95  all-star / national / custom / HOME / AWAY entries — never moved

Everything a record points at is a **self-relative** pointer: `target = field_offset +
value - 1` (value signed).  The pointer fields, established from live memory where the
same structs hold absolute addresses (memory offset + 8 == disk offset) and cross-checked
against the file:

    +0x08..+0x70  this team's player pointers, zero-terminated (20..26 used)
    +0xA8 city  +0xAC nickname  +0xB0 display code  +0xB4 lowercase  +0xB8 asset key
    +0xCC  -> this team's arena record
    +0xD0  -> the organisation's PRO roster array   (== record i, +8)
    +0xD4  -> the organisation's FARM roster array  (== record 30+i, +8)
    +0xD8  -> a per-team block in another chunk

## Why this can't be a memmove

Two reasons, both fatal if ignored:

1. `+0xD4` points *into another team record that is itself moving*, so a pointer's value
   depends on where its target ends up, not just on where the pointer ends up.
2. **11,068 pointers from outside the table point into it** — every rostered player has a
   back-pointer to its team at player record +0x190, plus ~9k more from the 0x100000..
   0x1FB800 region.  Miss those and every player silently changes team.

So instead of shifting bytes, every pointer is re-derived: resolve its target in the OLD
file, map that target through the permutation, then recompute the self-relative value from
the pointer's NEW offset.  Inbound pointers all land on exactly `record + 8` (which is
where the in-memory team struct starts), and are remapped the same way.  Anything else
that happens to resolve into the table is a numeric coincidence (~180 of them, about what
chance predicts for a 2.5 MB file) and is deliberately left alone.

Team **ids stay with their records** — +0xC8 is not touched — so Default Matchup and every
other id-based lookup keeps naming the same team after a reorder.

The file size never changes and a one-time `<roster>.ROS.orderbak` is written before the
first reorder.  The game caches the roster at load, so a change only shows after a restart.
"""
from __future__ import annotations
import shutil
import struct
from pathlib import Path

TEAM_CHUNK = 0x8489FAF3
STRIDE = 0x19C
NHL = 30                        # the shipping block size — see league0_slots()

OFF_PLAYERS = range(0x08, 0x74, 4)
OFF_CITY, OFF_NICK, OFF_CODE, OFF_LOWER, OFF_AKEY = 0xA8, 0xAC, 0xB0, 0xB4, 0xB8
OFF_ARENA, OFF_PRO, OFF_FARM, OFF_MISC = 0xCC, 0xD0, 0xD4, 0xD8
OFF_ID = 0xC8
OFF_LEAGUE = 0x110              # league << 28 | division << 24

PTR_FIELDS = list(OFF_PLAYERS) + [OFF_CITY, OFF_NICK, OFF_CODE, OFF_LOWER, OFF_AKEY,
                                  OFF_ARENA, OFF_PRO, OFF_FARM, OFF_MISC]

BACKUP_SUFFIX = ".ROS.orderbak"


class TeamOrderError(RuntimeError):
    pass


# ── low level ───────────────────────────────────────────────────────────────────

def _u32(d, o):
    return struct.unpack_from(">I", d, o)[0]


def _target(d, o):
    """self-relative pointer at `o` -> absolute file offset, or None when null"""
    v = _u32(d, o)
    if v == 0:
        return None
    sv = v - (1 << 32) if v & 0x80000000 else v
    return o + sv - 1


def _selfrel(target_off, field_off):
    return (target_off - field_off + 1) & 0xFFFFFFFF


def _text(d, o):
    """the UTF-16BE string a pointer field points at ('' if it isn't one)"""
    p = _target(d, o)
    if p is None or not 0 <= p < len(d) - 1:
        return ""
    e = d.find(b"\x00\x00", p)
    if e < 0 or e - p > 128:
        return ""
    if (e - p) % 2:
        e += 1
    try:
        s = d[p:e].decode("utf-16-be")
    except UnicodeDecodeError:
        return ""
    return s if s.isprintable() else ""


# ── locating the table ──────────────────────────────────────────────────────────

def _chunk_dir(d):
    """(directory offset, record count) of the team chunk."""
    n = _u32(d, 8)
    for i in range(min(n, 64)):                 # the real directory is short; the tail is junk
        h, cnt, off = struct.unpack_from(">III", d, 0x0C + i * 12)
        if h == TEAM_CHUNK and cnt >= 60:
            return off, cnt
    raise TeamOrderError(f"no team chunk ({TEAM_CHUNK:#010x}) in this Roster.ROS")


def _plausible(d, base, count):
    """Is `base` the start of record 0?

    The signature is the PRO/FARM pointers: each must resolve to exactly some record's
    +8.  That is a 96-record agreement on a 4-byte value, which cannot happen by accident,
    and — unlike 'record index == its own position' — it survives a reorder, so a roster
    this module has already reordered can still be found.
    """
    end = base + count * STRIDE
    if base < 0 or end > len(d):
        return False
    named = 0
    for i in range(count):
        r = base + i * STRIDE
        for f in (OFF_PRO, OFF_FARM):
            t = _target(d, r + f)
            if t is None:
                continue
            if not (base <= t < end) or (t - base - 8) % STRIDE:
                return False
        if _text(d, r + OFF_CITY) or _text(d, r + OFF_NICK):
            named += 1
    return named >= count * 3 // 4


def find_table(d) -> tuple[int, int]:
    """(file offset of record 0, record count)."""
    dir_off, count = _chunk_dir(d)
    # 0x47 is this container's data base (NOT the 0xB28 ros_file.py assumes); the window
    # scan is the fallback so a differently-built save still resolves.
    for base in [dir_off + 0x47] + [dir_off + k for k in range(-0x40, 0x800)]:
        if _plausible(d, base, count):
            return base, count
    raise TeamOrderError("found the team chunk but could not locate record 0")


# ── reading ─────────────────────────────────────────────────────────────────────

def league0_slots(d, base, count) -> list[int]:
    """Record indices of the top-league teams, in physical (= display) order.

    NOT `range(30)`: an expansion team lives in a spare record high in the table until it
    is reordered, so the menu list is "every league-0 record", however many that is and
    wherever they sit.  See [[project_expansion_teams]].
    """
    out = [i for i in range(count)
           if _u32(d, base + i * STRIDE + OFF_LEAGUE) >> 28 == 0
           and (_text(d, base + i * STRIDE + OFF_CITY) or
                _text(d, base + i * STRIDE + OFF_NICK))]
    if not out:
        raise TeamOrderError("no league-0 team records found")
    return out


def read_order(ros_path) -> list[dict]:
    """The top-league teams in their current display order.

    [{'slot', 'record', 'code', 'city', 'name', 'id', 'label'}, ...] — `slot` is the
    position in the menu list, `record` the physical record index, `id` the record's team
    id (+0xC8), which does NOT change when teams move.
    """
    d = Path(ros_path).read_bytes()
    base, count = find_table(d)
    out = []
    for slot, rec in enumerate(league0_slots(d, base, count)):
        r = base + rec * STRIDE
        city, name, code = (_text(d, r + o) for o in (OFF_CITY, OFF_NICK, OFF_CODE))
        label = " ".join(x for x in (city, name) if x) or f"record {slot}"
        out.append({"slot": slot, "record": rec, "code": code, "city": city, "name": name,
                    "id": d[r + OFF_ID], "label": label})
    return out


def order_codes(ros_path) -> list[str]:
    return [t["code"] for t in read_order(ros_path)]


# ── writing ─────────────────────────────────────────────────────────────────────

def apply_order(ros_path, order, backup=True, log=print) -> int:
    """Permute the top-league block into `order` and rewrite every affected pointer.

    `order` is the team display CODES in the wanted order (a permutation of what
    read_order() reports), or the current slot numbers.  Returns the number of teams
    that actually moved.  Writes in place; the file size is unchanged.

    The league-0 teams are packed to the FRONT of the table in `order`; every other record
    (affiliates, nationals, HOME/AWAY) keeps its current relative order in what is left.
    That is what promotes an expansion team out of a spare high record and into the menu.
    """
    ros_path = Path(ros_path)
    old = ros_path.read_bytes()
    base, count = find_table(old)
    end = base + count * STRIDE
    l0 = league0_slots(old, base, count)
    n = len(l0)

    # ── resolve `order` to a permutation of RECORD indices ─────────────────────
    current = read_order(ros_path)
    if all(isinstance(x, int) for x in order):
        perm = [current[s]["record"] for s in order]
    else:
        by_code = {}
        for t in current:
            by_code.setdefault(t["code"].upper(), []).append(t["record"])
        perm = []
        for code in order:
            recs = by_code.get(str(code).upper())
            if not recs:
                raise TeamOrderError(f"'{code}' is not one of this roster's {n} league teams")
            perm.append(recs.pop(0))
    if sorted(perm) != sorted(l0):
        raise TeamOrderError(f"the order must list each of the {n} teams exactly once "
                             f"(got {len(order)} entries)")

    # newpos[old record] = new record.  League-0 teams take positions 0..n-1 in `order`;
    # everything else keeps its relative order in the positions that remain.  Affiliate
    # linkage rides on the FARM pointer, which is re-derived below, so an organisation
    # stays together wherever its two records land.
    for rec in perm:
        r = base + rec * STRIDE
        if _target(old, r + OFF_PRO) != r + 8:
            raise TeamOrderError(f"record {rec} does not own its pro roster — refusing to reorder")
    newpos = {}
    for new_slot, old_rec in enumerate(perm):
        newpos[old_rec] = new_slot
    free = iter(range(n, count))
    for i in range(count):
        if i not in newpos:
            newpos[i] = next(free)
    if sorted(newpos.values()) != list(range(count)):
        raise TeamOrderError("internal error: the record permutation is not a bijection")

    moved = sum(1 for i in range(count) if newpos[i] != i)
    if not moved:
        log("  team order unchanged")
        return 0

    # ── every pointer INTO the table, from anywhere in the file ────────────────
    inbound = []
    for off in range(0, len(old) - 4, 4):
        if base <= off < end:
            continue
        v = struct.unpack_from(">I", old, off)[0]
        if v == 0:
            continue
        sv = v - (1 << 32) if v & 0x80000000 else v
        t = off + sv - 1
        if base <= t < end and (t - base - 8) % STRIDE == 0:
            inbound.append((off, t))

    def remap(t):
        if not (base <= t < end):
            return t
        j, within = divmod(t - base, STRIDE)
        return base + newpos[j] * STRIDE + within

    # ── move the bodies, then re-derive every pointer from its remapped target ─
    new = bytearray(old)
    for i in range(count):
        src = base + i * STRIDE
        new[base + newpos[i] * STRIDE:base + newpos[i] * STRIDE + STRIDE] = old[src:src + STRIDE]
    for i in range(count):
        old_rec, new_rec = base + i * STRIDE, base + newpos[i] * STRIDE
        for f in PTR_FIELDS:
            t = _target(old, old_rec + f)
            if t is not None:
                struct.pack_into(">I", new, new_rec + f, _selfrel(remap(t), new_rec + f))
    for off, t in inbound:
        struct.pack_into(">I", new, off, _selfrel(remap(t), off))

    if len(new) != len(old):
        raise TeamOrderError("internal error: file size changed")

    # ── verify before touching the file ───────────────────────────────────────
    for old_rec in range(count):
        r = base + newpos[old_rec] * STRIDE
        if _target(new, r + OFF_PRO) != r + 8 and _target(old, base + old_rec * STRIDE + OFF_PRO) \
                == base + old_rec * STRIDE + 8:
            raise TeamOrderError(f"verify failed: record {old_rec} lost its own roster")
        if [_target(new, r + f) for f in OFF_PLAYERS] != \
           [_target(old, base + old_rec * STRIDE + f) for f in OFF_PLAYERS]:
            raise TeamOrderError(f"verify failed: record {old_rec}'s roster changed")
    for off, t in inbound:
        was = (t - base - 8) // STRIDE
        now = _target(new, off)
        if now is None or not (base <= now < end) or (now - base - 8) // STRIDE != newpos[was]:
            raise TeamOrderError(f"verify failed: back-pointer at {off:#x} no longer names its team")

    if backup:
        bak = ros_path.with_suffix(BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copyfile(ros_path, bak)
            log(f"  backup -> {bak.name}")
    ros_path.write_bytes(bytes(new))
    log(f"  team order: {moved} team(s) moved, {len(inbound)} back-pointers remapped")
    return moved


def revert(ros_path, log=print) -> bool:
    """Restore the .orderbak taken before the first reorder."""
    ros_path = Path(ros_path)
    bak = ros_path.with_suffix(BACKUP_SUFFIX)
    if not bak.exists():
        return False
    shutil.copyfile(bak, ros_path)
    log(f"  restored {ros_path.name} from {bak.name}")
    return True


if __name__ == "__main__":
    import sys
    p = sys.argv[1]
    for t in read_order(p):
        print(f"  {t['slot']:2d}  {t['code']:5s} {t['label']:28s} id={t['id']}")
