"""player_model.py -- the Jersey Editor's preview figure, read out of the game's own model files.

The preview used to draw a mesh baked from a RenderDoc capture (launcher/data/player_mesh.npz)
and place its decals through a channel baked from the same capture (decal_atlas.npz). Both were
faithful -- but they were a copy of the game's data taken through a third party, and a copy is
not a source of truth. Everything here comes straight off global.iff instead:

* **The figure** is the game's skater (model table 0x1323AF4, char_model.KNOWN "Skater
  (player)"): jersey front/back/arms, collar, undershirt, pants, socks, skate boots, holders and
  blades, plus the gloves (0x355850), the helmet shell/trim/hair/chin strap (0xC7EB10) and the
  head with its eyes (0x87C310). Which RECORD of each model is drawn is listed in PARTS; the
  files carry every equipment alternative side by side (five collars, seven blade styles,
  seventeen helmet shells), and the ones picked are the alternatives the captured player wore,
  so nothing the preview showed before has moved. The one exception is the COLLAR: all five
  are read (records 16..20 -- plain V, LACED, V with tab, and two more plain Vs) and
  jersey_preview keeps the one the uniform's own ROS collar style names, so the Islanders'
  strings show on the Islanders and not on the Devils.

* **The decal channel** -- the per-vertex (decalU, decalV, slot) interpolator the jersey shader
  places numbers and letters through -- is a vertex attribute in those same streams. Every
  attribute stream lays it out the same way relative to the base V: U2 two bytes after V, V2 two
  after that, the slot two after that; snorm16 x2 for the UVs and x16 for the slot. Rasterised
  over the base UV into the same 1024^2 per-piece fields the capture bake produced, the result is
  bit-for-bit the same coverage and slot layout (checked piece by piece, |dUV| <= 5e-4 = the
  capture's f16 quantisation), so every consumer of `stamp_shader.atlas()` is unchanged.
  One rule on top of the raw values: a triangle whose three vertices all carry decal UV (0, 0)
  draws no decal, whatever its slot says. The garment pieces park such vertices at slot 6 (dead)
  anyway; the COLLAR leaves them at slot 0 -- only its six NHL-shield vertices have a real UV --
  and the captured pass shows the shield on that quad alone.

* **The glove colour zones** are the same channel: the glove shader has no decal, and its slot
  attribute is read as a palette COLUMN instead -- floor(snorm x 512) -- which is the 16-colour
  team palette index each region takes. That matched the capture's per-vertex zones exactly.

Positions are model space (1 unit ~ 1 cm, Y up, +Z front), bind pose. The game's own gameplay
camera puts the figure about 313 units from the eye (project_game_camera_mvp), which `render`
uses for its perspective.

All of it is cached (a few MB) under the launcher's own app-data folder, keyed on where the
archive currently holds global.iff and its size/mtime, so a modded model shows up on the next
launch and an untouched one costs a file read.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import numpy as np

from . import char_model as CM

ASSET = "global.iff"
SKATER, GLOVES, HELMET, HEAD = 0x1323AF4, 0x355850, 0xC7EB10, 0x87C310

# Distance eye -> figure the game's gameplay camera sits at, in model units.
GAME_CAM_DIST = 313.0

# Which records make the figure: (model table, record, part stem, sheet, has colour zones).
# The stems are what jersey_preview keys its behaviour on (PART_SLOT, "helmet_shell", ...).
PARTS = [
    (SKATER, 1, "jersey_front", "base", False),
    (SKATER, 2, "jersey_back", "base", False),
    (SKATER, 0, "jersey_arms", "base", False),
    # All five collars ride along (records 16..20, variant bits 0x40..0x400); jersey_preview
    # keeps the one the uniform's ROS collar style (+0x18 bits 24..26) names -- see COLLAR_RECS.
    (SKATER, 16, "collar", "base", False),        # style 0: plain V (NJD)
    (SKATER, 17, "collar", "base", False),        # style 1: LACED -- the tie-strings (NYI)
    (SKATER, 18, "collar", "base", False),        # style 2: V with the Edge tab (ANA/BUF/COL/NSH)
    (SKATER, 19, "collar", "base", False),        # style 3: plain V (ATL alt)
    (SKATER, 20, "collar", "base", False),        # style 4: plain V (EDM home)
    (SKATER, 15, "undershirt", "base", False),
    (SKATER, 21, "pants", "base", False),
    (SKATER, 23, "socks", "base", False),
    (SKATER, 24, "skates", "", False),
    (SKATER, 38, "skate_holders", "", False),     # holder/blade pair 7 of 7
    (SKATER, 37, "skate_blades", "", False),
    (GLOVES, 21, "glove_left", "", True),
    (GLOVES, 3, "glove_right", "", True),
    (HELMET, 57, "helmet_shell", "", False),      # shell style 17 (bit 0x10000)
    (HELMET, 59, "helmet_trim", "", False),
    (HELMET, 58, "hair", "", False),
    (HELMET, 56, "chin_strap", "", False),
    (HEAD, 0, "head_face", "", False),
    (HEAD, 5, "eye_left", "", False),
    (HEAD, 6, "eye_right", "", False),
]
SHEET_ID = {"": 0, "base": 1, "helmet": 2}

# The decal pieces stamp_shader evaluates, by (model, record). "shirt2" is the collar; the
# helmet has its own sheet and its own UV space and parks "no decal" at slot 0 instead of 6.
PIECES = {"sleeves": (SKATER, 0), "shirt": (SKATER, 1), "back": (SKATER, 2),
          "shirt2": (SKATER, 18), "pants": (SKATER, 21), "helmet": (HELMET, 57)}

# The collar: ROS collar style (uniform_colors.collar_style, 0..4) -> skater record. Each collar
# has its own NHL-shield quad in its own place on the base sheet, so the "shirt2" piece is
# rasterised once per collar ("shirt2@c<style>"); stamp_shader.select_collar aliases the one in
# use onto "shirt2". Style 2 is what the capture bake carried, so it stays the plain "shirt2".
COLLAR_RECS = {0: 16, 1: 17, 2: 18, 3: 19, 4: 20}
COLLAR_DEFAULT = 2
for _st, _rec in COLLAR_RECS.items():
    PIECES[f"shirt2@c{_st}"] = (SKATER, _rec)
NO_DECAL = {"helmet": (0,)}
NO_DECAL_DEFAULT = tuple(range(6, 128))
ATLAS_N = 1024

CACHE_VERSION = 3

_mesh: dict | None = None
_atlas: dict | None = None
_atlas_key: str | None = None
_failed: str | None = None


def _cache_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "NHL2K10 Mod Launcher" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _archive_key() -> str | None:
    """Where global.iff sits right now + the archive's size/mtime -- the cache key."""
    from . import archive_textures as A
    d = A.GAME_DIR
    if not d:
        return None
    try:
        loc = A.resolve(ASSET, A._dir(None))
    except Exception:
        return None
    if not loc:
        return None
    arc, off, size = loc[0], loc[1], loc[2]
    try:
        st = os.stat(A._arc_file(A._dir(None), arc))
    except OSError:
        return None
    h = hashlib.sha1(f"{CACHE_VERSION}|{d}|{arc}|{off}|{size}|{st.st_size}|{st.st_mtime_ns}|"
                     f"{PARTS}|{PIECES}".encode()).hexdigest()[:20]
    return h


