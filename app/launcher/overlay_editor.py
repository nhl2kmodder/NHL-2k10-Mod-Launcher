"""overlay_editor.py — read/edit per-element TRANSFORM + COLOR records in overlay IFFs, with
permanent file apply (re-encode DRAM + relocate). Foundation for the Scorebug/HUD tweaker.

Per-element record (overlay_static DRAM, 0x80 stride):
  +0x14 = element name-hash (id)
  +0x1C = X position (float)   +0x20 = Y position (float)
  +0x30,+0x34,+0x38,+0x3C = R,G,B,A color (float 0..1)
  +0x40,+0x44,+0x4C = skew / scale / extra (float)
Records detected by: a float[4] at +0x30 in [0,1] with A>=0.5, an id at +0x14, X/Y floats.
"""
import sys, struct
sys.path.insert(0, '.')
import archive_textures as at
from pathlib import Path

REC = 0x80
F_ID, F_X, F_Y = 0x14, 0x1C, 0x20
F_R, F_G, F_B, F_A = 0x30, 0x34, 0x38, 0x3C


def _load(iff, gdir):
    loc = at.resolve(iff, gdir)
    if not loc:
        raise ValueError(f"{iff}: not found")
    arc, off, size, idx, f3 = loc
    with open(Path(gdir) / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = at._walk_blobs(res, size)
    return res, size, off, arc, idx, blobs


def _is_rec(dram, o):
    if o + REC > len(dram):
        return False
    try:
        rgba = struct.unpack_from(">4f", dram, o + F_R)
    except struct.error:
        return False
    if not all(0.0 <= c <= 1.0 for c in rgba):
        return False
    if rgba[3] < 0.5 or sum(rgba[:3]) < 0.03:
        return False
    idv = struct.unpack_from(">I", dram, o + F_ID)[0]
    x, y = struct.unpack_from(">f", dram, o + F_X)[0], struct.unpack_from(">f", dram, o + F_Y)[0]
    return idv > 0x1000 and abs(x) < 5000 and abs(y) < 5000


def list_elements(iff="overlay_static.iff", gdir=None):
    """Enumerate per-element transform+color records. Returns [{off,id,x,y,rgba}]."""
    res, size, off, arc, idx, blobs = _load(iff, gdir or at.CLEAN_DIR)
    dram = blobs[0]["dec"]
    out = []
    for o in range(0, len(dram) - REC, 4):
        if _is_rec(dram, o):
            idv = struct.unpack_from(">I", dram, o + F_ID)[0]
            x, y = struct.unpack_from(">f", dram, o + F_X)[0], struct.unpack_from(">f", dram, o + F_Y)[0]
            rgba = struct.unpack_from(">4f", dram, o + F_R)
            out.append({"off": o, "id": idv, "x": x, "y": y, "rgba": rgba})
    return out


def apply_edits(edits, iff="overlay_static.iff", gdir=None, log=print):
    """edits = {record_off: {'x':..,'y':..,'rgba':(r,g,b,a)}}. Writes DRAM + relocates the iff."""
    gdir = Path(gdir)
    res, size, off, arc, idx, blobs = _load(iff, gdir)
    if len(blobs) < 2:
        raise ValueError(f"{iff}: expected >=2 blobs")
    dram_b, tex_b = blobs[0], blobs[-1]
    dram = bytearray(dram_b["dec"])
    for ro, ed in edits.items():
        if "x" in ed: struct.pack_into(">f", dram, ro + F_X, float(ed["x"]))
        if "y" in ed: struct.pack_into(">f", dram, ro + F_Y, float(ed["y"]))
        if "rgba" in ed:
            for k, c in enumerate(ed["rgba"]):
                struct.pack_into(">f", dram, ro + F_R + k*4, float(c))
        log(f"  rec@0x{ro:X} id=0x{struct.unpack_from('>I',dram,ro+F_ID)[0]:08X} -> "
            f"x={ed.get('x','-')} y={ed.get('y','-')} rgba={ed.get('rgba','-')}")
    dram_c = at.EE.encode_payload(bytes(dram), wparam=dram_b["wp"], codec=dram_b["codec"])
    err = at._verify_blob(dram_c, bytes(dram))
    if err:
        raise ValueError(f"DRAM re-encode failed: {err}")
    tex_c = res[tex_b["off"]:tex_b["off"] + tex_b["tot"]]
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + tex_c
    at._patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    at._patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(tex_c), dec_size=len(tex_b["dec"]))
    struct.pack_into(">I", new_res, 8, len(new_res))
    at._backup_once(gdir / arc, log)
    return at._relocate(iff, bytes(new_res), idx, gdir, 0, 0, "DXT4_5", log)


