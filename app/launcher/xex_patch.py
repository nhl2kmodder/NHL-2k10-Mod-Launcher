"""xex_patch.py — map a guest VA to its file offset in the FLAT (XexTool -c u -e u) default.xex
and patch bytes there. Used by the debug lab's 'Apply to XEX' to make live-tweaked constants
permanent.

The flat XEX uses XEX2 BASIC compression (file_format_info key 0x3FF, comp_type 1): the image is
a list of (data_size, zero_size) blocks. Each block stores data_size bytes in the file then
zero_size zero-bytes that exist only in memory (BSS compaction) — so VA->offset is NOT linear.
"""
import struct
from pathlib import Path

IMAGE_BASE_DEFAULT = 0x82000000


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
