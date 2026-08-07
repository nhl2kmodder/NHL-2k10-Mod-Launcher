"""
NHL 2k10 — Jersey Normal Stitcher  (the engine; its UI is a page in the Jersey Editor tab)

Problem it solves
-----------------
When you author NEW jersey art — a re-striped base colour, a new crest, a new number font — the
matching DXN normal map still carries the *stock* relief: the fine sewn-on look (edge ridges +
dashed stitches + twill infill) that traces the OLD art. This regenerates that relief so it
follows YOUR art, reusing the garment's wrinkles and construction seams (which never change —
same mesh, same UVs) as an untouched base.

Three sheets carry one, and all three are stitchable — see `SHEET_SPECS`:
  base    uniform_base_<t>_<k>.iff  #0 base    -> #2 base_normal
  stamps  uniform_<t>_<k>.iff       #0 stamps  -> #1 normal
  letters uniform_<t>_<k>.iff       #3 letters -> #4 letters_normal

How it works (all in the decoded-normal domain: R=X, G=Y, B=Z, flat = 128,128,255)
  1. CLEAN BASE  — heal the stock stripe-stitching out of the stock normal (found from the stock
                   colour's stripe edges), leaving only wrinkles + seams.
  2. DETECT      — find YOUR stripe edges/bands from your new colour (colour delta vs base fabric).
  3. SYNTHESISE  — stamp edge ridges + periodic dashed stitches + subtle twill as a height field,
                   convert to a detail normal.  Orientation-aware, so it follows any stripe angle.
  4. COMPOSITE   — partial-derivative (whiteout) blend of clean-base ⊕ stitch-detail.
  5. SAMPLE      — MEASURE the style off the stock pair: where the seam's crest sits relative to
                   the colour edge, how wide it is, its dash pitch along the seam, the split between
                   its constant and dashed parts, and the overall gain. Every value stays
                   overridable, but nothing here is a chosen number.

Why it is measured and not tuned: the test for this tool is that a regenerated normal is
indistinguishable from the one the artists shipped. So the gain is fitted by making our slope
distribution match the stock sheet's in the band where its stitching lives. Across the shipped kits
that lands within a few percent per sheet (cgy/ana/bos/tor x base/stamps/letters: -6%..+6%), with a
per-sheet gain from 1.1 to 13.5 -- which is why a single hand-picked `strength` could never be right
for all of them.

This module is pure numpy and has no UI of its own. It used to open its own window with its own
team/kit pickers, which meant choosing the kit twice and generating a normal that could not be
seen on anything. It now lives on the Jersey Editor's Normals page, where "the kit" is already
decided and the model preview lights the result.
"""
from __future__ import annotations
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, distance_transform_edt, map_coordinates, sobel

KITS = ["home", "away", "alt"]

# The three colour->normal pairs a kit carries, keyed by the Jersey Editor's sheet name.
#   asset:  "base" = uniform_base_<t>_<k>.iff, "uniform" = uniform_<t>_<k>.iff
#   index:  the texture record the generated normal is written to
# There is deliberately no helmet entry: the helmet sheet has no normal record.
SHEET_SPECS = {
    "base":    dict(asset="base",    label="base_normal",    index=2, order=0),
    "stamps":  dict(asset="uniform", label="normal",         index=1, order=1),
    "letters": dict(asset="uniform", label="letters_normal", index=4, order=2),
}
SHEET_ORDER = sorted(SHEET_SPECS, key=lambda k: SHEET_SPECS[k]["order"])

