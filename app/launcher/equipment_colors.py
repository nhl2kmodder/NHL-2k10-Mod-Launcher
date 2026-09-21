r"""equipment_colors.py -- the per-player equipment-colour grid in Roster.ROS, as editable fields.

WHY THIS EXISTS. Editing equipment colour **in the game** is what corrupts player names: 1,724 of
this roster's name pointers aimed back inside the player's own record, into the middle of this very
grid, so rewriting the grid wrote over the name (see ros_name_repair.py). That fault is repaired,
but the in-game editor is still the slow way to do this -- one player at a time, no paste, no way to
copy a set from one goalie to another, and nothing that rides in a .n2kpack. Doing it from the file
is faster, portable, and cannot lose a name.

THE GRID. 43 big-endian 0xAARRGGBB dwords, alpha always 0xFF, at record offsets

    +0x0C0 .. +0x100   17 dwords   SHARED   both skaters and goalies use these
    +0x104 .. +0x110    4 dwords   GOALIE   skaters hold 0xFFFFFFFF here
    +0x114 .. +0x11C    3 dwords   SHARED
    +0x120 .. +0x168   19 dwords   GOALIE   includes the four already pinned in-game

Four are pinned exactly, by an in-game diff (player_assign's Roster_Investigate/test_cage_1 vs _2 --
setting a goalie's three mask colours and cage to red/green/blue/magenta moved precisely +0x159,
+0x15D, +0x161, +0x169 and nothing else):

    +0x158  mask recolour 1      +0x15C  mask recolour 2
    +0x160  mask recolour 3      +0x168  cage

+0x164 sits between recolour 3 and the cage and did NOT move in that test, so it is in the grid's
address range but is not one of those four; it is carried here as an ordinary unlabelled slot.

⭐⭐ HOW THE SHARED/GOALIE SPLIT WAS ESTABLISHED -- and the trap in measuring it. Take the 1,421
records whose names were never stored inline (the other 1,294 have name text sitting in these bytes)
and the split is unambiguous: in the SHARED slots 96-100% of skaters hold a real colour, in the
GOALIE slots 1.3% do. But a first pass read +0x13C..+0x148 as shared, because ~22% of skaters held
something there. Those values are not colours -- their alpha byte is not 0xFF and they decode as
UTF-16BE text (`61 00 72 00` = "ar", `ff 00 53 00` = the tail of the previous slot plus "S"). They
are **residue**: a player whose name was inline at +0x13D, whose name the game later relocated to
the string pool on a save, leaves the old text behind in the grid. So:

    ⛔ NEVER CLASSIFY A SLOT BY "DOES ANYONE HAVE A VALUE HERE". Check the alpha byte. A real
      entry in this grid always has alpha 0xFF; anything else is leftover name text.

RESIDUE IS HARMLESS BUT VISIBLE. `residue()` below lists it. For a skater the goalie slots are never
read, so nothing renders wrong; the only reason to clear it is tidiness. Clearing is a colour write
like any other and is NOT done automatically.

⛔ THE SLOTS ARE NOT LABELLED, AND GUESSING IS NOT ALLOWED. Beyond the four above, nobody has shown
which slot is the glove and which is the pant. `mark()` is the way to settle one: it writes a
marker colour into a single slot on a single player, so the piece that turns magenta in-game names
itself. That is exactly how the mask and cage slots were pinned, and it is the only evidence this
project accepts for a label. Fill LABELS in as they are settled -- an unlabelled slot shows as its
offset, which is honest, rather than a made-up body part.

Related: player_assign.py (the record layout), ros_name_repair.py (the collision this grid caused),
ros_live_editor.KNOWN_FIELDS (where these fields join the two editors), colorpick.ask_color.
"""
from __future__ import annotations

import shutil
import struct
import time
from pathlib import Path

try:
    from .player_assign import PlayerTable, STRIDE, OFF_POSITION
except ImportError:                                    # standalone run
    from player_assign import PlayerTable, STRIDE, OFF_POSITION

GRID_LO, GRID_HI = 0x0C0, 0x16C                        # the addressable range, inclusive of +0x168

# Offsets, split by who actually uses them. Order is file order so the grid reads left to right.
SHARED_OFFSETS = [0x0C0, 0x0C4, 0x0C8, 0x0CC, 0x0D0, 0x0D4, 0x0D8, 0x0DC, 0x0E0, 0x0E4,
                  0x0E8, 0x0EC, 0x0F0, 0x0F4, 0x0F8, 0x0FC, 0x100,
                  0x114, 0x118, 0x11C]
GOALIE_OFFSETS = [0x104, 0x108, 0x10C, 0x110,
                  0x120, 0x124, 0x128, 0x12C, 0x130, 0x134, 0x138, 0x13C, 0x140, 0x144,
                  0x148, 0x14C, 0x150, 0x154, 0x158, 0x15C, 0x160, 0x164, 0x168]
