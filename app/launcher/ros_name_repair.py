# -*- coding: utf-8 -*-
r"""ros_name_repair.py -- move player names out of the equipment-colour array.

THE FAULT THIS REPAIRS (found 2026-09-18, Juuse Saros).
A player name is reached by a SIGNED self-relative pointer at +0x00 (last) / +0x04 (first). In
this save 1,724 of those pointers do not aim at the string pool at all -- they aim back INSIDE
the player's own record, at +0x11F (first) and +0x13D (last). That range is in the middle of the
record's equipment-colour array, a 4-byte ARGB grid running +0x0C0 .. +0x19C. The name text is
literally sitting in four colour slots.

Nothing goes wrong until the game writes that array. Edit a player's equipment colour in-game and
it rewrites all 44 slots, straight over the name:

    BEFORE  +0x120  4a 00 75 00 75 00 73 00 65 00 00 00   "J u u s e \0"
    AFTER   +0x120  ff ff ff ff ff 00 06 29 ff ff cc 00   four real colours

The name pointer still aims there, so the menu renders colour bytes as text -- the squares and odd
symbols. On save the game recovers by itself: it appends the name to the pool and repoints the
record. That recovery is correct, so the damage is cosmetic and momentary -- but the user has to
notice and retype the name, and a retype is where a real misspelling gets in ("Jusse" for "Juuse").

So the repair is not to restore anything. It is to do the game's relocation UP FRONT, for every
exposed name at once, so the collision can never fire again. The colour bytes are deliberately
left exactly as they are: they hold name text today and will hold the same name text afterwards,
so no equipment colour changes appearance. All that moves is where the pointer looks.

⛔ DO NOT ALLOCATE WITH `roster_names.NamePool`. Its free list is every 0xFF run in
POOL_LO..POOL_HI = 0x5000..0x24C000, and 0x5000 is *inside the player table* (chunk 0x1E159C31 at
0x4F24). Used here it hands out addresses inside player records -- the first attempt at this
repair wrote three names on top of live record fields and silently lost 'Nic', 'Coe' and 'Zac'.
This module allocates only from the pool's own free tail.

⭐⭐ THE POOL'S HIGH-WATER MARK IS THE DIRECTORY COUNT. Chunk 0xEB69DFB9's directory `count`
field is a used-byte counter, and the free tail begins at `COUNT_BASE + count` (COUNT_BASE = 231,
derived and re-asserted at run time). The game maintains it: relocating Saros moved count
2,318,989 -> 2,319,013, exactly the 24 bytes of "Jusse\0Saros\0" it appended. Allocating without
bumping count would leave the strings in the region the game hands out next, and the next in-game
save would write straight over them.

⭐ DEDUPLICATE FIRST. 1,150 of the 1,724 exposed names already exist in the pool (26 rows share
one "Alex"). Those cost nothing -- just repoint. Only 545 distinct strings, 8,790 bytes, actually
have to be allocated, out of a 34 KB tail.

SECOND REPAIR, INDEPENDENT OF THE FIRST: `--bindings=<ref.ROS>` copies +0x2C (PA + TV name picker)
and +0x2E (lines_ps) back per row from a reference roster and touches nothing else.

⚠ VERIFY BEFORE WRITING. Everything happens in memory and the whole roster is re-resolved and
compared name-for-name; the file is only written if that comes back clean. The first version
saved first and verified second, which left a damaged save on disk.

    python ros_name_repair.py <Roster.ROS>                        # report only
    python ros_name_repair.py <Roster.ROS> --apply
    python ros_name_repair.py <Roster.ROS> --apply --bindings=<ref.ROS>
    python ros_name_repair.py <Roster.ROS> --apply --rename=398:first=Juuse
"""
from __future__ import annotations

import shutil
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from player_assign import PlayerTable, STRIDE, OFF_LAST, OFF_FIRST
from ros_file import DIR_OFF

POOL_HASH = 0xEB69DFB9
DIR_ENT = 12
COUNT_BASE = 231                     # free tail = COUNT_BASE + directory count
OFF_BIND = 0x2C                      # u16 name picker (PA + TV) then u16 lines_ps
COLOUR_LO, COLOUR_HI = 0x0C0, 0x1A0  # the ARGB grid a colour edit rewrites


def _read(d, a):
    e = a
    while e + 1 < len(d) and d[e:e + 2] not in (b"\x00\x00", b"\xff\xff"):
        e += 2
    return bytes(d[a:e]).decode("utf-16-be", "replace")


