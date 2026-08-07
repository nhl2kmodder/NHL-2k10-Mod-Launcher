"""stamp_shader.py -- place jersey stamps with the game's own pixel-shader math.

WHAT CHANGED AND WHY
--------------------
The preview used to place each stamp by blitting its source cell into a measured destination
rectangle. That is not what the game does, and the difference is visible: a rect blit has to
STRETCH the art to fill its rect, so glyph proportions drifted and the numbers came out far
larger than in game (the stored crest rect was 278x277 where the shader's own mask works out to
204x205 -- a 36% oversize, and it ran into the jersey's stripes).

The game never has a destination rectangle. Its jersey pixel shader samples the 2048x512 stamps
sheet TWICE, by two different rules, and lets the mesh decide where the art lands:

  LAYER 1 -- "logo" (crest, and the shoulder-patch/sponsor family). Unconditional. The sheet
  coordinate is an affine function of the BASE UV:

      sheet.x = ku * u + bu
      sheet.y = kv * v + bv
      drawn only where `window` (umin, vmin, umax, vmax) contains it -- the window MASKS, it
      never clamps, so the art appears exactly on the band of the body map the affine maps into
      the window, at exactly the scale the affine implies.

  LAYER 2 -- "decal" (numbers, letters, the pants mark). Predicated on a per-vertex selector.
  Interpolator 3 carries (decalU, decalV, slot) baked into the MESH, and each slot has its own
  scale/offset pair:

      sheet.xy = decalUV * S.xy + S.zw     drawn where mask M contains it

  So a slot is a fixed quad on the garment, and the constants choose which piece of the sheet is
  pulled into it. One slot holds exactly ONE glyph, which is why a two-digit number occupies two
  slots.

Both were validated against the game's own render target: the shirt's crest and its "71", and the
back panel's "71", reproduce pixel-exactly with no destination rect anywhere. The per-slot roles
in data/stamp_shader.json were likewise read off the game's render rather than inferred -- see
tools/build_stamp_map.py for how the decal channel is baked.

THE GLYPH CONSTANT RULE
-----------------------
Digits are monospaced and the constants are derived from the art, so the launcher can synthesise
them for any glyph instead of needing a capture per digit. For a cell (cx0, cy0, cx1, cy1) whose
ink spans x [ix0, ix1] and an advance A:

    S = (A/SW,  (cy1-cy0)/SH,  (inkCentreX - A/2)/SW,  cy0/SH)
    M = (ix0/SW, cy0/SH, ix1/SW, cy1/SH)          -- tight in X, the full cell in Y

Checked against the captured constants for both '7' (ink x[1437,1508], centre 1472.5, A=79 ->
S.z*2048 = 1433.0) and '1' (ink x[1065,1111], centre 1088 -> S.z*2048 = 1048.5). Exact.

THE "LOGO LAYER" WAS A MISREADING
---------------------------------
The shield / manufacturer mark / helmet decals were thought to need a second, undecoded affine.
They do not. In ps1019 the c33-vs-c44 select that picks them is driven by `interp3.w > c9.x` --
the SAME selector the number ladder runs on. Every stamp on the model is a decal quad; only the
crest uses layer 1. So a stamp's destination is baked into the mesh and is measured directly by
rasterising interp3 into base UV, which is what tools/build_stamp_map.py does. There is no
destination rectangle and no undecoded coordinate anywhere.

That closed the last three sites: the NHL shield (collar and pants), the manufacturer wordmark, and
the helmet's team mark and number. The helmet draws from its OWN sheet -- index 2 of
uniform_<team>_<kit>.iff, 1024x256 -- not from the 2048x512 stamps sheet, so its sites carry
`"sheet": "helmet"`. Note also that the helmet parks "no decal here" at slot 0 and starts real
slots at 1, where the garment shaders park it at 6.

The team's SHOULDER PATCH is not a stamp at all: it is painted into the uniform base texture.

Piece `pants` (draw 1080) carries the NHL shield and the manufacturer wordmark -- the BAUER mark
the game renders on the thigh. It was briefly renamed "shoulders" by reading its slots' base-UV
y[209,264] as a body position; a texture coordinate does not tell you where a slot lands on the
player. The model capture's own part list names the draw `pants` and its mesh spans y[-39.2,+21.3].

WHAT IS NOT DONE HERE
---------------------
The back NAME is a third mechanism -- draw 1055 binds two 1024x256 RGBA8 textures holding a
pre-rendered name strip, placed by a switch/case affine, not by either layer above. It stays on
the old measured path in jersey_preview and is reported by `unshaded()`.

`helmet` slot 5 (a 230x527 band) is the FRONT SPONSOR badge -- the Reebok wordmark on the front of
the shell in the game's own render -- and is left unassigned on purpose. Its art is not in
uniform_<team>_<kit>.iff: the helmet draw binds a shared 256x256 BC3 sheet carrying an Easton
wordmark and an NHL shield, so no edit the Jersey Editor can make would change it, and drawing it
would only bake a fixed logo into the preview.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ATLAS_NAME = "decal_atlas.npz"
CONFIG_NAME = "stamp_shader.json"

_atlas = None
_config = None


def _data_dir() -> Path:
    try:
        from . import resources as R
        return R.data_path(CONFIG_NAME).parent
    except Exception:
        return Path(__file__).resolve().parent / "data"


def available() -> bool:
    d = _data_dir()
    return (d / ATLAS_NAME).exists() and (d / CONFIG_NAME).exists()


def config() -> dict:
    global _config
    if _config is None:
        p = _data_dir() / CONFIG_NAME
        _config = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _config


def atlas() -> dict:
    """The baked decal channel: per piece, `decalUV`, the slot id and coverage, in 1024^2 UV."""
    global _atlas
    if _atlas is None:
        p = _data_dir() / ATLAS_NAME
        if not p.exists():
            _atlas = {}
        else:
            with np.load(p, allow_pickle=False) as z:
                _atlas = {k: z[k] for k in z.files}
    return _atlas


def atlas_pieces() -> set[str]:
    """Which garment pieces the baked atlas actually carries."""
    return {k[:-4] for k in atlas() if k.endswith("_cov")}


def unshaded() -> list[str]:
    """Stamps still drawn the old way, or not at all, with the reason."""
    return list(config().get("not_shaded", {}).keys())


# ── per-kit glyph metrics ─────────────────────────────────────────────────────────────────────
#
# The digit advance and the per-digit ink rects are NOT a property of the site, and they are not
# derivable from the art. They live in the uniform's own DRAM blob and differ from kit to kit:
#
#     0x540   u32 advance in sheet pixels, u32 128 (the row height, constant)
#     0x550   27 x (u32 x, y, w, h) -- the kit's slot table; entry 8 + d is digit d's INK rect
#
# Measured: cgy_home 92, ana_away 104, chi_home 79. Those are exactly the advances the captures'
# own pixel-shader constant buffers carry for every garment draw in the matching capture, and the
# rects match the captured masks byte for byte (ana '7' -> entry 15 = (1419,256,104,256), captured
# mask x[1419,1523]; ana '4' -> entry 12 = (1298,0,91,256), S.z*2048 = 1298+45.5-52 = 1291.5).
#
# stamp_shader.json used to hardcode 79 for every garment number site, so a Calgary kit drew every
# digit 92/79 = 1.16x too large and the big front numbers 92/67 = 1.37x -- the "numbers look oddly
# big" report. A SMALLER advance stretches the same ink across more of the slot quad.
_METRICS: dict[str, dict | None] = {}

DIGIT_SLOT0 = 8       # slot-table index of digit 0; digit d is at DIGIT_SLOT0 + d


def kit_metrics(iff: str) -> dict | None:
    """{'advance': px, 'digit_rects': [(x0,y0,x1,y1)] * 10} for one uniform_<team>_<kit>.iff.

    Returns None if the asset or its DRAM blob cannot be read, in which case callers fall back to
    the per-site `advance_px` and the nominal 128x256 cell grid.
    """
    if iff in _METRICS:
        return _METRICS[iff]
    out = None
    try:
        import struct

        from . import archive_textures as A
        _loc, data, size = A._read_asset(iff, None, False)
        d = next((b["dec"] for b in A._walk_blobs(data, size)
                  if b.get("dec") and len(b["dec"]) > 0x550 + 27 * 16), None)
        if d is not None:
            adv = struct.unpack_from(">I", d, 0x540)[0]
            rects = []
            for dg in range(10):
                x, y, w, h = struct.unpack_from(">4I", d, 0x550 + (DIGIT_SLOT0 + dg) * 16)
                rects.append((x, y, x + w, y + h))
            # A kit with a plausible advance and ten non-empty rects, or nothing.
            if 8 <= adv <= 512 and all(r[2] > r[0] and r[3] > r[1] for r in rects):
                out = {"advance": int(adv), "digit_rects": rects}
    except Exception:
        out = None
    _METRICS[iff] = out
    return out


def digit_constants(metrics: dict, digit: int, sheet_size=(2048, 512)):
    """(S, M) for one digit, straight off the kit's own table -- no art inspection at all.

    glyph_constants() derives the ink span by scanning the drawn alpha, which is right when the
    launcher invents the art but wrong for matching the GAME: the game masks to the rect stored in
    the kit, whatever was painted into the cell. This is the 1:1 path.
    """
    SW, SH = sheet_size
    x0, y0, x1, y1 = metrics["digit_rects"][digit]
    A = float(metrics["advance"])
    centre = (x0 + x1) / 2.0
    S = (A / SW, (y1 - y0) / float(SH), (centre - A / 2.0) / SW, y0 / float(SH))
    return S, (x0 / float(SW), y0 / float(SH), x1 / float(SW), y1 / float(SH))


# ── sampling ──────────────────────────────────────────────────────────────────────────────────

def _sample(sheet: np.ndarray, su: np.ndarray, sv: np.ndarray) -> np.ndarray:
    """Bilinear fetch, matching the sampler the draws use (linear, no mip on this sheet)."""
    h, w = sheet.shape[:2]
    x = np.clip(su * w - 0.5, 0, w - 1)
    y = np.clip(sv * h - 0.5, 0, h - 1)
    x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    return ((sheet[y0, x0] * (1 - fx) + sheet[y0, x1] * fx) * (1 - fy)
            + (sheet[y1, x0] * (1 - fx) + sheet[y1, x1] * fx) * fy)


def _blend(out: np.ndarray, vis: np.ndarray, px: np.ndarray) -> None:
    a = px[:, 3:4] / 255.0
    view = out[vis]
    view[:, :3] = px[:, :3] * a + view[:, :3] * (1 - a)
    view[:, 3:4] = np.maximum(view[:, 3:4], px[:, 3:4])
    out[vis] = view


def _resize_nearest(a: np.ndarray, shape) -> np.ndarray:
    """Point-sample the 1024^2 atlas up or down to the working texture's size."""
    H, W = shape
    if a.shape[0] == H and a.shape[1] == W:
        return a
    yi = (np.arange(H) * (a.shape[0] / float(H))).astype(np.int32).clip(0, a.shape[0] - 1)
    xi = (np.arange(W) * (a.shape[1] / float(W))).astype(np.int32).clip(0, a.shape[1] - 1)
    return a[yi][:, xi]


