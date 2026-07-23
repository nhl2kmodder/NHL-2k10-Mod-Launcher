"""Add BRAND-NEW assets to the NHL 2K10 archives (not just replace existing ones).

The 0A TOC is full (2407/2407) but has 312 bytes of zero slack after it = room for ~19
NEW 16-byte TOC entries before the first asset (0x9800). So a custom asset = append its
data at the end of 1B (like the texture relocate) + write a new TOC entry in the slack +
bump the entry count at 0x10 + grow 1B's size at 0x48. The engine reads the count from
0x10, so the new name resolves in-game. Verified end-to-end on a faithful mini archive.

Use for custom goalie masks: occupy an UNUSED helmet name slot (172 free, e.g.
helmet_g02_pattern_06.iff), then set one goalie's mask model+pattern roster fields to it.
"""
import struct, zlib, shutil
from pathlib import Path
from PIL import Image

try:
    from . import archive_textures as AT
except ImportError:
    import archive_textures as AT

ALIGN = 0x800
SLACK_END = 0x9800          # first asset begins here; TOC slack is [0x96c8, 0x9800)


def build_mask_payload(template_name: str, edited_path, clean_dir=None, log=print) -> bytes:
    """Encode a custom image into a same-format mask container (a TEMPLATE asset from CLEAN)
    and return the finished asset bytes — ready to hand to add_custom_asset(). Reuses the
    exact rebuild/encode/verify path as archive_textures.replace()."""
    clean_dir = Path(clean_dir or AT.CLEAN_DIR)
    loc = AT.resolve(template_name, clean_dir)
    if not loc:
        raise ValueError(f"template {template_name} not found")
    arc, off, size, idx, f3 = loc
    with open(clean_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = AT._walk_blobs(res, size)
    vram, fetch = AT._find_primary(blobs)
    if not vram:
        raise ValueError(f"template {template_name}: no decodable primary texture")
    fmt, bpu, block, w, h, tiled, mip0 = fetch
    if fmt not in AT.REPLACE_FORMATS:
        raise ValueError(f"template format {fmt} not encodable")
    img = Image.open(edited_path).convert("RGBA")
    if img.size != (w, h):
        log(f"  fitting custom image {img.size} -> {w}x{h}")
        img = img.resize((w, h), Image.LANCZOS)
    new_dec = AT._rebuild_with_mips(vram["dec"], 0, fmt, w, h, tiled, img, log,
                                    ref_dec=AT._clean_ref_blob(template_name, 0))
    new_blob = AT.EE.encode_payload(new_dec)
    err = AT._verify_blob(new_blob, new_dec)
    if err:
        raise ValueError(f"payload encode failed: {err}")
    old_tot = vram["tot"]; vo = vram["off"]
    if len(new_blob) <= old_tot:
        new_res = bytearray(res)
        new_res[vo:vo + old_tot] = new_blob + b"\x00" * (old_tot - len(new_blob))
    else:
        new_res = bytearray(res[:vo] + new_blob + res[vo + old_tot:])
        AT._patch_grown_iff(new_res, vo, len(new_blob))
    return bytes(new_res)


def _toc_f2(d0a, i):
    return AT._BE(d0a, 0x58 + i * 16 + 8)          # name-hash (f2) of TOC entry i


def add_custom_asset(game_dir, new_name: str, payload: bytes, log=print) -> str:
    """Append `payload` to the end of 1B and register a NEW TOC entry for `new_name`
    (hash-addressed). Writes one-time .orig backups of 0A and 1B. Returns a status string.
    Raises if the TOC slack (19 entries) is exhausted or the name already exists.

    IMPORTANT — the TOC is stored **sorted by name-hash** and the engine BINARY-SEARCHES it, so a
    new entry must be INSERTED in sorted position (not appended at the end, which the search never
    finds — verified in-game: end-appended masks render invisible). Data still appends to 1B; only
    the 16-byte TOC entry is inserted in order, shifting the higher-hash entries down one slot."""
    game_dir = Path(game_dir)
    a0 = game_dir / "0A"; a1b = game_dir / "1B"
    h = zlib.crc32(new_name.upper().encode("ascii")) & 0xffffffff
    if AT.resolve(new_name, game_dir) is not None:
        raise ValueError(f"{new_name} already exists in TOC (use replace, not add)")

    d0a = bytearray(open(a0, "rb").read(SLACK_END))
    count = AT._BE(d0a, 0x10)
    if 0x58 + (count + 1) * 16 > SLACK_END:
        raise RuntimeError(f"TOC slack exhausted at {count} entries — no room for a new asset")
    sizes = [AT._BE(d0a, 0x18 + i * 16) * ALIGN for i in range(4)]
    base1b = sizes[0] + sizes[1] + sizes[2]

    # sorted insertion position (binary search on the hash-sorted TOC)
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        if _toc_f2(d0a, mid) < h:
            lo = mid + 1
        else:
            hi = mid
    pos = lo

    AT._backup_once(a0, log); AT._backup_once(a1b, log)
    old_1b = a1b.stat().st_size
    new_local = (old_1b + ALIGN - 1) & ~(ALIGN - 1)         # 0x800-aligned append point
    blob = payload + b"\x00" * ((-len(payload)) % ALIGN)
    with open(a1b, "r+b") as f:
        if new_local > old_1b:
            f.seek(old_1b); f.write(b"\x00" * (new_local - old_1b))
        f.seek(new_local); f.write(blob)
    total_1b = new_local + len(blob)
    new_sectors = (total_1b + ALIGN - 1) // ALIGN
    new_concat = base1b + new_local

    # shift entries [pos, count) down one slot, then write the new entry at `pos`
    ts = 0x58
    d0a[ts + (pos + 1) * 16 : ts + (count + 1) * 16] = bytes(d0a[ts + pos * 16 : ts + count * 16])
    struct.pack_into(">IIII", d0a, ts + pos * 16, 0, len(payload), h, new_concat // ALIGN)  # [flags,size,hash,f3]
    struct.pack_into(">I", d0a, 0x18 + 3 * 16, new_sectors)    # grow 1B size (header entry 3 @0x48)
    struct.pack_into(">I", d0a, 0x10, count + 1)               # bump entry count
    with open(a0, "r+b") as f:
        f.seek(0); f.write(d0a)
    return (f"ADDED {new_name} -> 1B @0x{new_local:x} (TOC sorted-insert at idx {pos}/{count}, "
            f"f3={new_concat // ALIGN}, {len(payload)} bytes); count {count}->{count+1}")


def add_custom_mask(game_dir, model: int, pattern: int, edited_image, clean_dir=None,
                    template=None, log=print) -> str:
    """Full convenience: build a custom mask texture from `edited_image` and register it at
    helmet_g{model}_pattern_{pattern}.iff (must be an UNUSED slot). Returns status.
    Then set a goalie's mask model/pattern roster fields to (model-1, pattern) to wear it."""
    name = f"helmet_g{model:02d}_pattern_{pattern:02d}.iff"
    # template = an existing same-shell mask of the right format (e.g. g{model}_pattern_05)
    template = template or f"helmet_g{model:02d}_pattern_00.iff"
    payload = build_mask_payload(template, edited_image, clean_dir, log)
    return add_custom_asset(game_dir, name, payload, log)
