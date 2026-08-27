"""ice_convert.py — thefaceoff.net reference sheet -> the two 2K10 ice textures.

The game stores each team's ice as two 1024x4096 textures, ``ice_<code>_playoffs.iff``
(which is ALSO the regular-season ice) and ``ice_<code>_finals.iff``. Both are the same
league-wide sheet except for five team-specific patches:

  * the centre-ice logo, stored as its LEFT and RIGHT halves, each rotated 90° clockwise
    and stacked as two 1024x~490 blocks at the top of the texture,
  * the two neutral-zone sponsor spots (the red-line side), side by side under them, and
  * the centre red line itself, a 1024x66 band rotated the same way as the logo halves.

A thefaceoff.net "current ice" sheet (5000x2236, one full rink) carries all five regions
at known RINK positions — but the art itself varies per team (Dallas's arena-name arc is a
bigger circle than Vancouver's; sponsor marks sit and size differently), so fixed crop
rectangles clip: the pieces are DETECTED instead.

  * Ink is anything DARKER than the sheet's ice white (darkness-only metric, so bright
    highlights always drop out — the author's magic-wand-the-white workflow).
  * The centre-ice logo is every ink component NOT in the sponsor corner rows (see
    AD_MIN_DY). The two halves are cropped symmetric about the red line, uniformly scaled
    (never above the guide's true rink scale, shrinking only when the art would not fit),
    and anchored at their line-side edge so the reassembled circle stays centred on ice.
  * Each sponsor spot is the ad ink in that LEFT corner of the neutral zone (ads sit in
    the zone's diagonal corners — far from centre in BOTH axes; logo parts never are),
    minus zone-border bleed and the neutral-zone faceoff dots. The art is cropped to its ink bbox
    and scaled so the ink spans 68% of the destination box width (measured off the
    author's finished sheet), centred. An empty spot stays clean ice.
  * The template's ice is one flat colour, so each destination box is flooded with it
    first — erasing the stock NHL/NHLPA marks and the blue centre-circle arc.
  * The centre-line band is pasted opaque, white-balanced to the template ice, with an
    8px alpha ramp feathering its top and bottom edges.

All rink-position constants below are in 5000x2236 reference pixels and are scaled to the
actual sheet size, so a re-export at another resolution still works.

Pure image work, no UI, no game-file access: the tab (ice_convert_gui) owns those.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import resources

REF_SIZE = (5000, 2236)           # the canonical sheet size the constants are measured in
RINK_CENTER = (2499.5, 1118.0)    # centre-ice dot
SEAM_L, SEAM_R = 2484, 2518       # red-line band edges, each trimmed 4px so no line
                                  # sliver rides along a logo half's cut edge
LINE_SRC = (2487, 1144, 2512, 2144)          # the red line (blue box in the guide)
ZONE = (1900, 250, 3100, 2010)               # the whole neutral zone, board to board
FACEOFF_DOTS = ((2015, 585), (2015, 1650), (2984, 585), (2984, 1650))
# Sponsor marks sit ABOVE/BELOW the centre circle, in the neutral-zone corner rows.
# Logo art — arena-name arcs included — never leaves the circle, whose radius tops out
# ~430 rink px across real sheets; ad ink starts at |dy| >= ~520. (An |dx| condition
# would clip an ad's letters nearest the centre line — SYMETRA's trailing A — so the
# vertical distance alone decides.)
AD_MIN_DY = 480
# The guide's fixed logo crop (Vancouver-measured) defines the TRUE rink->texture scale:
# its 876px-tall half maps to the 1024px box side. Detected art never renders larger.
GUIDE_LOGO_H = 876
GUIDE_LOGO_HALF_W = 416

TEMPLATE_SIZE = (1024, 4096)
DST = {                           # template pixels
    "logo_left":  (0, 0, 1024, 491),
    "logo_right": (0, 528, 1024, 1029),
    "sponsor_nw": (0, 1029, 485, 1599),
    "sponsor_sw": (485, 1029, 1024, 1599),
    "line":       (0, 2079, 1024, 2145),
}

VARIANTS = ("playoffs", "finals")

AD_WIDTH_FRAC = 0.68      # sponsor ink spans this much of its box width (measured off
AD_MAX_HFRAC = 0.80       # the author's finished sheet), capped at this much of its height
INK_T0, INK_T1 = 40, 100  # darkness soft-ramp: below T0 fully ice, above T1 fully ink
LINE_FEATHER = 8          # px alpha ramp on the line band's top/bottom edges


def template_path(variant: str) -> Path:
    return resources.data_path(f"ice_template_{variant}.png")


def _darkness(arr: np.ndarray, bg: np.ndarray | None = None) -> np.ndarray:
    """Ink metric: how much DARKER than the ice background each pixel is. Bright-white
    highlights score zero, so they always fall out — the magic-wand-the-white behavior.
    Pass the SHEET's ice colour as `bg` when the crop is mostly art (a solid-colour ad
    block skews its own percentile — a yellow-on-red mark would read as no ink)."""
    if bg is None:
        bg = np.percentile(arr.reshape(-1, 3), 85, axis=0)
    return np.maximum(bg - arr, 0).sum(2)


def _soft_alpha(arr: np.ndarray, bg: np.ndarray | None = None) -> np.ndarray:
    a = np.clip((_darkness(arr, bg) - INK_T0) / (INK_T1 - INK_T0), 0, 1)
    return ndimage.gaussian_filter(a, 1.0)


def _drop_border_ink(ink: np.ndarray) -> np.ndarray:
    """Kill ink components touching the crop border (guide bleed, faceoff-circle arcs)."""
    lab, _ = ndimage.label(ink)
    touch = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])
    touch.discard(0)
    return ink & ~np.isin(lab, list(touch))


def _detect(ref: Image.Image) -> dict:
    """Find the logo halves' and sponsor spots' crop boxes on this sheet (actual pixels)."""
    rw, rh = ref.size
    kx, ky = rw / REF_SIZE[0], rh / REF_SIZE[1]
    k = (kx + ky) / 2

    def sx(v): return int(round(v * kx))
    def sy(v): return int(round(v * ky))

    arr = np.array(ref, dtype=np.float64)
    x0, y0, x1, y1 = sx(ZONE[0]), sy(ZONE[1]), sx(ZONE[2]), sy(ZONE[3])
    region = arr[y0:y1, x0:x1]
    bg = np.percentile(region.reshape(-1, 3), 85, axis=0)
    ink = np.maximum(bg - region, 0).sum(2) >= INK_T0
    ink[:, sx(2475) - x0:sx(2525) - x0] = False        # mask the red-line band

    lab, n = ndimage.label(ink)
    ids = np.arange(1, n + 1)
    sizes = ndimage.sum(ink, lab, ids)
    cents = ndimage.center_of_mass(ink, lab, ids)      # (row, col) per component
    objs = ndimage.find_objects(lab)
    border = set(lab[0, :]) | set(lab[-1, :]) | set(lab[:, 0]) | set(lab[:, -1])

    cy, cx = RINK_CENTER[1] * ky - y0, RINK_CENTER[0] * kx - x0
    logo_ids, ad_ids = [], []
    for i, ((ry, rx), sz) in enumerate(zip(cents, sizes), start=1):
        if i in border or sz < 100 * k * k:            # zone bleed / jpeg speckle
            continue
        if any(np.hypot(ry - (dy * ky - y0), rx - (dx * kx - x0)) < 60 * k
               and sz < 6000 * k * k for dx, dy in FACEOFF_DOTS):
            continue                                   # a neutral-zone faceoff dot
        if abs(ry - cy) >= AD_MIN_DY * ky:
            ad_ids.append(i)                           # corner row: sponsor mark
        else:
            logo_ids.append(i)                         # centre art (logo, arcs, circle)

    def union_box(sel, pad):
        sl = [objs[i - 1] for i in sel]
        return (min(s[1].start for s in sl) + x0 - pad, min(s[0].start for s in sl) + y0 - pad,
                max(s[1].stop for s in sl) + x0 + pad, max(s[0].stop for s in sl) + y0 + pad)

    pad = int(round(4 * k))
    seam_l, seam_r = sx(SEAM_L), sx(SEAM_R)
    lx0, ly0, lx1, ly1 = union_box(logo_ids, 0)
    half_w = max(seam_l - lx0, lx1 - seam_r) + pad      # symmetric about the line
    ly0, ly1 = max(ly0 - pad, 0), min(ly1 + pad, rh)
    boxes = {
        "logo_left":  (seam_l - half_w, ly0, seam_l, ly1),
        "logo_right": (seam_r, ly0, seam_r + half_w, ly1),
    }

    # A logo crop is a rectangle, so an ad whose bbox pokes into it would ride along;
    # the composite therefore only keeps ink on LOGO components. The mask is dilated so
    # the soft alpha's halo around real ink survives, and the red-line band (masked out
    # of `ink` above) is forced on so centre-crossing art keeps its middle column.
    logo_mask = np.isin(lab, logo_ids)
    logo_mask[:, sx(2475) - x0:sx(2525) - x0] = True
    boxes["_logo_mask"] = ndimage.binary_dilation(logo_mask,
                                                  iterations=max(int(round(3 * k)), 1))
    boxes["_zone_origin"] = (x0, y0)

    # each sponsor spot = the ad components in that LEFT corner of the zone (the guide
    # only uses the two spots left of the line; a fixed search box clips trailing letters)
    for key, top in (("sponsor_nw", True), ("sponsor_sw", False)):
        sel = [i for i in ad_ids
               if cents[i - 1][1] < cx and (cents[i - 1][0] < cy) == top]
        boxes[key] = None if not sel else tuple(
            np.clip(v, 0, m) for v, m in zip(union_box(sel, int(round(6 * k))),
                                             (rw, rh, rw, rh)))

    boxes["_ky"] = ky
    boxes["_kx"] = kx
    boxes["_bg"] = bg                # the sheet's ice colour (zone 85th percentile)
    return boxes


def convert(reference: Image.Image | str | Path, variant: str,
            _det: dict | None = None) -> Image.Image:
    """One reference sheet -> one 1024x4096 ice texture ('playoffs' or 'finals')."""
    if variant not in VARIANTS:
        raise ValueError(f"variant must be one of {VARIANTS}, not {variant!r}")
    if not isinstance(reference, Image.Image):
        reference = Image.open(reference)
    ref = reference.convert("RGB")
    det = _det if _det is not None else _detect(ref)
    ref_bg = det["_bg"]              # sheet ice colour: the alpha reference for every crop
    out = Image.open(template_path(variant)).convert("RGB")
    tpl = np.array(out, dtype=np.float64)
    # the template's ice is one flat colour; sample it from a clean band of the sheet
    ice = np.median(tpl[2300:2500, 200:900].reshape(-1, 3), axis=0)

    # ── centre-line band: opaque, white-balanced, feathered top/bottom ────────────
    rw, rh = ref.size
    kx, ky = det["_kx"], det["_ky"]
    lx0, ly0, lx1, ly1 = LINE_SRC
    piece = ref.crop((round(lx0 * kx), round(ly0 * ky), round(lx1 * kx), round(ly1 * ky)))
    piece = piece.transpose(Image.ROTATE_270)
    x0, y0, x1, y1 = DST["line"]
    bw, bh = x1 - x0, y1 - y0
    p = np.array(piece.resize((bw, bh), Image.LANCZOS), dtype=np.float64)
    src_bg = np.percentile(p.reshape(-1, 3), 85, axis=0)
    p = np.clip(p * (ice / np.maximum(src_bg, 1.0)), 0, 255)
    ramp = np.ones(bh)
    f = min(LINE_FEATHER, bh // 2)
    ramp[:f] = np.linspace(0, 1, f, endpoint=False)
    ramp[-f:] = np.linspace(0, 1, f, endpoint=False)[::-1]
    alpha = np.repeat(ramp[:, None], bw, axis=1)
    comp = tpl[y0:y1, x0:x1] * (1 - alpha[..., None]) + p * alpha[..., None]
    out.paste(Image.fromarray(np.clip(comp, 0, 255).astype(np.uint8)), (x0, y0))

    # ── logo halves + sponsor spots ───────────────────────────────────────────────
    # never render larger than the guide's true rink scale; shrink only to fit the box
    s_true = min(1024 / (GUIDE_LOGO_H * ky), 491 / (GUIDE_LOGO_HALF_W * kx))
    for key in ("logo_left", "logo_right", "sponsor_nw", "sponsor_sw"):
        x0, y0, x1, y1 = DST[key]
        bw, bh = x1 - x0, y1 - y0
        # flood the box with flat ice: erases the stock marks / centre-circle arc under it
        out.paste(Image.fromarray(np.tile(ice.astype(np.uint8), (bh, bw, 1))), (x0, y0))
        box = det.get(key)
        if box is None:
            continue                        # blank sponsor spot — leave clean ice
        piece = ref.crop(box)

        if key.startswith("logo"):
            # crop the logo-components mask to this box (outside the zone = no ink)
            zx0, zy0 = det["_zone_origin"]
            zm = det["_logo_mask"]
            bx0, by0, bx1, by1 = box
            m = np.zeros((by1 - by0, bx1 - bx0), bool)
            ix0, iy0 = max(bx0, zx0), max(by0, zy0)
            ix1 = min(bx1, zx0 + zm.shape[1]); iy1 = min(by1, zy0 + zm.shape[0])
            if ix1 > ix0 and iy1 > iy0:
                m[iy0 - by0:iy1 - by0, ix0 - bx0:ix1 - bx0] = \
                    zm[iy0 - zy0:iy1 - zy0, ix0 - zx0:ix1 - zx0]
            mimg = Image.fromarray((m * 255).astype(np.uint8)).transpose(Image.ROTATE_270)
            piece = piece.transpose(Image.ROTATE_270)   # cut edge: left->bottom, right->top
            s = min(s_true, bw / piece.width, bh / piece.height)
            w2, h2 = round(piece.width * s), round(piece.height * s)
            px = x0 + (bw - w2) // 2                    # rink-vertical: centred
            py = y1 - h2 if key == "logo_left" else y0  # line-side edge hugs the seam
        else:
            a = np.array(piece, dtype=np.float64)
            ink = _drop_border_ink(_darkness(a, ref_bg) >= INK_T0)
            ys, xs = np.nonzero(ink)
            if len(xs) == 0:
                continue
            pad = 4
            piece = piece.crop((max(xs.min() - pad, 0), max(ys.min() - pad, 0),
                                min(xs.max() + pad, a.shape[1]),
                                min(ys.max() + pad, a.shape[0])))
            iw, ih = xs.max() - xs.min(), ys.max() - ys.min()
            s = min(AD_WIDTH_FRAC * bw / iw, AD_MAX_HFRAC * bh / ih)
            w2, h2 = round(piece.width * s), round(piece.height * s)
            px, py = x0 + (bw - w2) // 2, y0 + (bh - h2) // 2

        p = np.array(piece.resize((w2, h2), Image.LANCZOS), dtype=np.float64)
        alpha = _soft_alpha(p, ref_bg)
        if key.startswith("logo"):
            # neighbouring ad ink caught by the rectangle dies with the mask
            mres = np.array(mimg.resize((w2, h2), Image.LANCZOS), dtype=np.float64) / 255
            alpha = alpha * np.clip(ndimage.gaussian_filter(mres, 1.0), 0, 1)
        if key.startswith("sponsor"):
            # anything whose ink still touches the resized crop border dies too
            hard = _drop_border_ink(_darkness(p, ref_bg) >= INK_T0)
            alpha = alpha * ndimage.gaussian_filter(hard * 1.0, 1.5)
        base = np.array(out.crop((px, py, px + w2, py + h2)), dtype=np.float64)
        comp = base * (1 - alpha[..., None]) + p * alpha[..., None]
        out.paste(Image.fromarray(np.clip(comp, 0, 255).astype(np.uint8)), (px, py))
    return out


def convert_both(reference: Image.Image | str | Path) -> dict[str, Image.Image]:
    """{'playoffs': Image, 'finals': Image} from one reference sheet."""
    if not isinstance(reference, Image.Image):
        reference = Image.open(reference)
    det = _detect(reference.convert("RGB"))
    return {v: convert(reference, v, _det=det) for v in VARIANTS}
