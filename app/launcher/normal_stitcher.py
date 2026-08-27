"""
NHL 2k10 — Jersey Normal Stitcher  (the engine; its UI is a page in the Jersey Editor tab)

Problem it solves
-----------------
When you author NEW jersey art — a re-striped base colour, a new crest, a new number font — the
matching DXN normal map still carries the *stock* relief: the sewn-on look that traces the OLD art.
This regenerates that relief so it follows YOUR art.

Three sheets carry a normal, and all three are stitchable — see `SHEET_SPECS`:
  base    uniform_base_<t>_<k>.iff  #0 base    -> #2 base_normal
  stamps  uniform_<t>_<k>.iff       #0 stamps  -> #1 normal
  letters uniform_<t>_<k>.iff       #3 letters -> #4 letters_normal

Where the relief comes from  (2026-08-24 — this replaced the whole synthesiser)
------------------------------------------------------------------------------
It is no longer synthesised from parameters. It is **stamped from measured reference art** — see
`normal_templates.py`, which owns the templates and the measurements.

  * The GARMENT is a clean template sheet (`normal_tmpl_base.png`): the 2K10 base sheet's panel
    seams, cuffs, hem, collar and cloth weave, in the shipped UV layout, with no stripe stitching
    on it at all. It is not derived from the kit being replaced. Every trace of the old kit's
    striping is therefore gone by construction — where the previous version blurred the stock
    normal and hoped, which is why Anaheim's chest piping kept surviving into converted Seattle
    kits (white-on-white is not a colour change, so nothing selected it for healing, and a 4 px
    blur leaves ~45% of a wide ridge standing anyway). The stamps and letters sheets start FLAT,
    for the same reason: their stock relief belongs to the crest that is being replaced.
  * A STRIPE EDGE gets the measured cross-seam profile: a twin bead straddling the join about 2 px
    either side, a shallow dip on the join itself, and a groove pulled down 4-7 px inside the
    overlaid panel. Continuous — the reference art has no dashes, which is the single biggest
    quality change here. The old model stamped a dash row at a periodic phase along the seam, and
    a phase needs arc length, and arc length round a closed contour is multi-valued: a crest
    outline came out as speckle no matter how carefully the contour was walked. There is now no
    phase, no contour walk, and no `scipy.sparse` shortest path.
  * A LOGO / NUMBER SILHOUETTE gets the measured patch profile: a step up to a plateau with a satin
    bead just inside the outline. Colour boundaries INSIDE the patch get the stripe seam profile,
    which is what the reference crests show.
  * PANEL FILL gets the measured weave tile, laid inside the detected panels only. The garment
    template already carries the cloth's own weave everywhere else.

Everything is in slope units, so `strength = 1.0` means "exactly as deep as the reference art".
There is nothing to calibrate against the stock sheet and no gain to guess, which is why the
"Sample from stock" measurement machinery is gone: matching the shipped kit's seam excess is what
produced 1.22x-of-open-cloth stripes (i.e. invisible ones) on the base sheet.

`base_mode="stock"` keeps the old behaviour — build on the kit's own normal, healed — for the case
where a kit's authored relief is worth keeping and you only want to ADD to it.
"""
from __future__ import annotations
import numpy as np
from PIL import Image
from scipy.ndimage import (gaussian_filter, distance_transform_edt, sobel,
                          binary_erosion)

from . import normal_templates as NT

KITS = ["home", "away", "alt"]

# The three colour->normal pairs a kit carries, keyed by the Jersey Editor's sheet name.
#   asset:  "base" = uniform_base_<t>_<k>.iff, "uniform" = uniform_<t>_<k>.iff
#   index:  the texture record the generated normal is written to
#   kind:   which measured profile the sheet's art gets — a garment stripe, or a sewn-on patch
# There is deliberately no helmet entry: the helmet sheet has no normal record.
SHEET_SPECS = {
    "base":    dict(asset="base",    label="base_normal",    index=2, order=0, kind="stripe"),
    "stamps":  dict(asset="uniform", label="normal",         index=1, order=1, kind="logo"),
    "letters": dict(asset="uniform", label="letters_normal", index=4, order=2, kind="logo"),
}
SHEET_ORDER = sorted(SHEET_SPECS, key=lambda k: SHEET_SPECS[k]["order"])