# ─────────────────────────────────────────────────────────────────────────────
#  CORE  (numpy)  — importable & unit-testable without any GUI
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = dict(
    color_thr   = 40.0,   # colour distance (0-255) that counts as "not base fabric"
    edge_k      = 2.0,    # colour-change (× color_thr) that counts as a stripe edge (lower = more)
    ridge_w     = 1.6,    # px — half-width of the raised seam ridge at a stripe edge
    ridge_off   = 0.0,    # px — how far the ridge CENTRE sits from the colour edge (measured; the
                          #   stock art puts it a couple of pixels inside the stripe, not on the line)
    ridge_h     = 0.55,   # relief height of the edge ridge
    stitch_off  = 2.0,    # px — inset of the stitch row from the stripe edge
    stitch_w    = 1.4,    # px — width of the stitch row band
    stitch_spc  = 6.0,    # px — period between stitches along the edge
    dash_len    = 3.0,    # px — length of each stitch dash (must be < stitch_spc)
    stitch_h    = 0.40,   # relief height of the dashes
    twill_ang   = 45.0,   # deg — direction of the in-band weave
    twill_prd   = 3.0,    # px — weave period
    twill_h     = 0.06,   # relief height of the weave (subtle!)
    strength    = 0.0,    # height->normal gain. 0 = MEASURE it off this sheet's own stock normal
                          #   (see sample_style); type a number to override.
    heal_blur   = 4.0,    # px — blur radius used to heal out the stock stitching
    pre_blur    = 1.0,    # px — blur on the colour before the gradient (kills DXT block ringing)
    relief_blur = 0.6,    # px — blur on the finished height field (kills the EDT's pixel staircase)
)

def rgb_to_n(img: np.ndarray) -> np.ndarray:
    n = img.astype(np.float32) / 127.5 - 1.0
    nx, ny = n[..., 0], n[..., 1]
    nz = np.sqrt(np.clip(1 - nx * nx - ny * ny, 1e-6, 1))
    return np.stack([nx, ny, nz], -1)

def n_to_rgb(n: np.ndarray) -> np.ndarray:
    n = n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)
    rgb = (n + 1.0) * 127.5
    return np.clip(rgb, 0, 255).astype(np.uint8)

SOBEL_GAIN = 8.0     # scipy's sobel kernel sums to 8; divide it out to get d(height)/d(pixel)


def _height_to_n(h: np.ndarray, strength: float) -> np.ndarray:
    """Height field -> unit normals.

    The sobel is divided by its kernel weight so `strength` means what it says: the slope in
    height units per pixel. Without that every relief was rendered at 8x its authored height,
    which is what turned a stitch row into the bright bead of rope running along each stripe.
    """
    gy = sobel(h, axis=0, mode="nearest") / SOBEL_GAIN
    gx = sobel(h, axis=1, mode="nearest") / SOBEL_GAIN
    n = np.stack([-gx * strength, -gy * strength, np.ones_like(h)], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True).clip(1e-6)

def _blend(base: np.ndarray, det: np.ndarray, max_slope: float = 0.0) -> np.ndarray:
    """Partial-derivative (whiteout) detail-normal blend of two unit-normal fields.

    `max_slope` caps how steep the sum may get, in height-per-pixel. Adding two slopes can produce
    a surface steeper than either input, and a normal steep enough to round-trip through 8-bit RGB
    with n.z at zero reads as a hard bright bead rather than as cloth. The cap is not a taste knob:
    sample_style() reads it off the stock sheet's own steepest relief.
    """
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
                  pre_blur: float = 1.0):
    """From a base-colour texture -> (edge_mask, region_mask, tangent_x, tangent_y).

    Edges are taken from COLOUR transitions anywhere in the texture (per-channel gradient), so
    stripe↔stripe boundaries (e.g. black↔gold↔white bands stacked together) each get relief — not
    only where a stripe meets the base fabric.  `region` (far from the base fabric colour) is still
    used to gate the in-band twill.  `edge_k` scales the colour-change needed to count as an edge
    (lower = more sensitive).

    The colour is blurred first. These sheets arrive out of DXT, so a flat stripe edge carries the
    codec's 4x4 block ringing, and an unblurred gradient turns that ringing into relief — the
    stripe borders came out visibly ragged rather than sewn.

    The tangent comes from the STRUCTURE TENSOR, not from the gradient direction. A stripe's two
    borders have opposite gradient signs, so the raw tangent is a line field with a 180-degree
    ambiguity: smoothing it directly (which is what this used to do) cancels opposing neighbours
    to nearly zero, and the dash phase built on it degenerated into irregular blobs instead of a
    periodic row. Smoothing the doubled angle is sign-blind, so the field survives it.
    """
    c = gaussian_filter(color.astype(np.float32), (pre_blur, pre_blur, 0)) if pre_blur > 0 \
        else color.astype(np.float32)
    dist = np.linalg.norm(c - base_rgb, axis=-1)
    region = dist > thr
    # per-channel colour gradient -> magnitude captures ANY stripe boundary, incl. stripe↔stripe
    gX = np.stack([sobel(c[..., i], axis=1, mode="nearest") for i in range(3)], -1)
    gY = np.stack([sobel(c[..., i], axis=0, mode="nearest") for i in range(3)], -1)
    mag_per = np.hypot(gX, gY)                     # H×W×3
    edge = mag_per.sum(-1) > max(edge_k * thr, 1.0)
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