def available() -> bool:
    """A game folder is set and its global.iff resolves."""
    return _archive_key() is not None


def clear_cache() -> None:
    global _mesh, _atlas, _atlas_key, _failed
    _mesh = _atlas = _atlas_key = _failed = None


# ── reading the streams ───────────────────────────────────────────────────────────────────────

def _attr_i16(b: bytes, m: dict) -> np.ndarray:
    nv, st = m["nvtx"], m["att_stride"]
    raw = np.frombuffer(b[m["att_off"]:m["att_off"] + nv * st], np.uint8).reshape(nv, st)
    return raw[:, :st // 2 * 2].copy().view(">i2").reshape(nv, st // 2)


def decal_channel(b: bytes, m: dict) -> np.ndarray | None:
    """(decalU, decalV, slot) per vertex, or None when the stream has no room for it."""
    if m.get("f_v") is None:
        return None
    k = m["f_v"] + 2
    if k + 6 > m["att_stride"]:
        return None
    i16 = _attr_i16(b, m)
    c = k // 2
    out = np.empty((m["nvtx"], 3), np.float32)
    out[:, 0] = i16[:, c] / 32767.0 * 2.0
    out[:, 1] = i16[:, c + 1] / 32767.0 * 2.0
    out[:, 2] = i16[:, c + 2] / 32767.0 * 16.0
    return out


def colour_zones(b: bytes, m: dict) -> np.ndarray | None:
    """The palette column each vertex takes (gloves): the slot attribute read as floor(x512)."""
    if m.get("f_v") is None or m["f_v"] + 8 > m["att_stride"]:
        return None
    i16 = _attr_i16(b, m)
    return np.floor(i16[:, (m["f_v"] + 6) // 2] / 32767.0 * 512.0).astype(np.int32)


def rasterise(uv: np.ndarray, attr: np.ndarray, faces: np.ndarray, n: int = ATLAS_N):
    """Barycentric-interpolate `attr` into an n*n image indexed by `uv` (same rule as
    tools/build_stamp_map.rasterise, which baked the capture)."""
    C = attr.shape[1]
    out = np.zeros((n, n, C), np.float32)
    cov = np.zeros((n, n), bool)
    P = np.stack([uv[:, 0] * n, uv[:, 1] * n], 1).astype(np.float64)
    A = attr.astype(np.float64)
    for f in faces:
        p, a = P[f], A[f]
        x0 = max(int(np.floor(p[:, 0].min())), 0)
        x1 = min(int(np.ceil(p[:, 0].max())) + 1, n)
        y0 = max(int(np.floor(p[:, 1].min())), 0)
        y1 = min(int(np.ceil(p[:, 1].max())) + 1, n)
        if x1 <= x0 or y1 <= y0:
            continue
        d = ((p[1, 1] - p[2, 1]) * (p[0, 0] - p[2, 0])
             + (p[2, 0] - p[1, 0]) * (p[0, 1] - p[2, 1]))
        if abs(d) < 1e-12:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1]
        px, py = xx + 0.5, yy + 0.5
        l0 = ((p[1, 1] - p[2, 1]) * (px - p[2, 0]) + (p[2, 0] - p[1, 0]) * (py - p[2, 1])) / d
        l1 = ((p[2, 1] - p[0, 1]) * (px - p[2, 0]) + (p[0, 0] - p[2, 0]) * (py - p[2, 1])) / d
        l2 = 1.0 - l0 - l1
        msk = (l0 >= -1e-4) & (l1 >= -1e-4) & (l2 >= -1e-4)
        if not msk.any():
            continue
        ys, xs = yy[msk], xx[msk]
        out[ys, xs] = (l0[msk][:, None] * a[0] + l1[msk][:, None] * a[1]
                       + l2[msk][:, None] * a[2])
        cov[ys, xs] = True
    return out, cov


# ── building ──────────────────────────────────────────────────────────────────────────────────

def _zero_tris(ch: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """Triangles whose three vertices all carry a decal UV of exactly (0, 0): the game draws no
    decal there whatever the slot says. The garment pieces park those vertices at slot 6 anyway,
    but the COLLAR leaves them at slot 0 -- only its six NHL-shield vertices have a real UV -- and
    the captured pass confirms the shield lands on the quad alone (373 texels), nowhere else."""
    z = (ch[:, 0] == 0) & (ch[:, 1] == 0)
    return z[tri].all(1)


def _build(b: bytes) -> tuple[dict, dict]:
    """(mesh, atlas) straight from blob 0. Mesh keys mirror the old player_mesh.npz: pos, uv,
    nrm, tri, sheet, part, zone, names -- plus duv/slot, the decal channel per vertex."""
    models = {m["table"]: m for m in CM.scan_models(b, ASSET)}
    need = {t for t, *_ in PARTS} | {t for t, _ in PIECES.values()}
    missing = [f"0x{t:X}" for t in need if t not in models]
    if missing:
        raise ValueError(f"{ASSET}: model table(s) {', '.join(missing)} not found")
    read = {t: CM.read_model(b, models[t]) for t in need}
    chan = {t: decal_channel(b, models[t]) for t in need}
    zones = {t: colour_zones(b, models[t]) for t in need if any(z and tt == t for tt, _, _, _, z in PARTS)}

    P, U, N, T, SH, PT, Z, DUV, SL, names = [], [], [], [], [], [], [], [], [], []
    base = 0
    for pid, (table, rec, stem, sheet, zoned) in enumerate(PARTS):
        md = read[table]
        part = md["parts"][rec]
        tri = part["tris_idx"]
        if not len(tri):
            continue
        used = np.unique(tri)
        remap = np.full(len(md["pos"]), -1, np.int64)
        remap[used] = np.arange(len(used))
        P.append(md["pos"][used])
        U.append(md["uv"][used])
        N.append(md["nrm"][used])
        T.append(remap[tri] + base)
        SH.append(np.full(len(tri), SHEET_ID[sheet], np.int32))
        PT.append(np.full(len(tri), pid, np.int32))
        zc = zones.get(table)
        Z.append(zc[used] if (zoned and zc is not None) else np.full(len(used), -1, np.int32))
        ch = chan[table]
        if ch is not None:
            DUV.append(ch[used, :2])
            sl = np.rint(ch[used, 2]).astype(np.int8)
            live = np.zeros(len(md["pos"]), bool)
            live[tri[~_zero_tris(ch, tri)].ravel()] = True
            sl[~live[used]] = -1
            SL.append(sl)
        else:
            DUV.append(np.zeros((len(used), 2), np.float32))
            SL.append(np.full(len(used), -1, np.int8))
        names.append(f"{stem}_r{rec}")
        base += len(used)
    mesh = dict(pos=np.concatenate(P).astype(np.float32), uv=np.concatenate(U).astype(np.float32),
                nrm=np.concatenate(N).astype(np.float32), tri=np.concatenate(T).astype(np.int32),
                sheet=np.concatenate(SH), part=np.concatenate(PT), zone=np.concatenate(Z),
                duv=np.concatenate(DUV).astype(np.float32), slot=np.concatenate(SL),
                names=np.array(names))

    atlas = {}
    for name, (table, rec) in PIECES.items():
        md, ch = read[table], chan[table]
        if ch is None:
            continue
        tri = md["parts"][rec]["tris_idx"]
        img, cov = rasterise(md["uv"], ch, tri)
        slot = np.where(cov, np.floor(img[..., 2] + 1e-4), -1).astype(np.int8)
        for dead in NO_DECAL.get(name, NO_DECAL_DEFAULT):
            slot[slot == dead] = -1
        dead_t = _zero_tris(ch, tri)
        if dead_t.any():
            _img, dcov = rasterise(md["uv"], ch, tri[dead_t])
            _img, lcov = rasterise(md["uv"], ch, tri[~dead_t])
            slot[dcov & ~lcov] = -1
        atlas[name + "_uv"] = img[..., :2].astype(np.float16)
        atlas[name + "_slot"] = slot
        atlas[name + "_cov"] = cov
    return mesh, atlas


def _load(log=None) -> bool:
    """Populate the module caches from the app-data cache or, failing that, from the game."""
    global _mesh, _atlas, _atlas_key, _failed
    key = _archive_key()
    if key is None:
        return False
    if _mesh is not None and _atlas_key == key:
        return True
    if _failed == key:
        return False
    cf = _cache_dir() / f"player_preview_{key}.npz"
    if cf.exists():
        try:
            with np.load(cf, allow_pickle=False) as z:
                d = {k: z[k] for k in z.files}
            _mesh = {k[5:]: v for k, v in d.items() if k.startswith("mesh_")}
            _atlas = {k[6:]: v for k, v in d.items() if k.startswith("atlas_")}
            _atlas_key = key
            return True
        except Exception:
            pass
    try:
        b = CM.blob(True, None, ASSET)
        mesh, atlas = _build(b)
    except Exception as e:
        _failed = key
        if log:
            log(f"[preview] player model from {ASSET} failed: {e}")
        return False
    _mesh, _atlas, _atlas_key = mesh, atlas, key
    try:
        np.savez_compressed(cf, **{"mesh_" + k: v for k, v in mesh.items()},
                            **{"atlas_" + k: v for k, v in atlas.items()})
    except Exception:
        pass
    return True


def preview_mesh(log=None) -> dict | None:
    """The figure, in model space (see module doc), or None when no game folder is set."""
    return dict(_mesh) if _load(log) else None


def decal_atlas(log=None) -> dict | None:
    """The per-piece decal fields (`<piece>_uv/_slot/_cov`, 1024^2), or None."""
    return dict(_atlas) if _load(log) else None


def atlas_key() -> str | None:
    """Changes whenever the atlas would -- consumers key their derived caches on it."""
    return _atlas_key if _load() else None
