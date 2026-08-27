"""normal_templates.py — the AUTHORED relief the jersey stitcher builds from.

Why this module exists
----------------------
The stitcher used to invent its relief: a gaussian ridge on the colour edge, a row of dashes
stamped at a periodic phase along the seam, and a sine-wave "twill" inside the panel — all sitting
on top of the kit's OWN normal, with the stock stitching blurred out of it first. Every one of
those three is a source of garble:

  * the healed stock normal keeps whatever the blur could not reach (Anaheim's chest piping
    survived every re-stitch of the converted Seattle kit, because white-on-white is not a colour
    change and a 4 px blur leaves ~45% of a wide ridge standing);
  * dashes need a phase, a phase needs arc length, and arc length round a closed contour is
    multi-valued — the crest outline came out chewed no matter how the walk was done;
  * a gaussian bump is not what a sewn seam looks like. The real one is a TWIN ridge straddling
    the join with a groove running inside it, and it is continuous, not dashed.

So the relief is no longer invented. It is measured off reference art the user authored, and the
garment underneath is a clean template sheet rather than a blurred copy of the kit being replaced:

  normal_tmpl_base.png    the 2K10 base sheet's garment — panel seams, cuffs, hems, collar, the
                          cloth weave — with NO stripe stitching on it. Same UV layout as the
                          shipped uniform_base_<t>_<k>.iff record 2, 1024². This is the floor every
                          base normal is built on now; nothing of the old kit's relief carries over.
  normal_tmpl_seam.png    one stripe edge, cropped square across it. Measured -> SEAM profile.
  normal_tmpl_weave.png   the fill between two stripe edges. Measured -> the panel weave tile.
  normal_tmpl_stripe.png  a whole stripe (both edges + the fill). Reference; used to check the two
                          above agree, not read at runtime.
  normal_tmpl_logo_[123]  three crest patches. Measured -> the LOGO profile (see LOGO_PROFILE).

Units
-----
Everything here is a HEIGHT FIELD in the same units the stitcher's `_height_to_n` consumes: one
unit of height per pixel of run == one unit of surface slope. That is what makes the templates
authoritative — running the measured profile through the pipeline at gain 1.0 reproduces the
reference art's own steepness, so there is no gain to guess and nothing to calibrate against the
kit being thrown away.

Pixel scale
-----------
The stripe crops were authored against a **1024²** sheet (their weave repeats every 3 px, and so
does the 1024² base template's). The logo crops were authored against a **2048²** sheet (weave
repeat 6 px). Both are normalised here to 1024-sheet pixels; `normal_stitcher` then rescales to
whatever the sheet it is actually writing measures. Halving a pixel axis halves the height too —
slope is per-pixel and has to survive the conversion.
"""
from __future__ import annotations
from functools import lru_cache

import numpy as np
from PIL import Image

from . import resources

# The sheet size the stripe crops were authored at. Profiles and tiles are expressed in these
# pixels; the stitcher scales them by (sheet width / this).
TEMPLATE_SHEET = 1024

# The logo crops came off a sheet twice that size — measured from their cloth weave, which repeats
# every 6 px against the stripe crops' 3.
LOGO_TEMPLATE_SHEET = 2048


# ─────────────────────────────────────────────────────────────────────────────
#  Loading
# ─────────────────────────────────────────────────────────────────────────────
def _path(name: str):
    return resources.data_path(f"normal_tmpl_{name}.png")


@lru_cache(maxsize=None)
def _rgb(name: str) -> np.ndarray:
    return np.asarray(Image.open(_path(name)).convert("RGB"), np.uint8)


def _slopes(rgb: np.ndarray):
    """A tangent-space normal map -> the surface slope (dh/dx, dh/dy) it encodes.

    Dividing by n.z is not cosmetic: n.xy alone saturates as the surface tilts, and the templates'
    seam beads are steep enough for that to matter. Slope is also the quantity the stitcher's
    height field is expressed in, so the two are directly comparable.
    """
    n = rgb.astype(np.float32) / 127.5 - 1.0
    nz = np.sqrt(np.clip(1.0 - n[..., 0] ** 2 - n[..., 1] ** 2, 1e-6, 1.0))
    return n[..., 0] / nz, n[..., 1] / nz