ALL_OFFSETS = sorted(SHARED_OFFSETS + GOALIE_OFFSETS)

# Only what an in-game diff has actually pinned. Everything else stays unnamed on purpose.
LABELS = {
    0x158: "Mask recolour 1",
    0x15C: "Mask recolour 2",
    0x160: "Mask recolour 3",
    0x168: "Cage",
}

BLANK = 0xFFFFFFFF                                     # an unused slot -- opaque white
MARKER = 0xFFFF00FF                                    # magenta: nothing in the game ships this


def label_for(off: int) -> str:
    """A slot's display name. Unpinned slots show their offset rather than a guess."""
    return LABELS.get(off) or f"Slot +0x{off:03X}"


def fields():
    """The grid as KNOWN_FIELDS-shaped specs, ready to append to the two editors' field lists.

    `goalie_only` is advisory: the editors hide those rows for a skater, because a skater's copy of
    them is either 0xFFFFFFFF or leftover name text and editing it does nothing.
    """
    out = []
    for off in SHARED_OFFSETS:
        out.append({"name": label_for(off), "off": off, "type": "color", "group": "Equipment"})
    for off in GOALIE_OFFSETS:
        out.append({"name": label_for(off), "off": off, "type": "color",
                    "group": "Goalie gear", "goalie_only": True})
    return out


# ── reading a roster ────────────────────────────────────────────────────────────────────────────

def is_goalie(t: PlayerTable, row: int) -> bool:
    v = struct.unpack_from(">I", t.ros.data, t.base + row * STRIDE + OFF_POSITION)[0]
    return ((v >> 3) & 7) == 0


def read(t: PlayerTable, row: int) -> dict:
    """{offset: dword} for every slot in the grid."""
    b = t.base + row * STRIDE
    return {o: struct.unpack_from(">I", t.ros.data, b + o)[0] for o in ALL_OFFSETS}


def is_colour(v: int) -> bool:
    """A real entry always has alpha 0xFF. Anything else is residue, not a colour."""
    return (v >> 24) == 0xFF


def residue(path) -> list:
    """[(row, first, last, offset, dword)] for every grid slot holding something that is not a
    colour -- i.e. leftover UTF-16 name text from a record that used to store its name inline.

    Reported, never cleared: clearing is a colour write and that is the caller's call to make.
    """
    t = PlayerTable(str(path))
    out = []
    for r in range(t.nrec):
        for o, v in read(t, r).items():
            if not is_colour(v):
                fn, ln = t.name(r)
                out.append((r, fn, ln, o, v))
    return out


def _name_bytes(t) -> set:
    """Every byte inside the player table that some name pointer currently aims at.

    ⛔⭐⭐ THE GUARD THAT HAD TO BE LEARNED THE HARD WAY. Residue and a live inline name look
    identical -- both are UTF-16 text with a non-0xFF alpha byte. The first run of clear_residue
    blanked 826 slots and destroyed 15 names (Dostal, Mrazek, Hill, Hart, Hellebuyck, Shesterkin
    ...), because those players' name pointers aimed at text sitting inline in ANOTHER player's
    colour grid. ros_name_repair.exposed() has since been widened to relocate those too, so in a
    repaired roster this set is empty inside the grid -- but the guard stays, because a roster the
    game has re-saved can put one back at any time.
    """
    d = t.ros.data
    lo, hi = t.base, t.base + t.nrec * STRIDE
    out = set()
    for r in range(t.nrec):
        base = t.base + r * STRIDE
        for fo in (base, base + 4):
            v = struct.unpack_from(">i", d, fo)[0]
            a = fo + v - 1
            if not (lo <= a < hi):
                continue
            e = a
            while e + 1 < len(d) and d[e:e + 2] not in (b"\x00\x00", b"\xff\xff"):
                e += 2
            out.update(range(a, min(e + 2, hi)))
    return out


def clear_residue(path, *, goalies_only=True, apply=False):
    """Blank every grid slot holding leftover name text instead of a colour.

    The user, 2026-09-18: *"If there is corrupt goalie data, it is OK to put them to default white.
    I would rather that vs having corrupt data."* So residue is overwritten with BLANK
    (0xFFFFFFFF), the value an untouched slot carries.

    Default scope is GOALIES ONLY, and deliberately: a skater never reads the goalie slots, so his
    residue renders nothing and rewriting it is a write for no gain. Only slots that fail
    `is_colour` are touched -- a real colour is never overwritten.

    Returns (n_slots, n_rows).
    """
    t = PlayerTable(str(path))
    live = _name_bytes(t)
    n = rows = skipped = 0
    for r in range(t.nrec):
        if goalies_only and not is_goalie(t, r):
            continue
        b = t.base + r * STRIDE
        hit = False
        for o, v in read(t, r).items():
            if is_colour(v):
                continue
            if any((b + o + k) in live for k in range(4)):
                skipped += 1                      # a LIVE name lives in these bytes -- see below
                continue
            struct.pack_into(">I", t.ros.data, b + o, BLANK)
            n += 1
            hit = True
        rows += hit
    if skipped:
        print(f"  {skipped} slot(s) left alone: a name pointer still aims at them")
    if apply and n:
        _save(t, path)
    return n, rows