# ─────────────────────────────────────────────────────────────────────────────
#  CORE  (numpy)  — importable & unit-testable without any GUI
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = dict(
    color_thr   = 200.0,   # colour distance (0-255) that counts as "not base fabric"
    decal_thr   = 40.0,   # colour distance that counts as a new colour ON A DECAL sheet
    stitch_h    = 1.0,    # × — the zig-zag stitch round a crest or number (1 = as measured)
    edge_k      = 4.0,    # colour-change (× color_thr) that counts as a panel edge (lower = more)
    seam_scale  = 1.0,    # × — how wide the stripe seam runs against the reference art
    seam_h      = 1.0,    # × — how proud the stripe seam sits (1 = as measured)
    weave_h     = 1.0,    # × — the panel weave inside a stripe (1 = as measured)
    logo_scale  = 0.8,    # × — how wide a patch's sewn edge runs against the reference art
    logo_h      = 1.0,    # × — how proud a patch sits off the cloth (1 = as measured)
    strength    = 0.5,    # overall height->slope gain. 1 = exactly the reference art's depth.
    pre_blur    = 1.0,    # px — blur on the colour before the gradient (kills DXT block ringing)
    relief_blur = 0.3,    # px — blur on the finished slope field (kills the EDT's pixel staircase)
    heal_blur   = 4.0,    # px — blur used to heal the stock relief. base_mode="stock" only.
)

# A ceiling on the composite's steepness, in height-per-pixel. Adding two slopes can produce a
# surface steeper than either input, and a normal steep enough to round-trip through 8-bit RGB with
# n.z at zero reads as a hard bright bead rather than as cloth. The number is the reference art's
# own ceiling with headroom: the logo crops peak at ~1.1 of slope at the 2048 sheet they were cut
# from, which is ~2.2 once they are expressed in 1024-sheet pixels.
MAX_SLOPE = 2.5

FLAT_RGB = (128, 128, 255)


def rgb_to_n(img: np.ndarray) -> np.ndarray:
    n = img.astype(np.float32) / 127.5 - 1.0
    nx, ny = n[..., 0], n[..., 1]
    nz = np.sqrt(np.clip(1 - nx * nx - ny * ny, 1e-6, 1))
    return np.stack([nx, ny, nz], -1)

def n_to_rgb(n: np.ndarray) -> np.ndarray:
    n = n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)
    rgb = (n + 1.0) * 127.5
    return np.clip(rgb, 0, 255).astype(np.uint8)

def _blend(base: np.ndarray, det: np.ndarray, max_slope: float = MAX_SLOPE) -> np.ndarray:
    """Partial-derivative (whiteout) detail-normal blend of two unit-normal fields."""
    bxy = base[..., :2] / base[..., 2:3].clip(1e-3)
    dxy = det[..., :2] / det[..., 2:3].clip(1e-3)
    g = bxy + dxy
    if max_slope > 0:
        mag = np.linalg.norm(g, axis=-1, keepdims=True)
        g = g * np.minimum(1.0, max_slope / mag.clip(1e-6))
    n = np.concatenate([g, np.ones(g.shape[:2] + (1,), np.float32)], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)

def base_fabric_rgb(color: np.ndarray) -> np.ndarray:
    """Dominant fabric colour = median (robust to stripes/logos)."""
    return np.median(color.reshape(-1, 3), axis=0)

