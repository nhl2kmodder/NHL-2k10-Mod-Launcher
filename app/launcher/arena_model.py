"""NHL 2K10 — ARENA MODEL / LIGHTING library
============================================
Everything the Arena tab needs that is NOT a texture: the arena's GEOMETRY and its
BAKED LIGHTING, straight out of the archive, for any of the 30 teams — no RenderDoc
capture and no running game required.

WHAT AN ARENA IS
----------------
Four per-team assets, all with the same structure (blob 0 = DRAM: scene graph, submesh
tables, geometry, texture records / blob 1 = VRAM: the texture pixels):

    arena_<T>.iff               the BOWL      seats, signage, banners, gondola, jumbotron shell
    rink_<T>.iff                the RINK      ice, boards + dasher ads, glass, nets, benches
    led_<T>.iff                 ribbon-board LED frames
    arena_presentation_<T>.iff  presentation odds and ends

MODEL LAYOUT INSIDE BLOB 0  (all big-endian) — see docs/24 §7
------------------------------------------------------------
    submesh table   recs x 48 B   +0x00 prim(6=strip)  +0x04 first_idx  +0x08 n_idx
                                  +0x0C tris(=n_idx-2) +0x14 first_vtx  +0x18 n_vtx
                                  +0x20 material id    +0x24 LOD mask   +0x2C recs-1-r
    index block     u16 strip indices; first_idx[r+1] == first_idx[r] + n_idx[r] + 1
    vertex decl     N x 0x50 B    [hash][hash][0x20 oo 00 00][type] — oo = element byte offset
    vb header       ... [stride u16][n_verts*stride u32] ...
    vertex buffer   at (that header + 0x22)

VERTEX FORMAT (stride 24/28/32/36/40, world space, 1 unit ~ 1 cm; no normals)
    +0x00  position       float32 BE x3
    +0x0C  baked light    float16 BE x3   <- THE LIGHTING.  Stored at a per-material scale.
    +0x12  second scalar  float16 BE      <- ambient / AO term
    +0x14  UV             float16 BE x2

Because there are no normals and no light sources in the file, "editing the lighting"
means rescaling / retinting the bake that is already there (see apply_lighting).
The edit is SIZE-PRESERVING: blob 0 is re-encoded and padded back to its original packed
length, so blob 1 never moves and the TOC is untouched.
"""
from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import numpy as np

from . import archive_textures as A
from . import decode_e4837_fixed as DF
from . import encode_e4837_lazy as EE
from . import resources

F16MAX = 65504.0
KINDS = ("arena", "rink", "led", "arena_presentation")


# ───────────────────────────── assets ─────────────────────────────
def team_assets(code: str) -> list[str]:
    """The four .iff names for a team code ('van'), lower-case, in bowl-first order."""
    c = (code or "").lower()
    return [f"{k}_{c}.iff" for k in KINDS]


def _read_clean(iff: str, clean_dir=None):
    """(loc, bytes, size) of the PRISTINE asset (prefers the .orig archives)."""
    return A._read_asset(iff, clean_dir)


def _blob0(res: bytes, size: int):
    """The DRAM blob (blob 0) of an asset -> blob dict from _walk_blobs, or None."""
    for b in A._walk_blobs(res, size):
        if b["dec"] is not None and len(b["dec"]) > 0x1000:
            return b
    return None


_DRAM_CACHE = {}


def _stamp(path) -> tuple:
    """(mtime_ns, size) of a file, or () if it is not there."""
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ()


def dram(iff: str, clean_dir=None, current=False):
    """Decompressed blob 0 of an asset. current=True reads the (possibly modded) game copy."""
    key = (iff, str(clean_dir), current)
    if current:
        loc = A.resolve(iff, A._dir(clean_dir))
        if not loc:
            return None
        arc, off, size, idx, f3 = loc
        arcf = A._arc_file(A._dir(clean_dir), arc)
        # The game copy is what a *different* process (this launcher's own writers, or an
        # apply script run outside it) mutates, so clearing the cache on write is not enough:
        # stamp the key with the archive's mtime+size and any external edit misses the cache
        # by itself. That is what stops the tab showing yesterday's model.
        key = key + (str(arcf), off) + _stamp(arcf)
        hit = _DRAM_CACHE.get(key)
        if hit is not None:
            return hit
        with open(arcf, "rb") as f:
            f.seek(off)
            res = f.read(size + 0x400000)
    else:
        hit = _DRAM_CACHE.get(key)
        if hit is not None:
            return hit
        loc, res, size = _read_clean(iff, clean_dir)
        if loc is None:
            return None
        size = loc[2]
    b = _blob0(res, loc[2])
    out = b["dec"] if b else None
    if out is not None:
        _DRAM_CACHE[key] = out
        while len(_DRAM_CACHE) > 6:
            _DRAM_CACHE.pop(next(iter(_DRAM_CACHE)))
    return out