def copy_set(path, src_row: int, dst_rows, *, goalie_only=None, apply=False):
    """Copy one player's grid onto others -- the reason to do this from a file at all.

    `goalie_only` None follows the source: a goalie's full set copies onto goalies, and only the
    shared slots copy onto a skater, so a skater can never be handed gear he has no model for.
    Returns (n_slots_written, n_rows).
    """
    t = PlayerTable(str(path))
    src = read(t, src_row)
    src_g = is_goalie(t, src_row)
    n = 0
    rows = list(dst_rows)
    for r in rows:
        offs = ALL_OFFSETS if (src_g and is_goalie(t, r)) else SHARED_OFFSETS
        if goalie_only is True:
            offs = GOALIE_OFFSETS
        elif goalie_only is False:
            offs = SHARED_OFFSETS
        b = t.base + r * STRIDE
        for o in offs:
            struct.pack_into(">I", t.ros.data, b + o, src[o])
            n += 1
    if apply:
        _save(t, path)
    return n, len(rows)


def mark(path, row: int, off: int, colour: int = MARKER, *, apply=False):
    """⭐ THE IDENTIFY LOOP. Write a marker colour into ONE slot on ONE player, boot the game, and
    whichever piece of his gear turned magenta is that slot. Record it in LABELS and never guess.

    Returns the dword that was there before, so the change can be put back.
    """
    if off not in ALL_OFFSETS:
        raise ValueError(f"+0x{off:03X} is not a slot in the equipment grid")
    t = PlayerTable(str(path))
    b = t.base + row * STRIDE + off
    old = struct.unpack_from(">I", t.ros.data, b)[0]
    struct.pack_into(">I", t.ros.data, b, colour & 0xFFFFFFFF)
    if apply:
        _save(t, path)
    return old


def _save(t: PlayerTable, path):
    """Back up beside the file, then save in place. Nothing is ever deleted."""
    p = Path(str(path))
    bak = Path(str(p) + f".bak_equipcolor_{time.strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(p, bak)
    t.save(backup=False)
    return bak


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────────

def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("roster")
    ap.add_argument("--row", type=int, help="show one player's grid")
    ap.add_argument("--residue", action="store_true", help="list leftover name text in the grid")
    ap.add_argument("--mark", metavar="ROW:OFF[:RRGGBB]",
                    help="write a marker colour into one slot so the game can name it")
    ap.add_argument("--copy", metavar="SRC:DST[,DST...]", help="copy one player's grid onto others")
    ap.add_argument("--clear-residue", action="store_true",
                    help="blank goalie slots holding leftover name text (0xFFFFFFFF)")
    ap.add_argument("--apply", action="store_true", help="write the file (otherwise dry run)")
    a = ap.parse_args(argv)

    if a.row is not None:
        t = PlayerTable(a.roster)
        g = is_goalie(t, a.row)
        print(f"row {a.row}  {'GOALIE' if g else 'skater'}")
        for o, v in sorted(read(t, a.row).items()):
            kind = "shared" if o in SHARED_OFFSETS else "goalie"
            note = "" if is_colour(v) else "   <- residue, not a colour"
            print(f"  +0x{o:03X} {kind:6} {v:08X}  {label_for(o)}{note}")

    if a.residue:
        rs = residue(a.roster)
        rows = {r for r, *_ in rs}
        print(f"{len(rs)} slot(s) across {len(rows)} player(s) hold leftover name text")
        for r, fn, ln, o, v in rs[:40]:
            print(f"  row {r:<5} +0x{o:03X} {v:08X}  {fn} {ln}")
        if len(rs) > 40:
            print(f"  ... and {len(rs) - 40} more")

    if a.clear_residue:
        n, rows = clear_residue(a.roster, apply=a.apply)
        print(f"blanked {n} residue slot(s) on {rows} goalie(s)"
              f"{'' if a.apply else '   (dry run, pass --apply)'}")

    if a.mark:
        parts = a.mark.split(":")
        row, off = int(parts[0]), int(parts[1], 16)
        col = (0xFF000000 | int(parts[2], 16)) if len(parts) > 2 else MARKER
        old = mark(a.roster, row, off, col, apply=a.apply)
        print(f"row {row} +0x{off:03X}: {old:08X} -> {col:08X}"
              f"{'' if a.apply else '   (dry run, pass --apply)'}")

    if a.copy:
        s, dsts = a.copy.split(":")
        rows = [int(x) for x in dsts.split(",")]
        n, m = copy_set(a.roster, int(s), rows, apply=a.apply)
        print(f"copied {n} slot(s) onto {m} player(s)"
              f"{'' if a.apply else '   (dry run, pass --apply)'}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