def stripe_fields(color: np.ndarray, base_rgb: np.ndarray, thr: float, edge_k: float = 2.0,
                  pre_blur: float = 1.0, alpha: np.ndarray = None, edge_thr: float = None):
    """From a colour texture -> (edge_mask, region_mask, tangent_x, tangent_y).

    Edges are taken from COLOUR transitions anywhere in the texture (per-channel gradient), so
    stripe↔stripe boundaries (e.g. black↔gold↔white bands stacked together) each get relief — not
    only where a stripe meets the base fabric.  `region` (far from the base fabric colour) is the
    panel/patch itself: it gates the weave, and on a decal sheet it IS the art silhouette.

    The colour is blurred first. These sheets arrive out of DXT, so a flat stripe edge carries the
    codec's 4x4 block ringing, and an unblurred gradient turns that ringing into relief.

    `alpha`, when given, REPLACES the colour test for `region`. That is the decal sheets: there is
    no base fabric on a stamps or letters sheet, only art on nothing, and "far from the median
    colour" answers the wrong question there. Calgary settles it -- its flaming C flattens to
    (237, 215, 169) against the 128 grey `to_color` composites onto, a distance of 145, so at the
    threshold the body sheet needs (200, because a jersey's own stripes have to clear it) the whole
    crest read as base fabric and came out with NO relief at all. The alpha says exactly where the
    art is, at no threshold.

    `edge_thr` likewise lets the interior colour-edge test use a different scale from `thr`, which
    is what the decal sheets want: a satin join between two mid-tones inside a crest is a real sewn
    line even though neither side is anywhere near 200 from anything.

    The tangent comes from the STRUCTURE TENSOR, not from the gradient direction: a stripe's two
    borders have opposite gradient signs, so the raw tangent is a line field with a 180-degree
    ambiguity and smoothing it directly cancels opposing neighbours to nearly zero. Smoothing the
    doubled angle is sign-blind, so the field survives it. (Nothing in the relief needs the tangent
    any more — the dash phase that did is gone — but the Jersey Editor still reads it, and it is
    what tells a caller which way a seam runs.)
    """
    c = gaussian_filter(color.astype(np.float32), (pre_blur, pre_blur, 0)) if pre_blur > 0 \
        else color.astype(np.float32)
    dist = np.linalg.norm(c - base_rgb, axis=-1)
    region = (alpha >= 128) if alpha is not None else (dist > thr)
    # per-channel colour gradient -> magnitude captures ANY stripe boundary, incl. stripe↔stripe
    gX = np.stack([sobel(c[..., i], axis=1, mode="nearest") for i in range(3)], -1)
    gY = np.stack([sobel(c[..., i], axis=0, mode="nearest") for i in range(3)], -1)
    mag_per = np.hypot(gX, gY)                     # H×W×3
    edge = mag_per.sum(-1) > max(edge_k * (thr if edge_thr is None else edge_thr), 1.0)
    # strongest-changing channel carries the boundary
    ch = np.argmax(mag_per, axis=-1)[..., None]
    gx = np.take_along_axis(gX, ch, -1)[..., 0]
    gy = np.take_along_axis(gY, ch, -1)[..., 0]
    # structure tensor -> orientation mod pi -> tangent (perpendicular to the gradient)
    s = 2.0
    Jxx = gaussian_filter(gx * gx, s); Jyy = gaussian_filter(gy * gy, s)
    Jxy = gaussian_filter(gx * gy, s)
    ang = 0.5 * np.arctan2(2.0 * Jxy, Jxx - Jyy)   # dominant gradient direction
    tx, ty = -np.sin(ang), np.cos(ang)             # tangent = gradient rotated 90 degrees
    return edge, region, tx, ty


def _weave(shape, scale: float) -> np.ndarray:
    """The measured weave tile laid over an H×W field at `scale` sheet-px per template-px.

    Nearest lookup rather than a resample: on the sheets this actually runs on `scale` is 1.0 (the
    templates were authored against a 1024² sheet and that is what the game ships), so the tile is
    reproduced texel-for-texel, and off 1.0 a 3 px weave is close enough to Nyquist that no
    interpolation was going to save it anyway.
    """
    t = NT.weave_tile()
    n = t.shape[0]
    H, W = shape
    ys = (np.arange(H) / max(scale, 1e-3)).astype(np.int64) % n
    xs = (np.arange(W) / max(scale, 1e-3)).astype(np.int64) % n
    return t[ys[:, None], xs[None, :]]


# Sheet pixels per profile pixel on a DECAL sheet. Both decal atlases want the same number: the
# logo profile was measured on the 2048x512 `stamps` sheet, so 2 reproduces the reference art there
# texel for texel -- and 2 is also what matches the shipped `letters` sheet, whose own outline slope
# measures 1.09 against the 1.16 this produces. Neither sheet's width is the right thing to scale
# by: `letters` is 3968 px wide because it is a long strip of digit cells, not because a digit is
# drawn at four times a crest's density.
DECAL_PX = 2.0


def template_px(shape, kind: str) -> float:
    """Sheet pixels per profile pixel, for the sheet `kind` is being stitched on.

    The body sheet is square and ships at 1024, which is exactly what the templates were measured
    against, so it is a straight ratio: another size has to stretch them, or a seam authored 4 px
    wide would land 2 px wide on a 512 sheet and read as a scratch. The decal sheets take the fixed
    DECAL_PX instead.
    """
    H, W = shape
    if kind == "logo":
        return DECAL_PX
    return max(W / float(NT.TEMPLATE_SHEET), 1e-3)