def load_dram(iff, gdir):
    """Return (decompressed DRAM blob0 bytes, meta) for an overlay iff. meta has everything
    needed to re-pack + relocate. The debug lab edits the returned bytes then calls apply_dram."""
    res, size, off, arc, idx, blobs = _load(iff, gdir)
    if len(blobs) < 2:
        raise ValueError(f"{iff}: expected >=2 blobs (got {len(blobs)})")
    dram_b, tex_b = blobs[0], blobs[-1]
    meta = {"res": res, "size": size, "off": off, "arc": arc, "idx": idx,
            "dram_b": dram_b, "tex_b": tex_b}
    return bytearray(dram_b["dec"]), meta


def apply_dram(dram, meta, iff, gdir, log=print):
    """Write an edited DRAM blob0 back: re-encode, reuse the texture blob, relocate the iff.
    This is the generic engine behind every debug-lab edit (records, raw pokes, colors, …)."""
    gdir = Path(gdir)
    dram_b, tex_b, res = meta["dram_b"], meta["tex_b"], meta["res"]
    dram_c = at.EE.encode_payload(bytes(dram), wparam=dram_b["wp"], codec=dram_b["codec"])
    err = at._verify_blob(dram_c, bytes(dram))
    if err:
        raise ValueError(f"DRAM re-encode failed: {err}")
    tex_c = res[tex_b["off"]:tex_b["off"] + tex_b["tot"]]
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + tex_c
    at._patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    at._patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(tex_c), dec_size=len(tex_b["dec"]))
    struct.pack_into(">I", new_res, 8, len(new_res))
    at._backup_once(gdir / meta["arc"], log)
    return at._relocate(iff, bytes(new_res), meta["idx"], gdir, 0, 0, "DXT4_5", log)


def restore_from_clean(iff, gdir, log=print):
    """Repoint the iff's TOC entry back to the pristine CLEAN copy (undo all relocations)."""
    import shutil
    gdir = Path(gdir)
    cloc = at.resolve(iff, at.CLEAN_DIR)
    gloc = at.resolve(iff, gdir)
    if not cloc or not gloc:
        raise ValueError(f"{iff}: not found")
    carc, coff, csize, cidx, cf3 = cloc
    with open(at.CLEAN_DIR / carc, "rb") as f:
        f.seek(coff); clean = f.read(csize)
    # write clean bytes back into the game archive at the ORIGINAL offset + repoint TOC
    garc, goff, gsize, gidx, gf3 = gloc
    # restore the TOC f3 (concat offset) to the clean value so it loads the original location
    return at._relocate(iff, clean, cidx, gdir, 0, 0, "DXT4_5", log)


if __name__ == "__main__":
    gdir = at.CLEAN_DIR
    els = list_elements(gdir=gdir)
    print(f"{len(els)} transform+color elements in overlay_static.iff:")
    for e in els:
        r = e["rgba"]
        print(f"  @0x{e['off']:X} id=0x{e['id']:08X} pos=({e['x']:.1f},{e['y']:.1f}) "
              f"rgba=({r[0]:.2f},{r[1]:.2f},{r[2]:.2f},{r[3]:.2f})")
