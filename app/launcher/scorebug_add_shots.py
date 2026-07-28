"""scorebug_add_shots.py — add SOG text elements to the scorebug (overlay_static.iff).

Mechanism (RE 2026-07-27, see findings/08 §5): the scorebug's text elements are
0xE0-stride records in overlay_static blob0 (11 records, keys 0x20B4F8..0x20BDB8).
The engine finds the table via TWO self-relative base pointers (target = field_addr
+ value - 1) at 0x209650 / 0x20969C -> firstkey+0x18, with count at 0x209698.
Each record: +0x00/+0x04 = [crc32('scorebug_text')][crc32-raw(name)] key,
+0x08 self-rel ptr -> inline UTF-16BE name, +0x68/+0x6C X/Y, +0x7C size,
+0x88 color, +0xD8 = BIND HASH (crc32 of UPPERCASED bind name; matched against
the XEX bind-table rows, which supply the string-bank id -> live {TOKEN} text).

This tool relocates the record table to the end of blob0 (grown), appends two new
records cloned from gameclock4 ('shots_away', 'shots_home'), rewrites count and
both base pointers, and gives all three shots elements (gameclock4 = the 'Shots'
label) fresh bind hashes + on-screen default positions.

The matching XEX bind rows are written by scorebug_xex_rows.py.

STATUS: mechanism proven live in-game (string-bank token text renders per frame);
this file-side append is NOT yet verified in-game.
"""
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import overlay_editor as oe

IFF = "overlay_static.iff"

# ---- mapped structure (pristine overlay_static blob0) ----
TABLE_FIRST_KEY = 0x20B4F8          # away_abbrev record key (11 records * 0xE0)
TABLE_COUNT     = 11
REC_STRIDE      = 0xE0
COUNT_OFF       = 0x209698          # u32 record count (11)
BASEPTR_OFFS    = (0x209650, 0x20969C)   # self-rel -> firstkey+0x18
BASE_BIAS       = 0x18              # engine base ptr = firstkey + 0x18
KEY_SCOREBUG_TEXT = 0xE9015CE9      # crc32('scorebug_text')
GAMECLOCK4_KEY  = 0x20BDB8          # template record (unused in stock bug)

# new elements: (record name, bind name, X, Y, size)
# scene coords: +X right, +Y up; main text row Y~-149, bottom bar Y~-171.
NEW_ELEMENTS = [
    ("shots_away",  "SHOTS_AWAY",  -145.0, -166.0, 20.0),
    ("shots_home",  "SHOTS_HOME",   -75.0, -166.0, 20.0),
]
# gameclock4 becomes the label between them
LABEL_BIND = "SHOTS_LABEL"
LABEL_POS  = (-118.0, -166.0, 20.0)


def crc(s: str) -> int:
    return zlib.crc32(s.encode("ascii")) & 0xFFFFFFFF


def _read_selfrel(d, off):
    v = struct.unpack_from(">I", d, off)[0]
    return off + v - 1


def _write_selfrel(d, off, target):
    struct.pack_into(">I", d, off, (target - off + 1) & 0xFFFFFFFF)


