"""Xenos GPU texture tiling/untiling for Xbox 360 DXT1 / DXT5 textures.

Validated against 1024×1024 DXT5: 100% block match (65536/65536).
Formula sourced from Xenia texture_address.h (Tiled2D addressing).
"""
import struct

_FMT_PARAMS = {
    'DXT1': (8,  3),
    'BC1':  (8,  3),
    'DXT5': (16, 4),
    'BC3':  (16, 4),
}

# Cache tiled-block index mappings: (bw, bh, l2b) → list[int]
_MAP_CACHE: dict = {}


def _bparams(fmt: str):
    p = _FMT_PARAMS.get(fmt.upper())
    if p is None:
        raise ValueError(f'Unsupported format: {fmt!r}  (supported: DXT1, DXT5)')
    return p


def _tiled_idx(lx: int, ly: int, bw: int, l2b: int) -> int:
    """Xenos Tiled2D block address formula (Xenia texture_address.h)."""
    P     = (bw + 31) & ~31
    outer = ((ly >> 5) * (P >> 5) + (lx >> 5)) << 6
    inner = (((ly >> 1) & 7) << 3) | (lx & 7)
    oib   = (outer | inner) << l2b
    bank  = (ly >> 4) & 1
    pipe  = ((lx >> 3) & 3) ^ (((ly >> 3) & 1) << 1)
    r  = oib & 0xF
    r |= (ly & 1)         << 4
    r |= ((oib >> 4) & 1) << 5
    r |= pipe             << 6
    r |= ((oib >> 5) & 7) << 8
    r |= bank             << 11
    r |= (oib >> 8)       << 12
    return r >> l2b


def _swap8in16(data: bytes) -> bytes:
    """Xbox 360 8-in-16 endian correction: swap every adjacent byte pair."""
    b = bytearray(data)
    for i in range(0, len(b) - 1, 2):
        b[i], b[i + 1] = b[i + 1], b[i]
    return bytes(b)


def _get_mapping(bw: int, bh: int, l2b: int) -> list:
    """Return linear→tiled block index map (cached per unique dimension/format)."""
    key = (bw, bh, l2b)
    if key not in _MAP_CACHE:
        m = [_tiled_idx(lx, ly, bw, l2b)
             for ly in range(bh)
             for lx in range(bw)]
        _MAP_CACHE[key] = m
    return _MAP_CACHE[key]


def texture_size(width: int, height: int, fmt: str) -> int:
    """Raw byte size of a single-mip tiled texture."""
    bpb, _ = _bparams(fmt)
    return (width // 4) * (height // 4) * bpb


def untile(tiled_raw: bytes, width: int, height: int, fmt: str) -> bytes:
    """Convert Xenos-tiled DXT data to linear DXT (DDS-ready)."""
    bpb, l2b = _bparams(fmt)
    bw  = width  // 4
    bh  = height // 4
    n   = bw * bh
    mapping = _get_mapping(bw, bh, l2b)
    swapped = _swap8in16(tiled_raw)
    out = bytearray(n * bpb)
    for dst, src in enumerate(mapping):
        out[dst * bpb:(dst + 1) * bpb] = swapped[src * bpb:(src + 1) * bpb]
    return bytes(out)


def retile(linear: bytes, width: int, height: int, fmt: str) -> bytes:
    """Convert linear DXT (from DDS) back to Xenos-tiled format for memory injection."""
    bpb, l2b = _bparams(fmt)
    bw  = width  // 4
    bh  = height // 4
    n   = bw * bh
    mapping = _get_mapping(bw, bh, l2b)
    out = bytearray(n * bpb)
    for src, dst in enumerate(mapping):
        out[dst * bpb:(dst + 1) * bpb] = linear[src * bpb:(src + 1) * bpb]
    return _swap8in16(out)


def wrap_dds(linear: bytes, width: int, height: int, fmt: str) -> bytes:
    """Wrap linear DXT data in a minimal DDS container."""
    bpb, _  = _bparams(fmt)
    fourcc  = b'DXT1' if bpb == 8 else b'DXT5'
    linsize = (width // 4) * (height // 4) * bpb
    hdr     = bytearray()
    hdr += b'DDS '
    hdr += struct.pack('<I', 124)
    hdr += struct.pack('<I', 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)
    hdr += struct.pack('<I', height)
    hdr += struct.pack('<I', width)
    hdr += struct.pack('<I', linsize)
    hdr += struct.pack('<I', 0)
    hdr += struct.pack('<I', 1)
    hdr += b'\x00' * 44
    hdr += struct.pack('<I', 32)
    hdr += struct.pack('<I', 0x4)
    hdr += fourcc
    hdr += struct.pack('<I', 0) * 5
    hdr += struct.pack('<I', 0x1000)
    hdr += struct.pack('<I', 0) * 4
    return bytes(hdr) + linear


def parse_dds(dds: bytes) -> tuple:
    """Parse DDS bytes → (linear_data, width, height, fmt).  Raises ValueError on bad input."""
    if len(dds) < 128 or dds[:4] != b'DDS ':
        raise ValueError('Not a DDS file (missing magic)')
    height = struct.unpack_from('<I', dds, 12)[0]
    width  = struct.unpack_from('<I', dds, 16)[0]
    fourcc = dds[84:88]
    if fourcc == b'DXT1':
        fmt = 'DXT1'
    elif fourcc == b'DXT5':
        fmt = 'DXT5'
    else:
        raise ValueError(f'Unsupported DDS fourcc {fourcc!r} — only DXT1/DXT5 supported')
    return dds[128:], width, height, fmt