def _sdf(mask: np.ndarray, smooth: float) -> np.ndarray:
    """Signed distance to the boundary of `mask` -- positive inside -- with the CONTOUR smoothed.

    A distance transform of a hard boolean mask has polygonal iso-contours: every level line
    inherits the mask's own 1 px staircase, and stamping a step profile across it prints that
    staircase as visible facets around a crest (a diagonal reads as a flight of stairs and a curve
    as a polygon). Blurring the DISTANCE, not the mask and not the finished relief, fixes it where
    it belongs: across the boundary the field is a straight ramp, which a blur leaves alone, so the
    step keeps its measured cross-section while the contour itself comes out smooth.
    """
    sd = np.where(mask, distance_transform_edt(mask), -distance_transform_edt(~mask))
    return gaussian_filter(sd.astype(np.float32), smooth) if smooth > 0 else sd.astype(np.float32)


def _stamp(sd: np.ndarray, prof, scale: float, amp: float, mask=None):
    """Lay a measured 1-D profile across a signed-distance field -> (gx, gy) SLOPE.

    The slope is evaluated ANALYTICALLY: h(x) = P(sd(x)/scale), so grad h = P'(sd/scale) * grad(sd)
    / scale, and |grad sd| = 1 for a distance field. Stamping the height and differencing it
    afterwards is what the first version did, and a central difference halves any feature that
    turns over inside two pixels -- which is every one of these, since the reference art's outline
    steps rise in about one. Measured: 0.61 of slope out of a profile carrying 1.16.
    """
    d, hp = prof
    # Slope ACROSS ONE SHEET TEXEL, not the derivative at a point: the profile's step turns over
    # inside a single texel, so point-sampling dP/dd on the EDT's own quantised distances lands
    # beside the spike as often as on it and throws away most of the rise (0.69 of slope out of a
    # profile carrying 1.16). A difference over exactly one texel is what the texel can encode, and
    # it preserves the total height across the step whatever the scale.
    t = sd / scale
    half = 0.5 / scale
    mag = (np.interp(t + half, d, hp, left=hp[0], right=hp[-1])
           - np.interp(t - half, d, hp, left=hp[0], right=hp[-1])) * amp
    if mask is not None:
        mag = mag * mask
    uy, ux = np.gradient(sd)                      # unit direction of increasing distance
    n = np.hypot(ux, uy)
    n = np.where(n < 1e-6, 1.0, n)
    return mag * ux / n, mag * uy / n


# A sewn edge is a ZIG-ZAG, and everything above this line is blind to it: a profile swept along a
# distance field can only make a feature that is constant along the contour, so the best it produces
# is a smooth double bead. Measured on NHL 23's own font normal (Calgary home, placed at 2K10 stamps
# scale, four glyphs, both loops of each):
#
#   * the ALONG-edge slope is periodic at 7.33 px, and the ACROSS-edge slope at 3.67 px -- exactly
#     half. That factor of two IS the zig-zag: the thread crosses the band twice per cycle, so a
#     fixed distance from the edge is passed twice while the thread's own direction alternates once.
#   * it sits in the OUTLINE band, centred about 1.6 px inside the silhouette. Probing from the
#     outline/fill join found the same stitch at -1.5 px on the outline side, which is the same
#     pixels -- the outline measures 3.6 px wide -- so there is ONE stitch, not one per edge.
#   * band-passed slope amplitude 0.13 rms at the peak, falling away by about 3 px in.
#
# 2K10's own art agrees that the stitch is there and is not glyph-only: Toronto's stock stamps
# normal carries a 5.98 px periodicity round its digits at 11.5x the background, and Montreal's
# crest 4.31 px at 4.5x. So this runs on the whole decal sheet, crests included. The PERIOD follows
# NHL 23, which is what the reference art for this pass is.
STITCH_PERIOD = 7.33      # sheet px, one full zig AND zag
STITCH_DEPTH = 1.6        # sheet px inside the silhouette that the thread's centreline runs
STITCH_SWING = 0.9        # ± sheet px the thread swings across the band
STITCH_WIDTH = 0.9        # sheet px, the thread's own gaussian half-width
STITCH_GAIN = 1.575       # calibration: makes the band-passed slope come out at the measured 0.13