def _target(t, row, off):
    fo = t.base + row * STRIDE + off
    v = struct.unpack_from(">i", t.ros.data, fo)[0]
    return None if v == 0 else fo + v - 1


def _snapshot(t):
    out = {}
    for row in range(t.nrec):
        for off in (OFF_LAST, OFF_FIRST):
            a = _target(t, row, off)
            if a is not None:
                out[(row, off)] = _read(t.ros.data, a)
    return out


def exposed(t):
    """[(row, off, target, text, in_colour_array)] for every name stored inside a PLAYER RECORD.

    ⛔⭐⭐ THE OWNING RECORD IS NOT ALWAYS THE REFERRING ONE. The first version of this asked
    "does this pointer aim inside *my* record" (0 <= a - my_base < STRIDE) and so missed the names
    that are deduplicated ACROSS records -- row 2305 "Dostal" pointing at text sitting inline in an
    earlier goalie's colour grid. Fifteen names were left behind that way, and clearing that
    goalie's grid residue then destroyed all fifteen at once. The test is therefore "does this
    pointer aim anywhere inside the player table", and the colour-array question is asked of the
    record the text actually LIVES in.
    """
    out = []
    tbl_lo, tbl_hi = t.base, t.base + t.nrec * STRIDE
    for row in range(t.nrec):
        for off in (OFF_LAST, OFF_FIRST):
            a = _target(t, row, off)
            if a is None or not (tbl_lo <= a < tbl_hi):
                continue
            rel = (a - tbl_lo) % STRIDE                       # offset within the OWNING record
            out.append((row, off, a, _read(t.ros.data, a), COLOUR_LO <= rel < COLOUR_HI))
    return out


class PoolTail:
    """Bump allocator over the string pool's free tail, with dedup against what is already there."""

    def __init__(self, t):
        d = t.ros.data
        self.d = d
        self.dir_fo = None
        for i in range(19):
            fo = DIR_OFF + i * DIR_ENT
            h, cnt, ptr = struct.unpack_from(">III", d, fo)
            if h == POOL_HASH:
                self.dir_fo, self.count = fo, cnt
                self.lo = fo + 8 + ptr - 1
                self.hi = self.lo + self._chunk_size(t, i)
                break
        if self.dir_fo is None:
            raise SystemExit("no string-pool chunk 0xEB69DFB9 in this save")

        self.free = COUNT_BASE + self.count
        # the mark has to land on a terminator with clear space after it, or COUNT_BASE is wrong
        if d[self.free - 2:self.free] not in (b"\x00\x00", b"\xff\xff") or any(d[self.free:self.free + 64]):
            raise SystemExit(f"pool high-water mark 0x{self.free:X} does not look free -- refusing to allocate")
        self.limit = self.free
        while self.limit < self.hi and d[self.limit] == 0:
            self.limit += 1

        self.index = {}
        i = self.lo
        while i < self.hi - 1:
            if d[i] == 0 and 32 <= d[i + 1] < 127:
                j = i
                while j < self.hi - 1 and d[j:j + 2] not in (b"\x00\x00", b"\xff\xff"):
                    j += 2
                txt = bytes(d[i:j]).decode("utf-16-be", "replace")
                if txt:
                    self.index.setdefault(txt, i)
                i = j + 2
            else:
                i += 2

    @staticmethod
    def _chunk_size(t, idx):
        c = next(c for c in t.ros.chunks if c.index == idx)
        return c.size

    def room(self):
        return self.limit - self.free

    def addr_of(self, txt):
        """Address of `txt` in the pool, allocating from the tail if it is not there yet."""
        a = self.index.get(txt)
        if a is not None:
            return a
        blob = txt.encode("utf-16-be") + b"\x00\x00"
        if self.free + len(blob) > self.limit:
            raise SystemExit("string pool tail exhausted")
        a = self.free
        self.d[a:a + len(blob)] = blob
        self.free += len(blob)
        self.index[txt] = a
        return a

    def commit(self):
        """Write the new high-water mark back, the way the game does."""
        struct.pack_into(">I", self.d, self.dir_fo + 4, self.free - COUNT_BASE)


