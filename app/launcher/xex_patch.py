"""xex_patch.py — map a guest VA to its file offset in the FLAT (XexTool -c u -e u) default.xex
and patch bytes there. Used by the debug lab's 'Apply to XEX' to make live-tweaked constants
permanent.

The flat XEX uses XEX2 BASIC compression (file_format_info key 0x3FF, comp_type 1): the image is
a list of (data_size, zero_size) blocks. Each block stores data_size bytes in the file then
zero_size zero-bytes that exist only in memory (BSS compaction) — so VA->offset is NOT linear.
"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

IMAGE_BASE_DEFAULT = 0x82000000


def get_comp_type(xex_path):
    """(enc_type, comp_type) from the file_format_info header. comp_type 1 = basic (flat, what
    every VA patcher here needs); 2 = LZX-compressed (the stock disc/retail form)."""
    data = Path(xex_path).read_bytes()
    if data[:4] != b"XEX2":
        raise ValueError("not an XEX2 file")
    count = struct.unpack_from(">I", data, 0x14)[0]
    for i in range(count):
        key, val = struct.unpack_from(">II", data, 0x18 + i*8)
        if key == 0x000003FF:
            _sz, enc_type, comp_type = struct.unpack_from(">IHH", data, val)
            return enc_type, comp_type
    raise ValueError("no file_format_info (0x3FF) header")


def find_xextool(game_dir=None):
    """Locate xextool.exe: launcher tools\\ (next to the frozen exe / dev tree), the game folder,
    then PATH. Returns '' if absent."""
    cands = []
    try:
        cands.append(Path(sys.executable).parent / "tools" / "xextool.exe")   # frozen exe dir
    except Exception:
        pass
    cands.append(Path(__file__).resolve().parent.parent / "tools" / "xextool.exe")  # dev tree
    if game_dir:
        cands.append(Path(game_dir) / "xextool.exe")
    for c in cands:
        if c.is_file():
            return str(c)
    return shutil.which("xextool") or ""


def ensure_flat(xex_path, game_dir=None, log=print):
    """Make sure `xex_path` is the FLAT (basic-compression, unencrypted) form every VA patcher
    here requires. A stock retail default.xex ships LZX-compressed (comp_type 2, usually
    encrypted); the game runs either form identically under Xenia, so converting in place is
    safe. Conversion shells out to XexTool (-c u -e u); the original file is kept once as
    <xex>.compressed.orig. Returns a one-line status; raises with a friendly message when the
    file is compressed and XexTool can't be found/run."""
    xex_path = Path(xex_path)
    enc, comp = get_comp_type(xex_path)
    if comp == 1 and enc == 0:
        return "already flat"
    tool = find_xextool(game_dir)
    if not tool:
        raise ValueError(
            f"default.xex is compressed (comp_type={comp}) and xextool.exe was not found — "
            "place xextool.exe in the launcher's tools folder or the game folder")
    bak = xex_path.with_suffix(xex_path.suffix + ".compressed.orig")
    if not bak.exists():
        shutil.copyfile(xex_path, bak)
    tmp = xex_path.with_suffix(xex_path.suffix + ".flat_tmp")
    try:
        r = subprocess.run(
            [tool, "-c", "u", "-e", "u", "-o", str(tmp), str(xex_path)],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            raise ValueError(f"XexTool failed (rc={r.returncode}): "
                             f"{(r.stdout or '').strip()[-200:]} {(r.stderr or '').strip()[-200:]}")
        enc2, comp2 = get_comp_type(tmp)
        if comp2 != 1:
            raise ValueError(f"XexTool output still comp_type={comp2} — aborting")
        parse_basic_blocks(tmp)                       # full sanity: block list parses
        shutil.move(str(tmp), str(xex_path))
    finally:
        if tmp.exists():
            try: tmp.unlink()
            except OSError: pass
    log(f"  default.xex auto-flattened (was comp_type={comp}, enc={enc}); "
        f"original kept as {bak.name}")
    return f"auto-flattened from comp_type={comp} (original kept as {bak.name})"


def parse_basic_blocks(xex_path):
    """Return (data_start, image_base, [(data_size, zero_size), ...]) or raise."""
    data = Path(xex_path).read_bytes()
    if data[:4] != b"XEX2":
        raise ValueError("not an XEX2 file")
    header_size = struct.unpack_from(">I", data, 0x08)[0]      # = file offset where image begins
    count = struct.unpack_from(">I", data, 0x14)[0]
    image_base = IMAGE_BASE_DEFAULT
    ffi_off = None
    for i in range(count):
        key, val = struct.unpack_from(">II", data, 0x18 + i*8)
        if key == 0x00010201:          # image base (inline)
            image_base = val
        elif key == 0x000003FF:        # file format info (offset to struct)
            ffi_off = val
    if ffi_off is None:
        raise ValueError("no file_format_info (0x3FF) header")
    info_size, enc_type, comp_type = struct.unpack_from(">IHH", data, ffi_off)
    if comp_type != 1:
        raise ValueError(f"comp_type={comp_type} (expected 1=basic). Re-export flat with XexTool -c u -e u")
    nblocks = (info_size - 8) // 8
    blocks = [struct.unpack_from(">II", data, ffi_off + 8 + j*8) for j in range(nblocks)]
    return header_size, image_base, blocks


def va_to_offset(xex_path, va):
    """Guest VA -> flat-XEX file offset, or None if VA lands in a zero (BSS) gap / out of range."""
    data_start, image_base, blocks = parse_basic_blocks(xex_path)
    img_off = va - image_base
    if img_off < 0:
        return None
    mem = 0; file = data_start
    for data_size, zero_size in blocks:
        if img_off < mem + data_size:                  # inside the data portion -> patchable
            return file + (img_off - mem)
        if img_off < mem + data_size + zero_size:       # inside the zero gap -> not in file
            return None
        mem += data_size + zero_size
        file += data_size
    return None


def patch_va(xex_path, va, new_bytes, expect=None, log=print):
    """Write new_bytes at the file offset for `va`. If `expect` (bytes) is given, verify the
    current file bytes match it first (safety). Returns the file offset patched."""
    off = va_to_offset(xex_path, va)
    if off is None:
        raise ValueError(f"VA 0x{va:X} not patchable (out of range or in a zeroed BSS gap)")
    with open(xex_path, "r+b") as f:
        f.seek(off); cur = f.read(len(new_bytes))
        if expect is not None and cur != expect:
            raise ValueError(f"verify failed @0x{off:X}: file has {cur.hex()} not {expect.hex()} "
                             f"(VA mapping wrong or already patched)")
        f.seek(off); f.write(new_bytes)
    log(f"  XEX patched @0x{off:X} (VA 0x{va:X}): {new_bytes.hex()}")
    return off


if __name__ == "__main__":
    XEX = r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    hs, ib, blks = parse_basic_blocks(XEX)
    print(f"data_start=0x{hs:X} image_base=0x{ib:X} blocks={len(blks)}")
    for va, want in ((0x8499EF48, 0x2354F48), (0x8499EF10, 0x2354F10)):
        got = va_to_offset(XEX, va)
        print(f"  VA 0x{va:X} -> 0x{got:X}" + (f"  MATCH 0x{want:X}" if got == want else f"  !! expected 0x{want:X}"))