def _arclength(mask: np.ndarray) -> np.ndarray | None:
    """Distance travelled ALONG the boundary of `mask`, spread to every pixel. -> float32, or None.

    This is the coordinate the stitch is periodic in, and there is no way to fake it: a phase built
    by projecting position onto the local tangent is exactly constant round a circle, so a round
    crest would come out with no stitch at all. So the contour is actually walked, diagonal steps
    counted as sqrt(2), and every other pixel takes the arclength of the boundary pixel nearest it.

    Each loop's total is rounded to a whole number of periods before it is handed back, so the
    stitch closes on itself instead of ending in a half-stitch wherever the walk happened to start.
    """
    b = mask & ~binary_erosion(mask)
    if not b.any():
        return None
    pts = set(map(tuple, np.argwhere(b)))
    NB = ((-1, 0), (0, 1), (1, 0), (0, -1), (-1, 1), (1, 1), (1, -1), (-1, -1))
    ph = np.zeros(mask.shape, np.float32)
    while pts:
        cur = next(iter(pts))
        loop, run = [], 0.0
        while True:
            loop.append((cur, run))
            pts.discard(cur)
            nxt = None
            for i, (dy, dx) in enumerate(NB):
                c = (cur[0] + dy, cur[1] + dx)
                if c in pts:
                    nxt, run = c, run + (1.0 if i < 4 else 1.41421356)
                    break
            if nxt is None:
                break
            cur = nxt
        if run < STITCH_PERIOD:                     # a speck -- no room for even one stitch
            continue
        k = max(round(run / STITCH_PERIOD), 1) * STITCH_PERIOD / run
        for (y, x), t in loop:
            ph[y, x] = t * k
    idx = distance_transform_edt(~b, return_indices=True)[1]
    return ph[idx[0], idx[1]]


def _zigzag(region: np.ndarray, amp: float):
    """The sewn edge itself -> (gx, gy) slope. See the measurement above.

    The thread is modelled as a ridge whose centreline weaves across the band -- radial offset
    SWING * triangle(s / PERIOD) -- and the field is the gaussian distance to that centreline. The
    weave is what puts the across-edge component at half the period; a plain cosine round the
    contour would put both at the same one, which is not what the reference art does.
    """
    ph = _arclength(region)
    if ph is None:
        return 0.0, 0.0
    sd = np.where(region, distance_transform_edt(region),
                  -distance_transform_edt(~region)).astype(np.float32)
    tri = (2.0 / np.pi) * np.arcsin(np.sin(2.0 * np.pi * ph / STITCH_PERIOD))
    # Perpendicular distance to a centreline that is not parallel to the contour, hence the tilt.
    tilt = np.hypot(1.0, 4.0 * STITCH_SWING / STITCH_PERIOD)
    r = (sd - STITCH_DEPTH - STITCH_SWING * tri) / (STITCH_WIDTH * tilt)
    h = np.exp(-0.5 * r * r).astype(np.float32) * (amp * STITCH_GAIN)
    gy, gx = np.gradient(h)
    return gx, gy