def build(dram: bytearray):
    """Mutate dram in place; returns dict of info."""
    d = dram
    # sanity: pristine structure
    cnt = struct.unpack_from(">I", d, COUNT_OFF)[0]
    if cnt != TABLE_COUNT:
        raise RuntimeError(f"count @{COUNT_OFF:#x} = {cnt}, expected {TABLE_COUNT} "
                           "(already modified? wrong blob?)")
    for po in BASEPTR_OFFS:
        t = _read_selfrel(d, po)
        if t != TABLE_FIRST_KEY + BASE_BIAS:
            raise RuntimeError(f"base ptr @{po:#x} -> {t:#x}, expected "
                               f"{TABLE_FIRST_KEY + BASE_BIAS:#x}")
    if struct.unpack_from(">I", d, GAMECLOCK4_KEY)[0] != KEY_SCOREBUG_TEXT:
        raise RuntimeError("gameclock4 record key not found")

    # --- assemble the new table at the (aligned) end of the blob ---
    new_base = (len(d) + 0xF) & ~0xF
    d.extend(b"\x00" * (new_base - len(d)))

    old = bytes(d[TABLE_FIRST_KEY:TABLE_FIRST_KEY + TABLE_COUNT * REC_STRIDE])
    records = [bytearray(old[i * REC_STRIDE:(i + 1) * REC_STRIDE])
               for i in range(TABLE_COUNT)]

    # clone gameclock4 (last record) twice for the new elements
    template = bytes(records[-1])
    new_recs = [bytearray(template) for _ in NEW_ELEMENTS]
    all_recs = records + new_recs

    # lay out: [13 records][name strings]
    recs_bytes = (TABLE_COUNT + len(NEW_ELEMENTS)) * REC_STRIDE
    name_area = new_base + recs_bytes
    names_blob = bytearray()
    name_offsets = {}
    for name, _, _, _, _ in NEW_ELEMENTS:
        name_offsets[name] = name_area + len(names_blob)
        names_blob += name.encode("utf-16-be") + b"\x00\x00"

    # fix each MOVED record's +0x08 name self-rel (old target stays in place)
    for i, rec in enumerate(all_recs):
        old_field = (TABLE_FIRST_KEY + i * REC_STRIDE + 8) if i < TABLE_COUNT else None
        new_field = new_base + i * REC_STRIDE + 8
        if i < TABLE_COUNT:
            target = old_field + struct.unpack_from(">I", rec, 8)[0] - 1
        else:
            name = NEW_ELEMENTS[i - TABLE_COUNT][0]
            target = name_offsets[name]
        struct.pack_into(">I", rec, 8, (target - new_field + 1) & 0xFFFFFFFF)

    # configure the two new records
    for j, (name, bind, x, y, size) in enumerate(NEW_ELEMENTS):
        rec = all_recs[TABLE_COUNT + j]
        struct.pack_into(">I", rec, 0x04, crc(name))          # raw name hash
        struct.pack_into(">f", rec, 0x68, x)
        struct.pack_into(">f", rec, 0x6C, y)
        struct.pack_into(">f", rec, 0x7C, size)
        struct.pack_into(">I", rec, 0xD8, crc(bind))          # bind hash (upper)

    # gameclock4 (index 10) -> the 'Shots' label element
    gc4 = all_recs[10]
    struct.pack_into(">f", gc4, 0x68, LABEL_POS[0])
    struct.pack_into(">f", gc4, 0x6C, LABEL_POS[1])
    struct.pack_into(">f", gc4, 0x7C, LABEL_POS[2])
    struct.pack_into(">I", gc4, 0xD8, crc(LABEL_BIND))

    # write table + names
    for i, rec in enumerate(all_recs):
        d[new_base + i * REC_STRIDE:new_base + (i + 1) * REC_STRIDE] = rec
    d.extend(b"\x00" * (new_base + recs_bytes - len(d)))
    d[name_area:name_area] = names_blob

    # count + base pointers
    struct.pack_into(">I", d, COUNT_OFF, TABLE_COUNT + len(NEW_ELEMENTS))
    for po in BASEPTR_OFFS:
        _write_selfrel(d, po, new_base + BASE_BIAS)

    # neutralize the OLD table keys so signature scans don't double-match
    for i in range(TABLE_COUNT):
        struct.pack_into(">I", d, TABLE_FIRST_KEY + i * REC_STRIDE, 0)

    return {
        "new_base": new_base,
        "count": TABLE_COUNT + len(NEW_ELEMENTS),
        "binds": {LABEL_BIND: crc(LABEL_BIND),
                  **{b: crc(b) for _, b, _, _, _ in NEW_ELEMENTS}},
    }


def apply(gdir: str):
    r = oe.load_dram(IFF, gdir)
    if isinstance(r, tuple):
        dram, meta = bytearray(r[0]), r[1]
    else:
        dram, meta = bytearray(r), None
    info = build(dram)
    oe.apply_dram(bytes(dram), meta, IFF, gdir)
    return info


if __name__ == "__main__":
    gd = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\cloug\Documents\NHL 2k10 Extracted"
    print(apply(gd))
