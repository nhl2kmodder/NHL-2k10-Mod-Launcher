"""portrait_512_converter.py — convert ALL 1478 player portraits to 512x512 DXT4_5.

WHY ALL: the portrait cache clones ONE portrait's descriptor as the template for all 128 VRAM slots
(proven in-game: a single 512 portrait re-templated every slot -> all blank -> crash). So sizes can't
be mixed — the whole pack converts in one shot, keeping the template consistent at 512.

LAYOUT (mirrors native, measured from the shipped pack): every mip level is stored tiled with a
minimum footprint of one 32x32-block tile (0x4000 for DXT4_5), the level's blocks at top-left of a
128px canvas when smaller. Native 256: mip0=0x10000 + tail[128 | 64pad | 32pad | 16pad]=0x10000.
Scaled 512: mip0=0x40000 + tail[256=0x10000 | 128=0x4000 | 64pad | 32pad | 16pad]=0x20000.
Descriptor per blob: dims=512, +0x70=0x40000, +0x74=0x20000, +0x94=0x840000FE, +0x9C=0x3FE1FF,
+0xA8=0x40A00 (format fields stay DXT4_5). portrait.iff's read table is re-synced afterwards.

Content: each native 256 mip0 is LANCZOS-upscaled to 512 (no new detail, but block-free at card size);
if a hi-res source exists in Modified/portrait named *_<key>.png, it's used instead (with alpha-bleed).

Usage:  python portrait_512_converter.py [--workers N] [--out PACK.bin] [--apply]
Without --apply it builds + verifies the pack to a temp file only.
"""
import sys, os, struct, glob, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).parent))
import archive_textures as AT
from PIL import Image
import numpy as np

GAME = Path(r"C:\Users\cloug\Documents\NHL 2k10 Extracted")
HIRES_DIR = GAME / "NHL2k10_Extracted_Files" / "Textures" / "Modified" / "portrait"
PACK = "disc_b9610aac.iff"
MIP0_512 = 0x40000
TAIL_512 = 0x20000


def _pad_tile(img_lvl):
    """Place a <=64px level at top-left of a transparent 128px canvas (native min-tile padding)."""
    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    canvas.paste(img_lvl, (0, 0))
    return canvas


def build_512_blob(img512):
    """512x512 RGBA -> native-layout DXT4_5 bytes: mip0 0x40000 + tail 0x20000 (levels 512..16)."""
    out = bytearray(AT._encode_tiled(img512, "DXT4_5", 1))                 # mip0 512
    assert len(out) == MIP0_512
    for wh in (256, 128):                                                  # >= one tile: stored plain
        out += AT._encode_tiled(img512.resize((wh, wh), Image.LANCZOS), "DXT4_5", 1)
    for wh in (64, 32, 16):                                                # sub-tile: top-left padded
        out += AT._encode_tiled(_pad_tile(img512.resize((wh, wh), Image.LANCZOS)), "DXT4_5", 1)
    assert len(out) == MIP0_512 + TAIL_512, hex(len(out))
    return bytes(out)