# ── layer 1: logos ────────────────────────────────────────────────────────────────────────────

def logo_layer(out: np.ndarray, sheet: np.ndarray, site: dict, cov: np.ndarray) -> bool:
    """sheet.xy = (ku*u + bu, kv*v + bv), masked by `window`. Returns whether anything drew."""
    H, W = out.shape[:2]
    u = ((np.arange(W) + 0.5) / W)[None, :]
    v = ((np.arange(H) + 0.5) / H)[:, None]
    su = np.broadcast_to(u * site["ku"] + site["bu"], (H, W))
    sv = np.broadcast_to(v * site["kv"] + site["bv"], (H, W))
    m = site["window"]
    vis = cov & (su > m[0]) & (sv > m[1]) & (m[2] >= su) & (m[3] >= sv)
    if not vis.any():
        return False
    px = _sample(sheet, su[vis], sv[vis])
    if not (px[:, 3] > 8).any():
        return False
    _blend(out, vis, px)
    return True


# ── layer 2: decals ───────────────────────────────────────────────────────────────────────────

def glyph_constants(sheet: np.ndarray, cell, advance_px: float | None, fit: str = "cell"):
    """(S, M) for one source cell -- the game's own per-slot constants, derived from the art.

    With `advance_px` the cell is treated as a monospaced glyph: the slot spans one advance and
    the ink is centred in it, which is the rule the captured digit constants follow exactly.

    Without one, `fit` picks the rule:

      "cell" (default, and what the GAME does) -- the whole cell is mapped across the slot:
          S = (cellW/SW, cellH/SH, cellX0/SW, cellY0/SH),  M = the cell rect
      Read byte-exact off three independent captured sites: the NHL shield, the manufacturer
      wordmark and the helmet team mark. The mark's size on the garment therefore comes from the
      slot quad in the mesh, not from how much ink the cell happens to hold.

      "ink" -- fit the slot to the ink bbox instead. Only for sites whose source cell is supplied
      by the launcher rather than measured off the game's own atlas, where a generously padded
      cell would otherwise blow the art up to fill the quad.
    """
    SH, SW = sheet.shape[:2]
    cx0, cy0, cx1, cy1 = (int(round(t)) for t in cell)
    cx0, cy0 = max(0, cx0), max(0, cy0)
    cx1, cy1 = min(SW, cx1), min(SH, cy1)
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    a = sheet[cy0:cy1, cx0:cx1, 3]
    ys, xs = np.nonzero(a > 8)
    if not len(xs):
        return None
    ix0, ix1 = cx0 + int(xs.min()), cx0 + int(xs.max()) + 1
    if advance_px:
        A = float(advance_px)
        centre = (ix0 + ix1) / 2.0
        S = (A / SW, (cy1 - cy0) / float(SH), (centre - A / 2.0) / SW, cy0 / float(SH))
        return S, (ix0 / float(SW), cy0 / float(SH), ix1 / float(SW), cy1 / float(SH))
    if fit == "ink":
        iy0, iy1 = cy0 + int(ys.min()), cy0 + int(ys.max()) + 1
        S = ((ix1 - ix0) / float(SW), (iy1 - iy0) / float(SH), ix0 / float(SW), iy0 / float(SH))
        return S, (ix0 / float(SW), iy0 / float(SH), ix1 / float(SW), iy1 / float(SH))
    S = ((cx1 - cx0) / float(SW), (cy1 - cy0) / float(SH), cx0 / float(SW), cy0 / float(SH))
    return S, (cx0 / float(SW), cy0 / float(SH), cx1 / float(SW), cy1 / float(SH))