def seam_arclength(edge: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """Distance travelled ALONG each seam, spread out to every pixel from its nearest seam point.

    `mod(this, stitch_spc)` is the dash phase. It used to be `mod(x*tx + y*ty, spc)` -- the pixel's
    position projected on the local tangent. On a straight stripe that IS arc length, which is why
    the base sheet's horizontal bands always came out as a tidy periodic row of stitches. On a
    CURVED seam it is not: the tangent rotates while x,y stay large, so the phase swings by the
    distance to the origin per radian of turn. Round the crest -- an outline a few hundred pixels
    from the corner of the sheet -- one lap swept well over a hundred dash periods for a contour
    only a few dozen dashes long. That is the crumbly speckle the crest border was reported with:
    straight seams looked sewn, curved ones looked chewed.

    Integrating the tangent field does not fix it either. Round a closed outline the tangent field
    is pure circulation, so it has no potential at all and a least-squares solve returns a nearly
    flat field (measured |grad| ~ 0.06 instead of 1, i.e. no dashes). Arc length round a loop is
    genuinely multi-valued.

    So walk the contour instead: shortest path over the 8-connected graph of edge pixels, seeded
    once per connected component. On a loop that runs both ways from the seed and meets at the
    antipode, which puts one join in the dash rhythm -- exactly what a real sewn seam has. Band
    pixels take the arc length of the edge pixel they are nearest to (`indices`, straight out of
    the same EDT that gives the band its width), so a dash is a tick running square across the
    band rather than a slice of a plane wave.
    """
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra
    from scipy.ndimage import label as _label

    H, W = edge.shape
    ys, xs = np.nonzero(edge)
    arc = np.zeros((H, W), np.float32)
    n = len(ys)
    if n == 0:
        return arc
    order = -np.ones((H, W), np.int64)
    order[ys, xs] = np.arange(n)
    r, c, w = [], [], []
    for dy, dx in ((0, 1), (1, 0), (1, 1), (1, -1)):
        y2, x2 = ys + dy, xs + dx
        ok = (y2 >= 0) & (y2 < H) & (x2 >= 0) & (x2 < W)
        j = np.where(ok, order[y2.clip(0, H - 1), x2.clip(0, W - 1)], -1)
        ok &= j >= 0
        r.append(np.arange(n)[ok]); c.append(j[ok])
        w.append(np.full(int(ok.sum()), float(np.hypot(dy, dx)), np.float32))
    g = csr_matrix((np.concatenate(w), (np.concatenate(r), np.concatenate(c))), shape=(n, n))
    g = g + g.T
    lab, nlab = _label(edge, np.ones((3, 3), bool))
    # one seed per component: the first edge pixel of each label, in raster order
    seen = np.zeros(nlab + 1, bool)
    keep = np.zeros(n, bool)
    for i, l in enumerate(lab[ys, xs]):
        if not seen[l]:
            seen[l] = True; keep[i] = True
    dist = dijkstra(g, indices=np.nonzero(keep)[0], min_only=True, unweighted=False)
    dist[~np.isfinite(dist)] = 0.0
    arc[ys, xs] = dist.astype(np.float32)
    return arc[indices[0], indices[1]]

def synth_stitch_height(color: np.ndarray, base_rgb: np.ndarray, p: dict):
    """Build the stripe-following relief height field from a colour texture."""
    edge, region, tx, ty = stripe_fields(color, base_rgb, p["color_thr"], p.get("edge_k", 2.0),
                                         p.get("pre_blur", 1.0))
    H, W = region.shape
    d, near_i = distance_transform_edt(~edge, return_indices=True)
    h = np.zeros((H, W), np.float32)
    # 1) edge ridge. Centred `ridge_off` px INSIDE the colour edge rather than on it: the stock
    #    normals put the seam's crest a couple of pixels off the line (measured -- see sample_style),
    #    which is what a real overlaid panel does, and a ridge sitting exactly on the colour
    #    transition reads as an outline drawn on the cloth instead of a piece sewn onto it.
    h += np.exp(-((d - p.get("ridge_off", 0.0)) / max(p["ridge_w"], 1e-3)) ** 2) * p["ridge_h"]
    # 2) dashed stitches, in a band inset from the edge, periodic along ARC LENGTH round the seam
    #    (see seam_arclength -- projecting onto the tangent only works where the seam is straight).
    ys, xs = np.mgrid[0:H, 0:W]
    band = (d >= p["stitch_off"]) & (d <= p["stitch_off"] + p["stitch_w"])
    along = seam_arclength(edge, near_i)
    duty = np.mod(along, max(p["stitch_spc"], 1e-3)) < p["dash_len"]
    dash = gaussian_filter((band & duty).astype(np.float32), 0.6) * p["stitch_h"]
    h += dash
    # 3) subtle in-band twill
    a = np.deg2rad(p["twill_ang"])
    twill = np.sin((xs * np.cos(a) + ys * np.sin(a)) / max(p["twill_prd"], 1e-3)) * p["twill_h"]
    h += np.where(region, twill, 0.0)
    # The distance field is quantised to whole pixels, so every ridge came out of step 1 as a
    # staircase and the stitch band as a run of hard-cornered blocks. Half a pixel of blur costs
    # nothing in shape and is the difference between a sewn seam and a jagged one.
    smooth = float(p.get("relief_blur", 0.6))
    return (gaussian_filter(h, smooth) if smooth > 0 else h), region, edge

def clean_base_normal(orig_normal_rgb: np.ndarray, orig_color: np.ndarray,
                      base_rgb: np.ndarray, p: dict, new_color: np.ndarray = None) -> np.ndarray:
    """Heal the stock stripe-stitching out of the stock normal -> wrinkles + seams only.

    Two things have to go. The seam relief in a band around the OLD colour edges, because the new
    art's edges are somewhere else -- that is the `reach` term, and it was all this used to do.
    And the INTERIOR relief of any area the conversion re-authored: the stock art embroiders the
    inside of a logo, not just its border, and an ~8 px band around the old outline never reached
    it. Calgary's stock crest carries a radial satin fan across its whole face; invisible under the
    black crest it was drawn for, it read as white scratches under the new white crest and is what
    "logo/crest stitching is especially weird" was looking at. So heal wherever the colour actually
    changed as well. Art the conversion left alone keeps the relief the artists authored for it.
    """
    old_edge, _, _, _ = stripe_fields(orig_color, base_rgb, p["color_thr"], p.get("edge_k", 2.0),
                                      p.get("pre_blur", 1.0))
    reach = p.get("ridge_off", 0.0) + p["ridge_w"] + p["stitch_off"] + p["stitch_w"] + 2.0
    heal = distance_transform_edt(~old_edge) < reach
    if new_color is not None:
        changed = np.linalg.norm(new_color.astype(np.float32) - orig_color.astype(np.float32),
                                 axis=-1) > p["color_thr"]
        # grow a little: an antialiased rim can match the old colour while its relief belongs to
        # the art that was there before.
        heal |= distance_transform_edt(~changed) < 2.0
    base_n = rgb_to_n(orig_normal_rgb)
    smooth = np.stack([gaussian_filter(base_n[..., i], p["heal_blur"]) for i in range(3)], -1)
    smooth = smooth / np.linalg.norm(smooth, axis=-1, keepdims=True).clip(1e-6)
    return np.where(heal[..., None], smooth, base_n)

def generate_normal(orig_normal_rgb: np.ndarray, orig_color: np.ndarray,
                    new_color: np.ndarray, p: dict, heal: bool = True) -> np.ndarray:
    """Full pipeline -> new normal-map RGB (uint8).  All inputs same HxW.

    A `strength` of 0 (the default) means "match the game": the gain is measured off THIS sheet's
    own stock normal, so the new thread sits as proud as the thread the artists shipped, instead of
    at whatever number happened to look right on one kit. Each sheet gets its own measurement —
    embroidery on the letters sheet is not as deep as a garment seam on the base.
    """
    old_base = base_fabric_rgb(orig_color)
    new_base = base_fabric_rgb(new_color)
    if float(p.get("strength", 0.0)) <= 0 or "max_slope" not in p:
        got = sample_style(orig_color, orig_normal_rgb, p)
        p = dict(p, max_slope=got.get("max_slope", 0.0))
        if float(p.get("strength", 0.0)) <= 0:
            # 0.2 is the fallback for a sheet with no stock relief to measure (a flat normal), not
            # a tuned value -- there is nothing to match in that case.
            p["strength"] = float(got["strength"]) if got["strength"] > 0 else 0.2
    clean = (clean_base_normal(orig_normal_rgb, orig_color, old_base, p, new_color) if heal
             else rgb_to_n(orig_normal_rgb))
    h, _, new_edge = synth_stitch_height(new_color, new_base, p)
    cap = float(p.get("max_slope", 0.0))

    # ── CLOSE THE LOOP ON THE GAIN ────────────────────────────────────────────────────────────
    # `strength` is an open-loop estimate: it is calibrated against the synthesised height field
    # BEFORE the whiteout blend and its slope cap, and against the stock sheet's seams rather than
    # ours. On the Calgary home base sheet that estimate landed 2.1x hot -- the shipped art's seams
    # carry 0.025 of slope above open cloth and a first pass put out 0.054, which is the difference
    # between the artists' barely-there dotted seam and a zipper running down every stripe. So
    # measure what actually came out, against the NEW art's own edges, and correct. The height
    # field is already built, so each extra pass is a couple of blurs rather than another walk of
    # the contours.
    target = _seam_excess(relief_slope(orig_normal_rgb),
                          stripe_fields(orig_color, old_base, p["color_thr"],
                                        p.get("edge_k", 2.0), p.get("pre_blur", 1.0))[0])
    dn = distance_transform_edt(~new_edge)
    near, far = dn < 8.0, dn > 20.0
    s = float(p["strength"])
    out = _blend(clean, _height_to_n(h, s), cap)
    if target > 1e-5 and near.sum() > 256 and far.sum() > 256:
        for _ in range(4):
            got = _seam_excess(relief_slope(n_to_rgb(out)), None, near, far)
            if got <= 1e-6 or abs(got - target) <= 0.08 * target:
                break
            s *= float(np.clip(target / got, 0.2, 5.0))
            out = _blend(clean, _height_to_n(h, s), cap)
    return n_to_rgb(out)

def relief_slope(normal_rgb: np.ndarray, hp_sigma: float = 6.0) -> np.ndarray:
    """A normal map -> the DETAIL slope |d(height)/d(pixel)| it encodes.

    Slope, not raw n.xy, because that is the quantity `strength` scales and the quantity a
    synthesised height field can be compared against directly. The low frequencies are removed so
    what is left is stitching and weave rather than the garment's drape.
    """
    n = rgb_to_n(normal_rgb)
    nz = n[..., 2].clip(1e-3)
    s = np.hypot(n[..., 0] / nz, n[..., 1] / nz)
    return np.abs(s - gaussian_filter(s, hp_sigma))


def _seam_excess(R: np.ndarray, edge: np.ndarray = None, near=None, far=None) -> float:
    """How much more detail slope a sheet carries AT its seams than in open cloth.

    The absolute figure is not the thing to match: cloth relief is present everywhere, seams or no
    seams, so matching the total makes synthesised thread as proud as the deepest wrinkle. The
    excess is what tells embroidery (stamps sheet, ~28x open cloth on the shipped Calgary kit) from
    a sublimated stripe the artists never sewed (base sheet, 1.2x).
    """
    if near is None:
        d = distance_transform_edt(~edge)
        near, far = d < 8.0, d > 20.0
    if near.sum() < 256 or far.sum() < 256:
        return 0.0
    return float(np.percentile(R[near], 90) - np.percentile(R[far], 90))


def _period_along(field: np.ndarray, mask: np.ndarray, tx: np.ndarray, ty: np.ndarray,
                  kmin: int, kmax: int) -> float | None:
    """The repeat length of `field` measured ALONG the local tangent, in pixels.

    This used to be an FFT of the masked values in raster order, which is meaningless: that 1-D
    sequence jumps between unrelated seams every time a scanline leaves one. Here the field is
    resampled at each texel displaced k pixels along its OWN tangent and correlated with itself, so
    the lag being tested is genuinely arc length down the seam. The winning lag is the first
    correlation peak, which is the stitch pitch; taking the global max would happily return a
    harmonic.
    """
    ys, xs = np.nonzero(mask)
    if ys.size < 256:
        return None
    if ys.size > 200_000:                              # cap the cost; the pitch is global anyway
        sel = np.linspace(0, ys.size - 1, 200_000).astype(np.int64)
        ys, xs = ys[sel], xs[sel]
    f0 = field[ys, xs]
    f0 = f0 - f0.mean()
    den = float(np.dot(f0, f0)) + 1e-9
    tX, tY = tx[ys, xs], ty[ys, xs]
    corr = []
    for k in range(kmin, kmax + 1):
        fk = map_coordinates(field, [ys + k * tY, xs + k * tX], order=1, mode="nearest")
        fk = fk - fk.mean()
        corr.append(float(np.dot(f0, fk)) / den)
    c = np.array(corr)
    # first interior local maximum, i.e. the shortest lag that lines the pattern back up
    for i in range(1, len(c) - 1):
        if c[i] > c[i - 1] and c[i] >= c[i + 1] and c[i] > 0.02:
            return float(kmin + i)
    return None


def sample_style(orig_color: np.ndarray, orig_normal_rgb: np.ndarray, p: dict) -> dict:
    """Measure this kit's thread style off the stock colour/normal pair.

    Everything here is a measurement of the shipped art, because the standard for this tool is that
    a regenerated normal is indistinguishable from the one the artists made, not that it looks
    plausible. What comes out:

      ridge_w / stitch_off / stitch_w   the relief profile ACROSS a seam, read off the stock normal
      ridge_h : stitch_h : twill_h      the same profile's relative heights
      stitch_spc / twill_prd            repeat lengths ALONG the seam and inside the panel
      strength                          the gain that makes our slope distribution match the stock's

    Degrades gracefully to whatever was passed in on any failure.
    """
    out = dict(p)
    try:
        base_rgb = base_fabric_rgb(orig_color)
        edge, region, tx, ty = stripe_fields(orig_color, base_rgb, p["color_thr"],
                                             p.get("edge_k", 2.0), p.get("pre_blur", 1.0))
        if edge.sum() < 50:
            return out
        R = relief_slope(orig_normal_rgb)
        d = distance_transform_edt(~edge)

        # ── the cross-seam profile: median relief at each whole pixel of distance from the edge ──
        # Median, not mean: a crest or a number crossing the band would otherwise drag the profile.
        NP = 14
        prof = np.array([np.median(R[(d >= k) & (d < k + 1)]) if ((d >= k) & (d < k + 1)).any()
                         else 0.0 for k in range(NP)], np.float32)
        floor = float(np.median(prof[8:])) if NP > 8 else 0.0    # flat cloth, far from any seam
        j = int(np.argmax(prof[:6]))                             # WHERE the crest actually is
        above = prof - floor
        if above[j] > 1e-4:
            # The crest is not on the colour edge. On the stock base sheets it sits ~2 px inside the
            # stripe, and the profile is a single bump there -- there is no second, separate stitch
            # row to find. Modelling it as "ridge at 0 plus a row further out" was the reason the
            # sampler kept pinning ridge_w at its clip ceiling: the value at d=0 is already down in
            # the floor, so nothing ever decayed relative to it.
            out["ridge_off"] = float(np.clip(j, 0.0, 6.0))
            thr = above[j] / np.e
            hi = next((k for k in range(j + 1, NP) if above[k] < thr), j + 2)
            lo = next((k for k in range(j - 1, -1, -1) if above[k] < thr), max(j - 2, 0))
            out["ridge_w"] = float(np.clip((hi - lo) * 0.5, 0.8, 4.0))
            # The dashes ride ON the crest -- they are a modulation of it along the seam, which is
            # why they show up as a dotted line in the stock art rather than as a parallel row. So
            # the stitch band IS the ridge band, and the split between a constant part (ridge_h) and
            # a dashed part (stitch_h) comes from the crest's own trough-to-peak contrast.
            out["stitch_off"] = float(max(0.0, out["ridge_off"] - out["ridge_w"]))
            out["stitch_w"] = float(2.0 * out["ridge_w"])
            crest = (d >= out["stitch_off"]) & (d <= out["stitch_off"] + out["stitch_w"])
            if crest.sum() > 256:
                q10, q90 = np.percentile(R[crest], [10, 90])
                total = float(p["ridge_h"])
                frac = float(np.clip(q10 / max(q90, 1e-6), 0.05, 0.95))   # always-present share
                out["ridge_h"] = total * frac
                out["stitch_h"] = total * (1.0 - frac)
            # the weave is whatever relief is left in the middle of a panel -- on these sheets, none
            unit = above[j]
            out["twill_h"] = float(np.clip(p["ridge_h"] * max(above[NP - 1], 0.0) / unit, 0.0, 0.3))

        # ── repeat lengths, measured along the seam / across the panel ──
        band = (d >= out["stitch_off"] - 0.5) & (d <= out["stitch_off"] + out["stitch_w"] + 0.5)
        spc = _period_along(R, band, tx, ty, 3, 14)
        if spc:
            out["stitch_spc"] = float(spc)
            out["dash_len"] = float(np.clip(spc * 0.5, 1.0, spc - 1.0))
        inner = region & (d > out["stitch_off"] + out["stitch_w"] + 2.0)
        prd = _period_along(R, inner, tx, ty, 2, 8)
        if prd:
            out["twill_prd"] = float(prd)

        # ── OVERALL GAIN ──
        # `strength` converts height units into a surface slope, so the honest way to set it is to
        # make the synthesised relief carry the SAME slope the game's own normal carries where its
        # stitching lives. Run the synthesiser on the STOCK colour at unit gain and take the ratio
        # of the two slope distributions. p90 rather than max: a few texels sit on a construction
        # seam far deeper than any stitch, and matching those would over-drive everything else.
        h_ref, _, _ = synth_stitch_height(orig_color, base_rgb, dict(out, strength=1.0))
        gy = sobel(h_ref, axis=0, mode="nearest") / SOBEL_GAIN
        gx = sobel(h_ref, axis=1, mode="nearest") / SOBEL_GAIN
        slope_ref = np.hypot(gx, gy)
        # R is high-passed, so the reference has to be too or the ratio is biased low and the output
        # comes out shallower than the art it is supposed to match.
        slope_ref = np.abs(slope_ref - gaussian_filter(slope_ref, 6.0))
        m = d < (out["ridge_w"] + out["stitch_off"] + out["stitch_w"] + 2.0)
        if m.sum() > 256:
            # ...and the slope to match is the EXCESS at the seam over the panel's own cloth, not
            # the total. Cloth relief is everywhere, seams or no seams, and matching the total made
            # the synthesised thread as proud as the deepest wrinkle in the sheet. Measured on the
            # shipped Calgary home pair: the base sheet's seams carry only 1.22x the relief of open
            # panel (p90 0.1415 vs 0.1164) -- the artists put essentially NO stitching on the
            # stripes, which is exactly what the stock kit renders as. Calibrating on the total
            # over-drove it 5.6x and turned every stripe into a rope ladder. The stamps sheet
            # measures 28x and the letters sheet 10x on the same test, so real embroidery is
            # unaffected: this only backs off where the art has nothing to match.
            far = d > 20.0
            floor_R = float(np.percentile(R[far], 90)) if far.sum() > 256 else 0.0
            a = max(float(np.percentile(R[m], 90)) - floor_R, 0.0)
            b = float(np.percentile(slope_ref[m], 90))
            if b > 1e-4:
                # The bounds are a sanity rail, not a tuning range: measured gains across the
                # shipped kits run from ~1.1 (a soft base sheet) to ~12 (deep letter embroidery).
                out["strength"] = float(np.clip(a / b, 0.05, 30.0))
        # A ceiling for the composite, taken from the stock sheet rather than invented. The shipped
        # normals never exceed this steepness, and without the cap the detail-blend produced texels
        # steep enough to round-trip through 8 bits with n.z at zero -- the hard bright bead.
        out["max_slope"] = float(np.percentile(R[m] if m.any() else R, 99.9) * 1.5)
    except Exception:
        pass
    return out

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
# turns that alpha edge into a colour edge, so the same stripe detector finds it. The value only has
# to differ from the art; mid-grey does for anything short of a deliberately 50% grey crest.
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


def stitch_sheet(stock_color, stock_normal, new_color, params: dict | None = None,
                 heal: bool = True, scale: float = 1.0) -> Image.Image:
    """One sheet's regenerated normal, as a PIL image the size of `stock_normal`.

    `scale` < 1 shrinks everything to a working resolution for a responsive preview; the pixel-sized
    parameters are scaled with it, or the stitch density would come out wrong at preview size and
    then change under you on export. Pass 1.0 for the real thing.
    """
    p = dict(DEFAULTS if params is None else params)
    on = to_color(stock_normal)
    oc = _fit(to_color(stock_color), on.shape[:2])
    nc = _fit(to_color(new_color), on.shape[:2])
    if scale < 1.0:
        H, W = on.shape[:2]
        nh, nw = max(1, int(H * scale)), max(1, int(W * scale))
        rs = lambda a: np.asarray(Image.fromarray(a).resize((nw, nh), Image.NEAREST))
        on, oc, nc = rs(on), rs(oc), rs(nc)
        for k in ("ridge_w", "ridge_off", "stitch_off", "stitch_w", "stitch_spc", "dash_len",
                  "twill_prd", "heal_blur", "pre_blur", "relief_blur"):
            p[k] = max(0.3, p.get(k, DEFAULTS.get(k, 1.0)) * scale)
    return Image.fromarray(generate_normal(on, oc, nc, p, heal=heal))


def stitch_kit(stock: dict, new: dict, params: dict | None = None, heal: bool = True,
               sheets=None, scale: float = 1.0) -> dict:
    """Every stitchable sheet at once: {sheet -> new normal image}.

    `stock` and `new` are the editor's own working sets. A sheet is skipped, silently, when either
    its colour or its stock normal is missing — a kit that ships no letters_normal is a normal state,
    not an error.
    """
    out = {}
    for name in (sheets if sheets is not None else SHEET_ORDER):
        sc, sn, nc = stock.get(name), stock.get(_normal_key(name)), new.get(name)
        if sc is None or sn is None or nc is None:
            continue
        out[name] = stitch_sheet(sc, sn, nc, params, heal, scale)
    return out


def _normal_key(sheet: str) -> str:
    """The working-set key a sheet's normal is stored under ('base' -> 'base_normal')."""
    return SHEET_SPECS[sheet]["label"]


def normal_key(sheet: str) -> str:
    return _normal_key(sheet)