# ───────────────────────────── model scan ─────────────────────────────
def scan_models(blob: bytes) -> list[dict]:
    """Find every mesh in a DRAM blob. Pure signature scan — works for all 30 teams."""
    if not blob:
        return []
    W = np.frombuffer(blob[:len(blob) // 4 * 4], dtype=">u4")
    n = len(W)
    if n < 16:
        return []
    out = []
    cand = np.nonzero((W[:n - 11] == 6) & (W[1:n - 10] == 0) & (W[5:n - 6] == 0))[0]
    for i in cand:
        o = int(i) * 4
        recs = int(W[i + 11]) + 1
        if not (1 <= recs <= 4096) or o + recs * 48 > len(blob):
            continue
        T = np.frombuffer(blob[o:o + recs * 48], dtype=">u4").reshape(recs, 12)
        if not (T[:, 0] == 6).all():
            continue
        if not (T[:, 11] == np.arange(recs - 1, -1, -1)).all():
            continue
        fi, ni, tri, fv, nv = (T[:, k].astype(np.int64) for k in (1, 2, 3, 5, 6))
        if not (ni >= 3).all() or not (tri == ni - 2).all():
            continue
        if recs > 1 and (not (fi[1:] == fi[:-1] + ni[:-1] + 1).all()
                         or not (fv[1:] == fv[:-1] + nv[:-1]).all()):
            continue
        nidx, nvtx = int(fi[-1] + ni[-1]), int(fv[-1] + nv[-1])
        ib0 = o + recs * 48
        ibend = ib0 + nidx * 2
        if ibend > len(blob) or nvtx < 3:
            continue
        win = blob[ibend:ibend + 0x2000]
        stride = hdr = None
        for st in (24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 64):
            k = win.find(struct.pack(">HI", st, nvtx * st))
            if k >= 0:
                stride, hdr = st, ibend + k
                break
        if stride is None:
            continue
        vb = hdr + 0x22                                   # constant, verified on 13 models
        if vb + nvtx * stride > len(blob):
            continue
        out.append(dict(table=o, recs=recs, ib0=ib0, nidx=nidx, nvtx=nvtx,
                        stride=stride, vb=vb, tris=int(tri.sum()),
                        mats=sorted({int(x) for x in T[:, 8]})))
    out.sort(key=lambda m: m["table"])
    return out


def submeshes(blob: bytes, m: dict) -> list[dict]:
    T = np.frombuffer(blob[m["table"]:m["table"] + m["recs"] * 48],
                      dtype=">u4").reshape(m["recs"], 12)
    return [dict(rec=r, first_idx=int(T[r, 1]), n_idx=int(T[r, 2]), tris=int(T[r, 3]),
                 first_vtx=int(T[r, 5]), n_vtx=int(T[r, 6]), mat=int(T[r, 8]),
                 lod=int(T[r, 9])) for r in range(m["recs"])]


def _strip_to_tris(ib: np.ndarray) -> np.ndarray:
    """u16 triangle-strip run (0xFFFF = restart) -> (n,3) int32 triangle list.

    Winding parity is counted from the START OF EACH STRIP, not from the start of the run —
    that is what the GPU does with a cut index, and these submeshes lean on it hard: the one
    that made this obvious is 580 separate 4-index quad strips in a row, so every second strip
    would come out inside-out if parity were global.
    """
    ib = ib.astype(np.int64)
    if len(ib) < 3:
        return np.zeros((0, 3), np.int32)
    pos = np.arange(len(ib))
    segstart = np.maximum.accumulate(np.where(ib == 0xFFFF, pos + 1, 0))
    a, b, c = ib[:-2], ib[1:-1], ib[2:]
    ok = (a != b) & (b != c) & (a != c) & (a != 0xFFFF) & (b != 0xFFFF) & (c != 0xFFFF)
    even = ((pos[:len(a)] - segstart[:len(a)]) % 2) == 0
    tri = np.where(even[:, None], np.stack([a, b, c], 1), np.stack([a, c, b], 1))
    return tri[ok].astype(np.int32)


LIGHT_MAX = 16.0        # the bake is a linear multiplier; real values sit well under 2


def is_lit(blob: bytes, m: dict) -> bool:
    """True when this model's vertices really do carry the baked lighting at +0x0C.

    A few models per arena use a DIFFERENT stride-32 declaration — position, a constant packed
    colour at +0x0C, 16-bit UNORM UVs at +0x10 — instead of the usual f16 light + f16 ambient +
    f16 UV. In arena_van those are models 13-15, the big backdrop quads above the bowl. Reading
    f16 light out of them gives values in the thousands (which previews as lurid green), and
    WRITING relit values back would corrupt whatever that field actually is, so relight() and
    light_stats() skip them and the preview draws them unlit.
    """
    NV, ST, vb = m["nvtx"], m["stride"], m["vb"]
    n = max(min(NV, 4096), 1)
    R = np.frombuffer(blob[vb:vb + n * ST], np.uint8).reshape(n, ST)
    L = np.nan_to_num(R[:, 12:18].copy().view(">f2").astype(np.float32))
    return float(np.percentile(L, 99)) <= LIGHT_MAX


def read_model(blob: bytes, m: dict, lod0_only: bool = False) -> dict:
    # NOTE lod0_only defaults OFF: +0x24 is a bitmask but its bits are NOT a LOD chain of one
    # part — sibling submeshes in the same table carry 1,2,4,8,16,… and every one of them is a
    # distinct piece of the arena. Filtering on it drops most of the bowl.
    """-> pos (n,3) float32 world, uv (n,2), light (n,3), amb (n,), and per-submesh triangles."""
    NV, ST, vb = m["nvtx"], m["stride"], m["vb"]
    R = np.frombuffer(blob[vb:vb + NV * ST], dtype=np.uint8).reshape(NV, ST)
    pos = R[:, 0:12].copy().view(">f4").astype(np.float32)
    lit = is_lit(blob, m)
    if lit:
        light = np.nan_to_num(R[:, 12:18].copy().view(">f2").astype(np.float32))
        amb = np.nan_to_num(R[:, 18:20].copy().view(">f2").astype(np.float32)).ravel()
        uv = np.nan_to_num(R[:, 20:24].copy().view(">f2").astype(np.float32)) \
            if ST >= 24 else np.zeros((NV, 2), np.float32)
    else:                                   # the alternate declaration — see is_lit()
        light = np.ones((NV, 3), np.float32)
        amb = np.zeros(NV, np.float32)
        uv = (R[:, 16:20].copy().view(">u2").astype(np.float32) / 32767.0
              if ST >= 20 else np.zeros((NV, 2), np.float32))
    IB = np.frombuffer(blob[m["ib0"]:m["ib0"] + m["nidx"] * 2], dtype=">u2")
    parts = []
    for s in submeshes(blob, m):
        if lod0_only and s["lod"] and not (s["lod"] & 1):
            continue
        tri = _strip_to_tris(IB[s["first_idx"]:s["first_idx"] + s["n_idx"]])
        if len(tri):
            tri = tri[(tri < NV).all(1)]
        parts.append(dict(s, tris_idx=tri))
    return dict(pos=pos, uv=uv, light=light, amb=amb, parts=parts, model=m, lit=lit)


# ───────────────────────────── material -> texture ─────────────────────────────
_MATMAP = None


def _matmap():
    """Learned material-id -> texture-record bindings (see docs/24 §8).  Shipped as data
    because the binding lives in the shader/effect state, not in a table in the file: it was
    recovered from a RenderDoc capture (fetch-constant VRAM address -> texture record)."""
    global _MATMAP
    if _MATMAP is None:
        p = resources.data_path("arena_materials.json")
        try:
            _MATMAP = json.loads(Path(p).read_text())
        except Exception:
            _MATMAP = {}
    return _MATMAP


def material_texture(iff: str, mat: int):
    """Texture-record index bound to a material, or None when it isn't known for this arena."""
    t = _matmap().get(iff.lower()) or {}
    v = t.get(str(mat))
    return v[0] if isinstance(v, list) and v else v


def material_runtime(iff: str, mat: int):
    """Describe the runtime surface a material samples, or None for ordinary materials.

    The jumbotron screens and the LED ribbon never sample an archive texture — the game
    rewrites those pixels in guest RAM and re-uploads them every frame, and the mesh only
    supplies a 0..1 UV quad.  So they read as "no texture" to material_texture(), which is
    correct but misleading; this says *why*.  See docs/24 §12.
    """
    return ((_matmap().get("_runtime") or {}).get(iff.lower()) or {}).get(str(mat))


# ───────────────────────────── lighting ─────────────────────────────
def light_stats(blob: bytes, models: list[dict]) -> dict:
    """Summary of the baked lighting across the given models."""
    cs, as_ = [], []
    for m in models:
        if not is_lit(blob, m):
            continue
        R = np.frombuffer(blob[m["vb"]:m["vb"] + m["nvtx"] * m["stride"]],
                          dtype=np.uint8).reshape(m["nvtx"], m["stride"])
        cs.append(np.nan_to_num(R[:, 12:18].copy().view(">f2").astype(np.float64)))
        as_.append(np.nan_to_num(R[:, 18:20].copy().view(">f2").astype(np.float64)).ravel())
    if not cs:
        return {}
    C = np.concatenate(cs)
    Am = np.concatenate(as_)
    return dict(verts=int(len(C)), mean=float(C.mean()), p99=float(np.percentile(C, 99)),
                max=float(C.max()), amb_mean=float(Am.mean()),
                tint=[float(C[:, i].mean()) for i in range(3)])


def relight(blob: bytearray, models: list[dict], gain=1.0, tint=(1.0, 1.0, 1.0), amb=1.0):
    """Multiply the baked lighting in place. Multiplicative because the bake is stored at a
    DIFFERENT scale per material (mean 0.0019 on one model, 0.32 on another) — an absolute
    value would blow out half the arena."""
    T = np.asarray(tint, np.float64)
    n = 0
    for m in models:
        if not is_lit(blob, m):        # different declaration: +0x0C is not light, don't write
            continue
        NV, ST, vb = m["nvtx"], m["stride"], m["vb"]
        R = np.frombuffer(blob[vb:vb + NV * ST], dtype=np.uint8).reshape(NV, ST).copy()
        C = np.nan_to_num(R[:, 12:18].copy().view(">f2").astype(np.float64)) * gain * T
        Am = np.nan_to_num(R[:, 18:20].copy().view(">f2").astype(np.float64)) * amb
        R[:, 12:18] = np.clip(C, 0, F16MAX).astype(">f2").view(np.uint8).reshape(NV, 6)
        R[:, 18:20] = np.clip(Am, 0, F16MAX).astype(">f2").view(np.uint8).reshape(NV, 2)
        blob[vb:vb + NV * ST] = R.tobytes()
        n += NV
    return n


# ───────────────────────────── write-back ─────────────────────────────
def write_dram(iff: str, new_dram: bytes, game_dir, log=print) -> str:
    """Put a modified blob 0 back into the game archive, SIZE-PRESERVING: re-encode with the
    blob's own codec/window, pad the packed bytes back to the original length so blob 1 does
    not move and the TOC needs no edit. Refuses (without touching anything) if it won't fit."""
    game_dir = Path(game_dir)
    loc = A.resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off)
        res = bytearray(f.read(size))
    b = _blob0(res, size)
    if b is None:
        raise ValueError(f"{iff}: no DRAM blob")
    if len(new_dram) != len(b["dec"]):
        raise ValueError(f"{iff}: DRAM size changed ({len(b['dec'])} -> {len(new_dram)})")
    blob = EE.encode_payload(bytes(new_dram), wparam=b["wp"], codec=b["codec"])
    if len(blob) > b["tot"]:
        raise ValueError(
            f"{iff}: edited geometry/lighting re-compresses to {len(blob)} bytes but its slot is "
            f"{b['tot']} (+{len(blob)-b['tot']}). Nothing was written. Try a smaller change "
            f"(rink_* assets in particular have almost no slack).")
    padded = blob[:8] + struct.pack(">I", b["tot"]) + blob[12:] + b"\x00" * (b["tot"] - len(blob))
    chk = DF.decompress_codec(padded[20:b["tot"]], b["dec_sz"], (1 << b["wp"]) - 1, b["wp"])
    if chk != bytes(new_dram):
        raise ValueError(f"{iff}: re-encoded DRAM did not round-trip — nothing written")
    res[b["off"]:b["off"] + b["tot"]] = padded
    A._backup_once(game_dir / arc, log)
    with open(game_dir / arc, "r+b") as f:
        f.seek(off)
        f.write(res)
    _DRAM_CACHE.clear()
    return (f"IN-PLACE: {iff} blob 0 -> {arc}:0x{off:X} "
            f"({len(blob)}/{b['tot']} bytes, {len(blob)*100//b['tot']}% of slot)")


def restore(iff: str, game_dir, log=print) -> str:
    """Put the pristine blob 0 back (undo every geometry/lighting edit for this asset)."""
    clean = dram(iff)
    if clean is None:
        raise ValueError(f"{iff}: no pristine copy available")
    return write_dram(iff, clean, game_dir, log)


def headroom(iff: str, clean_dir=None) -> dict:
    """How much compressed slack blob 0 has — the budget every geometry/lighting edit lives in."""
    loc, res, size = _read_clean(iff, clean_dir)
    if loc is None:
        return {}
    b = _blob0(res, loc[2])
    if b is None:
        return {}
    packed = len(EE.encode_payload(bytes(b["dec"]), wparam=b["wp"], codec=b["codec"]))
    return dict(slot=b["tot"], packed=packed, free=b["tot"] - packed, dram=len(b["dec"]))


# ───────────────────────────── OBJ / MTL export ─────────────────────────────
def export_obj(iff: str, dest: Path, blob: bytes = None, models: list[dict] = None,
               textures: bool = True, bake_to_vertex_colour: bool = True,
               indices: list[int] = None, stem: str = None,
               log=print) -> Path:
    """Write <dest>/<stem>.obj + .mtl (+ textures/*.png) — open it straight in Blender.

    Every submesh becomes an OBJ group with its own material, so the UVs and textures the
    game uses are already assigned; nothing has to be hooked up by hand. When the baked
    lighting is exported too it rides along as vertex colours (the `v x y z r g b` extension
    Blender reads), because there are no normals or lights in the file to rebuild it from.

    `indices` is the real model index of each entry of `models`. It matters whenever `models`
    is a subset: the group names are the round-trip key back to `replace_part`, so a model
    exported on its own still has to be called m20, not m00.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    blob = blob if blob is not None else dram(iff)
    if not blob:
        raise ValueError(f"{iff}: no DRAM blob")
    models = models if models is not None else scan_models(blob)
    if indices is None:
        indices = list(range(len(models)))
    stem = stem or Path(iff).stem
    recs = A.list_textures(iff) if textures else []
    used_tex, mats = {}, {}

    obj = dest / f"{stem}.obj"
    with open(obj, "w") as f:
        f.write(f"# NHL 2K10 {iff} — {len(models)} models, exported by the NHL2K10 Mod Launcher\n")
        f.write(f"mtllib {stem}.mtl\n")
        base = 1
        for mi, m in zip(indices, models):
            d = read_model(blob, m)
            P, UV, C = d["pos"], d["uv"], d["light"]
            if bake_to_vertex_colour and len(C):
                hi = max(float(np.percentile(C, 99)), 1e-9)     # bake scale is per material
                RGB = np.clip(C / hi, 0, 1) ** (1 / 2.2)
            else:
                RGB = None
            for i in range(len(P)):
                if RGB is None:
                    f.write("v %.4f %.4f %.4f\n" % tuple(P[i]))
                else:
                    f.write("v %.4f %.4f %.4f %.4f %.4f %.4f\n"
                            % (P[i][0], P[i][1], P[i][2], RGB[i][0], RGB[i][1], RGB[i][2]))
            for i in range(len(UV)):
                f.write("vt %.6f %.6f\n" % (UV[i][0], 1.0 - UV[i][1]))
            for p in d["parts"]:
                if not len(p["tris_idx"]):
                    continue
                name = f"m{mi:02d}_sub{p['rec']:03d}_mat{p['mat']}"
                mname = f"mat{p['mat']}"
                ti = material_texture(iff, p["mat"])
                mats[mname] = ti
                if ti is not None:
                    used_tex[ti] = mname
                f.write(f"g {name}\nusemtl {mname}\n")
                t = p["tris_idx"] + base
                for a, b_, c in t:
                    f.write(f"f {a}/{a} {b_}/{b_} {c}/{c}\n")
            base += len(P)

    texdir = dest / "textures"
    if textures and used_tex:
        texdir.mkdir(exist_ok=True)
        by_index = {r["index"]: r for r in recs}
        for ti in sorted(used_tex):
            p = texdir / f"tex{ti:03d}.png"
            if p.exists() or ti not in by_index:
                continue
            try:
                img = A.decode_record(iff, by_index[ti])
                if img:
                    img.save(p)
            except Exception as e:
                log(f"  texture {ti}: {e}")
    with open(dest / f"{stem}.mtl", "w") as f:
        for mname, ti in sorted(mats.items()):
            f.write(f"newmtl {mname}\nKd 0.8 0.8 0.8\n")
            if ti is not None and (texdir / f"tex{ti:03d}.png").exists():
                f.write(f"map_Kd textures/tex{ti:03d}.png\n")
            f.write("\n")
    log(f"  {iff}: {len(models)} models, {len(mats)} materials, {len(used_tex)} textures -> {obj}")
    return obj


def export_part_obj(iff: str, blob: bytes, m: dict, part: dict, dest: Path) -> Path:
    """One submesh on its own — the file you edit and hand back to replace_part().

    World-space, exactly as it is in the game, so an unedited round trip is a no-op and an
    edited one lands in the right place. UVs come out with V flipped (OBJ convention) and are
    flipped back on import.
    """
    d = read_model(blob, m)
    tri = None
    for p in d["parts"]:
        if p["rec"] == part["rec"]:
            tri = p["tris_idx"]
    if tri is None or not len(tri):
        raise ValueError(f"part {part['rec']} has no triangles")
    lo, hi = part["first_vtx"], part["first_vtx"] + part["n_vtx"]
    P, UV = d["pos"][lo:hi], d["uv"][lo:hi]
    dest = Path(dest)
    with open(dest, "w") as f:
        f.write(f"# NHL 2K10 {iff} part {part['rec']} (material {part['mat']})\n"
                f"# {part['n_vtx']} vertices, {len(tri)} triangles — WORLD SPACE, 1 unit = 1 cm.\n"
                f"# Budget when you import it back: at most {part['n_vtx']} vertices and "
                f"{(part['n_idx'] + 1) // 4} triangles (the slot in the file is fixed).\n")
        for p_ in P:
            f.write("v %.4f %.4f %.4f\n" % tuple(p_))
        for u in UV:
            f.write("vt %.6f %.6f\n" % (u[0], 1.0 - u[1]))
        f.write(f"g part{part['rec']}\n")
        for a, b_, c in (tri - lo + 1):
            f.write(f"f {a}/{a} {b_}/{b_} {c}/{c}\n")
    return dest


def _obj_raw(path):
    """(positions, uvs, {group: [face, ...]}) — one pass over the file, nothing resolved yet."""
    P, UV, G = [], [], {}
    cur = ""
    for line in Path(path).read_text(errors="replace").splitlines():
        w = line.split()
        if not w:
            continue
        if w[0] == "v":
            P.append([float(x) for x in w[1:4]])
        elif w[0] == "vt":
            UV.append([float(w[1]), float(w[2]) if len(w) > 2 else 0.0])
        elif w[0] in ("g", "o"):
            cur = w[1] if len(w) > 1 else ""
        elif w[0] == "f":
            idx = []
            for tok in w[1:]:
                a = tok.split("/")
                vi = int(a[0])
                ti = int(a[1]) if len(a) > 1 and a[1] else vi
                idx.append((vi, ti))
            faces = G.setdefault(cur, [])
            for k in range(1, len(idx) - 1):           # fan-triangulate n-gons
                faces.append([idx[0], idx[k], idx[k + 1]])
    return P, UV, G


GROUP_RE = re.compile(r"m(\d+)_sub(\d+)_mat(\d+)")


def export_model_obj(iff: str, dest: Path, mi: int, blob: bytes = None,
                     models: list[dict] = None, textures: bool = True,
                     bake_to_vertex_colour: bool = True, log=print) -> Path:
    """One WHOLE model — every submesh of it — as a single OBJ, with its materials.

    Between export_obj (the entire arena) and export_part_obj (one submesh) there was nothing
    that gave you an editable object: the jumbotron is a dozen submeshes across two LODs, and
    part-by-part export loses the relationship between them. Groups keep the per-submesh
    identity, so replace_model_obj can put the edited file straight back.
    """
    blob = blob if blob is not None else dram(iff, current=True) or dram(iff)
    if not blob:
        raise ValueError(f"{iff}: no DRAM blob")
    models = models if models is not None else scan_models(blob)
    if not (0 <= mi < len(models)):
        raise ValueError(f"{iff}: no model {mi}")
    return export_obj(iff, dest, blob=blob, models=[models[mi]], indices=[mi],
                      textures=textures, bake_to_vertex_colour=bake_to_vertex_colour,
                      stem=f"{Path(iff).stem}_model{mi:02d}", log=log)


def replace_model_obj(blob: bytearray, models: list[dict], path, mi: int = None,
                      log=print) -> list[str]:
    """Import an OBJ written by export_model_obj: each group goes back to its own submesh.

    Groups are matched by the `m<NN>_sub<RRR>_mat<M>` name, so renaming or deleting a group in
    the editor is how you leave a submesh alone. `mi` overrides the model index in the name,
    for the common case of pasting one LOD's export onto the other.
    """
    groups = read_obj_groups(path)
    msgs, done = [], 0
    for name, mesh in groups.items():
        g = GROUP_RE.search(name)
        if not g:
            log(f"  skipped group {name!r}: not a m##_sub###_mat# name")
            continue
        gmi, rec = int(g.group(1)) if mi is None else mi, int(g.group(2))
        if not (0 <= gmi < len(models)):
            log(f"  skipped group {name!r}: no model {gmi}")
            continue
        m = models[gmi]
        part = next((p for p in submeshes(blob, m) if p["rec"] == rec), None)
        if part is None:
            log(f"  skipped group {name!r}: model {gmi} has no submesh {rec}")
            continue
        msgs.append(f"m{gmi:02d} sub{rec:03d}: " + replace_part(blob, m, part, mesh, log=log))
        done += 1
    if not done:
        raise ValueError(f"{Path(path).name}: no group matched a submesh in this model")
    return msgs


def read_obj(path) -> dict:
    """Minimal OBJ reader: positions, UVs and triangulated faces. Ignores everything else."""
    P, UV, G = _obj_raw(path)
    F = [f for faces in G.values() for f in faces]
    return _resolve_faces(Path(path).name, P, UV, F)


def read_obj_groups(path) -> dict:
    """Same, but keyed by OBJ group — the round trip for a whole-model export.

    export_obj names every group `m<NN>_sub<RRR>_mat<M>`, which carries the model index and the
    submesh record, so an edited file can be handed straight back part by part without the user
    having to say which is which.
    """
    P, UV, G = _obj_raw(path)
    out = {}
    for name, F in G.items():
        if F:
            out[name] = _resolve_faces(f"{Path(path).name} [{name or 'default'}]", P, UV, F)
    if not out:
        raise ValueError(f"{Path(path).name}: no faces found")
    return out


def _resolve_faces(what: str, P, UV, F) -> dict:
    if not P or not F:
        raise ValueError(f"{what}: no vertices or faces found")
    P = np.array(P, np.float32)
    UV = np.array(UV, np.float32) if UV else np.zeros((0, 2), np.float32)

    # OBJ lets a vertex carry several UVs; the game's vertex buffer cannot. Split on the
    # (position, uv) pair so the imported mesh keeps its seams instead of losing them.
    remap, out_p, out_uv, tris = {}, [], [], []
    for face in F:
        t = []
        for vi, ti in face:
            key = (vi, ti)
            if key not in remap:
                remap[key] = len(out_p)
                out_p.append(P[vi - 1 if vi > 0 else len(P) + vi])
                if len(UV) and (0 < ti <= len(UV) or ti < 0):
                    u = UV[ti - 1 if ti > 0 else len(UV) + ti]
                    out_uv.append([u[0], 1.0 - u[1]])
                else:
                    out_uv.append([0.0, 0.0])
            t.append(remap[key])
        tris.append(t)
    return dict(pos=np.array(out_p, np.float32), uv=np.array(out_uv, np.float32),
                tris=np.array(tris, np.int32))


def _stripify(T) -> list[list[int]]:
    """Greedy triangle-strip builder. The index slot in the file is fixed, so a replacement has
    to be packed about as tightly as the artist's original strips were: emitting one 4-index
    strip per triangle costs 4 indices/tri and an unedited round trip would not even fit.

    Do not bother making this cleverer without measuring first. Growing each strip BOTH ways
    (forward, reverse, forward again) and walking into the most-constrained neighbour instead of
    the first one were both tried on 72 of the game's hair shells and changed the packed length
    by exactly zero indices — those meshes are alpha-cut cards with little shared connectivity,
    so there is nothing for a smarter walk to merge. Seeding from the least-connected triangle
    was worse (+3.5%). The artist's own strips still beat this by 12.6%, and that gap is not
    closable from here: where a replacement has to fit a slot the artist's packing sized, carry
    the original index stream across instead (see the `strip` key in replace_part).
    """
    faces = [tuple(int(x) for x in f) for f in np.asarray(T)]
    edge: dict = {}
    for fi, (a, b, c) in enumerate(faces):
        for u, v in ((a, b), (b, c), (c, a)):
            edge.setdefault((u, v) if u < v else (v, u), []).append(fi)
    used = bytearray(len(faces))

    def oriented(fi, u, v):                       # face fi runs u -> v -> x the right way round
        a, b, c = faces[fi]
        return (a, b) == (u, v) or (b, c) == (u, v) or (c, a) == (u, v)

    strips = []
    for f0 in range(len(faces)):
        if used[f0]:
            continue
        used[f0] = 1
        strip = list(faces[f0])
        while True:
            u, v = strip[-2], strip[-1]
            # the triangle we are about to make starts at position len(strip)-2 in the strip;
            # odd positions are decoded with the two trailing vertices swapped
            want = (u, v) if (len(strip) - 2) % 2 == 0 else (v, u)
            nxt = None
            for fi in edge.get((u, v) if u < v else (v, u), ()):
                if not used[fi] and oriented(fi, want[0], want[1]):
                    nxt = fi
                    break
            if nxt is None:
                break
            used[nxt] = 1
            a, b, c = faces[nxt]
            strip.append(({a, b, c} - {u, v}).pop())
        strips.append(strip)
    return strips


def _strip_stream(strips) -> list[int]:
    """Strips -> one index run separated by single 0xFFFF restarts — exactly the packing the
    game's own submeshes use (each restart costs one index and resets the winding parity)."""
    out: list[int] = []
    for s in strips:
        if out:
            out.append(0xFFFF)
        out.extend(s)
    return out


def part_budget(part: dict) -> dict:
    """What a replacement mesh has to fit in. The submesh record is NOT rewritten (see
    replace_part), so the vertex range and the index range are both fixed. max_tris is a
    guide only — how many triangles actually fit depends on how well they strip, and
    replace_part does the exact check."""
    return dict(max_verts=part["n_vtx"], max_indices=part["n_idx"],
                max_tris=int(part["n_idx"] / 1.6), cur_verts=part["n_vtx"],
                cur_tris=part["tris"])


def replace_part(blob: bytearray, m: dict, part: dict, mesh: dict, log=print) -> str:
    """Overwrite ONE submesh's geometry in place.

    The submesh record is deliberately left byte-identical: instead of renumbering
    first_idx/n_idx/first_vtx/n_vtx (which would shift every later submesh in the model), the
    new mesh is written INSIDE the existing ranges and the leftover indices are filled with
    0xFFFF restarts, which draw nothing. So the only thing that changes is geometry data.

    The mesh is re-stripified (_stripify) before it is written, because the index slot is only
    ~2.5 indices per triangle wide — the size the artist's own strips packed down to.

    Baked lighting for the new vertices is taken from the nearest ORIGINAL vertex of this part:
    there are no normals or lights in the file to relight from, so inheriting is the only
    honest option (and use the Arena tab's lighting sliders afterwards if it needs a lift).
    """
    NV, ST, vb = m["nvtx"], m["stride"], m["vb"]
    P, UV, T = mesh["pos"], mesh["uv"], mesh["tris"]
    bud = part_budget(part)
    if len(P) > bud["max_verts"]:
        raise ValueError(f"{len(P)} vertices, but this part's slot holds {bud['max_verts']}. "
                         "Decimate the mesh (or replace a larger part). Nothing was written.")
    lo = part["first_vtx"]
    if lo + len(P) >= 0xFFFF:                          # 0xFFFF is the strip-restart sentinel
        raise ValueError("this part sits past vertex 65534 — it cannot be indexed. "
                         "Nothing was written.")
    orig = np.frombuffer(blob[vb + lo * ST:vb + (lo + part["n_vtx"]) * ST],
                         dtype=np.uint8).reshape(part["n_vtx"], ST)
    opos = orig[:, 0:12].copy().view(">f4").astype(np.float32)

    # nearest original vertex -> inherit its baked light / ambient (and any unknown tail bytes)
    d2 = ((P[:, None, :] - opos[None, :, :]) ** 2).sum(2) if len(P) * len(opos) <= 4_000_000 \
        else None
    if d2 is not None:
        near = np.argmin(d2, 1)
    else:                                              # too big for the dense form — chunk it
        near = np.empty(len(P), np.int64)
        for k in range(0, len(P), 512):
            blk = P[k:k + 512]
            near[k:k + 512] = np.argmin(((blk[:, None, :] - opos[None, :, :]) ** 2).sum(2), 1)

    R = orig[near].copy()
    R[:, 0:12] = P.astype(">f4").view(np.uint8).reshape(len(P), 12)
    if ST >= 24:
        R[:, 20:24] = np.clip(UV, -F16MAX, F16MAX).astype(">f2").view(np.uint8).reshape(len(P), 4)
    blob[vb + lo * ST:vb + (lo + len(P)) * ST] = R.tobytes()

    strips = _stripify(np.asarray(T) + lo)
    stream = _strip_stream(strips)
    if len(stream) > part["n_idx"]:
        raise ValueError(
            f"{len(T)} triangles pack into {len(stream)} indices but this part's slot holds "
            f"{part['n_idx']} (its own geometry uses {part['tris'] + 2}). Decimate the mesh — "
            "roughly {0} triangles is the ceiling here. Nothing was written."
            .format(int(len(T) * part['n_idx'] / max(len(stream), 1))))
    ib = np.full(part["n_idx"], 0xFFFF, ">u2")
    ib[:len(stream)] = np.array(stream, np.int64).astype(">u2")
    off = m["ib0"] + part["first_idx"] * 2
    blob[off:off + part["n_idx"] * 2] = ib.tobytes()
    log(f"  part {part['rec']}: {len(P)}/{bud['max_verts']} verts, {len(T)} tris in "
        f"{len(strips)} strips = {len(stream)}/{part['n_idx']} indices")
    return (f"part {part['rec']} replaced — {len(P)} vertices, {len(T)} triangles "
            f"({len(stream)}/{part['n_idx']} indices used)")
