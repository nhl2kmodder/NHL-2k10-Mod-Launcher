"""
NHL 2K10 — Per-screen live texture capture
===========================================
For the "loader-repacked" packs (global.iff, franchise.iff, …) the file does NOT tabulate
each sub-texture's offset (record +0x6C == 1) and the loader re-packs + de-duplicates them
into VRAM in an order the file doesn't encode. So they can't be split purely offline.

This tool captures whatever screen is LOADED right now in the running game and maps each of
its textures back to its REAL offset inside the source .iff:
  1. find the loaded records blob in guest RAM (by the file's records-header signature),
  2. read each record's resolved +0x6C  = the texture's live VRAM pointer (+ dims/fmt),
  3. read that texture's bytes from VRAM and CONTENT-MATCH them against the file's texture
     blob -> the texture's true file offset (byte-identical; de-dups detected),
  4. emit a catalog (iff, file_offset, w, h, fmt, dupcount) + extracted PNGs.

Run on each screen you visit; the catalog accumulates. Later this feeds archive_textures so
those offsets become extract/repack targets.

    python live_capture.py global.iff
    python live_capture.py            # auto: tries the known loader-repacked packs
"""
import sys, os, struct, json, time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import xenia_mem as xm
import archive_textures as at
T = at.T

REPACKED = ["global.iff", "franchise.iff", "gamedata.iff", "jukebox.iff",
            "online.iff", "playercreate.iff", "frontend_sync.iff"]
# Capture output lives beside the game files (<game>/live_capture/), matching where
# archive_textures.LIVE_CATALOG is repointed at startup. Set via set_out_root(); the module-level
# default is only a placeholder — a path baked in from one dev machine wrote captures into a folder
# that doesn't exist on any other install.
OUT_ROOT = Path("live_capture")
CATALOG = OUT_ROOT / "live_offsets.json"


def set_out_root(game_dir):
    """Point capture output at <game_dir>/live_capture/ (call before capturing)."""
    global OUT_ROOT, CATALOG
    OUT_ROOT = Path(game_dir) / "live_capture"
    CATALOG = OUT_ROOT / "live_offsets.json"
    return OUT_ROOT


def be16(b, o): return struct.unpack_from(">H", b, o)[0]
def be32(b, o): return struct.unpack_from(">I", b, o)[0]


def _parse_rec(buf, base):
    """Parse a 0xE0 texture record -> dims/fmt/mip0 + resolved +0x6C pointer (or None)."""
    if base + 0xE0 > len(buf):
        return None
    w = be16(buf, base + 0x60); h = be16(buf, base + 0x62)
    if not (4 <= w <= 8192 and 4 <= h <= 8192):
        return None
    m0 = be32(buf, base + 0x70)
    if m0 <= 0:
        return None
    for o in range(base, base + 0xE0 - 12, 4):
        d0, d1, d2 = struct.unpack_from(">III", buf, o)
        if (d0 & 3) == 2 and (d2 & 0x1FFF) + 1 == w and ((d2 >> 13) & 0x1FFF) + 1 == h \
           and (d1 & 0x3F) in at._FETCH_FMT:
            nm, bpu, blk = T.FMT[d1 & 0x3F]
            return dict(w=w, h=h, fmt=nm, bpu=bpu, block=blk, tiled=(d0 >> 31) & 1,
                        mip0=m0, ptr=be32(buf, base + 0x6C))
    return None


def _strong_sig(dram, want=24):
    """A distinctive window (16-aligned) of `dram` with many distinct nonzero bytes -> (offset,bytes).
    Used to locate a loaded records blob whose 16-byte header is too weak (mostly zero) to find."""
    best = None
    for o in range(0, min(len(dram), 0x4000), 16):
        w = dram[o:o + want]
        if len(w) < want:
            break
        nz = len(set(w)) - (1 if 0 in w else 0)
        if nz >= 14 and w.count(0) <= 6:
            return o, w
        if best is None or len(set(w)) > len(set(best[1])):
            best = (o, w)
    return best if best else (0, dram[:want])


def _scan(handle, phys_base, pattern):
    """First host address of `pattern` in the guest physical window, or None."""
    plen = len(pattern)
    for base, sz in xm.enum_committed_regions(handle, phys_base, xm.PHYS_SIZE):
        off = 0
        while off < sz:
            n = min(0x100000, sz - off)
            chunk = xm.read_bytes(handle, base + off, n)
            if chunk:
                j = chunk.find(pattern)
                if j >= 0:
                    return base + off + j
            off += n - plen if n > plen else n
    return None