def synth_relief(color: np.ndarray, base_rgb: np.ndarray, p: dict, kind: str = "stripe",
                 alpha: np.ndarray = None):
    """Build the slope field this art asks for, from the measured reference profiles.

    Returns (gx, gy, region, edge) in height-per-pixel, ready for `_slope_to_n`.
    """
    if kind == "logo":
        # A decal sheet is art on nothing: the silhouette is the ALPHA, and the colours inside it
        # are all art, so both tests want the decal scale rather than the body sheet's.
        dt = float(p.get("decal_thr", 40.0))
        edge, region, _, _ = stripe_fields(color, base_rgb, dt, p.get("edge_k", 2.0),
                                           p.get("pre_blur", 1.0), alpha=alpha, edge_thr=dt)
    else:
        edge, region, _, _ = stripe_fields(color, base_rgb, p["color_thr"], p.get("edge_k", 2.0),
                                           p.get("pre_blur", 1.0))
    H, W = region.shape
    px = template_px((H, W), kind)
    seam_s = max(px * float(p.get("seam_scale", 1.0)), 1e-3)
    logo_s = max(px * float(p.get("logo_scale", 1.0)), 1e-3)

    gx = np.zeros((H, W), np.float32)
    gy = np.zeros((H, W), np.float32)
    seam = NT.seam_profile()

    def add(pair):
        nonlocal gx, gy
        gx = gx + pair[0]
        gy = gy + pair[1]

    if kind == "logo":
        # The patch's own silhouette: cloth outside, a step up to the embroidered face inside.
        # `region` is the art -- on a decal sheet `to_color` has composited the alpha onto flat
        # grey, so the alpha edge IS this boundary.
        din = distance_transform_edt(region)
        add(_stamp(_sdf(region, 0.6 * logo_s), NT.logo_profile(), logo_s,
                   float(p.get("logo_h", 1.0))))
        # Colour boundaries INSIDE the patch are satin lines, and the reference crests are full of
        # them. Same seam profile as a stripe, applied symmetrically: both sides of an interior
        # join are patch, so both get the bead and the groove.
        #
        # The silhouette itself is EXCLUDED. The outline is a colour edge as well as an alpha edge,
        # so without this it gets the patch step AND a seam bead stacked on it -- 1.13 of height
        # where the reference art carries 0.85, i.e. a crest with a bright wire round it.
        #
        # The exclusion is NARROW on purpose: only where the two profiles would be centred on the
        # same pixel. Widening it to the seam profile's full reach (9 units, so 18 px here) was
        # enough to swallow a crest's whole outline band -- Calgary's flaming C came out as one
        # flat plateau with a wire around it, none of the concentric satin the stock art has.
        deep = 1.5 + 0.5 * logo_s
        inner = edge & (din > deep)
        if inner.any():
            add(_stamp(gaussian_filter(distance_transform_edt(~inner).astype(np.float32),
                                       0.6 * seam_s),
                       seam, seam_s, float(p.get("seam_h", 1.0)), mask=(din > 0)))
        # ...and the stitch that actually holds the patch on. Silhouette only: the measurement found
        # the outline/fill join carrying the SAME stitch, not a second one.
        sh = float(p.get("stitch_h", 1.0))
        if sh > 0:
            add(_zigzag(region, sh))
    else:
        # A stripe edge. Signed so the profile knows which side is the overlaid panel: the groove
        # runs INSIDE the stripe. At a stripe-on-stripe join both sides are panel and both get the
        # interior branch, which is what an overlaid-on-overlaid seam looks like.
        d = distance_transform_edt(~edge)
        sd = np.where(region, d, -d)
        add(_stamp(gaussian_filter(sd.astype(np.float32), 0.6 * seam_s), seam, seam_s,
                   float(p.get("seam_h", 1.0))))

    # Panel weave, inside the panels only: the garment template already carries the cloth's own
    # weave everywhere, and doubling it up over the whole sheet reads as noise. This one IS a
    # height field -- it is a tile, not a profile of a distance -- so it is differenced, which is
    # harmless: the weave is smooth at this scale, which is the case a central difference handles.
    wh = float(p.get("weave_h", 1.0))
    if wh > 0:
        w = _weave((H, W), seam_s) * wh * region
        wy, wx = np.gradient(w)
        gx, gy = gx + wx, gy + wy

    smooth = float(p.get("relief_blur", 0.3))
    if smooth > 0:
        gx, gy = gaussian_filter(gx, smooth), gaussian_filter(gy, smooth)
    return gx, gy, region, edge


def _slope_to_n(gx: np.ndarray, gy: np.ndarray, strength: float) -> np.ndarray:
    """A slope field -> unit normals."""
    n = np.stack([-gx * strength, -gy * strength, np.ones_like(gx)], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)


def relief_slope(normal_rgb: np.ndarray, hp_sigma: float = 6.0) -> np.ndarray:
    """A normal map -> the DETAIL slope |d(height)/d(pixel)| it encodes.

    The low frequencies are removed so what is left is stitching and weave rather than the
    garment's drape.
    """
    n = rgb_to_n(normal_rgb)
    nz = n[..., 2].clip(1e-3)
    s = np.hypot(n[..., 0] / nz, n[..., 1] / nz)
    return np.abs(s - gaussian_filter(s, hp_sigma))


def clean_base_normal(orig_normal_rgb: np.ndarray, orig_color: np.ndarray,
                      base_rgb: np.ndarray, p: dict, new_color: np.ndarray = None,
                      line_heal: bool = False, kind: str = "stripe") -> np.ndarray:
    """Heal the stock stitching out of the stock normal -> wrinkles + seams only.

    Only reached by `base_mode="stock"`. The default path does not heal anything: it starts from
    the clean garment template instead, which is strictly better because healing can only ever
    remove what it can find, and the stock base sheet's stitching mostly sits in PLAIN CLOTH —
    piping curves, hem rows, yoke seams, the woven box label — where neither a colour edge nor a
    colour change selects it.
    """
    old_edge, _, _, _ = stripe_fields(orig_color, base_rgb, p["color_thr"], p.get("edge_k", 2.0),
                                      p.get("pre_blur", 1.0))
    # Reach as far as the measured seam profile actually extends, scaled to this sheet.
    dd, _ = NT.seam_profile()
    reach = float(np.max(np.abs(dd))) * template_px(orig_color.shape[:2], kind) \
        * float(p.get("seam_scale", 1.0)) + 2.0
    heal = distance_transform_edt(~old_edge) < reach
    if new_color is not None:
        changed = np.linalg.norm(new_color.astype(np.float32) - orig_color.astype(np.float32),
                                 axis=-1) > p["color_thr"]
        # grow a little: an antialiased rim can match the old colour while its relief belongs to
        # the art that was there before.
        heal |= distance_transform_edt(~changed) < 2.0
    if line_heal:
        # The normal itself testifies where stitching is: thin high-frequency ridge lines, which a
        # tight high-pass separates cleanly from the broad drape.
        R = relief_slope(orig_normal_rgb, hp_sigma=3.0)
        lines = R > max(0.04, 3.0 * float(np.median(R)))
        heal |= distance_transform_edt(~lines) < 3.0
    base_n = rgb_to_n(orig_normal_rgb)
    smooth = np.stack([gaussian_filter(base_n[..., i], p["heal_blur"]) for i in range(3)], -1)
    smooth = smooth / np.linalg.norm(smooth, axis=-1, keepdims=True).clip(1e-6)
    return np.where(heal[..., None], smooth, base_n)


