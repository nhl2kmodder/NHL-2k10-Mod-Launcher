"""Scorebug node tool — parse + edit the scorebug widget node array in overlay_static.iff
(the layout SOURCE: the engine rebuilds the scorebug from this file on load). Each node is a
0x40-byte record: id@+0x00, (a,b,a,b)@+0x18, scale(float)@+0x28, type@+0x30/+0x3C.
Edits go into the DRAM blob (blob0); the texture blob (blob1) is reused unchanged; the resource
is relocated (proven safe for overlay_static)."""
import sys, struct
sys.path.insert(0, '.')
import archive_textures as at
from pathlib import Path

ROOT_ID = 0x523CBCEB          # first scorebug node (array starts here)


def _load(iff, game_dir):
    loc = at.resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found")
    arc, off, size, idx, f3 = loc
    with open(Path(game_dir) / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = at._walk_blobs(res, size)
    return res, size, off, arc, idx, blobs


def _node_array_start(dram):
    p = dram.find(struct.pack(">I", ROOT_ID))
    return p if p >= 0 else None


def parse_nodes(iff="overlay_static.iff", game_dir=None, n=16):
    game_dir = Path(game_dir)
    res, size, off, arc, idx, blobs = _load(iff, game_dir)
    dram = blobs[0]["dec"]
    start = _node_array_start(dram)
    if start is None:
        raise ValueError("scorebug node array not found")
    out = []
    for i in range(n):
        o = start + i * 0x40
        if o + 0x40 > len(dram):
            break
        nid = struct.unpack_from(">I", dram, o)[0]
        a, b = struct.unpack_from(">I", dram, o + 0x18), struct.unpack_from(">I", dram, o + 0x1C)
        scale = struct.unpack_from(">f", dram, o + 0x28)[0]
        t = struct.unpack_from(">I", dram, o + 0x3C)[0]
        # stop when we leave the consistent node block (type field stops being 3)
        if i > 0 and t != 3 and nid not in (0,):
            pass
        out.append({"i": i, "off": o, "id": nid, "a": a[0], "b": b[0], "scale": scale, "t": t})
    return out, start


def set_nodes(edits, iff="overlay_static.iff", game_dir=None, log=print):
    """edits = {node_index: {'a':int,'b':int,'scale':float}}. Writes the modified DRAM + reuses the
    texture blob, relocates the iff."""
    game_dir = Path(game_dir)
    res, size, off, arc, idx, blobs = _load(iff, game_dir)
    if len(blobs) < 2:
        raise ValueError(f"{iff}: expected 2 blobs")
    dram_b, tex_b = blobs[0], blobs[-1]
    dram = bytearray(dram_b["dec"])
    start = _node_array_start(dram)
    if start is None:
        raise ValueError("node array not found")
    for ni, ed in edits.items():
        o = start + ni * 0x40
        if "a" in ed:
            struct.pack_into(">I", dram, o + 0x18, ed["a"]); struct.pack_into(">I", dram, o + 0x20, ed["a"])
        if "b" in ed:
            struct.pack_into(">I", dram, o + 0x1C, ed["b"]); struct.pack_into(">I", dram, o + 0x24, ed["b"])
        if "scale" in ed:
            struct.pack_into(">f", dram, o + 0x28, ed["scale"])
        log(f"  node[{ni}] id=0x{struct.unpack_from('>I',dram,o)[0]:08X} -> a={ed.get('a','-')} b={ed.get('b','-')} scale={ed.get('scale','-')}")
    dram_c = at.EE.encode_payload(bytes(dram), wparam=dram_b["wp"], codec=dram_b["codec"])
    err = at._verify_blob(dram_c, bytes(dram))
    if err:
        raise ValueError(f"DRAM re-encode failed: {err}")
    tex_c = res[tex_b["off"]:tex_b["off"] + tex_b["tot"]]            # texture blob unchanged
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + tex_c
    at._patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    at._patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(tex_c), dec_size=len(tex_b["dec"]))
    struct.pack_into(">I", new_res, 8, len(new_res))
    at._backup_once(Path(game_dir) / arc, log)
    return at._relocate(iff, bytes(new_res), idx, game_dir, 0, 0, "DXT4_5", log)


if __name__ == "__main__":
    GAME = Path(r"C:\Users\cloug\Documents\NHL 2k10 Extracted")
    nodes, start = parse_nodes(game_dir=GAME)
    print(f"node array @ DRAM 0x{start:X}  ({len(nodes)} nodes):")
    for nd in nodes:
        print(f"  [{nd['i']:2}] id=0x{nd['id']:08X}  a={nd['a']:4} b={nd['b']:4}  scale={nd['scale']:.2f}  t={nd['t']}")