def decal_layer(out: np.ndarray, sheet: np.ndarray, duv: np.ndarray, slot: np.ndarray,
                cov: np.ndarray, slot_id: int, S, M) -> bool:
    """sheet.xy = decalUV * S.xy + S.zw, masked by M, on the texels carrying `slot_id`."""
    su = duv[..., 0] * S[0] + S[2]
    sv = duv[..., 1] * S[1] + S[3]
    vis = cov & (slot == slot_id) & (su > M[0]) & (sv > M[1]) & (M[2] >= su) & (M[3] >= sv)
    if not vis.any():
        return False
    px = _sample(sheet, su[vis], sv[vis])
    if not (px[:, 3] > 8).any():
        return False
    _blend(out, vis, px)
    return True


def piece(name: str, shape):
    """(decalUV, slot, coverage) for one garment piece, at the working texture's resolution."""
    A = atlas()
    if name + "_cov" not in A:
        return None
    duv = _resize_nearest(A[name + "_uv"], shape).astype(np.float32)
    return duv, _resize_nearest(A[name + "_slot"], shape), _resize_nearest(A[name + "_cov"], shape)


# ── per-FRAGMENT evaluation ───────────────────────────────────────────────────────────────────
#
# Everything above bakes the two layers into a texture in base-UV space. That is right for the
# files the game is given -- the game does its own stamping and never sees a baked copy -- but it
# is wrong for the 3-D preview, because a stamp's resolution is then capped by how much of the
# base map its quad happens to own. The collar badge owns a 94x10 sliver of the 1024^2 atlas and
# 47x5 of the 512^2 render texture, so it arrives as a smear where the game draws a crisp shield.
#
# The game has no such cap: it evaluates the shader per FRAGMENT, at screen resolution. So does
# this. `decalUV` is smooth inside a slot quad, so resampling it out of the baked 1024^2 field
# reconstructs it essentially exactly; it is the SHEET fetch that must not be pre-baked, and here
# it happens at the fragment, straight off the 2048x512 art.