def repair(path, apply=False, bindings=None, renames=(), log=print):
    t = PlayerTable(path)
    before = _snapshot(t)
    for row, off, txt in renames:
        before[(row, off)] = txt

    ex = exposed(t)
    hot = [e for e in ex if e[4]]
    pool = PoolTail(t)
    todo = {e[3] for e in hot} - set(pool.index)
    cost = sum(len(x.encode("utf-16-be")) + 2 for x in todo)

    log(f"{t.nrec} player records")
    log(f"  names stored inside the record        : {len(ex)}")
    log(f"  ... inside the colour array           : {len(hot)}   <- an equipment edit destroys these")
    log(f"  players exposed                       : {len({e[0] for e in hot})}")
    log(f"  of those, text already in the pool    : {len(hot) - sum(1 for e in hot if e[3] in todo)} (repoint, 0 B)")
    log(f"  distinct strings to allocate          : {len(todo)}  ({cost:,} B)")
    log(f"  pool free tail                        : {pool.room():,} B at 0x{pool.free:X}")

    ref = None
    if bindings:
        ref = PlayerTable(bindings)
        if ref.nrec != t.nrec:
            raise SystemExit(f"binding reference has {ref.nrec} rows, this save has {t.nrec}")
        diff = [r for r in range(t.nrec)
                if ref.ros.data[ref.base + r * STRIDE + OFF_BIND:ref.base + r * STRIDE + OFF_BIND + 4]
                != t.ros.data[t.base + r * STRIDE + OFF_BIND:t.base + r * STRIDE + OFF_BIND + 4]]
        log(f"  announcer bindings differing from ref : {len(diff)}")

    if not apply:
        log("\nreport only -- pass --apply to write")
        return

    moved = 0
    for row, off, _a, txt, _ in hot:
        fo = t.base + row * STRIDE + off
        struct.pack_into(">i", t.ros.data, fo, pool.addr_of(txt) - fo + 1)
        moved += 1
    log(f"  relocated {moved} name field(s) into the pool")

    if ref is not None:
        n = 0
        for r in range(t.nrec):
            s = ref.base + r * STRIDE + OFF_BIND
            d = t.base + r * STRIDE + OFF_BIND
            if t.ros.data[d:d + 4] != ref.ros.data[s:s + 4]:
                t.ros.data[d:d + 4] = ref.ros.data[s:s + 4]
                n += 1
        log(f"  restored {n} announcer binding(s)")

    for row, off, txt in renames:
        fo = t.base + row * STRIDE + off
        struct.pack_into(">i", t.ros.data, fo, pool.addr_of(txt) - fo + 1)
        log(f"  renamed row {row} {'first' if off == OFF_FIRST else 'last'} -> {txt!r}")

    pool.commit()

    # -- verify IN MEMORY, before anything reaches the disk ------------------
    after = _snapshot(t)
    bad = [(k, before[k], after.get(k, "<gone>")) for k in before if after.get(k, "<gone>") != before[k]]
    extra = [k for k in after if k not in before]
    still = [e for e in exposed(t) if e[4]]
    log(f"\nVERIFY  names changed unintentionally: {len(bad)}   appeared: {len(extra)}   "
        f"still in the colour array: {len(still)}")
    for (row, off), b, a in bad[:10]:
        log(f"   row {row} +0x{off:02X}  {b!r} -> {a!r}")
    if bad or extra or still:
        raise SystemExit("repair did not verify clean -- nothing was written")

    bak = Path(str(path) + f".bak_namerepair_{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(path, bak)
    log(f"  backup -> {bak.name}")
    t.ros.save(backup=False)

    # -- and again from disk -------------------------------------------------
    v = PlayerTable(path)
    va = _snapshot(v)
    bad = [(k, before[k], va.get(k, "<gone>")) for k in before if va.get(k, "<gone>") != before[k]]
    still = [e for e in exposed(v) if e[4]]
    log(f"VERIFY (from disk)  mismatches: {len(bad)}   still in the colour array: {len(still)}")
    if bad or still:
        raise SystemExit("on-disk verify failed -- restore from the backup above")
    log("VERIFY  clean")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    path = args[0]
    apply_ = "--apply" in args
    bind = next((a.split("=", 1)[1] for a in args if a.startswith("--bindings=")), None)
    ren = []
    for a in args:
        if a.startswith("--rename="):
            row, rest = a.split("=", 1)[1].split(":", 1)
            fld, txt = rest.split("=", 1)
            ren.append((int(row), OFF_FIRST if fld == "first" else OFF_LAST, txt))
    repair(path, apply=apply_, bindings=bind, renames=ren)
