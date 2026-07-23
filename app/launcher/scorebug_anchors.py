"""scorebug_anchors.py — read/patch the UI anchor-mode table in the flat default.xex.

The table (Ghidra DAT_8499EF10, guest VA 0x8499EF10) is the GLOBAL anchor table consumed by
Scene_AnchorSolvePosition @0x83D2A2E8: 10 entries x 8 bytes, entry = (x_type, y_type) as BE u32.
A UI container's anchor MODE (container+0x48) indexes this table; the solver then pins the
widget to that screen anchor. Modes 1-9 = the full 3x3 grid; mode 0 = "no anchor".

  x_type: 1=Left  2=Center  5=Right
  y_type: 3=Top   4=Bottom  5=Middle     (live-verified 2026-06-25: mode2 (5,3)=top-right,
                                          mode3 (2,3)=top-center, stock scorebug mode7 (1,4)
                                          renders bottom-left)

The SCORECLOCK (in-game scorebug) root container is hardcoded to mode 7 by
Scorebug_UpdateActive @0x840C1160 — so patching mode 7's pair moves the whole scoreclock to
any of the 9 anchors. CAVEAT: everything else that uses mode 7 (e.g. the instant-replay
watermark) moves with it. Patch is read at game launch -> takes effect on next launch.

File offsets are resolved per-XEX via xex_patch.va_to_offset (XEX2 basic-compression block
list), then sanity-checked against the table's invariants before any write:
  - mode 0 == (0,0)
  - modes 1-9 values within the legal x/y sets
  - the 16B record array that follows (+0x50: 2C 96 F1 AF .. 83 BE 60 18) is in place
"""
import struct
from pathlib import Path

try:
    from . import xex_patch
except ImportError:                       # run as a loose script from launcher/
    import xex_patch

ANCHOR_TABLE_VA = 0x8499EF10             # mode 0 entry; modes 0..9 follow at +8*mode
N_MODES         = 10
SCOREBUG_MODE   = 7

X_NAMES = {1: "Left", 2: "Center", 5: "Right"}
Y_NAMES = {3: "Top", 4: "Bottom", 5: "Middle"}
X_VALS  = {v: k for k, v in X_NAMES.items()}
Y_VALS  = {v: k for k, v in Y_NAMES.items()}

# Pristine table (default_flat_ORIG.xex). Restore = write these back; no backup file needed.
STOCK = {
    1: (1, 3), 2: (5, 3), 3: (2, 3),      # Top-Left, Top-Right, Top-Center
    4: (1, 5), 5: (5, 5), 6: (2, 5),      # Middle-Left, Middle-Right, Middle-Center
    7: (1, 4), 8: (5, 4), 9: (2, 4),      # Bottom-Left, Bottom-Right, Bottom-Center
}

# What is known to READ each mode (from RE / marker tests). Unlisted = not yet identified;
# identify by setting a mode to an extreme corner, booting, and seeing what moved.
MODE_USERS = {
    7: "Scoreclock (scorebug root) + instant-replay watermark",
}


def pos_name(x, y):
    """(1,4) -> 'Bottom-Left'. Unknown values render as the raw ints."""
    return f"{Y_NAMES.get(y, f'y={y}')}-{X_NAMES.get(x, f'x={x}')}"


def table_offset(xex_path):
    """File offset of the mode-0 entry in this XEX, after validating the table really is
    there (mode-0 zeros, legal anchor values, trailing record-array signature)."""
    off = xex_patch.va_to_offset(xex_path, ANCHOR_TABLE_VA)
    if off is None:
        raise ValueError("anchor table VA not mapped by this XEX (not the flat basefile?)")
    with open(xex_path, "rb") as f:
        f.seek(off)
        raw = f.read(N_MODES * 8 + 0x10)
    if raw[0:8] != b"\x00" * 8:
        raise ValueError(f"anchor table check failed @0x{off:X}: mode 0 not (0,0)")
    for m in range(1, N_MODES):
        x, y = struct.unpack_from(">II", raw, m * 8)
        if x not in X_NAMES or y not in Y_NAMES:
            raise ValueError(f"anchor table check failed: mode {m} = ({x},{y}) out of range")
    if raw[0x50:0x54] != b"\x2C\x96\xF1\xAF" or raw[0x58:0x5C] != b"\x83\xBE\x60\x18":
        raise ValueError("anchor table check failed: trailing record-array signature missing")
    return off


def read_table(xex_path):
    """{mode: (x, y)} for modes 1..9 (validated)."""
    off = table_offset(xex_path)
    with open(xex_path, "rb") as f:
        f.seek(off)
        raw = f.read(N_MODES * 8)
    return {m: struct.unpack_from(">II", raw, m * 8) for m in range(1, N_MODES)}


def write_modes(xex_path, edits, log=print):
    """edits = {mode: (x, y)}. Validates the table, then patches each entry in place.
    Returns the number of entries actually changed (already-matching entries are skipped)."""
    for m, (x, y) in edits.items():
        if not 1 <= m < N_MODES:
            raise ValueError(f"mode {m} is not editable (1-{N_MODES - 1})")
        if x not in X_NAMES or y not in Y_NAMES:
            raise ValueError(f"mode {m}: ({x},{y}) is not a legal anchor pair")
    off = table_offset(xex_path)
    changed = 0
    try:
        with open(xex_path, "r+b") as f:
            for m, (x, y) in sorted(edits.items()):
                f.seek(off + m * 8)
                cur = struct.unpack(">II", f.read(8))
                if cur == (x, y):
                    continue
                f.seek(off + m * 8)
                f.write(struct.pack(">II", x, y))
                changed += 1
                log(f"  anchor mode {m}: {pos_name(*cur)} -> {pos_name(x, y)} "
                    f"@0x{off + m * 8:X}")
    except PermissionError:
        raise ValueError("XEX is locked (game running?) — close NHL 2k10 and retry")
    return changed


def get_scorebug_anchor(xex_path):
    """(x, y) of the scoreclock's anchor slot (mode 7)."""
    return read_table(xex_path)[SCOREBUG_MODE]


def set_scorebug_anchor(xex_path, x, y, log=print):
    """Move the scoreclock: patch mode 7 to (x, y). Effect on next game launch."""
    return write_modes(xex_path, {SCOREBUG_MODE: (x, y)}, log)


def restore_stock(xex_path, modes=None, log=print):
    """Write the pristine pairs back for `modes` (default: all)."""
    modes = modes or list(STOCK)
    return write_modes(xex_path, {m: STOCK[m] for m in modes}, log)


if __name__ == "__main__":
    XEX = r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    off = table_offset(XEX)
    print(f"anchor table @file 0x{off:X} (VA 0x{ANCHOR_TABLE_VA:X})")
    for m, (x, y) in read_table(XEX).items():
        tag = " <- SCORECLOCK" if m == SCOREBUG_MODE else ""
        mod = "" if STOCK[m] == (x, y) else f"   [stock {pos_name(*STOCK[m])}]"
        print(f"  mode {m}: ({x},{y}) {pos_name(x, y)}{mod}{tag}")