def garment_base(shape, kind: str) -> np.ndarray:
    """The floor the new relief is stamped onto, as a unit-normal field.

    The body sheet gets the clean 2K10 garment template — panel seams, cuffs, hem, collar, cloth
    weave, in the shipped UV layout. The decal sheets get FLAT: everything on a stamps or letters
    sheet is art, so the only honest floor under a replaced crest is no relief at all. Anything
    else there is the OLD crest showing through.
    """
    if kind == "stripe":
        return rgb_to_n(NT.base_normal(shape))
    flat = np.zeros(tuple(shape) + (3,), np.float32)
    flat[..., 2] = 1.0
    return flat


def generate_normal(orig_normal_rgb: np.ndarray, orig_color: np.ndarray,
                    new_color: np.ndarray, p: dict, kind: str = "stripe",
                    base_mode: str = "template", line_heal: bool = False,
                    alpha: np.ndarray = None) -> np.ndarray:
    """Full pipeline -> new normal-map RGB (uint8).  All inputs same HxW.

    `base_mode="template"` (the default) throws the kit's own normal away and builds on the clean
    garment template; `"stock"` heals the kit's normal and builds on that, which is the option to
    reach for when a kit carries authored relief worth keeping.
    """
    new_base = base_fabric_rgb(new_color)
    gx, gy, _, _ = synth_relief(new_color, new_base, p, kind, alpha=alpha)
    if base_mode == "template":
        clean = garment_base(new_color.shape[:2], kind)
    else:
        old_base = base_fabric_rgb(orig_color)
        clean = clean_base_normal(orig_normal_rgb, orig_color, old_base, p, new_color,
                                  line_heal, kind)
    s = float(p.get("strength", 1.0))
    return n_to_rgb(_blend(clean, _slope_to_n(gx, gy, s), MAX_SLOPE))


def _fit(color: np.ndarray, target_hw) -> np.ndarray:
    """Resize a colour array to (H,W) if needed (nearest keeps stripe edges crisp)."""
    H, W = target_hw
    if color.shape[:2] == (H, W):
        return color
    return np.asarray(Image.fromarray(color.astype(np.uint8)).resize((W, H), Image.NEAREST))


# ─────────────────────────────────────────────────────────────────────────────
#  SHEET-LEVEL API  — what the Jersey Editor's Normals page drives
# ─────────────────────────────────────────────────────────────────────────────
# What a transparent decal texel counts as when a sheet is flattened for edge detection. The stamps
# and letters sheets are mostly empty, and their real relief boundary is the ART SILHOUETTE — an
# embroidered crest or a twill number is sewn on around its outline. Flattening onto a flat grey
# turns that alpha edge into a colour edge, so the same detector finds it. The value only has to
# differ from the art; mid-grey does for anything short of a deliberately 50% grey crest.
FLATTEN_BG = 128


def to_color(img) -> np.ndarray:
    """A PIL image (any mode) -> the H×W×3 uint8 array the detector wants.

    RGBA is composited onto FLATTEN_BG rather than dropped, because for a decal sheet the alpha
    edge IS the seam and throwing it away would leave nothing to stitch around.
    """
    im = img if isinstance(img, Image.Image) else Image.fromarray(np.asarray(img))
    if im.mode == "RGBA":
        bg = Image.new("RGBA", im.size, (FLATTEN_BG, FLATTEN_BG, FLATTEN_BG, 255))
        im = Image.alpha_composite(bg, im)
    return np.asarray(im.convert("RGB"), np.uint8)


