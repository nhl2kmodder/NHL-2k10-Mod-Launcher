"""scorebug_xex_rows.py — XEX side of the scorebug SOG elements mod (v2, in-place).

Adds three text BIND ROWS for the new scorebug elements (shots_away /
gameclock4-as-label / shots_home). Row format (16B): [scene_hash][bind_hash]
[callback][slot_ptr]; the slot dword holds a STRING-BANK id; cb 0x83BE6018 =
the generic per-frame text setter ({TOKEN}s resolved live). findings/08 §5.

The scorebug's primary bind table (0x8499FE50..0x8499FF30 + null @0x8499FF40)
has no slack, so the table is RELOCATED — v2 places it in the zero padding at
the tail of the .reloc section (last real reloc byte @ VA 0x851A22A4; padding
runs to the image block end 0x851A8000; relocs are load-time-only and Xenon
images never rebase). New table @ VA 0x851A7000. No file resize, no block-map
changes (v1's zero-block->data conversion made Xenia unmap the region — dead
end, do not retry). The registry pointer @0x849A0E7C is repointed.

STATUS: not yet verified in-game.
"""
import shutil
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import xex_patch

OLD_TABLE_VA    = 0x8499FE50
OLD_TABLE_ROWS  = 15                 # rows before the null terminator @0x8499FF40
REGISTRY_PTR_VA = 0x849A0E7C         # scorebug registry entry +0x20 (root hash @+0x1C)
SCOREBUG_HASH   = 0x41267075
CB_TEXT         = 0x83BE6018
NEW_TABLE_VA    = 0x851A7000         # .reloc tail padding (zeros to 0x851A8000)

# (bind hash, string-bank id) — hashes = crc32(upper name), ids exist in english bank
NEW_ROWS = [
    (0x72135BFD, 0x34EF4867),   # SHOTS_AWAY  -> '{0:PT_SUBJECT:TEAM_AWAY:STAT:SHOTS:VALUE}'
    (0x07C5503A, 0x125F4849),   # SHOTS_LABEL -> 'Shots'
    (0xA59AF5FC, 0xD09D14AD),   # SHOTS_HOME  -> '{0:PT_SUBJECT:TEAM_HOME:STAT:SHOTS:VALUE}'
]


def apply(xex_path):
    xex_path = str(xex_path)
    data = bytearray(Path(xex_path).read_bytes())

    reg_off = xex_patch.va_to_offset(xex_path, REGISTRY_PTR_VA)
    cur = struct.unpack_from(">I", data, reg_off)[0]
    if cur == NEW_TABLE_VA:
        return "already patched"
    if cur != OLD_TABLE_VA:
        raise RuntimeError(f"registry ptr = {cur:08X}, expected {OLD_TABLE_VA:08X}")
    rh = struct.unpack_from(">I", data, xex_patch.va_to_offset(xex_path, REGISTRY_PTR_VA - 4))[0]
    if rh != SCOREBUG_HASH:
        raise RuntimeError(f"registry root hash = {rh:08X}, expected scorebug")

    tbl_off = xex_patch.va_to_offset(xex_path, NEW_TABLE_VA)
    if tbl_off is None:
        raise RuntimeError("new table VA not file-backed")
    need = (OLD_TABLE_ROWS + len(NEW_ROWS) + 1) * 16 + len(NEW_ROWS) * 4
    if any(data[tbl_off:tbl_off + need]):
        raise RuntimeError("target padding not zero — refusing")

    old_off = xex_patch.va_to_offset(xex_path, OLD_TABLE_VA)
    rows = bytes(data[old_off:old_off + OLD_TABLE_ROWS * 16])
    ids_va = NEW_TABLE_VA + (OLD_TABLE_ROWS + len(NEW_ROWS) + 1) * 16
    out = bytearray(rows)
    for i, (bh, _sid) in enumerate(NEW_ROWS):
        out += struct.pack(">4I", SCOREBUG_HASH, bh, CB_TEXT, ids_va + i * 4)
    out += b"\x00" * 16
    for _bh, sid in NEW_ROWS:
        out += struct.pack(">I", sid)
    data[tbl_off:tbl_off + len(out)] = out
    struct.pack_into(">I", data, reg_off, NEW_TABLE_VA)

    bak = xex_path + ".sogbak"
    if not Path(bak).exists():
        shutil.copyfile(xex_path, bak)
    Path(xex_path).write_bytes(bytes(data))
    return f"patched in place: table @ {NEW_TABLE_VA:08X}, ids @ {ids_va:08X} (no resize)"


def revert(xex_path):
    """Surgically undo apply(): repoint the registry back to the stock table and re-zero the
    relocated table in the .reloc tail padding. Safer than restoring .sogbak (which may predate
    other, unrelated XEX edits). Idempotent — returns 'not patched' when already stock."""
    xex_path = str(xex_path)
    data = bytearray(Path(xex_path).read_bytes())
    reg_off = xex_patch.va_to_offset(xex_path, REGISTRY_PTR_VA)
    cur = struct.unpack_from(">I", data, reg_off)[0]
    if cur == OLD_TABLE_VA:
        return "not patched"
    if cur != NEW_TABLE_VA:
        raise RuntimeError(f"registry ptr = {cur:08X}, expected {NEW_TABLE_VA:08X} — not this mod")
    tbl_off = xex_patch.va_to_offset(xex_path, NEW_TABLE_VA)
    need = (OLD_TABLE_ROWS + len(NEW_ROWS) + 1) * 16 + len(NEW_ROWS) * 4
    data[tbl_off:tbl_off + need] = b"\x00" * need
    struct.pack_into(">I", data, reg_off, OLD_TABLE_VA)
    Path(xex_path).write_bytes(bytes(data))
    return f"reverted: registry -> {OLD_TABLE_VA:08X}, relocated table re-zeroed"


def restore(xex_path):
    bak = str(xex_path) + ".sogbak"
    if not Path(bak).exists():
        return "no backup"
    shutil.copyfile(bak, str(xex_path))
    return "restored"


if __name__ == "__main__":
    xex = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    if len(sys.argv) > 2 and sys.argv[2] == "restore":
        print(restore(xex))
    else:
        print(apply(xex))
