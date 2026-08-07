"""jersey_convert.py -- turn an NHL 23 jersey texture set into an NHL 2K10 one.

The two games flatten a hockey shirt almost identically, which is what makes this tractable:
NHL 23's jersey base and 2K10's `uniform_base` differ by a single anisotropic scale about the
texture centre (fitted in data/jersey_convert_profile.json; sleeve stripes land within ~1px).
Both scales are below 1, so the edges of the destination ask for source that does not exist;
`body.relax` eases that shortfall out over a wide band instead of padding it (see body_transform).
Everything else is repacking, because the two engines SPLIT the same art differently:

    NHL 23                             NHL 2K10
    base   jersey only, logos baked    uniform_base  jersey + pants + collar + socks, no logos
    pant   separate file               uniform       stamps / normal / helmet / letters / ...
    sock   separate file
    font   one atlas, literal colours  letters+stamps+helmet, COLOUR-KEYED

So the conversion is three jobs:

  1. Repack the base. Which pixel of the 1024x1024 base belongs to which garment is a property
     of the MESH, so it is read from data/uniform_uv_regions.png (baked by
     tools/build_uv_regions.py from the captured player mesh) rather than eyeballed. The jersey
     islands come from NHL 23's base through the fitted transform; pants and socks come from
     their own files; the collar is borrowed from the base's top band because NHL 23 has no
     collar texture at all.

  2. Lift the logos. 2K10 wants the crest and shoulder patch OUT of the base and INTO the stamp
     sheet, so each is detected, cut out, and the hole filled. Detection and fill are the same
     operation: estimate the background by sampling far along each axis (stripes survive that,
     compact blobs do not), and the difference IS the logo while the estimate IS the fill.

  3. Rebuild the glyphs. NHL 23 keeps letters, jersey numbers, helmet numbers and the C/A
     patches in one atlas with literal colours. 2K10 splits them across three sheets and expects
     them COLOUR-KEYED -- blue is the fill mask, red and green are the outline masks, and the
     shader substitutes team palette colours. Converted glyphs are fitted into the STOCK sheet's
     own ink boxes, so each team keeps its own metrics and no glyph-UV table has to be found.

Nothing here touches the game files; `convert()` returns images and the caller writes them.
Runs standalone for testing:

    python -m launcher.jersey_convert --src <dir> --stock <kit dir> --out <dir>
"""
from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

try:
    from . import resources as R
except ImportError:                                    # standalone / CLI
    R = None

PROFILE_NAME = "jersey_convert_profile.json"
REGION_NAME = "uniform_uv_regions.png"

# Palette index -> region name, mirroring tools/build_uv_regions.py REGIONS order.
REGION_ORDER = ("jersey_back", "jersey_front", "jersey_arms", "pants", "collar", "socks")

# Smallest bounding-box area a connected island must have to count as a real one under
# `per_island`. The region map is rasterised from the mesh, so a triangle that grazes a texel
# leaves one- and two-pixel specks -- jersey_arms has a 2px island at x=521, right between the
# two sleeves. Left in, it eats a sleeve's entry in `src_rects` and shifts the right arm onto
# the wrong crop.
MIN_ISLAND = 256

# The four NHL 23 inputs. `stem` is what a file must contain to be auto-detected.
SOURCE_KINDS = (("base", "base"), ("pant", "pant"), ("sock", "sock"), ("font", "font"))

# 2K10 texture index -> what it is, inside uniform_*.iff and uniform_base_*.iff.
UNIFORM_TEX = {0: "stamps", 1: "normal", 2: "helmet", 3: "letters", 4: "letters_normal", 5: "crowd"}
BASE_TEX = {0: "base", 1: "details", 2: "base_normal"}

# Sheets this module produces, and the (asset kind, texture index) each is written to.
OUTPUTS = {"base": ("base", 0), "stamps": ("uniform", 0),
           "helmet": ("uniform", 2), "letters": ("uniform", 3)}


# ── data files ────────────────────────────────────────────────────────────────────────────────

def _data_dir() -> Path:
    if R is not None:
        try:
            return R.data_path(PROFILE_NAME).parent
        except Exception:
            pass
    return Path(__file__).resolve().parent / "data"


def load_profile(path: Path | None = None) -> dict:
    return json.loads(Path(path or _data_dir() / PROFILE_NAME).read_text(encoding="utf-8"))


def load_regions(path: Path | None = None) -> dict[str, np.ndarray]:
    """region name -> bool mask over the 1024x1024 base. Empty dict if the bake is missing."""
    p = Path(path or _data_dir() / REGION_NAME)
    if not p.exists():
        return {}
    idx = np.array(Image.open(p).convert("P"))
    return {name: idx == i for i, name in enumerate(REGION_ORDER, start=1)}


# ── image IO ──────────────────────────────────────────────────────────────────────────────────

_DDS_RGB, _DDS_ALPHA, _DDS_FOURCC = 0x40, 0x1, 0x4


def load_image(path) -> Image.Image:
    """Open a source texture as RGBA.

    Pillow reads block-compressed DDS (NHL 23 ships DXT1/DXT5) but NOT the uncompressed
    A8R8G8B8 that the Mod Launcher writes when it extracts a 2K10 texture, so that case is
    unpacked here. Both show up in this workflow -- NHL 23 art comes in compressed, and the
    stock 2K10 sheet you are converting AGAINST comes out of the launcher uncompressed.
    """
    path = Path(path)
    if path.suffix.lower() != ".dds":
        return Image.open(path).convert("RGBA")
    raw = path.read_bytes()
    if len(raw) < 128 or raw[:4] != b"DDS ":
        raise ValueError(f"{path.name}: not a DDS")
    h, w = struct.unpack_from("<II", raw, 12)
    pf_flags = struct.unpack_from("<I", raw, 80)[0]
    if pf_flags & _DDS_FOURCC:
        return Image.open(path).convert("RGBA")
    bpp = struct.unpack_from("<I", raw, 88)[0]
    if bpp != 32:
        raise ValueError(f"{path.name}: unsupported uncompressed DDS ({bpp}bpp)")
    body = np.frombuffer(raw, np.uint8, count=w * h * 4, offset=128).reshape(h, w, 4)
    return Image.fromarray(body[:, :, [2, 1, 0, 3]], "RGBA")     # BGRA -> RGBA


# NHL 23 ships several maps per garment under the same stem, distinguished only by the last
# underscore token: `..._color` is the albedo we want, `..._coeff` is a packed PBR coefficient
# map that LOOKS like plausible art in a thumbnail and silently ruins a conversion. Anything in
# this list that isn't `color` is an instant reject; a name with no map suffix at all is still
# accepted, because the hand-renamed proto files (`base_NHL23.DDS`) have none.
_MAP_SUFFIXES = frozenset((
    "color", "coeff", "normal", "nrm", "norm", "spec", "gloss", "rough", "metal",
    "mask", "ao", "alpha", "opacity", "disp", "emis", "emissive", "cube", "tint"))

# kind -> substrings that identify it, MOST SPECIFIC FIRST. Every name is tested against the
# kinds in this order, so `jersey_adidas_tor_home_numbers_color` lands on `font` and not on
# `base`, even though it also says "jersey", and `helmetlogo_adidas_toronto_home_0_color` lands
# on the helmet decal instead of winning the base slot on the word "adidas" alone.
_KIND_HINTS = (
    ("helmet_logo", ("helmetlogo", "helmet_logo", "helmetdecal")),
    ("font", ("font", "number", "digit")),
    ("pant", ("pant",)),
    ("sock", ("sock",)),
    ("base", ("base", "jersey", "adidas")),
)

# A garment folder also holds crest/patch/badge art that shares the `_adidas_<team>_<kit>_color`
# shape. Those are decals, never a garment sheet, so nothing carrying one of these words is
# allowed to fill a garment slot no matter how well the rest of the name matches.
_NOT_A_GARMENT = ("logo", "crest", "patch", "badge", "decal", "emblem", "capt", "helmet",
                  "glove", "stick", "skate", "mask")


def _source_rank(name: str, kind: str) -> int | None:
    """Preference for `name` as source `kind`. None rejects it outright.

    3 = the canonical `<kind-ish>_adidas_…_color` shape, 2 = some other explicit `_color` map,
    1 = no map suffix at all (so probably already the albedo -- the hand-renamed proto files).
    """
    stem = name.rsplit(".", 1)[0]
    tail = stem.rsplit("_", 1)[-1]
    if tail in _MAP_SUFFIXES:
        if tail != "color":
            return None
        rank = 2
    else:
        rank = 1
    if kind != "helmet_logo" and any(w in stem for w in _NOT_A_GARMENT):
        return None
    # `jersey_…` / `pant_…` / `sock_…` as the FIRST token is the shipped naming; prefer it over a
    # name that merely mentions the word somewhere in the middle.
    head = stem.split("_", 1)[0]
    if rank == 2 and head in ("jersey", "pant", "pants", "sock", "socks"):
        rank = 3
    return rank