def apply_overlay(base_rgb: np.ndarray, overlay: Image.Image) -> np.ndarray:
    """Lay an AUTHORED normal over a synthesised one, where the overlay says it has data.

    The one case is NHL 23's font normal (see jersey_convert.build_font_normal): EA drew the
    embroidery for these glyphs -- twill face, bevelled outline step, and a zig-zag chain stitch
    round both edges -- and nothing derived from a distance field will reproduce a stitch. Where
    it has coverage it simply WINS; there is no blend, because averaging two normals for the same
    surface flattens the one that has detail.

    The alpha is used as a soft mask so the glyph's antialiased rim hands back to the cloth
    instead of ending on a step, and the composite is renormalised: the two sides are unit normals
    but a linear mix of them is not, and a short vector reads as a shallower surface than either.
    """
    a = np.asarray(overlay.convert("RGBA"))
    if a.shape[:2] != base_rgb.shape[:2]:
        overlay = overlay.resize((base_rgb.shape[1], base_rgb.shape[0]), Image.LANCZOS)
        a = np.asarray(overlay.convert("RGBA"))
    w = (a[:, :, 3:4].astype(np.float32) / 255.0)
    n = rgb_to_n(a[:, :, :3]) * w + rgb_to_n(base_rgb) * (1.0 - w)
    return n_to_rgb(n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6))


def stitch_sheet(stock_color, stock_normal, new_color, params: dict | None = None,
                 base_mode: str = "template", scale: float = 1.0, kind: str = "stripe",
                 line_heal: bool = False, size=None, overlay=None) -> Image.Image:
    """One sheet's regenerated normal, as a PIL image.

    `size` is the (H, W) to produce; it defaults to the stock normal's, which is what the archive
    demands (a DXN normal is replaced IN PLACE and must keep its dimensions). `scale` < 1 shrinks
    everything to a working resolution for a responsive preview — the relief is stamped from
    templates measured in sheet pixels, so the scale has to reach the synthesiser rather than being
    applied afterwards, or the stitch density changes under you on export.

    `base_mode="stock"` still needs the stock pair; `"template"` uses neither, and passes them
    through only so one call site covers both.
    """
    p = dict(DEFAULTS if params is None else params)
    on = to_color(stock_normal) if stock_normal is not None else None
    if size is None:
        size = on.shape[:2] if on is not None else to_color(new_color).shape[:2]
    H, W = size
    if scale < 1.0:
        H, W = max(1, int(H * scale)), max(1, int(W * scale))
    rs = lambda a: _fit(a, (H, W))
    nc = rs(to_color(new_color))
    oc = rs(to_color(stock_color)) if stock_color is not None else nc
    on = rs(on) if on is not None else np.full((H, W, 3), FLAT_RGB, np.uint8)
    # The decal sheets' silhouette lives in the alpha, which `to_color` has just composited away.
    na = None
    if isinstance(new_color, Image.Image) and new_color.mode in ("RGBA", "LA", "PA"):
        a3 = np.asarray(new_color.convert("RGBA"))[:, :, 3]
        na = rs(np.dstack([a3] * 3))[:, :, 0]
    out = generate_normal(on, oc, nc, p, kind=kind, base_mode=base_mode, line_heal=line_heal,
                          alpha=na)
    if overlay is not None:
        out = apply_overlay(out, overlay)
    return Image.fromarray(out)


def stitch_kit(stock: dict, new: dict, params: dict | None = None,
               base_mode: str = "template", sheets=None, scale: float = 1.0,
               overlays: dict | None = None) -> dict:
    """Every stitchable sheet at once: {sheet -> new normal image}.

    `stock` and `new` are the editor's own working sets. A sheet is skipped, silently, when its new
    colour is missing, or — in `base_mode="stock"` only — when its stock pair is. The template path
    needs neither: a kit that ships no letters_normal still gets one cut for it, at the size of its
    letters colour sheet.
    """
    out = {}
    for name in (sheets if sheets is not None else SHEET_ORDER):
        spec = SHEET_SPECS[name]
        sc, sn, nc = stock.get(name), stock.get(_normal_key(name)), new.get(name)
        if nc is None:
            continue
        if base_mode == "stock" and (sc is None or sn is None):
            continue
        out[name] = stitch_sheet(sc, sn, nc, params, base_mode, scale, kind=spec["kind"],
                                 line_heal=(name == "base"),
                                 overlay=(overlays or {}).get(name))
    return out


def _normal_key(sheet: str) -> str:
    """The working-set key a sheet's normal is stored under ('base' -> 'base_normal')."""
    return SHEET_SPECS[sheet]["label"]


def normal_key(sheet: str) -> str:
    return _normal_key(sheet)