_BBOX: dict[str, tuple] = {}


def piece_bbox(name: str):
    """The piece's UV bounding box, so a fragment pass can skip the fragments it cannot touch.

    A garment piece owns a few percent of the map; testing a fragment against four floats first
    is what keeps per-fragment evaluation affordable on a preview that redraws while you drag.
    """
    if name not in _BBOX:
        A = atlas()
        C = A.get(name + "_cov")
        if C is None:
            _BBOX[name] = None
        else:
            ys, xs = np.nonzero(C)
            h, w = C.shape
            _BBOX[name] = (None if not len(xs) else
                           (xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h))
    return _BBOX[name]


def sample_piece(name: str, uv: np.ndarray):
    """(decalUV, slot, coverage) sampled at arbitrary UV points rather than on a texel grid.

    `decalUV` is interpolated bilinearly among only those neighbours that share the centre
    texel's slot: it is smooth within a quad and jumps between quads, so an unguarded tap on a
    slot boundary would bleed one digit's coordinates into the next.
    """
    A = atlas()
    if name + "_cov" not in A:
        return None
    D, SL, C = A[name + "_uv"].astype(np.float32), A[name + "_slot"], A[name + "_cov"]
    h, w = C.shape
    x = np.clip(uv[:, 0] * w - 0.5, 0, w - 1)
    y = np.clip(uv[:, 1] * h - 0.5, 0, h - 1)
    x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    xi, yi = np.rint(x).astype(np.int32).clip(0, w - 1), np.rint(y).astype(np.int32).clip(0, h - 1)
    slot, cov = SL[yi, xi], C[yi, xi]

    acc = np.zeros((len(x), 2), np.float32)
    wsum = np.zeros((len(x), 1), np.float32)
    for yy, xx, ww in ((y0, x0, (1 - fx) * (1 - fy)), (y0, x1, fx * (1 - fy)),
                       (y1, x0, (1 - fx) * fy), (y1, x1, fx * fy)):
        ok = ((SL[yy, xx] == slot) & C[yy, xx])[:, None] * ww
        acc += D[yy, xx] * ok
        wsum += ok
    duv = np.where(wsum > 1e-6, acc / np.maximum(wsum, 1e-6), D[yi, xi])
    return duv, slot, cov