def base_normal(size: tuple[int, int] | None = None) -> np.ndarray:
    """The clean 2K10 garment sheet as an RGB uint8 normal map, resized to `size` = (H, W).

    BILINEAR, not nearest: this is a vector field, and nearest-resampling a normal map leaves the
    weave aliasing into moire the moment the sheet is not 1024².
    """
    img = Image.fromarray(_rgb("base"))
    if size is not None and img.size != (size[1], size[0]):
        img = img.resize((size[1], size[0]), Image.BILINEAR)
    return np.asarray(img, np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
#  The stripe seam, measured
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=None)
def seam_profile() -> tuple[np.ndarray, np.ndarray]:
    """(d, h) — the height across a stripe edge, measured off `normal_tmpl_seam.png`.

    `d` is signed: NEGATIVE outside the stripe (open cloth), POSITIVE inside it. `h` is height in
    slope units with open cloth at zero.

    Method: the crop is a stripe edge standing square in the frame, so the cross-seam derivative is
    one image axis and the median down the other rejects the cloth weave without touching the seam.
    Integrating that median gives the profile directly. Both far ends of the crop are flat cloth, so
    any residual tilt in the result is integration drift and is removed as a straight line.

    What it finds (and what no gaussian ridge was ever going to reproduce): a TWIN bead, one thread
    row either side of the join about 2 px out, with a shallow dip on the join itself, and a groove
    running 4-7 px inside the stripe where the overlaid panel is pulled down by the seam. It is
    CONTINUOUS — there are no dashes in the reference art at all, which is why the old dash phase
    had nothing to lock onto and turned every curve into speckle.
    """
    gx, gy = _slopes(_rgb("seam"))
    # The crop's long axis runs along the seam; the short one runs across it. Take the median along
    # the seam, then pick whichever channel actually carries the cross-seam derivative — the crop is
    # a rotated cut out of a larger sheet, so the vectors in it were never rotated with the pixels
    # and the "across" derivative can sit in either channel.
    if gx.shape[0] >= gx.shape[1]:                      # seam runs down the image; across = columns
        cx, cy = np.median(gx, axis=0), np.median(gy, axis=0)
    else:                                               # seam runs across; across = rows
        cx, cy = np.median(gx, axis=1), np.median(gy, axis=1)
    g = cx if cx.var() > cy.var() else cy

    h = -np.cumsum(g)                                   # n = -dh/dx, so height is the negated sum
    h = h - np.linspace(h[0], h[-1], len(h))            # both ends are open cloth: remove the drift

    # Put d = 0 on the join, i.e. midway between the two beads. The beads are the profile's two
    # tallest points; anything between them is the dip on the join itself.
    peaks = np.argsort(h)[-2:]
    centre = float(np.mean(peaks))
    d = np.arange(len(h), dtype=np.float32) - centre

    # Orient it: the stripe INTERIOR is the side carrying the groove (the pulled-down band), which
    # is the side whose tail sits lower. Positive d must point into the stripe.
    lo, hi = h[: len(h) // 2 - 1], h[len(h) // 2 + 2:]
    if np.median(lo) < np.median(hi):
        d, h = -d[::-1], h[::-1]

    h = h - float(np.mean(h[:2]))                        # open cloth == zero
    # The whole crop is kept, both ends included. Held-flat extrapolation past either end is the
    # point: outside, that is open cloth at zero; inside, it is the panel's own level, also zero.
    # Trimming to a symmetric window around the join dropped the far interior sample and left the
    # profile clamping at the bottom of the groove, which sank every wide stripe by a fifth of a
    # seam's height.
    return d.astype(np.float32), h.astype(np.float32)


# The weave repeats every 3 px on both axes (measured by autocorrelation on the fill crop AND on
# the 1024² base template — they agree, which is how the stripe crops were placed at 1024 scale).
WEAVE_PERIOD = 3


@lru_cache(maxsize=None)
def weave_tile() -> np.ndarray:
    """The panel fill between two seams, as a small zero-mean, seamlessly-tiling height patch.

    Four whole repeats square, taken from the middle of `normal_tmpl_weave.png`, so it wraps with
    no seam of its own. Integrated in the frequency domain: for something that genuinely wraps that
    is exact, where a running sum would walk off by its own accumulated error every repeat.

    The crop is a rotated cut out of a larger sheet, so its stored vectors are NOT in the crop's own
    pixel frame — and that is not cosmetic here. Feeding a Poisson solve a gradient field rotated 90
    degrees hands it pure curl, which has no potential at all: the first attempt came out at an
    eighth of the reference art's steepness because the two channels were cancelling. `_orient`
    picks the frame back up first, by choosing the assignment that leaves the least curl.
    """
    gx, gy = _orient(*_slopes(_rgb("weave")))
    H, W = gx.shape
    n = min(WEAVE_PERIOD * 4, H, W)
    y0, x0 = (H - n) // 2, (W - n) // 2
    h = _integrate_periodic(gx[y0:y0 + n, x0:x0 + n], gy[y0:y0 + n, x0:x0 + n])
    return (h - h.mean()).astype(np.float32)


def _orient(gx: np.ndarray, gy: np.ndarray):
    """Put a crop's stored normals back into the crop's own pixel frame.

    A crop lifted out of a bigger sheet at 90 degrees keeps the vectors it was authored with, so
    what is stored in R is the derivative along a direction that is no longer the image's x. A real
    height field's gradient is curl-free; a rotated one is almost entirely curl. So try the four
    right-angle frames and keep whichever leaves the least curl energy.
    """
    cands = ((gx, gy), (gy, -gx), (-gx, -gy), (-gy, gx))
    best, out = None, cands[0]
    for cx, cy in cands:
        curl = np.gradient(cy, axis=1) - np.gradient(cx, axis=0)
        e = float(np.mean(curl * curl))
        if best is None or e < best:
            best, out = e, (cx, cy)
    return out


def _integrate_periodic(gx: np.ndarray, gy: np.ndarray) -> np.ndarray:
    """Slopes -> height for a field that tiles: the Poisson solve, done as one FFT divide.

    n = -dh/dx (and likewise y), so the height whose gradient best matches the pair is
    h = F^-1[ (conj(ikx)*F(-gx) + conj(iky)*F(-gy)) / (|kx|^2 + |ky|^2) ].
    """
    H, W = gx.shape
    ky = (np.fft.fftfreq(H) * 2j * np.pi)[:, None]
    kx = (np.fft.fftfreq(W) * 2j * np.pi)[None, :]
    den = np.abs(kx) ** 2 + np.abs(ky) ** 2
    den[0, 0] = 1.0
    num = np.conj(kx) * np.fft.fft2(-gx) + np.conj(ky) * np.fft.fft2(-gy)
    F = num / den
    F[0, 0] = 0.0
    return np.real(np.fft.ifft2(F))


# ─────────────────────────────────────────────────────────────────────────────
#  The logo patch, measured
# ─────────────────────────────────────────────────────────────────────────────
# Height across the silhouette of an embroidered crest, in 1024-sheet px, height in slope units,
# open cloth at zero. Baked rather than measured at import because getting it needs the patch's
# SILHOUETTE, and recovering that from a normal map alone is circular — the height is what tells
# you where the patch is. It was measured off normal_tmpl_logo_2.png (the Oilers disc, the one
# reference with a boundary that can be located independently: a least-squares circle through its
# slope ridge), by integrating across the silhouette on each scanline and aligning the scanlines on
# their own step before taking the median. Re-derive it with tools/measure_normal_templates.py.
#
# The shape it found: NOT a bevel. An embroidered patch is a STEP — a shallow trench right on the
# outline where the thread pulls the cloth in, then a climb at about 2.3 of slope to a satin bead
# 1 px inside, then a face sitting ~1.2 proud of the cloth. The soft 6 px ramp a radial average
# appeared to show was the average smearing that bead around a circle whose fitted radius is only
# good to about a pixel.
#
# Two things about the measurement are load-bearing, and both were got wrong first time round:
#   * The scanlines are aligned on their own edge to SUBPIXEL precision. Aligning on the integer
#     peak spreads a 1 px step over 3 px, which came out as crest edges a fifth as steep as the art.
#   * The crop is at 2048-sheet scale, so one crop pixel is HALF a unit here, not two. Inverting
#     that scales the measured slope by 4.
# The far tails are detrended against the CLOTH side only: integrating a slope field recovers height
# up to a low-frequency term, and left in, that term walked the patch face from +1.1 down through
# zero, i.e. a crest embroidered along one edge and cut into the cloth along the opposite one.
LOGO_D = np.arange(-2, 3 + 1e-6, 0.125, dtype=np.float32)
LOGO_H = np.array([-0.122, -0.088, 0.053, 0.091, 0.098, 0.098, 0.039, -0.041, -0.043, -0.075,
                   -0.010, 0.149, 0.357, 0.340, 0.326, 0.434, 0.478, 0.381, 0.492, 0.661, 0.950,
                   1.075, 1.130, 1.229, 1.264, 1.264, 1.194, 1.138, 1.090, 1.176, 1.217, 1.220,
                   1.223, 1.242, 1.233, 1.220, 1.164, 1.182, 1.257, 1.262, 1.301], np.float32)


def logo_profile() -> tuple[np.ndarray, np.ndarray]:
    """(d, h) across an embroidered patch's outline. d < 0 = cloth, d > 0 = inside the patch."""
    return LOGO_D, LOGO_H


def sample_profile(d: np.ndarray, prof: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    """Look a signed-distance field up in a measured profile.

    Outside the measured span the profile is held flat at its end values — cloth on one side, the
    patch/panel plateau on the other — which is exactly right and is why this is an interpolation
    and not a stamp: a crest 200 px across and a 4 px number stroke both get the same authored edge,
    with the plateau filling whatever is between.
    """
    dd, hh = prof
    return np.interp(d, dd, hh).astype(np.float32)
