"""Character skeletons (rigs) from `global.iff`.

The animation clips in `default.xex` (see `animations.py`) drive a bone hierarchy that is
**not** in the executable — it ships inside the model asset `global.iff`, immediately in
front of each model's submesh table (doc 22 / 24).

Bone record — 48 bytes (0x30), big-endian:

    +0x00  float[4]  bind translation in MODEL space (w = 1.0)
    +0x10  float[4]  bind translation, LOCAL to the parent (w = 1.0)
    +0x20  u32       -> bone name, UTF-16BE (a fix-up token on disk, a pointer in RAM)
    +0x24  u32       crc32(name) - STABLE across models, so it is the cross-model bone id
    +0x28  s16       parent index          (-1 on the root)
    +0x2A  s16       first child index     (-1 = leaf)
    +0x2C  s16       next sibling index    (-1 = last child)
    +0x2E  s16       0

The file is redundant twice over, which is what makes the layout self-proving rather than
inferred: `+0x2A/+0x2C` must agree with `+0x28` (`verify()`), and `+0x00` must equal `+0x10`
accumulated down the tree (`check_world()`). Both hold exactly on every model.

The runtime consumer is `Anim_TransformPoseBatch @0x83B47920` (v1.0), which loops
`skeleton+0x60` (bone count) over the array at `skeleton+0x64` at stride 0x30, testing
`+0x28` against the previously processed index, and writes a 0x20-byte-per-bone palette.

Bone names survive in the asset. `+0x20` is a pointer to the bone's name (relocated at
load), the names sit in per-model UTF-16BE pools in the same blob, and `+0x24` is
`crc32(name)` in plain ASCII -- confirmed against the live game, where the goalie rig reads
back `def_Pelvis, def_Spine, def_Spine1, def_Spine2, def_Neck, def_Head, def_L_Clavicle...`.
`bone_names()` rebuilds the whole table straight from the file: 888/888 bones on all 20 rigs.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from pathlib import Path

try:
    from . import arena_model as _AM
except ImportError:                                     # pragma: no cover - direct import
    import arena_model as _AM

STRIDE = 0x30
MODEL_ASSET = "global.iff"

# Skeleton bases inside the decompressed blob 0 of global.iff. Each sits just ahead of the
# matching submesh table from doc 22's inventory.
KNOWN = {
    "skater":  0x1322AF0,       # submesh table 0x1323AF4 - the decoded player (doc 22)
    "goalie":  0x6444F0,        # submesh table 0x6456F0  - the goalie        (doc 24)
}

# Bone names are NOT stripped. `+0x20` is a pointer to the bone's name, stored on disk as a
# fix-up token and relocated at load; the names themselves live in per-model UTF-16BE string
# pools inside the same blob (the skater's is at 0x58C708, right behind the model name).
# And `+0x24` is simply **crc32 of the plain-ASCII name**, verified live against the running
# game. So every bone is named by harvesting the blob's strings and matching the hash --
# which resolves 888/888 bones across all 20 rigs.
_NAME_RE = re.compile(rb"(?:\x00[\x20-\x7e]){2,}")

_BLOB_CACHE: dict[str, bytes] = {}
_NAMES_CACHE: dict[int, dict[int, str]] = {}


def bone_names(blob: bytes) -> dict[int, str]:
    """Map bone-name hash (`+0x24`) -> name, harvested from the asset's own string pools.

    Every UTF-16BE ASCII string in the blob is hashed with crc32; a bone's `+0x24` is that
    hash of its plain-ASCII name. Cached per blob.
    """
    key = id(blob)
    if key not in _NAMES_CACHE:
        out: dict[int, str] = {}
        for m in _NAME_RE.finditer(blob):
            s = m.group()[1::2].decode("ascii")
            out.setdefault(zlib.crc32(s.encode()) & 0xFFFFFFFF, s)
        _NAMES_CACHE[key] = out
    return _NAMES_CACHE[key]





# ────────────────────────────── reading ──────────────────────────────

def model_blob(clean_dir=None, refresh: bool = False) -> bytes:
    """Decompressed blob 0 of global.iff (~22.5 MB). Cached per process."""
    key = str(clean_dir)
    if refresh:
        _BLOB_CACHE.pop(key, None)
    if key not in _BLOB_CACHE:
        _BLOB_CACHE[key] = bytes(_AM.dram(MODEL_ASSET, clean_dir))
    return _BLOB_CACHE[key]


def _bone(blob: bytes, base: int, k: int, names: dict[int, str]) -> dict:
    o = base + k * STRIDE
    va = struct.unpack_from(">4f", blob, o)
    vb = struct.unpack_from(">4f", blob, o + 0x10)
    nptr, h = struct.unpack_from(">II", blob, o + 0x20)
    parent, child, sib, flag = struct.unpack_from(">4h", blob, o + 0x28)
    return {
        "index": k, "world": va[:3], "offset": vb[:3], "name_ptr": nptr, "hash": h,
        "parent": parent, "first_child": child, "next_sibling": sib, "flag": flag,
        "name": names.get(h, "bone_%08X" % h),
    }


def read(blob: bytes, base: int, limit: int = 400) -> list[dict]:
    """Read the bone array at `base`. The count is not stored next to the array on disk,
    so it is recovered from the tree invariants (parent < index, links in range)."""
    names = bone_names(blob)
    bones: list[dict] = []
    for k in range(limit):
        if base + (k + 1) * STRIDE > len(blob):
            break
        b = _bone(blob, base, k, names)
        if k == 0:
            if b["parent"] != -1:
                break
        elif not (0 <= b["parent"] < k):
            break
        if b["flag"] != 0:
            break
        if b["first_child"] != -1 and not (0 < b["first_child"] <= limit):
            break
        if b["next_sibling"] != -1 and not (0 < b["next_sibling"] <= limit):
            break
        bones.append(b)
    # every link must land inside the array, so trim until it does
    while bones and any(b["first_child"] >= len(bones) or b["next_sibling"] >= len(bones)
                        for b in bones):
        bones.pop()
    return bones


def verify(bones: list[dict]) -> int:
    """Rebuild first-child / next-sibling from parent[] and count disagreements.
    0 means the array boundaries and the record layout are both right."""
    bad = 0
    for b in bones:
        kids = [q["index"] for q in bones if q["parent"] == b["index"]]
        if b["first_child"] != (kids[0] if kids else -1):
            bad += 1
        sibs = [q["index"] for q in bones if q["parent"] == b["parent"]]
        i = sibs.index(b["index"])
        if b["next_sibling"] != (sibs[i + 1] if i + 1 < len(sibs) else -1):
            bad += 1
    return bad


def scan(blob: bytes, min_bones: int = 20, min_parents: int = 8) -> list[dict]:
    """Find every skeleton in the blob. Returns dicts with base / bones / errors."""
    out, i, n = [], 0, len(blob)
    while i + STRIDE * min_bones < n:
        if blob[i + 0x28] == 0xFF and blob[i + 0x29] == 0xFF:
            bones = read(blob, i)
            if len(bones) >= min_bones and len({b["parent"] for b in bones}) >= min_parents:
                out.append({"base": i, "bones": bones, "errors": verify(bones)})
                i += STRIDE * len(bones)
                continue
        i += 4
    return out


def load(which: str = "goalie", clean_dir=None) -> dict:
    """Convenience: read one of the KNOWN skeletons."""
    base = KNOWN[which]
    blob = model_blob(clean_dir)
    bones = read(blob, base)
    return {"name": which, "base": base, "bones": bones, "errors": verify(bones)}


# ────────────────────────────── posing ──────────────────────────────

def world_positions(bones: list[dict]) -> list[tuple]:
    """Bind-pose positions in model space, straight from `+0x00`.

    Same units and frame as the mesh in doc 22 (~1 unit = 1 cm, +Y up, +Z front), so these
    drop onto the decoded vertex positions with no extra transform.
    """
    return [tuple(b["world"]) for b in bones]


def accumulate(bones: list[dict]) -> list[tuple]:
    """Same positions, but rebuilt by walking `+0x10` (local offsets) down the tree."""
    pos = []
    for b in bones:
        o = b["offset"]
        if b["parent"] < 0:
            pos.append((o[0], o[1], o[2]))
        else:
            p = pos[b["parent"]]
            pos.append((p[0] + o[0], p[1] + o[1], p[2] + o[2]))
    return pos


def check_world(bones: list[dict], tol: float = 1e-2) -> float:
    """Max distance between the stored `+0x00` positions and the `+0x10` accumulation.
    Small values confirm both the parent links and the two translation fields at once."""
    worst = 0.0
    for a, b in zip(world_positions(bones), accumulate(bones)):
        d = sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5
        worst = max(worst, d)
    return worst


# ────────────────────────────── export ──────────────────────────────

def export_json(sk: dict, dest) -> Path:
    dest = Path(dest)
    wp = world_positions(sk["bones"])
    dest.write_text(json.dumps({
        "model": sk["name"],
        "base": "0x%X" % sk["base"],
        "bone_count": len(sk["bones"]),
        "link_errors": sk["errors"],
        "bones": [{
            "index": b["index"], "name": b["name"], "hash": "%08X" % b["hash"],
            "parent": b["parent"], "offset": [round(v, 6) for v in b["offset"]],
            "world": [round(v, 6) for v in wp[b["index"]]],
        } for b in sk["bones"]],
    }, indent=2), encoding="utf-8")
    return dest


def export_obj(sk: dict, dest) -> Path:
    """Bind pose as an OBJ line skeleton - opens in Blender/MeshLab next to the mesh OBJs
    that `arena_model.export_obj` writes, in the same coordinate frame."""
    dest = Path(dest)
    bones, wp = sk["bones"], world_positions(sk["bones"])
    lines = ["# NHL 2K10 %s skeleton - %d bones (bind pose)" % (sk["name"], len(bones))]
    for b, p in zip(bones, wp):
        lines.append("v %.6f %.6f %.6f" % p)
    for b in bones:
        lines.append("# %d %s" % (b["index"], b["name"]))
    for b in bones:
        if b["parent"] >= 0:
            lines.append("l %d %d" % (b["parent"] + 1, b["index"] + 1))
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def tree_text(sk: dict) -> str:
    bones, wp = sk["bones"], world_positions(sk["bones"])
    out = []

    def emit(k, d):
        b, p = bones[k], wp[k]
        out.append("%s[%3d] %-22s %08X  (%8.2f,%8.2f,%8.2f)"
                   % ("  " * d, k, b["name"], b["hash"], p[0], p[1], p[2]))
        for q in bones:
            if q["parent"] == k:
                emit(q["index"], d + 1)

    if bones:
        emit(0, 0)
    return "\n".join(out)