def find_sources(folder) -> dict[str, Path]:
    """Auto-detect the NHL 23 inputs in a folder.

    Handles both the shipped naming (`jersey_adidas_{team}_{variation}_color.dds` and its
    pants/socks/numbers siblings) and the hand-renamed proto files (`base_NHL23.DDS`). Besides
    the four garment sheets this can return `helmet_logo`, which is a decal rather than a source
    texture -- the tab routes it to the helmet-logo art slot.
    """
    folder = Path(folder)
    best: dict[str, tuple[int, Path]] = {}
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix.lower() not in (".dds", ".png", ".tga"):
            continue
        low = f.name.lower()
        if "2k10" in low:                              # a 2K10 sheet sitting alongside the sources
            continue
        for kind, hints in _KIND_HINTS:
            if not any(h in low for h in hints):
                continue
            rank = _source_rank(low, kind)
            if rank is not None and rank > best.get(kind, (0, None))[0]:
                best[kind] = (rank, f)
            break
    return {k: v[1] for k, v in best.items()}


# ── small image helpers ───────────────────────────────────────────────────────────────────────

def _to_arr(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGBA"), dtype=np.float32)


def _to_img(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGBA")


def ink_box(img: Image.Image, rect=None, thresh: int = 12):
    """(x, y, w, h) of the non-transparent ink inside `rect`, in FULL-image coordinates.

    None when the slot is blank -- which is information, not an error: a jersey with no shoulder
    patch ships that cell empty, and that is how an unused slot is recognised.
    """
    x0, y0, w, h = rect or (0, 0, img.width, img.height)
    a = np.asarray(img.convert("RGBA"))[y0:y0 + h, x0:x0 + w, 3]
    ys, xs = np.where(a > thresh)
    if not len(xs):
        return None
    return (x0 + int(xs.min()), y0 + int(ys.min()),
            int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1)


def fit_into(cell: Image.Image, box, pad: float = 0.0) -> tuple[Image.Image, tuple[int, int]]:
    """Scale `cell` to sit inside `box` (x, y, w, h) preserving aspect. -> (image, paste xy)."""
    bx, by, bw, bh = box
    bw, bh = max(1, int(bw * (1 - pad))), max(1, int(bh * (1 - pad)))
    src = ink_box(cell)
    if src:                                            # trim transparent margin before fitting
        cell = cell.crop((src[0], src[1], src[0] + src[2], src[1] + src[3]))
    if cell.width < 1 or cell.height < 1:
        return cell, (int(bx), int(by))
    s = min(bw / cell.width, bh / cell.height)
    nw, nh = max(1, round(cell.width * s)), max(1, round(cell.height * s))
    out = cell.resize((nw, nh), Image.LANCZOS)
    # Centre on the ORIGINAL box, not the padded one, so padding shrinks without shifting.
    return out, (round(bx + (box[2] - nw) / 2), round(by + (box[3] - nh) / 2))


def paste_rgba(dst: Image.Image, src: Image.Image, xy) -> None:
    dst.paste(src, (int(xy[0]), int(xy[1])), src)


# Slots whose art is TYPE and therefore sits on a baseline. Scaling those about the cell centre
# would walk the whole set up or down the jersey, so they anchor to the bottom of the cell instead.
BASELINE_PREFIXES = ("digit_", "small_num_", "capt_")


def scale_for(scales: dict | None, name: str) -> float:
    """Per-name scale, falling back to the sheet-wide `"*"` entry. 1.0 = leave it alone."""
    if not scales:
        return 1.0
    return float(scales.get(name, scales.get("*", 1.0)))


def rescale_slot(sheet: Image.Image, rect, s: float, anchor: str = "center") -> None:
    """Grow or shrink whatever a slot already holds, in place, clipped to the slot.

    Size ON THE BODY is not authorable: the shader maps a fixed sheet cell onto a fixed patch of
    jersey, so the only way to make a crest bigger or a number smaller is to draw its art bigger or
    smaller INSIDE its cell. That is what this does.

    It runs as a post-pass over the finished sheet, which is what makes it work in every mode: the
    mark can have come from a conversion, from uploaded patch art, or from the kit's own texture
    with nothing else touched, and this resamples it either way. Rebuilds always restart from the
    stock sheet, so a scale is applied to the original art each time and never compounds.

    Clipping to the slot is not optional -- the stamp cells are packed edge to edge, and a crest
    scaled past its cell would otherwise bleed into the shoulder patch next door.
    """
    if abs(s - 1.0) < 1e-3:
        return
    x, y, w, h = (int(round(v)) for v in rect)
    if w < 1 or h < 1:
        return
    cell = sheet.crop((x, y, x + w, y + h))
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    grown = cell.resize((nw, nh), Image.LANCZOS)
    ox = x + (w - nw) // 2
    oy = y + (h - nh) if anchor == "bottom" else y + (h - nh) // 2
    sheet.paste(Image.new("RGBA", (w, h), (0, 0, 0, 0)), (x, y))
    ix0, iy0 = max(x, ox), max(y, oy)
    ix1, iy1 = min(x + w, ox + nw), min(y + h, oy + nh)
    if ix1 > ix0 and iy1 > iy0:
        sheet.paste(grown.crop((ix0 - ox, iy0 - oy, ix1 - ox, iy1 - oy)), (ix0, iy0))


# ── 1. the base ───────────────────────────────────────────────────────────────────────────────

def _relaxed_axis(n: int, c: float, s: float, knee: float) -> np.ndarray:
    """Inverse map dst -> src for one axis: exact 1/s in the core, easing to the edge outside it.

    The fitted scale is BELOW 1 on both axes, so a plain scale about the centre asks for source
    pixels that do not exist -- ~18px past the left/right edges and ~50px past the top/bottom.
    That shortfall has to be absorbed somewhere, and WHERE decides how the result looks:

      * Absorb it at the edge by replicating the last source row (what this used to do). The hem
        row and the neck tag then smear across ~50 rows of the atlas. That is the stretched tag.
      * Absorb it across the whole panel by scaling harder. Every horizontal feature then drifts
        toward the centre -- the hem stripes ride ~45px up the shirt.
      * Absorb it across a WIDE band next to the edge only. The core keeps the fitted mapping
        exactly, and the leftover ~50 rows are spread over a 256-row run, so the hem gains about
        1.1x of stretch: below the eye, and the stripes land within a few pixels of true.

    The third is what this does. `knee` is the half-width (in destination pixels, from the
    centre) of the core that keeps the fitted scale untouched -- outside it the map is the
    quadratic that matches both the value and the slope at the knee and lands exactly on the last
    source pixel at the edge, so there is no crease where the two meet and nothing samples out of
    bounds. Positive slope at the edge is asserted by the caller's choice of knee, which is why
    the profile keeps `relax` beside `scale` instead of hardcoding a fraction here.
    """
    d = np.arange(n, dtype=np.float64)
    g = c + (d - c) / s                                # the fitted core, everywhere
    for sign, far in ((1.0, n - 1.0), (-1.0, 0.0)):    # far = the destination edge on this side
        yk = c + sign * knee                           # knee, in destination pixels
        gk = c + sign * knee / s                       # the source coord it samples
        w = far - yk                                   # destination pixels left past the knee
        if w == 0 or (sign > 0) != (w > 0):            # knee at or beyond the edge: core only
            continue
        u = np.clip((d - yk) / w, 0.0, 1.0)
        # Quadratic matching the core's value AND slope (1/s) at the knee, and landing exactly
        # on the source edge (`far`) at the destination edge -- so nothing samples out of bounds.
        g = np.where((d - yk) * sign >= 0, gk + (w / s) * u + (far - gk - w / s) * u * u, g)
    return np.clip(g, 0.0, n - 1)


def _resample_axis(arr: np.ndarray, coords: np.ndarray, axis: int) -> np.ndarray:
    """Linear resample along one axis at fractional `coords` (len == output size)."""
    n = arr.shape[axis]
    i0 = np.clip(np.floor(coords).astype(np.int64), 0, n - 1)
    i1 = np.clip(i0 + 1, 0, n - 1)
    t = (coords - i0).astype(np.float32)
    a0 = np.take(arr, i0, axis=axis)
    a1 = np.take(arr, i1, axis=axis)
    shape = [1] * arr.ndim
    shape[axis] = -1
    t = t.reshape(shape)
    return a0 * (1.0 - t) + a1 * t


def body_transform(base: Image.Image, profile: dict) -> Image.Image:
    """NHL 23 base -> 2K10 base space.

    The two atlases differ by one anisotropic scale about the texture centre (fitted; see the
    profile). The scale is separable, so this is two 1D resamples rather than a PIL affine --
    which is what lets the edge bands be eased instead of padded. See `_relaxed_axis` for why
    the edges need their own treatment at all; `body.relax` in the profile sets how wide the
    untouched core is. With `relax` absent this degrades to the plain scale, and then the outer
    rows clamp to the source edge exactly as PIL's `mode="edge"` pad used to.
    """
    b = profile["body"]
    dw, dh = b["dst_size"]
    sx, sy = b["scale"]
    cx, cy = b["center"][0] * dw, b["center"][1] * dh
    rel = b.get("relax") or {}
    img = np.asarray(base.convert("RGBA").resize((dw, dh), Image.LANCZOS), np.float32)
    xs = _relaxed_axis(dw, cx, sx, rel.get("x", dw))   # knee >= size => core everywhere
    ys = _relaxed_axis(dh, cy, sy, rel.get("y", dh))
    out = _resample_axis(_resample_axis(img, ys, 0), xs, 1)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


def window_background(arr: np.ndarray, window, margin: int = 10,
                      mask: np.ndarray | None = None) -> np.ndarray:
    """Reconstruct the cloth under `mask` by ramping along each row to the nearest clean cloth.

    Without a mask this falls back to filling the whole window, which is only meaningful as a
    rough preview -- the real call passes the detected decal mask.

    Why "nearest clean cloth" and not "the strip just outside the window": the windows are
    measured once, on one team, and a window that fits Chicago's yoke overhangs Anaheim's. When
    it overhangs, the strip outside it is a DIFFERENT material -- white body instead of black
    yoke -- and ramping from it painted a grey/red gradient across the shoulder. Anchoring on
    the nearest unmasked pixel in the same row cannot do that: whatever the decal sits on is by
    definition what touches it. Still exact for the two things a jersey panel actually is under
    a decal, a flat field or horizontal trim, and now also correct on a shaped yoke.

    A global filter was tried first and is the wrong tool here: the base is an atlas, so a
    kernel wide enough to straddle a 280px crest also straddles the seam into a different
    garment and mixes in colours that were never near the logo.
    """
    x0, y0, x1, y1 = window
    h, w = arr.shape[:2]
    lo, hi = max(0, x0 - margin), min(w, x1 + margin)
    strip = arr[y0:y1, lo:hi, :3].astype(np.float32)
    m = np.zeros(strip.shape[:2], bool)
    if mask is None:
        m[:, x0 - lo:x1 - lo] = True
    else:
        m[:, x0 - lo:x1 - lo] = mask
    out = strip.copy()
    idx = np.arange(strip.shape[1])
    solved = np.zeros(strip.shape[0], bool)
    for y in range(strip.shape[0]):
        row = m[y]
        if row.all():                                  # no anchor anywhere on this row
            continue
        solved[y] = True
        if not row.any():
            continue
        good = idx[~row]
        bad = idx[row]
        for c in range(3):
            # np.interp clamps outside the anchor range, so a decal running off the end of the
            # row continues the last clean pixel instead of extrapolating into nonsense.
            out[y, bad, c] = np.interp(bad, good, strip[y, ~row, c])
    # Rows the decal covers END TO END have no anchor of their own -- a collar spans its whole
    # window, and so does a wide crest. Left alone they estimated "the decal is the background",
    # the colour test then found no difference, and the decal survived in a band across its own
    # middle (the black smear where a collar used to be). Fill those rows DOWN each column from
    # the nearest rows that did solve: the cloth behind a decal varies far more slowly than the
    # decal does, and this is the same one-dimensional ramp, just along the other axis.
    if solved.any() and not solved.all():
        rows = np.arange(strip.shape[0])
        for c in range(3):
            out[~solved, :, c] = np.stack(
                [np.interp(rows[~solved], rows[solved], out[solved, x, c])
                 for x in range(strip.shape[1])], axis=1)
    return out[:, x0 - lo:x1 - lo]


@dataclass
class Logo:
    """A decal lifted off the NHL 23 base."""
    name: str
    image: Image.Image                 # cut-out, alpha-masked
    box: tuple                         # (x, y, w, h) in 1024-base space
    stamp: str | None = None           # stamp slot it feeds, if any


def lift_logos(base1024: Image.Image, profile: dict,
               windows: dict | None = None, threshold: float | None = None
               ) -> tuple[Image.Image, list[Logo], np.ndarray]:
    """Cut every configured decal off the base. -> (cleaned base, logos, detection mask).

    `windows` / `threshold` override the profile so the tab can re-run this live while the user
    drags a box; everything else is read from `profile["strip"]`.
    """
    from scipy import ndimage

    cfg = profile["strip"]
    thr = cfg["threshold"] if threshold is None else threshold
    over = windows or {}
    arr = _to_arr(base1024)
    out = arr.copy()
    h, w = arr.shape[:2]

    logos: list[Logo] = []
    keep = np.zeros((h, w), bool)
    for reg in cfg["regions"]:
        x0, y0, x1, y1 = over.get(reg["name"], reg["window"])
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(w, int(x1)), min(h, int(y1))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        sub = arr[y0:y1, x0:x1]
        # TWO stages, because neither test alone is right.
        #
        # Stage 1 -- WHERE THE CLOTH ISN'T. Edge energy: cloth is flat, a decal is not. This is
        # only a SEED; it is deliberately not the answer, because a filled outline misses the
        # parts of a logo that merge tonally with what they sit on (Chicago's crest lost its
        # outer feathers that way). What it IS reliable for is telling us which pixels must not
        # be trusted as background anchors.
        # Measured on a PADDED window, because the seed's job is to decide what is cloth, and
        # that question is not answerable inside a 134px box: a collar and a yoke look identical
        # there. What separates them is whether the flat area runs OUT of the neighbourhood.
        pad = int(cfg.get("seed_pad", 48))
        bx0, by0 = max(0, x0 - pad), max(0, y0 - pad)
        bx1, by1 = min(w, x1 + pad), min(h, y1 + pad)
        big = arr[by0:by1, bx0:bx1, :3]
        g = ndimage.gaussian_filter(big.mean(2), 1.0)
        energy = np.hypot(ndimage.sobel(g, 0), ndimage.sobel(g, 1)) / 4.0  # sobel gain -> 0..255
        seed = ndimage.binary_closing(energy > thr, np.ones((7, 7)))
        # Flat areas the outlines fully enclose are decal too -- the inside of a letter, the
        # black field of a roundel. They matter most for the palette below: an unclaimed patch
        # interior would put its colour into the "cloth" list and license that colour anywhere.
        labf, nf = ndimage.label(~seed, structure=np.ones((3, 3)))
        outside = set(labf[0]) | set(labf[-1]) | set(labf[:, 0]) | set(labf[:, -1])
        seed |= np.isin(labf, [i for i in range(1, nf + 1) if i not in outside])
        # A decal's flat INTERIOR carries no edge energy either -- a collar is a ring, and the
        # neck hole inside it is as flat as cloth -- so the outline alone is not enough. The
        # second half of the seed is a COLOUR test against the frame: whatever cloth this window
        # sits on must also show up in the padded band AROUND it, because cloth does not stop at
        # a box we measured on a different team. Anything whose colour is absent from that band
        # is decal, however flat it is. On the shoulder the band carries the yoke's black, so the
        # yoke stays an anchor and survives; around the neck the band is body colour only, so the
        # collar AND the hole inside it are both cut. That is the collar/yoke distinction.
        frame = np.ones(seed.shape, bool)
        frame[y0 - by0:y1 - by0, x0 - bx0:x1 - bx0] = False
        pal = big[frame & ~seed].reshape(-1, 3)
        if len(pal):
            q, cnt = np.unique((pal // 16).astype(np.int16), axis=0, return_counts=True)
            q = (q[cnt >= max(1, int(0.005 * len(pal)))] * 16 + 8).astype(np.float32)
            if len(q):
                d = np.abs(big[None, :, :, :] - q[:, None, None, :]).max(3).min(0)
                seed |= d > thr
        seed = ndimage.binary_dilation(seed, np.ones((9, 9)))              # past the fringe
        seed = seed[y0 - by0:y1 - by0, x0 - bx0:x1 - bx0]
        # Stage 2 -- WHAT THE CLOTH WOULD BE. Reconstruct the background by ramping in from the
        # nearest pixel the seed did NOT claim, then flag everything that differs from it. This
        # is the original colour test, but with an anchor set that can no longer be the decal
        # itself. That is the whole fix: on a shoulder the anchors become black yoke, so bg is
        # black and only the white roundel is flagged (the yoke survives); on a crest they
        # become body cloth, so bg is the body colour and the WHOLE crest is flagged, including
        # the low-contrast parts the edge map dropped.
        est = window_background(arr, (x0, y0, x1, y1), cfg.get("margin", 10), seed)
        hot = np.abs(sub[:, :, :3] - est).max(axis=2) > thr
        hot = ndimage.binary_fill_holes(ndimage.binary_closing(hot, np.ones((7, 7))))
        lab, n = ndimage.label(hot, structure=np.ones((3, 3)))
        if not n:
            continue
        # Anything touching the window border is cloth the window clipped, not the decal --
        # dropping it is what lets a loose window stay safe.
        edge = set(lab[0]) | set(lab[-1]) | set(lab[:, 0]) | set(lab[:, -1])
        sizes = ndimage.sum_labels(hot, lab, range(1, n + 1))
        ids = [i for i in range(1, n + 1)
               if i not in edge and sizes[i - 1] >= cfg.get("min_area", 60)]
        if not ids:
            continue
        # A crest window also frames whatever else the jersey wears up there -- collar LACES are
        # the common one (Toronto home). They survive both filters above: they don't touch the
        # window border and they're well over min_area. So keep the DOMINANT blob and only the
        # components that cling to it. Clinging is measured as PIXEL distance, not bounding-box
        # overlap: a curved seam sweeping past the decal has a box that overlaps the decal's
        # while its pixels stay 30px clear, and on the bbox test the whole arc came along -- that
        # is what wiped the ends of Anaheim's yoke. Set a region's "keep": "all" in the profile
        # if its decal really is several scattered pieces.
        if reg.get("keep", "dominant") == "dominant" and len(ids) > 1:
            gap = cfg.get("merge_gap", 24)
            ids.sort(key=lambda i: sizes[i - 1], reverse=True)
            # ...and a candidate only counts as a PIECE of the decal if it has some bulk. The
            # yoke's curved edge passes 10px from Anaheim's shoulder roundel, well inside any
            # useful merge gap, so distance alone still swallowed it. A seam is a hairline; a
            # decal is not, so anything that vanishes under a small erosion is a seam. The
            # dominant blob itself is exempt -- it is the decal by construction, thin or not.
            thin = int(cfg.get("min_thickness", 9))
            rest = [i for i in ids[1:]
                    if ndimage.binary_erosion(lab == i, np.ones((thin, thin))).any()]
            acc = [ids[0]]
            near = ndimage.binary_dilation(lab == ids[0], np.ones((2 * gap + 1, 2 * gap + 1)))
            grew = True
            while grew and rest:
                grew = False
                for i in list(rest):
                    if (near & (lab == i)).any():
                        acc.append(i)
                        rest.remove(i)
                        near |= ndimage.binary_dilation(lab == i,
                                                        np.ones((2 * gap + 1, 2 * gap + 1)))
                        grew = True
            ids = acc
        m = np.isin(lab, ids)
        keep[y0:y1, x0:x1] |= m

        ys, xs = np.where(m)
        bx, by = int(xs.min()), int(ys.min())
        bw, bh = int(xs.max() - bx) + 1, int(ys.max() - by) + 1
        cut = sub[by:by + bh, bx:bx + bw].copy()
        cut[:, :, 3] = m[by:by + bh, bx:bx + bw] * 255.0
        logos.append(Logo(reg["name"], _to_img(cut), (x0 + bx, y0 + by, bw, bh),
                          reg.get("stamp")))

        # Grow the fill past the mask so no antialiased fringe of the old logo survives, and
        # reconstruct from the cloth that touches THAT -- so the anchors are outside the fringe.
        fill = ndimage.binary_dilation(m, np.ones((5, 5)))
        bg = window_background(arr, (x0, y0, x1, y1), cfg.get("margin", 10), fill)
        out[y0:y1, x0:x1, :3] = np.where(fill[:, :, None], bg, sub[:, :, :3])
    return _to_img(out), logos, keep


_ARM_PARAM: dict | None = None


def arm_param() -> dict:
    """{island: (ts, cov)} -- the sleeve cylinder maps baked by tools/build_arm_param.py."""
    global _ARM_PARAM
    if _ARM_PARAM is None:
        from . import resources
        z = np.load(resources.data_path("arm_param.npz"), allow_pickle=False)
        _ARM_PARAM = {i: (z[f"i{i}_ts"], z[f"i{i}_cov"]) for i in (0, 1) if f"i{i}_ts" in z}
    return _ARM_PARAM


def _arm_fit(out: np.ndarray, src: np.ndarray, mask: np.ndarray, spec: dict) -> None:
    """Paint the sleeves by sampling the source panel at (t along the arm, s around it).

    `src_panels` gives one [x_at_t0, y_at_s0, x_at_t1, y_at_s1] per island, in the SOURCE image's
    pixel space. x_at_t0 is the cuff end of the NHL 23 sleeve panel and x_at_t1 the shoulder end,
    so the right sleeve -- whose panel is mirrored in the NHL 23 layout -- is expressed simply by
    giving it x_at_t0 > x_at_t1 rather than by a separate flip flag. `s` is the angle around the
    arm and wraps, so its sampling wraps too: the branch cut was already seated on the island's own
    UV seam when the map was baked.
    """
    sh, sw = src.shape[:2]
    panels = spec["src_panels"]
    done = np.zeros_like(mask)
    for isl, (ts, cov) in sorted(arm_param().items()):
        m = mask & cov
        if not m.any():
            continue
        done |= m
        x0, y0, x1, y1 = [float(v) for v in panels[min(isl, len(panels) - 1)]]
        t, s = ts[m, 0], ts[m, 1]
        # x0/x1 are the panel's own CUFF and ARMHOLE seams, so t maps the whole NHL 23 sleeve onto
        # the whole 2K10 sleeve and there is nothing outside the panel left to sample. An earlier
        # window overshot the armhole by 88 px and needed a clamp to stop it dragging torso fabric
        # (Anaheim's black neck yoke) down the arm; see the profile's `__superseded` note.
        sx = np.clip(x0 + t * (x1 - x0), 0, sw - 1.001)
        sy = np.mod(y0 + s * (y1 - y0), sh)
        ix, iy = sx.astype(np.int32), sy.astype(np.int32)
        fx, fy = (sx - ix)[:, None], (sy - iy)[:, None]
        ix1, iy1 = np.minimum(ix + 1, sw - 1), (iy + 1) % sh
        out[m] = ((src[iy, ix] * (1 - fx) + src[iy, ix1] * fx) * (1 - fy)
                  + (src[iy1, ix] * (1 - fx) + src[iy1, ix1] * fx) * fy)

    # The (t,s) rasterisation is a hair tighter than the region mask -- a triangle that only grazes
    # a texel leaves it uncovered -- so a raw arm fit is peppered with unpainted specks along every
    # island edge. Carry the nearest painted arm texel into them; the seam sliver the mesh never
    # samples is exactly what this is for.
    gap = mask & ~done
    if gap.any() and done.any():
        from scipy import ndimage
        _, (iy, ix) = ndimage.distance_transform_edt(~done, return_indices=True)
        out[gap] = out[iy[gap], ix[gap]]


def build_base(sources: dict[str, Image.Image], profile: dict, regions: dict[str, np.ndarray],
               logos_out: list | None = None, strip: bool = True) -> Image.Image:
    """Assemble the 1024x1024 uniform_base from the NHL 23 base / pant / sock files."""
    body = body_transform(sources["base"], profile)
    if strip:
        body, lifted, _ = lift_logos(body, profile)
        if logos_out is not None:
            logos_out.extend(lifted)

    dw, dh = profile["body"]["dst_size"]
    out = np.zeros((dh, dw, 4), np.float32)
    out[:, :, 3] = 255.0
    body_arr = _to_arr(body)

    from scipy import ndimage
    for name, spec in profile["garments"].items():
        mask = regions.get(name)
        if mask is None or name.startswith("_") or not mask.any():
            continue
        fit = spec["fit"]
        if fit == "body":
            out[mask] = body_arr[mask]
            continue
        img = sources.get(spec["src"])
        if img is None:
            continue
        if spec["src"] == "base":
            img = body                                 # crop the ALREADY-warped, de-logoed base

        # `arm` samples through the sleeve's CYLINDER parameterisation instead of resizing a rect
        # onto the island's bounding box. The 2K10 sleeve unwrap tapers toward the cuff and runs
        # diagonally, so a rect fit both widens the stripe ring and rides it up the arm -- see
        # tools/build_arm_param.py for the mesh derivation and the ring test that validates it.
        if fit == "arm":
            _arm_fit(out, _to_arr(img.convert("RGBA")), mask, spec)
            continue

        # `per_island` maps the source onto EACH connected island separately instead of onto the
        # region's bounding box. The collar needs it: it is several narrow bars, and stretching
        # one stripe band across all of them gives each bar a slice of the pattern, when what
        # the game shows is the whole pattern repeated per bar.
        if spec.get("per_island"):
            lab, n = ndimage.label(mask)
            boxes = sorted((sl[1].start, sl[0].start,
                            sl[1].stop - sl[1].start, sl[0].stop - sl[0].start)
                           for sl in ndimage.find_objects(lab) if sl is not None)
            boxes = [b for b in boxes if b[2] * b[3] >= MIN_ISLAND]
        else:
            ys, xs = np.where(mask)
            boxes = [(int(xs.min()), int(ys.min()),
                      int(xs.max() - xs.min()) + 1, int(ys.max() - ys.min()) + 1)]

        # `src_rects` gives each island its OWN crop, in island order (left to right, then top to
        # bottom). The two sleeves need it: NHL 23 draws them as two separate panels, and reusing
        # one panel mirrored would lose a design that is not bilaterally symmetric. `src_rect`
        # stays as the single-crop form, repeated for every island, which is what the collar bars
        # and the two pant legs want.
        rects = spec.get("src_rects") or [spec["src_rect"]] * len(boxes)
        rot = int(spec.get("rotate", 0))
        pieces = []
        for r in rects[:len(boxes)] + [rects[-1]] * max(0, len(boxes) - len(rects)):
            p = img.convert("RGBA").crop(tuple(int(v) for v in r))
            pieces.append(p.rotate(rot, expand=True) if rot else p)

        src = np.zeros_like(out)
        for i, (bx, by, bw, bh) in enumerate(boxes):
            p = pieces[i]
            if i and spec.get("mirror_odd"):
                p = p.transpose(Image.FLIP_LEFT_RIGHT)
            src[by:by + bh, bx:bx + bw] = _to_arr(p.resize((bw, bh), Image.LANCZOS))
        out[mask] = src[mask]

    # Unassigned pixels (seams, and the sliver the mesh never samples) get the nearest garment
    # colour rather than black, so a filtering tap that strays off an island stays plausible.
    covered = np.zeros((dh, dw), bool)
    for m in regions.values():
        covered |= m
    if not covered.all():
        from scipy import ndimage
        _, (iy, ix) = ndimage.distance_transform_edt(~covered, return_indices=True)
        out[~covered] = out[iy[~covered], ix[~covered]]
    out[:, :, 3] = 255.0                               # base is 565 in-game: no alpha channel
    return _to_img(out)


# ── 2. glyphs ─────────────────────────────────────────────────────────────────────────────────

def _kmeans(x: np.ndarray, k: int, iters: int = 24) -> np.ndarray:
    """Tiny k-means over RGB. k is 2 or 3 here, so this beats pulling in scikit-learn."""
    lo, hi = x.min(0), x.max(0)
    cent = np.linspace(0, 1, k)[:, None] * (hi - lo) + lo
    lbl = np.zeros(len(x), int)
    for _ in range(iters):
        d = ((x[:, None, :] - cent[None, :, :]) ** 2).sum(2)
        new = d.argmin(1)
        if (new == lbl).all():
            break
        lbl = new
        for i in range(k):
            if (lbl == i).any():
                cent[i] = x[lbl == i].mean(0)
    return lbl


def detect_layers(cells, max_layers: int = 3, min_share: float = 0.03,
                  min_sep: float = 40.0) -> int:
    """How many colour layers a glyph SET actually has. -> 1, 2 or 3.

    A jersey font, a helmet font and the name letters are three separate designs and routinely
    carry different numbers of outlines -- a fill-only helmet digit next to a double-outlined
    back number is normal -- so this is measured per set rather than asked of the user.

    Counted over the whole set at once, not per glyph: a thin '1' may show only two of its three
    colours convincingly, and the answer has to be the same for every glyph anyway or the set
    keys inconsistently. k is accepted only while the clusters stay far apart in RGB and each
    still holds a real share of the ink, which is what stops fabric noise inside a flat fill
    from reading as an extra outline.
    """
    px = []
    for c in cells:
        arr = _to_arr(c)
        solid = arr[:, :, 3] > 128
        if solid.sum() >= 8:
            px.append(arr[:, :, :3][solid])
    if not px:
        return 2
    x = np.concatenate(px)
    if len(x) > 20000:                                 # k-means here is O(n*k); sampling is plenty
        x = x[:: len(x) // 20000 + 1]
    best = 1
    for k in range(2, max(2, int(max_layers)) + 1):
        if len(np.unique(x.round(), axis=0)) < k:
            break
        lbl = _kmeans(x, k)
        cent, share = [], []
        for i in range(k):
            m = lbl == i
            if not m.any():
                break
            cent.append(x[m].mean(0))
            share.append(m.mean())
        if len(cent) < k or min(share) < min_share:
            break
        sep = min(float(np.linalg.norm(cent[i] - cent[j]))
                  for i in range(k) for j in range(i + 1, k))
        if sep < min_sep:
            break
        if k == 3 and _is_blend(cent, share):
            break
        best = k
    return best


def _is_blend(cent, share, max_perp: float = 25.0, max_share: float = 0.15) -> bool:
    """True if one of three clusters is just the ANTIALIAS ramp between the other two.

    A glyph drawn white-on-black has a grey halo along every stroke, and it is easily 5% of the
    ink -- enough to pass the share test and invent an outline that isn't there. A real second
    outline is its own hue and sits well off the line joining the other two; a ramp sits ON it.
    """
    for i in range(3):
        a, b = cent[(i + 1) % 3], cent[(i + 2) % 3]
        d = b - a
        n = float(np.linalg.norm(d))
        if n < 1e-6:
            continue
        t = float(np.dot(cent[i] - a, d)) / (n * n)
        perp = float(np.linalg.norm((cent[i] - a) - t * d))
        if 0.15 < t < 0.85 and perp < max_perp and share[i] < max_share:
            return True
    return False


def key_recolor(cell: Image.Image, colors: list, layers: int = 2) -> Image.Image:
    """Literal glyph art -> 2K10's colour-keyed form.

    2K10 does not store glyph colours; it stores MASKS -- blue is the fill, red and green are
    the outlines -- and the shader substitutes the team palette. NHL 23 stores the finished
    colours instead, so the layers have to be recovered before they can be re-keyed.

    Layers are separated by colour (k-means) and then ordered by HOW MUCH OF EACH LAYER TOUCHES
    THE GLYPH'S OUTER EDGE. An outline is by definition mostly rim; a fill barely touches the
    edge at all. Ordering by mean depth instead was tried and gets narrow glyphs wrong -- an
    'A' has strokes so thin that its fill is no deeper than its outline, which came out inverted
    (red body, blue edge) while the rounder 'C' beside it came out right. Brightness is no good
    either: white-on-black and black-on-white would order oppositely.
    """
    from scipy import ndimage

    arr = _to_arr(cell)
    a = arr[:, :, 3]
    solid = a > 96
    if solid.sum() < 8:
        return cell
    dist = ndimage.distance_transform_edt(np.pad(solid, 1))[1:-1, 1:-1][solid]
    px = arr[:, :, :3][solid]
    k = max(1, min(int(layers), 3, len(np.unique(px.round(), axis=0))))
    lbl = _kmeans(px, k) if k > 1 else np.zeros(len(px), int)
    rank = []
    for i in range(k):
        m = lbl == i
        rank.append(((dist[m] <= 1.5).mean(), -dist[m].mean()) if m.any() else (2.0, 0.0))
    order = sorted(range(k), key=lambda i: rank[i])     # innermost first -> fill, edge1, edge2

    out = arr.copy()
    rgb = np.zeros_like(px)
    for rank, cluster in enumerate(order):
        rgb[lbl == cluster] = colors[min(rank, len(colors) - 1)]
    out[:, :, :3][solid] = rgb
    # Antialiased rim pixels have no cluster of their own; give them the outermost colour so a
    # glyph keeps a clean edge instead of a halo of whatever the fill happens to be.
    rim = (a > 12) & ~solid
    out[:, :, :3][rim] = colors[min(len(order) - 1, len(colors) - 1)]
    return _to_img(out)


def segment_font(font: Image.Image, profile: dict) -> tuple[dict[str, dict[str, Image.Image]], list]:
    """Cut the NHL 23 font atlas into labelled glyphs. -> ({band: {label: image}}, warnings)."""
    from scipy import ndimage

    cfg = profile["nhl23_font"]
    img = font.convert("RGBA")
    if img.size != tuple(cfg["size"]):
        img = img.resize(tuple(cfg["size"]), Image.LANCZOS)
    a = np.asarray(img)[:, :, 3]
    lab, n = ndimage.label(a > 16, structure=np.ones((3, 3)))

    comps = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        if sl is None or (lab[sl] == i).sum() < 40:
            continue
        ys, xs = sl
        comps.append((xs.start, ys.start, xs.stop - xs.start, ys.stop - ys.start))

    out: dict[str, dict[str, Image.Image]] = {}
    warn: list[str] = []
    for band in cfg["bands"]:
        y0, y1 = band["y"]
        bx0, bx1 = band["x"]
        got = sorted((c for c in comps
                      if y0 <= c[1] + c[3] / 2 < y1 and bx0 <= c[0] + c[2] / 2 < bx1),
                     key=lambda c: c[0])
        labels = band["labels"]
        if len(got) != len(labels):
            warn.append(f"{band['name']}: found {len(got)} glyphs, expected {len(labels)}")
        cells: dict[str, Image.Image] = {}
        for lb, c in zip(labels, got):
            cells[lb] = img.crop((c[0], c[1], c[0] + c[2], c[1] + c[3]))
        out[band["name"]] = cells

    # The diaeresis is two dots -- one glyph split into two components. Harmless (2K10 has no
    # diacritics to fill), but it would otherwise read as a count mismatch, so note it quietly.
    return out, [w for w in warn if not w.startswith("letters_2")] + \
        [w for w in warn if w.startswith("letters_2") and "expected 15" not in w]


# ── 3. the 2K10 sheets ────────────────────────────────────────────────────────────────────────

def _slot_target(stock: Image.Image | None, rect, pad: float = 0.06):
    """Where new art goes inside a slot: the STOCK ink box when there is one, else the cell.

    Using the stock team's own ink box is what keeps per-team metrics -- Boston's letters are
    wider than Chicago's, and the numbers sit at different heights -- without needing to find
    the game's glyph-UV table. Falls back to an inset of the cell for slots the stock sheet
    leaves empty (a jersey with no shoulder patch, say).
    """
    x, y, w, h = rect
    if stock is not None:
        box = ink_box(stock, (x, y, w, h))
        if box:
            return box
    m = pad * min(w, h)
    return (x + m, y + m, w - 2 * m, h - 2 * m)


def place_set(out: Image.Image, cells: dict, rects: dict, stock: Image.Image | None,
              colors: list, layers: int = 2, keyed: bool = True) -> None:
    """Draw a whole glyph SET (the digits, the alphabet) with one shared scale and baseline.

    Fitting each glyph into its own stock ink box independently looks wrong, because a '1' is
    narrower than a '3' and aspect-preserving fit then caps it by width and leaves it SHORTER
    than its neighbours. A font does not work that way: one scale for the set, every glyph
    sitting on a common baseline, and only the horizontal centre taken per slot.

    Both metrics are read from the stock sheet, so the converted set inherits the team's own
    number size and baseline. The SCALE is shared across the whole set, but the baseline is per
    ROW of cells -- the digits live on two rows (evens above odds), and one baseline for all ten
    would stack every digit on the lower row. Slots whose stock ink is far shorter than the rest
    -- the apostrophe and full stop -- are outliers to both metrics and keep their own box.
    """
    boxes = {k: _slot_target(stock, r) for k, r in rects.items() if k in cells}
    if not boxes:
        return
    med = lambda v: sorted(v)[len(v) // 2]
    med_dst = med([b[3] for b in boxes.values()])
    med_src = med([cells[k].height for k in boxes]) or 1
    # Scale each axis independently, from the set's median width and height. Preserving aspect
    # is WRONG here: 2K10's stamp cells are anisotropic (the shader maps a 128x256 cell onto a
    # roughly 57x70 patch of jersey), so its sheet font is drawn about half as wide as NHL 23's
    # for the same height. Matching both axes to the stock set reproduces the team's own
    # proportions, and a narrow glyph like '1' stays narrow because it is scaled, not refitted.
    sy = med_dst / med_src
    sx = med([b[2] for b in boxes.values()]) / (med([cells[k].width for k in boxes]) or 1)

    baselines: dict = {}
    for k, b in boxes.items():
        if b[3] >= 0.6 * med_dst:
            baselines.setdefault(rects[k][1], []).append(b[1] + b[3])
    baselines = {row: med(v) for row, v in baselines.items()}

    for k, box in boxes.items():
        cell = key_recolor(cells[k], colors, layers) if keyed else cells[k]
        baseline = baselines.get(rects[k][1]) if box[3] >= 0.6 * med_dst else None
        if baseline is None:
            fitted, xy = fit_into(cell, box)
        else:
            nw = max(1, round(cell.width * sx))
            nh = max(1, round(cell.height * sy))
            fitted = cell.resize((nw, nh), Image.LANCZOS)
            xy = (round(box[0] + box[2] / 2 - nw / 2), round(baseline - nh))
        paste_rgba(out, fitted, xy)


def build_letters(glyphs: dict, profile: dict, stock: Image.Image | None,
                  colors: list, layers: int = 2, scales: dict | None = None,
                  canvas: Image.Image | None = None) -> Image.Image:
    """The nameplate alphabet. With `canvas`, EDIT that sheet — see build_stamps for why."""
    cfg = profile["letters"]
    w, h = cfg["size"]
    out = (canvas.convert("RGBA").copy() if canvas is not None
           else Image.new("RGBA", (w, h), (0, 0, 0, 0)))
    cw, ch = cfg["cell"]
    src = dict(glyphs.get("letters_1", {}))
    src.update(glyphs.get("letters_2", {}))
    # 2K10 carries a lowercase 'c' (McDavid, O'Connor) but NHL 23's atlas is caps-only, so it is
    # drawn from the capital; the stock sheet's own 'c' box is much shorter than a cap, so
    # place_set treats it as an outlier and its x-height comes out right for free.
    cells = {k: src[k] for k in cfg["slots"] if k in src}
    cells.update({k: src[k.upper()] for k in cfg["slots"]
                  if k not in src and k.upper() in src})
    rects = {k: (slot * cw, 0, cw, ch) for k, slot in cfg["slots"].items()}
    if cells:
        place_set(out, cells, rects, stock, colors, layers, cfg.get("keyed", True))
    for k, r in rects.items():
        rescale_slot(out, r, scale_for(scales, k), anchor="bottom")
    return out


def discover_colors(images, limit: int = 12, sample: int = 320,
                    merge: int = 26, min_share: float = 0.004) -> list[tuple[str, str]]:
    """The colours the kit's art is actually made of, most-used first. -> [(hex, share), ...]

    For colour MATCHING, not analysis: the gloves, helmet and lettering are palette entries in
    Roster.ROS, not pixels, so matching them to the jersey meant opening the sheet in Photoshop
    and eyedropping it. These are the same values, already on hand.

    Quantised to a 8-level-per-channel grid and then merged at `merge` so a DXT-compressed field
    that decodes to two dozen near-identical reds offers ONE red, not twenty. Ordered by area,
    which is what makes the list useful: the body colour comes first, then the stripes, then the
    trim -- the same order someone would pick them by eye.
    """
    counts: dict[tuple, int] = {}
    for im in images:
        if im is None:
            continue
        a = np.asarray(im.convert("RGBA").resize((sample, sample), Image.NEAREST))
        px = a.reshape(-1, 4)
        px = px[px[:, 3] > 128][:, :3].astype(np.int32) // 8 * 8 + 4
        if not len(px):
            continue
        keys, cnt = np.unique(px, axis=0, return_counts=True)
        for k, n in zip(keys, cnt):
            t = (int(k[0]), int(k[1]), int(k[2]))
            counts[t] = counts.get(t, 0) + int(n)
    total = sum(counts.values())
    if not total:
        return []
    # Fold every near-neighbour INTO its representative rather than dropping it, so the share
    # reported is the share of that colour on the kit -- a red split across a dozen DXT variants
    # is one red at its true weight, not a 0% straggler.
    out: list[list] = []
    for col, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        for o in out:
            if max(abs(col[i] - o[0][i]) for i in range(3)) <= merge:
                o[1] += n
                break
        else:
            out.append([col, n])
    # Everything under min_share is antialiasing or JPEG-ish noise, not a colour anyone picks.
    # Re-sort AFTER folding -- a colour that arrives in many near-identical pieces can outweigh
    # one that arrives whole, and the list is only useful if it reads biggest-first.
    out = sorted((o for o in out if o[1] >= min_share * total), key=lambda o: -o[1])[:limit]
    return [("#%02X%02X%02X" % tuple(c), f"{100.0 * n / total:.0f}%") for c, n in out]


def art_dir() -> Path:
    return _data_dir() / "stamp_art"


def tint_art(cell: Image.Image, color) -> Image.Image:
    """Recolour patch art to a literal RGB, keeping its shading and its alpha.

    Sponsor and manufacturer marks are NOT colour-keyed: unlike the glyph sheets, nothing in the
    game substitutes a palette entry for these slots, so whatever colour is stored is exactly
    what renders. The picker in the tab therefore paints the art here and now. Luminance is kept
    as a multiplier so a mark with an embossed or shaded edge doesn't flatten into a silhouette;
    art that was already flat stays flat.
    """
    if isinstance(color, str):
        c = color.lstrip("#")
        color = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    arr = _to_arr(cell)
    rgb = arr[:, :, :3]
    lum = (rgb * np.array([0.299, 0.587, 0.114], np.float32)).sum(2, keepdims=True) / 255.0
    ink = arr[:, :, 3] > 8
    if ink.any():
        # Normalise against the mark's own brightest ink, so a black-on-transparent logo comes
        # out the picked colour rather than near-black.
        peak = float(np.percentile(lum[ink], 95))
        lum = np.clip(lum / peak, 0.0, 1.0) if peak > 0.02 else np.ones_like(lum)
    out = arr.copy()
    out[:, :, :3] = np.clip(lum * np.array(color, np.float32), 0, 255)
    return _to_img(out)


def resolve_art(name) -> Path | None:
    """Art file by bare name (bundled in data/stamp_art) or by absolute path."""
    if not name:
        return None
    p = Path(name)
    if not p.is_absolute():
        p = art_dir() / p.name
    return p if p.exists() else None


def build_stamps(glyphs: dict, logos: list, profile: dict, stock: Image.Image | None,
                 colors: list, layers: int = 2, enabled: dict | None = None,
                 rects: dict | None = None, art: dict | None = None,
                 canvas: Image.Image | None = None,
                 scales: dict | None = None) -> Image.Image:
    """Build the stamps sheet. With `canvas`, EDIT that sheet instead of composing a new one.

    The tab uses `canvas` for its generic-editor mode: with no NHL 23 art loaded there is nothing
    to compose from, and starting blank would silently drop every mark the kit already wears.
    Handed the kit's own sheet, the passes below only touch the slots you actually changed.
    """
    cfg = profile["stamps"]
    w, h = cfg["size"]
    out = (canvas.convert("RGBA").copy() if canvas is not None
           else Image.new("RGBA", (w, h), (0, 0, 0, 0)))
    enabled = enabled or {}
    rects = rects or {}
    slots = cfg["slots"]

    def on(name: str, default: bool = True) -> bool:
        return bool(enabled.get(name, default))

    def rect_of(name: str):
        return rects.get(name, slots[name]["rect"])

    # Slots 2K10 owns and NHL 23 has no equivalent for (league/cup/sponsor marks, the small
    # numbers): keep the stock art rather than blanking it, so a converted kit does not lose
    # patches the game still stamps.
    if stock is not None and canvas is None:
        for name in cfg.get("preserve", []):
            if not on(name) or name not in slots:
                continue
            x, y, sw, sh = slots[name]["rect"]
            out.paste(stock.crop((x, y, x + sw, y + sh)), (x, y))

    # Bundled art overwrites the stock mark for slots whose 2009 sponsor is simply wrong now
    # (Reebok, and a Cup patch in the old style). Drawn after the preserve pass so clearing a
    # slot's art in the tab falls straight back to whatever the kit shipped with.
    # The profile's bundled replacements (Fanatics for Reebok, a modern Cup patch) are part of
    # COMPOSING a kit from NHL 23 art. When editing an existing kit they are not: the sheet
    # already has marks, and swapping them because a different slot was touched is not an edit
    # anyone asked for. So in canvas mode only the caller's own art counts.
    for name, spec in ({**cfg.get("art", {}), **(art or {})} if canvas is None
                       else dict(art or {})).items():
        if name.startswith("_") or name not in slots or not on(name):
            continue
        src = resolve_art(spec.get("file")) if isinstance(spec, dict) else resolve_art(spec)
        if src is None:
            # No new art, just a colour: repaint the mark the slot ALREADY carries. This is the
            # common case in the tab -- the kit's sponsor patch is fine, it is the wrong colour.
            base = canvas if canvas is not None else stock
            if isinstance(spec, dict) and spec.get("tint") and base is not None:
                x, y, sw, sh = slots[name]["rect"]
                cell = base.convert("RGBA").crop((x, y, x + sw, y + sh))
                out.paste(tint_art(cell, spec["tint"]), (x, y))
            continue
        cell = load_image(src)
        if isinstance(spec, dict) and spec.get("tint"):
            cell = tint_art(cell, spec["tint"])
        x, y, sw, sh = rect_of(name)
        out.paste(Image.new("RGBA", (sw, sh), (0, 0, 0, 0)), (x, y))   # clear the stock mark
        fitted, xy = fit_into(cell, (x, y, sw, sh), pad=0.08)
        paste_rgba(out, fitted, xy)

    # Editing an existing sheet, "off" has to actively erase: there is no blank start to fall
    # back to, so a slot the user turned off would otherwise keep the kit's own mark.
    if canvas is not None:
        for name in slots:
            if not name.startswith("_") and not on(name):
                x, y, sw, sh = slots[name]["rect"]
                out.paste(Image.new("RGBA", (sw, sh), (0, 0, 0, 0)), (x, y))

    by_stamp = {lg.stamp: lg for lg in logos if lg.stamp}
    for name, logo in by_stamp.items():
        if name not in slots or not on(name):
            continue
        fitted, xy = fit_into(logo.image, _slot_target(stock, rect_of(name)))
        paste_rgba(out, fitted, xy)

    # The thigh patch is the TEAM MARK, and nothing supplies it. NHL 23's atlas is cut for a
    # crest and two shoulder patches -- there is no thigh region to lift, so no Logo carries
    # stamp "pants_patch" -- and the shipped 2K10 sheets leave the cell blank too (Calgary Home
    # measures 0 ink in all 65,536 texels). So `pants_mark`, a real quad the game stamps on the
    # front-left thigh, had no source at all and drew nothing: the front of the pants came out
    # bare while the NHL shield sat correctly on the rear hip. Falling back to the crest is what
    # a real pair of pants wears. Composition only -- editing an existing kit must not grow a
    # patch it never had -- and skipped if anything already filled the cell.
    if canvas is None and "pants_patch" in slots and on("pants_patch") \
            and "pants_patch" not in by_stamp and by_stamp.get("crest") is not None:
        x, y, sw, sh = rect_of("pants_patch")
        if not out.crop((x, y, x + sw, y + sh)).getchannel("A").getbbox():
            fitted, xy = fit_into(by_stamp["crest"].image, (x, y, sw, sh), pad=0.08)
            paste_rgba(out, fitted, xy)

    digits = glyphs.get("digits_hi", {}) | glyphs.get("digits_lo", {})
    dcells = {f"digit_{d}": c for d, c in digits.items()
              if f"digit_{d}" in slots and on(f"digit_{d}")}
    place_set(out, dcells, {k: rect_of(k) for k in dcells}, stock, colors, layers)

    caps = {n: (glyphs.get(n) or {}).get(n[-1].upper()) for n in ("capt_a", "capt_c")}
    caps = {n: c for n, c in caps.items() if c is not None and on(n)}
    place_set(out, caps, {k: rect_of(k) for k in caps}, stock, colors, layers)

    # Size, last: it resamples whatever ended up in the cell, whoever put it there.
    for name in slots:
        if name.startswith("_") or not on(name):
            continue
        s = scale_for(scales, name)
        if s != 1.0:
            anchor = "bottom" if name.startswith(BASELINE_PREFIXES) else "center"
            rescale_slot(out, rect_of(name), s, anchor)
    return out


def build_helmet(glyphs: dict, logos: list, profile: dict, stock: Image.Image | None,
                 colors: list, layers: int = 2, keep_logo: bool = True,
                 from_jersey: bool = True, logo_art: dict | None = None,
                 canvas: Image.Image | None = None,
                 scales: dict | None = None) -> Image.Image:
    """Helmet sheet: the team logo plus the number set.

    `from_jersey` takes the digits from the JERSEY number row rather than NHL 23's own smaller
    helmet row, so the helmet number is the same design as the back number (scaled to the
    helmet cell) -- which is what the shipped 2K10 sheets do.

    The logo is left as the stock art by default: real helmets carry team-specific decals and
    sponsor marks that no jersey texture contains, so guessing here would be worse than keeping
    what the kit already had. `logo_art` ({"file":…, "tint":…}) uploads one instead, and outranks
    both the stock decal and the lifted crest.
    """
    cfg = profile["helmet"]
    w, h = cfg["size"]
    out = (canvas.convert("RGBA").copy() if canvas is not None
           else Image.new("RGBA", (w, h), (0, 0, 0, 0)))
    lx, ly, lw, lh = cfg["logo"]
    src = resolve_art((logo_art or {}).get("file"))
    if src is not None:
        cell = load_image(src)
        if logo_art.get("tint"):
            cell = tint_art(cell, logo_art["tint"])
        out.paste(Image.new("RGBA", (lw, lh), (0, 0, 0, 0)), (lx, ly))
        fitted, xy = fit_into(cell, (lx, ly, lw, lh), pad=0.06)
        paste_rgba(out, fitted, xy)
    elif (logo_art or {}).get("tint") and (canvas if canvas is not None else stock) is not None:
        base = canvas if canvas is not None else stock          # recolour the decal already there
        out.paste(tint_art(base.convert("RGBA").crop((lx, ly, lx + lw, ly + lh)),
                           logo_art["tint"]), (lx, ly))
    elif keep_logo and stock is not None:
        out.paste(stock.crop((lx, ly, lx + lw, ly + lh)), (lx, ly))
    elif not keep_logo:
        crest = next((lg for lg in logos if lg.stamp == "crest"), None)
        if crest is not None:
            fitted, xy = fit_into(crest.image, _slot_target(stock, (lx, ly, lw, lh)))
            paste_rgba(out, fitted, xy)

    rescale_slot(out, (lx, ly, lw, lh), float((scales or {}).get("logo", 1.0)))

    digits = (glyphs.get("digits_hi", {}) | glyphs.get("digits_lo", {})) if from_jersey \
        else glyphs.get("helmet", {})
    ox, oy = cfg["digit_origin"]
    cw, ch = cfg["digit_cell"]
    # Editing a kit with no new font redraws nothing, but the cells still get sized: the sheet
    # already carries the team's own digits, and that is exactly what a size change is for.
    rects = {d: (ox + (int(d) // 2) * cw, oy + (int(d) % 2) * ch, cw, ch)
             for d in (digits or {str(d): None for d in range(10)})}
    if digits:
        place_set(out, digits, rects, stock, colors, layers, cfg.get("keyed", True))
    for d, r in rects.items():
        rescale_slot(out, r, scale_for(scales, d), anchor="bottom")
    return out


# ── the whole job ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Options:
    # None = detect the layer count per glyph set (see detect_layers). An int forces it on every
    # set, which is only useful when a source's colours are too close for detection to see them.
    layers: int | None = None
    strip_logos: bool = True
    helmet_from_jersey: bool = True
    helmet_keep_logo: bool = True
    enabled: dict = field(default_factory=dict)        # stamp slot -> bool
    rects: dict = field(default_factory=dict)          # stamp slot -> [x, y, w, h] override
    art: dict = field(default_factory=dict)            # stamp slot -> {"file":…, "tint":"#rrggbb"}
    helmet_logo: dict = field(default_factory=dict)    # {"file":…, "tint":…} uploaded helmet decal
    colors: list = field(default_factory=list)         # key colours, innermost first
    # sheet -> {slot name (or "*" for the whole sheet) -> size multiplier}. See rescale_slot: this
    # is how a crest or a number is made bigger, since where it lands on the body is fixed.
    scales: dict = field(default_factory=dict)


@dataclass
class Result:
    sheets: dict = field(default_factory=dict)         # 'base'/'stamps'/'letters'/'helmet' -> Image
    logos: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # The segmented NHL 23 glyphs, kept so a caller that only changed a stamp slot can re-run
    # build_stamps() on its own instead of paying for the whole conversion again (the tab does
    # this on every drag of a slot box).
    glyphs: dict = field(default_factory=dict)
    layers: dict = field(default_factory=dict)         # 'letters'/'helmet'/'stamps' -> layers used


def convert(sources: dict[str, Image.Image], stock: dict[str, Image.Image] | None = None,
            profile: dict | None = None, opts: Options | None = None,
            regions: dict | None = None) -> Result:
    """NHL 23 textures -> the four 2K10 sheets.

    `sources` needs at least 'base'; 'pant'/'sock' fill their garment islands when present and
    'font' drives the glyph sheets. `stock` is the kit being replaced -- optional, but without
    it every slot falls back to a generic cell inset instead of the team's real metrics, so
    pass it whenever the kit has been extracted.
    """
    profile = profile or load_profile()
    regions = load_regions() if regions is None else regions
    opts = opts or Options()
    stock = stock or {}
    kc = profile["key_colors"]
    colors = opts.colors or [kc["fill"], kc["edge1"], kc["edge2"]]
    colors = [np.array(c, np.float32) for c in colors]

    res = Result()
    if not regions:
        res.warnings.append(f"{REGION_NAME} missing — run tools/build_uv_regions.py; "
                            f"the base cannot be repacked without it")

    if "base" in sources:
        logos: list[Logo] = []
        res.sheets["base"] = build_base(sources, profile, regions, logos, strip=opts.strip_logos)
        res.logos = logos
    else:
        res.warnings.append("no NHL 23 base supplied — base and crest were not built")

    glyphs: dict = {}
    if "font" in sources:
        glyphs, warn = segment_font(sources["font"], profile)
        res.warnings.extend(warn)
    else:
        res.warnings.append("no NHL 23 font supplied — letters/numbers were not built")

    res.glyphs = glyphs
    # One layer count per SET, not one for the job: the three sheets are three different fonts.
    def nlayers(*bands) -> int:
        if opts.layers:
            return int(opts.layers)
        cells = [c for b in bands for c in (glyphs.get(b) or {}).values()]
        return detect_layers(cells) if cells else 2

    if glyphs:
        res.layers = {"letters": nlayers("letters_1", "letters_2"),
                      "helmet": nlayers(*(("digits_hi", "digits_lo") if opts.helmet_from_jersey
                                          else ("helmet",))),
                      "stamps": nlayers("digits_hi", "digits_lo")}
        res.sheets["letters"] = build_letters(glyphs, profile, stock.get("letters"),
                                              colors, res.layers["letters"],
                                              opts.scales.get("letters"))
        res.sheets["helmet"] = build_helmet(glyphs, res.logos, profile, stock.get("helmet"),
                                            colors, res.layers["helmet"], opts.helmet_keep_logo,
                                            opts.helmet_from_jersey, opts.helmet_logo,
                                            scales=opts.scales.get("helmet"))
    if glyphs or res.logos:
        res.sheets["stamps"] = build_stamps(glyphs, res.logos, profile, stock.get("stamps"),
                                            colors, res.layers.get("stamps", 2), opts.enabled,
                                            opts.rects, opts.art,
                                            scales=opts.scales.get("stamps"))
    return res


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────

def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Convert an NHL 23 jersey set to NHL 2K10 sheets.")
    ap.add_argument("--src", required=True, help="folder holding the NHL 23 base/pant/sock/font")
    ap.add_argument("--stock", help="extracted 2K10 kit folder (base.dds, stamps.dds, …) to use "
                                    "as the layout template")
    ap.add_argument("--out", required=True, help="folder to write the converted sheets into")
    ap.add_argument("--layers", type=int, default=0,
                    help="force colour-key layers per glyph (2 or 3); 0 = detect per set")
    ap.add_argument("--no-strip", action="store_true", help="leave logos on the base")
    a = ap.parse_args(argv)

    src_paths = find_sources(a.src)
    if not src_paths:
        print(f"no NHL 23 textures found in {a.src}")
        return 1
    print("sources:")
    for k, p in sorted(src_paths.items()):
        print(f"  {k:5} {p.name}")
    sources = {k: load_image(p) for k, p in src_paths.items()}

    stock: dict[str, Image.Image] = {}
    if a.stock:
        files = [f for f in sorted(Path(a.stock).iterdir())
                 if f.is_file() and f.suffix.lower() in (".dds", ".png", ".tga")]
        for name in ("stamps", "letters", "helmet", "base"):
            hit = next((f for f in files if name in f.name.lower()), None)
            if hit is not None:
                stock[name] = load_image(hit)
        print(f"stock template: {sorted(stock)}")

    res = convert(sources, stock,
                  opts=Options(layers=a.layers or None, strip_logos=not a.no_strip))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if res.layers:
        print("key layers: " + "  ".join(f"{k}={v}" for k, v in sorted(res.layers.items())))
    for name, img in res.sheets.items():
        img.save(out / f"{name}.png")
        print(f"  wrote {name}.png  {img.width}x{img.height}")
    for lg in res.logos:
        lg.image.save(out / f"logo_{lg.name}.png")
        print(f"  lifted {lg.name:15} {lg.box}  -> stamp {lg.stamp}")
    for w in res.warnings:
        print(f"  WARNING: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