def _blend01(dst: np.ndarray, vis: np.ndarray, px: np.ndarray) -> None:
    """Alpha-over one sheet fetch into a 0..1 RGB fragment buffer."""
    a = (px[:, 3:4] / 255.0).astype(np.float32)
    dst[vis] = px[:, :3] * (a / 255.0) + dst[vis] * (1.0 - a)


def logo_points(dst: np.ndarray, sheet: np.ndarray, site: dict, uv: np.ndarray,
                cov: np.ndarray) -> np.ndarray | None:
    """`logo_layer` at fragments. Returns the visibility mask, or None if nothing drew."""
    su = uv[:, 0] * site["ku"] + site["bu"]
    sv = uv[:, 1] * site["kv"] + site["bv"]
    m = site["window"]
    vis = cov & (su > m[0]) & (sv > m[1]) & (m[2] >= su) & (m[3] >= sv)
    if not vis.any():
        return None
    px = _sample(sheet, su[vis], sv[vis])
    _blend01(dst, vis, px)
    return vis


def decal_points(dst: np.ndarray, sheet: np.ndarray, duv: np.ndarray, slot: np.ndarray,
                 cov: np.ndarray, slot_id: int, S, M) -> np.ndarray | None:
    """`decal_layer` at fragments. Returns the visibility mask, or None if nothing drew."""
    su = duv[:, 0] * S[0] + S[2]
    sv = duv[:, 1] * S[1] + S[3]
    vis = cov & (slot == slot_id) & (su > M[0]) & (sv > M[1]) & (M[2] >= su) & (M[3] >= sv)
    if not vis.any():
        return None
    px = _sample(sheet, su[vis], sv[vis])
    _blend01(dst, vis, px)
    return vis


def sample_at(sheet: np.ndarray, uv: np.ndarray, site: dict = None, S=None, M=None):
    """The raw sheet fetch a logo/decal draw makes at the given fragments -- used by the relief
    pass, which needs the NORMAL sheet's value at exactly the texels the colour draw covered."""
    if site is not None:
        return _sample(sheet, uv[:, 0] * site["ku"] + site["bu"],
                       uv[:, 1] * site["kv"] + site["bv"])
    return _sample(sheet, uv[:, 0] * S[0] + S[2], uv[:, 1] * S[1] + S[3])