def _read_region(handle, base, cap):
    """Read up to `cap` bytes from `base` (the committed region it lands in), chunked."""
    out = bytearray()
    while len(out) < cap:
        n = min(0x100000, cap - len(out))
        chunk = xm.read_bytes(handle, base + len(out), n)
        if not chunk:
            break
        out += chunk
    return bytes(out)


def capture(iff, handle, phys_base, save_png=True):
    """Capture the currently-loaded textures of `iff` -> list of catalog entries."""
    loc, data, size = at._read_asset(iff, at.CLEAN_DIR)
    if loc is None:
        return []
    blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
    if len(blobs) < 2:
        return []
    fdram = min(blobs, key=len); ftex = max(blobs, key=len)
    rec_base = _scan(handle, phys_base, fdram[:16])
    if rec_base is None:
        # fdram[:16] can be weak (leading count + zeros, e.g. gamedata.iff) -> locate the records
        # blob by a distinctive DEEP window instead, and back-compute the blob base.
        so, sw = _strong_sig(fdram)
        if sw:
            m = _scan(handle, phys_base, sw)
            if m is not None:
                rec_base = m - so
    if rec_base is None:
        return []
    mem = _read_region(handle, rec_base, min(len(fdram), 0x1800000))

    # enumerate currently-loaded records (resolved +0x6C pointing into VRAM)
    recs = []; b = 0
    while b + 0xE0 <= len(mem):
        r = _parse_rec(mem, b)
        if r is not None and 0xA0000000 <= r["ptr"] < 0xC0000000:
            recs.append(r); b += 0xE0
        else:
            b += 4
    if not recs:
        return []

    out_dir = OUT_ROOT / iff[:-4]
    if save_png:
        out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for i, r in enumerate(recs):
        vram = xm.read_bytes(handle, phys_base + (r["ptr"] & 0x1FFFFFFF), r["mip0"])
        if not vram:
            continue
        foff = ftex.find(vram)                       # full-footprint key = unique
        if foff < 0:
            continue
        dup = ftex.count(vram) if r["mip0"] <= 0x40000 else 1
        e = dict(iff=iff, file_offset=foff, w=r["w"], h=r["h"], fmt=r["fmt"],
                 bpu=r["bpu"], block=r["block"], tiled=r["tiled"], mip0=r["mip0"], dup=dup)
        entries.append(e)
        if save_png:
            try:
                img = T.decode(ftex[foff:foff + r["mip0"]], r["w"], r["h"], r["fmt"],
                               r["bpu"], r["block"], r["tiled"], 0).convert("RGBA")
                img.save(out_dir / f"{foff:08x}_{r['w']}x{r['h']}_{r['fmt']}.png")
            except Exception:
                pass
    return entries


def _merge_catalog(new):
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    cat = {}
    if CATALOG.exists():
        for e in json.loads(CATALOG.read_text()):
            cat[(e["iff"], e["file_offset"])] = e
    for e in new:
        cat[(e["iff"], e["file_offset"])] = e
    rows = sorted(cat.values(), key=lambda e: (e["iff"], e["file_offset"]))
    CATALOG.write_text(json.dumps(rows, indent=1))
    return rows


def main(argv):
    targets = [argv[1]] if len(argv) > 1 else REPACKED
    pid = xm.find_pid()
    if not pid:
        print("Xenia not running."); return
    h = xm.open_process(pid)
    phys = xm.find_phys_base(h) or xm.PHYS_BASE
    print(f"attached pid={pid}  phys_base=0x{phys:X}")
    found_any = []
    for iff in targets:
        t = time.time()
        e = capture(iff, h, phys)
        if e:
            uniq = len({x["file_offset"] for x in e})
            print(f"  {iff:20} loaded: {len(e)} textures ({uniq} unique file offsets)  {time.time()-t:.1f}s")
            found_any += e
        else:
            print(f"  {iff:20} not currently loaded")
    xm.close_handle(h)
    if found_any:
        rows = _merge_catalog(found_any)
        print(f"\ncatalog now holds {len(rows)} entries -> {CATALOG}")
        print(f"PNGs -> {OUT_ROOT}\\<asset>\\")
    else:
        print("\nNothing captured — navigate to a screen that uses these packs, then re-run.")


if __name__ == "__main__":
    main(sys.argv)