def patch_descriptor(desc_dec):
    dd = bytearray(desc_dec)
    struct.pack_into(">H", dd, 0x60, 512); struct.pack_into(">H", dd, 0x62, 512)
    struct.pack_into(">I", dd, 0x70, MIP0_512); struct.pack_into(">I", dd, 0x74, TAIL_512)
    struct.pack_into(">I", dd, 0x94, 0x80000000 | ((512 // 32) << 22) | 0x00FE)
    struct.pack_into(">I", dd, 0x9C, 511 | (511 << 13))
    struct.pack_into(">I", dd, 0xA8, (MIP0_512 & ~0xFFF) | 0xA00)
    return bytes(dd)


def convert_one(task):
    """(i, desc_comp, port_comp, dwp, dcodec, pwp, pcodec, hires_path) -> (i, desc_blob, port_blob)."""
    i, dcomp, pcomp, dwp, dcodec, pwp, pcodec, hires = task
    ddec = AT.DF.decompress_codec(dcomp[20:], 0xE0, (1 << dwp) - 1, dwp)
    pdec = AT.DF.decompress_codec(pcomp[20:], 0x20000, (1 << pwp) - 1, pwp)
    if hires:
        src = Image.open(hires).convert("RGBA")
        img = src if src.size == (512, 512) else src.resize((512, 512), Image.LANCZOS)
        img = AT._alpha_bleed(img)                       # external cut-out: fill transparent RGB
    else:
        img = AT.T.decode(pdec[:0x10000], 256, 256, "DXT4_5", 16, 1, 1, 0).convert("RGBA")
        img = img.resize((512, 512), Image.LANCZOS)      # native: backdrop RGB already sane
    new_pdec = build_512_blob(img)
    new_ddec = patch_descriptor(ddec)
    dblob = AT.EE.encode_payload(new_ddec, wparam=dwp, codec=dcodec)
    pblob = AT.EE.encode_payload(new_pdec, wparam=pwp, codec=pcodec)
    for blob, dec in ((dblob, new_ddec), (pblob, new_pdec)):
        err = AT._verify_blob(blob, dec)
        if err:
            raise ValueError(f"blob {i}: {err}")
    return i, dblob, pblob


def main():
    workers = 10
    apply = "--apply" in sys.argv
    out_path = Path(os.environ.get("P512_OUT", str(GAME / "portrait_pack_512.bin")))
    for a in sys.argv:
        if a.startswith("--workers="):
            workers = int(a.split("=")[1])

    t0 = time.time()
    # source = CLEAN pack (pristine 256 content)
    loc, data, size = AT._read_asset(PACK, AT.CLEAN_DIR)
    blobs = AT._walk_blobs(data, size)
    pairs = []
    i = 0
    while i + 1 < len(blobs):
        a, b = blobs[i], blobs[i + 1]
        if a["dec"] is not None and len(a["dec"]) == 0xE0 and b["dec"] is not None and len(b["dec"]) == 0x20000:
            pairs.append((a, b)); i += 2
        else:
            i += 1
    print(f"pairs: {len(pairs)}", flush=True)
    assert len(pairs) == 1478

    # hi-res sources by key -> blob
    key_blob = AT.portrait_key_blob_map()
    hires_by_blob = {}
    for f in glob.glob(str(HIRES_DIR / "*.png")):
        stem = Path(f).stem
        try:
            key = int(stem.split("_")[-1])
        except ValueError:
            continue
        b = key_blob.get(key)
        if b is not None:
            hires_by_blob[b] = f
    print(f"hi-res sources: {len(hires_by_blob)} {sorted(hires_by_blob)[:8]}", flush=True)

    tasks = []
    for idx2, (a, b) in enumerate(pairs):
        dcomp = data[a["off"]:a["off"] + a["tot"]]
        pcomp = data[b["off"]:b["off"] + b["tot"]]
        tasks.append((idx2, dcomp, pcomp, a["wp"], a["codec"], b["wp"], b["codec"],
                      hires_by_blob.get(idx2)))

    results = {}
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(convert_one, t) for t in tasks]
        for fu in as_completed(futs):
            i2, dblob, pblob = fu.result()
            results[i2] = (dblob, pblob)
            done += 1
            if done % 50 == 0 or done == len(tasks):
                el = time.time() - t0
                print(f"  {done}/{len(tasks)}  ({el:.0f}s, {el/done:.2f}s/blob)", flush=True)

    new_pack = bytearray()
    for i2 in range(len(pairs)):
        dblob, pblob = results[i2]
        new_pack += dblob + pblob
    print(f"pack built: {len(new_pack)} bytes ({len(new_pack)/1048576:.0f} MB) in {time.time()-t0:.0f}s", flush=True)

    # self-verify: walk + pair count + spot-decode 3 blobs
    nb = AT._walk_blobs(bytes(new_pack), len(new_pack))
    np_pairs = 0
    j = 0
    while j + 1 < len(nb):
        a, b = nb[j], nb[j + 1]
        if a["dec"] is not None and len(a["dec"]) == 0xE0 and b["dec"] is not None and len(b["dec"]) == MIP0_512 + TAIL_512:
            np_pairs += 1; j += 2
        else:
            j += 1
    print(f"verify: {np_pairs}/1478 pairs OK", flush=True)
    assert np_pairs == 1478
    out_path.write_bytes(bytes(new_pack))
    print(f"wrote {out_path}", flush=True)

    if apply:
        gloc = AT.resolve(PACK, GAME)
        AT._backup_once(GAME / gloc[0], print)
        AT._sync_portrait_index(bytes(new_pack), GAME, print)
        st = AT._relocate(PACK, bytes(new_pack), gloc[3], GAME, 512, 512, "DXT4_5", print)
        print("APPLIED:", st, flush=True)
    else:
        print("built only (run with --apply to write into the game)", flush=True)


if __name__ == "__main__":
    main()
