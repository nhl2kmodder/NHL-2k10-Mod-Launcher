"""dump_overlay_names.py — enumerate EVERY scene-node name in an overlay iff's DRAM blob0.

The overlay scene graph stores each node's name as an inline UTF-16BE string (twice). Scanning
for those strings recovers the FULL roster of editable elements (far more than the hard-coded
lists in scorebug_layout.py). A node is a MESH (vertex/index geometry, hide/move candidate) when
a header [crc32(name)][FF FF FF FF FF FF 00 00] (or the logo_2k variant [00 00 FF FF FF FF 00 00])
exists; otherwise it's a joint/group/text node.

crc32 here is over the RAW case-sensitive name (intra-scene refs), NOT uppercased (asset refs).

Usage:  python dump_overlay_names.py [overlay_static.iff]
"""
import re, struct, zlib, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import overlay_editor as oe

GAME = r"C:\Users\cloug\Documents\NHL 2k10 Extracted"
IFF = sys.argv[1] if len(sys.argv) > 1 else "overlay_static.iff"


def crc(s):
    return zlib.crc32(s.encode()) & 0xFFFFFFFF


def dump(iff=IFF, gdir=GAME):
    dram, _ = oe.load_dram(iff, gdir)
    dram = bytes(dram)
    names = {}
    i, n = 0, len(dram)
    while i < n - 3:
        if dram[i] == 0 and 0x20 <= dram[i + 1] < 0x7F:
            j, chars = i, []
            while j < n - 1 and dram[j] == 0 and 0x20 <= dram[j + 1] < 0x7F:
                chars.append(chr(dram[j + 1]))
                j += 2
            s = "".join(chars)
            if len(s) >= 3 and re.match(r'^[A-Za-z0-9_]+$', s):
                names.setdefault(s, []).append(i)
            i = j
        else:
            i += 1
    print(f"{iff}: DRAM {len(dram)} B, {len(names)} distinct node names\n")
    for s in sorted(names):
        h = struct.pack(">I", crc(s))
        mesh = (dram.find(h + b"\xFF\xFF\xFF\xFF\xFF\xFF\x00\x00") >= 0 or
                dram.find(h + b"\x00\x00\xFF\xFF\xFF\xFF\x00\x00") >= 0)
        # earliest header offset locates the node's DRAM cluster (which scene it belongs to)
        off = min(names[s])
        print(f"  {s:30} x{len(names[s]):<3} crc={crc(s):08X} @0x{off:06X}{' [MESH]' if mesh else ''}")
    return names


if __name__ == "__main__":
    dump()
