"""face_builder.py — build a player head ("cyberface") from reference photographs.

WHAT THIS SOLVES
    Every shipped head is a 2009 player. Repainting one in an image editor means hand-projecting a
    face into an unwrap you can't see, so nobody does it. This module does the projection
    automatically: point it at a headshot and it returns the three 512x512 maps a
    player_head_id_*.iff wants, ready to install.

WHY IT WORKS — the two facts that make it automatic
  1. ALL 447 HEADS SHARE ONE UV LAYOUT. Every head mesh is a morph of one base whose face island is
     1,397 vertices, so the unwrap is identical across the set: face centred and roughly
     front-projected, ears at the left/right edges, neck and upper chest along the bottom, hair
     wrapping the top corners. A correspondence built for one head is valid for all of them.
  2. THE UNWRAP IS FRONTAL ENOUGH THAT A FACE LANDMARKER LOCKS ONTO IT. Running mediapipe's
     478-point face mesh on the game's own colour map returns a correct fit. That gives dense
     photo <-> UV correspondence for free, with no hand-placed landmarks and no 3D fitting: the
     photo's 478 points are the source, the base map's 478 points are the destination, and a
     piecewise-affine warp over their Delaunay triangulation carries one into the other.

TWO WAYS IN
    build()       one photo, composited into the base map behind a feathered face oval. Fast, and
                  what you want when you only have a headshot. Its `shape` knob fakes proportion in
                  UV space, which reads more like the player but drifts out of register with the
                  base normal/occlusion maps -- ~0.35 is as far as it is worth pushing.
    build_multi() SEVERAL photos at different angles, projected by visibility and reassembled into a
                  WHOLE new map. No oval, no composite, so no jaw seam; the base map contributes its
                  low-frequency shading and nothing else, so skin tone, hair colour and detail are
                  entirely the player's. Use it with face_shape.fit(), which moves the GEOMETRY to
                  the player's proportions -- then the texture has no proportion work left to do and
                  `shape` stays at 0, where it keeps perfect register with every other map.

WHAT IT DOES NOT DO
    Eyeballs, teeth and hair are SEPARATE GEOMETRY. The colour map's eye area is an empty socket, so
    the sockets are punched back out of the blend mask -- otherwise the photo's open eyes get pasted
    into a hole the game then draws an eyeball in front of. Hair is recoloured, never replaced: its
    silhouette is geometry, so pick a base head whose haircut already suits the player.

INSTALL SAFETY
    A head's VRAM blob is exactly 983,040 bytes with all three surfaces packed back-to-back and no
    slack (see archive_textures.player_head_records), and their descriptors carry +0x6C == 1, i.e.
    the loader places them sequentially. So writes are same-dimension, same-format, in place — never
    grown or format-upgraded, which would shift the loader's cursor and desync the maps.

    Install into an id NOBODY uses. 26 shipped heads are referenced by no player and are free real
    estate; free_slots() reports them against a given roster. Overwriting a head that is in use
    repaints every player pointing at it -- for the generic 8500-8559 preset band, that is dozens.
"""
from __future__ import annotations
import functools
import numpy as np
from pathlib import Path

try:
    from . import archive_textures as A
    from . import resources
except ImportError:
    import archive_textures as A
    import resources

HEAD_FMT = "player_head_id_{:04d}.iff"
UV = 512
# Depth-test tolerance, in the mesh's units (cm). A texel more than VIS_FAR behind whatever the
# camera sees first is hidden; the band up to VIS_NEAR is "close enough to be the same surface" and
# absorbs the depth buffer's own quantisation. An ear is ~1 cm thick, so the window sits under that.
VIS_NEAR, VIS_FAR = 0.25, 0.65
# Band split for the multi-view blend, in texels: below it a texel is "detail", above it "tone".
DETAIL_SIGMA, DETAIL_CLAMP = 2.0, 14.0
# A texel whose photo footprint is under this fraction of the view's median is being smeared.
FOOT_MIN = 0.35
# Scale, in texels, at which the views are made to AGREE in lightness, and how hard. Below this the
# views keep their own detail; above it they are forced onto the consensus.
CONSENSUS_SIGMA, CONSENSUS = 10.0, 0.9
# How far, in texels, measured colour may be carried past the edge of what the cameras saw before
# the fill takes over, and over how many texels the handover happens. Skin carries a long way — it
# is one flat colour and smearing it reads as skin. Hair carries barely at all: it has structure, so
# a smear of it reads as a bald patch, and the crown is only ~20 texels from the fringe in the map
# however far apart they are on the head.
PUSH_NEAR, PUSH_FADE = 16.0, 30.0
HAIR_NEAR, HAIR_FADE = 3.0, 10.0
# Width of the landmark residual field, as a fraction of the face's size in the photograph.
RESIDUAL_SIGMA = 0.055
# Octaves in the Laplacian blend. The shipped two-band split handed everything between a pore and a
# nasolabial fold to a plain weighted average, and that middle band is what the eye reads as facial
# FORM; six octaves put each scale on its own transition width.
BLEND_BANDS = 6
# Soft-knee half-width for the transferred detail, in L units, and the harder one for chroma. See
# the note at the transfer for why a hard clip at DETAIL_CLAMP was flattening every real feature.
SOFT_KNEE, CHROMA_KNEE = 45.0, 22.0
# Largest lightness correction the albedo-level match may apply, in L units. It exists to remove a
# systematic photographic exposure offset — measured at 21 L on the Boeser references — not to move
# one player onto another's complexion, so it stops well short of the ~60 L that separates the
# lightest and darkest shipped heads.
LEVEL_CAP = 30.0
# Furthest the hair's baked occlusion may swing a texel from the mass's own median, in L units. The
# span it is scaled to is measured, but it is measured on photographs that can contain a black
# shadow between two lit strands, and half of that span is not a statement about the crown.
# Raised from 34 once the level was right and the span could be read cleanly: at 34 the tanh sat on
# its ceiling over the whole side of the head and the mass spanned 35 L against the 55 both artists
# draw, so the cap was setting the answer instead of bounding it.
HAIR_OCC_MAX = 45.0
# …and how far the hair's level may be moved onto the photographs' own. Raised from 40 when the
# anchor was corrected to measure the front of the head (see the level pass): the shift it now asks
# for on a light head is 65, and at 40 the cap was again deciding the answer rather than bounding a
# bad measurement — the failure this file has already had to fix twice, at HAIR_OCC_MAX and at the
# key's gain. It binds on neither of the two reference heads; if it ever does, the build says so.
HAIR_LEVEL_CAP = 75.0

# What the RENDERER does to hair on its own, both measured with a flat albedo and the real normal
# map, over the same front-of-head region the level anchor uses, on both reference heads:
#
#   Boeser  hair sits -12.9 L under the face   crown-to-side p10-p90 spread  54.1 L
#   Makar   hair sits -11.2 L under the face   crown-to-side p10-p90 spread  56.9 L
#
# with no hair information in the map whatsoever — it is geometry and the preview light, which is
# itself calibrated to the game's. Both numbers matter because both are things the PHOTOGRAPHS also
# contain, and an albedo that carries them ships them twice.
HAIR_RENDER_SHADE = 12.0  # …so an albedo aiming at a lit photograph must sit this much lighter
HAIR_RENDER_SPAN = 55.0   # …and only owes the span the renderer does NOT already supply


def _bp(x, lo, hi):
    """Band-pass a lightness plane between two texel scales."""
    import cv2                                   # module-level; cv2 is imported lazily in this file
    return cv2.GaussianBlur(x, (0, 0), lo) - cv2.GaussianBlur(x, (0, 0), hi)


# Scale, in texels, over which the view weights are smoothed before the detail donor is chosen. Large
# on purpose: the point is that a whole feature comes from ONE photograph.
LABEL_SIGMA = 25.0
# Joint-alignment passes, and the largest displacement in texels that is still credible as
# registration error rather than the flow having found a different feature entirely.
FLOW_PASSES, FLOW_MAX = 2, 5.0
# Relief multiplier for the rebuilt normal layer, over the artist's own measured relief.
NORM_BOOST = 2.2
# Coverage below which a texel's projected colour stops being believed on its own and is regressed
# toward the coverage-weighted estimate of its neighbours, and the exponent of that ramp. `raw_have`
# is already normalised so 1.0 means "a view saw this properly", so 0.35 says: trust a texel that
# got a third of a view's worth of evidence, and distrust one that got less in proportion.
#
# ⭐ THIS IS THE NOSE-BRIDGE HOLE. The pinhole fill below used to trigger on a hard `raw_have < 0.05`
# — wacc below 0.001, i.e. essentially nothing at all — and repaired those texels completely. What
# it never touched was the PENUMBRA around such a hole, where coverage is 2-30% of a view: not zero,
# so not a pinhole, but a colour that is the average of one or two samples nobody should be believing
# at full strength. Head 3040's bridge is exactly that. 38 texels at map (257..264, 196..206), 66% of
# the way from the bridge top down to the tip, mean L 52.6 against a nose median of 79.6 — a nostril,
# painted two thirds of the way up a nose, on a man whose own portrait has a specular ridge there and
# no mark at all. It has been in every colour map since gen 13 and was worse before (gen 12 bottomed
# at L 15.7), which is what a partial repair looks like: the hole's core got filled and its rim did
# not. Measured against the alternatives — it is not geometry (the flat-albedo control render is
# clean), not the normal map, and not the detail transfer (rebuilding with DETAIL_SIGMA = 0 left the
# pit at min L 42.4 against 42.0, i.e. untouched).
FILL_TRUST, FILL_RAMP = 0.35, 1.0

# How much of a view's weight survives being the SOFTEST photograph in the set, and how sharply the
# penalty ramps in between. Applied on top of the existing resolution term, not instead of it.
#
# ⭐ SIZE AND SHARPNESS ARE NOT THE SAME MEASUREMENT, and only the first one was ever weighted. The
# projection already carries `min(face_px / 220, 1.4)` below, so a small photograph is worth less
# than a large one — but face_px is the width of the face in pixels and says nothing about whether
# those pixels are in focus. A frame grabbed from broadcast video at 520 px across, motion-blurred,
# has a larger face_px than a crisp 220 px portrait and outvoted it everywhere, even though the
# thing we are extracting from it — pore and crease relief — is precisely what blur destroys first.
#
# Measured over both reference sets, the two rankings genuinely disagree, which is what makes the
# term worth having rather than merely defensible (focus = high-pass std banded at a fixed fraction
# of the face, so it is scale-free; both columns shown as a fraction of the best in that set):
#
#   Boeser  Capture3.JPG    face 520.8 px (1.00 of best)   focus 5.08 (0.54)   <- biggest, 2nd softest
#           Capture.JPG     face 220.6 px (0.42)           focus 9.34 (1.00)   <- sharpest in the set
#   Makar   maxresdefault   face 287.6 px (1.00)           focus 6.55 (0.67)
#           1762547939885   face 212.9 px (0.74)           focus 9.82 (1.00)   <- sharpest, outranked
#
# In both sets the sharpest photograph the man has is NOT the largest, and under size alone it was
# losing. The floor is deliberately generous — a soft view still carries real colour and still
# covers geometry no other view reaches, so this is a de-rating and never an exclusion.
#
# Texture only. `face_shape` weights the same views for the geometric fit and is left alone on
# purpose: blur costs you detail, but it does not MOVE a landmark, so sharpness is not evidence
# about where the corner of a mouth is.
SHARP_FLOOR, SHARP_RAMP = 0.45, 1.5

# How much of the detected specular veil to take back out of the albedo. See deshine() — 0.6 is the
# knee: the sweep buys almost all of the available correction by there and flattens after it.
DESHINE = 0.6

# mediapipe 478-point face-mesh index sets (the canonical topology, stable across versions).
FACE_OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378,
             400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54,
             103, 67, 109)
L_EYE = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
R_EYE = (263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466)
# the UPPER lid alone, outer corner to inner, for the lash line
UP_LID_L = (33, 246, 161, 160, 159, 158, 157, 173, 133)
UP_LID_R = (263, 466, 388, 387, 386, 385, 384, 398, 362)
LO_LID_L = (33, 7, 163, 144, 145, 153, 154, 155, 133)
LO_LID_R = (263, 249, 390, 373, 374, 380, 381, 382, 362)

# Half-depth thickness of a lash line, in eye-widths. Measured by walking outward from the upper lid
# along its own normal on both reference portraits (lashprobe): the trough sits ON the lid line
# (h = 0.000, +0.010, -0.010, +0.010 across the four eyes) and is 0.100, 0.090, 0.110, 0.100 wide.
# Four eyes on two men agreeing to a hundredth is a constant, not a fit, so it is stated as one; the
# DEPTH is not, because it is a property of the face — Boeser's troughs run -28.9 and -27.8 L against
# his face mean while Makar's run -41.7 and -41.0 against his — so that is measured per build below.
LASH_WIDTH_EW = 0.10

# The face oval and the hair box the verification pass uses, restated here so the builder can score
# itself with the same ruler it is scored by. The box is in eye-widths from the eye midpoint, so it
# needs no common frame and can be evaluated in any image's own pixels.
QA_OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377,
           152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109)
QA_HAIR_BOX = (-0.95, -1.55, 0.95, -0.70)

# …and the skin regions it scores, by the same landmark indices. Eyes and ears are deliberately
# absent: a shading-only render (uniform albedo, flat normal) already carries most of what the eye
# region measures — it is the socket and the globe catching light — so no albedo correction can be
# held responsible for it, and the ear band is a wide box whose contents depend on which way the
# head is turned, measuring anywhere from -1 to -30 L across one man's own photographs.
QA_SKIN_REGIONS = {
    "forehead": (67, 109, 10, 338, 297, 66, 296, 9, 8),
    "eyebrows": (70, 63, 105, 66, 107, 55, 65, 52, 53, 46,
                 300, 293, 334, 296, 336, 285, 295, 282, 283, 276),
    "cheeks": (116, 117, 118, 119, 100, 142, 36, 205, 345, 346, 347, 348, 329, 371, 266, 425),
    # the nose is scored on its own because it fails on its own: Makar's rendered nose sat 8.2 L
    # under his face where his portrait puts it +0.4, a dark smear down one side that no other
    # region's box contains and that nothing therefore corrected.
    "nose": (168, 193, 122, 196, 174, 198, 49, 64, 98, 97, 2, 326, 327, 278, 294, 420, 399, 417),
    "mouth": (0, 267, 269, 270, 409, 291, 375, 321, 405, 314, 17, 84, 181, 91, 146, 61, 185,
              40, 39, 37, 2, 164, 393, 167),
    "chin": (152, 148, 176, 149, 150, 377, 400, 378, 379, 175, 199, 200, 18),
    "jaw": (172, 136, 58, 132, 397, 365, 288, 361, 215, 435, 138, 367),
}
# most the region loop may move any one region, in real L. 9.0 was binding on Makar's mouth, which
# asked +1.4 then +6.7 and arrived at the ceiling rather than at his portrait — a mouth box still
# 3.8 L dark because the 2009 artist's lip line is heavier ink than this particular man's mouth.
LEVEL_LOOP_CAP = 13.0
LEVEL_LOOP_TOL = 0.5
# …and the region CHROMA loop, per Lab channel. This is a MAP-SPACE allowance, and it has to be
# about twice the error it is aimed at, because only about half of it arrives: pushing a known +6 a
# through each man's finished nose and re-rendering moved the reading +3.01 on Boeser and +3.06 on
# Makar — 50.1% and 51.1%. At 7.0 both men's noses spent the whole allowance and landed ~3 a short,
# which read as "Makar's nose does not respond" until the push was actually measured. It responds
# exactly as well as Boeser's; it simply started twice as far away.
AB_LOOP_CAP = 14.0
AB_LOOP_TOL = 0.7           # faults it exists to fix are six and a half wide and sign-inverted
# how far the strand-contrast stage may take a head's hair BELOW what the map arrives carrying. A
# floor rather than zero because the photographs measure a lit head of hair against a de-lit map and
# the two are not the same quantity — see the note at `HAIR_DETAIL`.
HAIR_STRAND_FLOOR = 0.35
HAIR_LOOP_CAP = 14.0        # most the closed loop below may move a head's hair, in real L
HAIR_LOOP_TOL = 0.4         # …and how close is close enough to stop
# where `_soft_lift` stops shifting and starts compressing, on the 0..255 LAB scale (= real L 80).
# Above this an albedo texel is already at the top of what any shipped head paints, so what is left
# of the 255 is highlight headroom and is worth more as spread than as level.
LIFT_KNEE = 204.0
# …and the most it may move the hair's chroma, per Lab channel. 9.0 was binding: Makar's hair needed
# +5.9 then +3.1 on b and arrived at the ceiling rather than at his portrait, leaving it 3.9 blue of
# where it belongs — the reason his hair still reads cold against a warm brown photograph.
HAIR_AB_CAP = 14.0
EYE_SHRINK_FLOOR = 0.78     # smallest the loop may make an eyeball, as a fraction of as-authored
EYE_PINCH_CAP = 0.30        # most it may draw the two lid rings together, in cm
# ⭐ …and the other way, because the aperture stage used to be a RATCHET: it tested `_erel > 0.04`
# and clipped both its steps to a non-negative range, so it could shut an eye and never re-open one.
# Boeser's overshot to -5.3%/-6.9% against his portrait and had no path back, and that overshoot is
# not cosmetic — swapping one factor at a time (stock maps on our mesh vs stock maps on the stock
# mesh) puts 4.5 L of his eye box's darkness, and 6.0 L of Makar's, on our geometry alone. A smaller
# globe shows less lit eyeball and more socket, and the box is scored on what it shows.
EYE_GROW_CEIL = 1.08        # largest it may make one again, same fraction
EYE_SPREAD_CAP = 0.10       # most it may push the lid rings back APART, in cm
BROW_SHIFT_CAP = 0.022      # most the loop may slide a brow, as a fraction of face height
MOUTH_SHIFT_CAP = 0.035     # …and a mouth line, which starts further out and has further to go
# most the loop may SCALE the painted lip band by, about its own line. A lip that renders too thick
# is ink painted too tall — measured: sliding the ink ten texels moved the mesh not at all and moved
# the reading by the full ten — and Boeser's renders 14% thick off a 2009 artist's heavy mouth.
LIP_SCALE_CAP = 1.35
# most the loop may lift the LID SKIN, in real L, chasing the eye box.
#
# ⭐ Small on purpose, and the number is measured rather than cautious. Pushing a known +8 L over
# the whole eye footprint of each finished map moves the rendered box 0.71 L on Boeser and 0.21 on
# Makar — 8.9% and 2.7%, against 57.5%/56.7% for the nose through the identical probe. The map is
# already saturated here: it lifts the box +4.6/+1.9 L over stock and then stops, because most of
# what the box shows is the EYEBALL material, which build_scene holds out of our maps and which the
# game picks by a roster field, so it is shared across all 447 heads and cannot be retinted for one
# man. What is left over is the socket, and that is geometry — see EYE_GROW_CEIL, which is where
# the eye budget now goes. A larger allowance here would only buy a bright rim of eyelid.
EYE_LIFT_CAP = 3.0
# most the loop may multiply any one region's detail by, cumulatively. 2.2 was binding on Makar's
# JAW, which asked x1.27, x1.39, x1.25 — 2.21, stopped by the ceiling rather than by having arrived
# — and finished at 2.87 against his portrait's 4.20. The timidity this number encodes is about
# inventing grain the photograph cannot supply, and that is not what is happening here: the target
# IS his own portrait's measured grain and the render is a third short of it.
TEX_LOOP_CAP = 3.0
# …and how far it may cut, which is FURTHER, because the two directions are not the same problem.
# Adding grain invents something the photograph could not supply and has to be timid; cutting grain
# removes something the projection oversupplied, and 1/2.2 was binding on both men's cheeks — 1.75
# against a portrait's 1.02 after the gain had already spent its whole allowance going down.
TEX_CUT_CAP = 3.5
TEX_LOOP_TOL = 0.08
# …and the most it may move the face's ABSOLUTE tone, per Lab channel (real L, then a, b). Generous
# on L because the fault it exists to fix measured thirteen; tighter on chroma, because a face that
# is a little dark still reads as a man and a face that is a little green does not.
TONE_LOOP_CAP = (18.0, 7.0, 8.0)
TONE_LOOP_TOL = 0.8
SKIN_DETAIL_SIGMA = 2.5     # coarsest thing the skin detail gain may amplify, in texels — a pore is
DETAIL_ADD_CAP = 6.0        # under this, a blotch is over it — and the most it may add to one texel
LOOP_PASSES = 6             # how many render-measure-correct rounds a head gets

# ── the shipped-albedo bar ───────────────────────────────────────────────────
# Measured over every shipped head albedo (433 of 447 parse), masks from skin_hair_masks, L on the
# 0..100 scale. This is the only quality bar in this file that no photograph can move: it says what
# a 2K artist's face map actually looks like, so a generated map can be judged without asking a
# studio portrait — which is exactly the leak the closed loop below suffers from, because its
# targets are read off LIT photographs and it installs their exposure into a DE-LIT albedo.
#
#             p5     median   p95     max
#   skin L    53.3   63.5     69.4    86.3
#   hair dL  -64.7  -48.2    -21.6   -10.6      ⭐ never once positive, in 433 heads
#   blown     0.0    0.0      0.0      0.001    (fraction of the map over L 92)
#
# The hair line is the important one. Hair albedo is ALWAYS far darker than skin, because the
# game's key light blows the crown out by itself — what you paint is not what you see. A blond
# player is not an exception; blond lives near the -21.6 end and is still well under the face.
BAR_SKIN_LO, BAR_SKIN_HI = 53.3, 69.4
BAR_HAIR_DL_MAX = -10.6     # the brightest hair, relative to skin, in the whole shipped library
BAR_BLOWN_MAX = 0.001       # the most blown texels any shipped head has
BAR_TOL = 0.25

# Diagnostic switch, default OFF: skip the collar graft that carries the neck back onto the stock
# map. Off by default because the graft is the only thing matching the head to the BODY asset.
SKIP_COLLAR_GRAFT = False              # cost units a loop step may worsen the map by before it is declined

# ── the SHIP ladder ──────────────────────────────────────────────────────────
# ⭐ Decided on renders, 2026-08-17, and it is a CURATED SUBSET, not a truncation. The A/B that
# settled it put the full ladder's output beside the flatten stage plus one level curve, and the
# flatten won — the full ladder was overcooking skin (the loop drove one head to L 83.5 with 12.4%
# of the map blown against a shipped library maximum of 0.1%). But shipping the raw flatten
# capture, which is what the first cut of this did, threw five stages out with the bathwater and
# every one of them came back as a user report: the eye punch (photographic eyeballs left painted
# in the sockets = "scary eyes"), the collar graft (photo neck against the body asset's tone =
# "blotchy under the chin"), the mouth line, the lash line and the seam heal.
#
# So `build_multi(mode="ship")` — the default, and what the editor calls — runs the ladder but
# holds out exactly the stages the A/B convicted: the two contrast-restore bands, the grain
# synthesis, the base-anchored albedo level (replaced by the shipped-distribution curve below),
# the brow comb, and the whole render-measure-correct loop. mode="full" is the old ladder,
# kept for measurement.
SHIP_SKIP = frozenset({"contrast-restore", "grain", "albedo-level", "brows", "loop"})
# The level the ship path DOES apply: one gamma curve taking the map's own skin median onto the
# shipped-library median (see the bar above — 433 heads put skin at 63.5 L), with a tanh knee so
# highlights compress instead of clipping. Both constants are read off the shipped distribution,
# not tuned: 92 is where the library's blown-texel fraction leaves zero.
ALBEDO_SKIN_L = 63.5
ALBEDO_KNEE_L = 92.0
# In ship mode the hair would take the DONOR's structure outright, not just where no camera
# reached. Measured (hairlook.png, 2026-08-17): the multi-view blend preserves strand ENERGY and
# cancels strand ORIENTATION, so photo-resolved hair arrives as incoherent bright/dark streaks —
# while the haircut donor's texels are an artist's coherent locks on this very unwrap.
# ⛔ OFF (user verdict, 2026-08-17): the takeover keys on the photo hair mask, and on a light-blond
# buzz the segmenter leaves a HOLE in the scalp — donor paint all around it, pale photo mush inside
# it, rendering as a bald wedge. The mask can only gate the takeover once coverage comes from the
# donor/base paint and the photographs decide nothing but the hairline band; until that is built
# and verified, blended photo hair beats coherent paint with a hole in it.
HAIR_FROM_DONOR = False
# ⭐ The geometric AO's noise floor. mesh_occlusion normalises by the island's 90th percentile, so
# the 90% of texels under it all darken a little — on the forehead and temples, which are convex
# and should read 1.0, that printed as a grey smear the artist's map does not have (ablate.png:
# swapping in the artist's occlusion cleaned the brow). Occlusion shallower than this fraction is
# treated as that normalisation noise and lifted back to open-surface; real creases (the mandible
# measured ~20%) keep their depth.
AO_DEADBAND = 0.06


def _hair_dl(img, lm=None):
    """Hair-box mean L minus face mean L on this image, in real Lab L, or None.

    Exactly the statistic the verification pass reports as the head's hair tone, so a render and a
    photograph can be compared with it directly and the difference is a number the builder can act
    on rather than a number a human has to interpret.
    """
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    L = cv2.cvtColor(a, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    ew = float(np.hypot(*(P[263] - P[33])))
    if ew < 8:
        return None
    face = np.zeros(L.shape, np.uint8)
    cv2.fillPoly(face, [np.round(P[list(QA_OVAL)]).astype(np.int32)], 255)
    fm = face > 0
    c = 0.5 * (P[33] + P[263])
    x0, y0, x1, y1 = (int(c[0] + QA_HAIR_BOX[0] * ew), int(c[1] + QA_HAIR_BOX[1] * ew),
                      int(c[0] + QA_HAIR_BOX[2] * ew), int(c[1] + QA_HAIR_BOX[3] * ew))
    m = np.zeros(L.shape, bool)
    m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    m &= ~fm
    m &= a.sum(2) > 40                                   # not background
    if m.sum() < 200 or fm.sum() < 200:
        return None
    return float(L[m].mean() - L[fm].mean()) / 2.55


def _aperture(img, lm=None):
    """How far the eyes are open, both of them, as a fraction of face height. -> (L, R) or None.

    The verification pass's own measure, and the one the user reacted to first ("those eyes are
    scary"). ⚠ It is read off LANDMARKS, and mediapipe finds the lid margin by shading, not by
    geometry: the fitted mesh's own lid points measured 17% too CLOSED at the same time the render
    measured 13-17% too open, sliding the eyeball half a centimetre moved it 2-3 points, and merely
    re-lighting unchanged geometry moved it ten. So it is answered where it is caused — in the
    albedo, by how hard the lash line states where the eye ends.
    """
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    fh = float(np.hypot(*(P[10] - P[152])))
    if fh < 20:
        return None
    return (float(np.hypot(*(P[159] - P[145])) / fh),
            float(np.hypot(*(P[386] - P[374])) / fh))


def _brow_gap(img, lm=None):
    """How far each brow sits above its eye, as a fraction of face height. -> (L, R) or None."""
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    fh = float(np.hypot(*(P[10] - P[152])))
    if fh < 20:
        return None
    return (float(np.hypot(*(P[105] - P[159])) / fh),
            float(np.hypot(*(P[334] - P[386])) / fh))


def _mouth_line(img, lm=None):
    """Where the mouth's closing line sits between the nose base and the chin. -> float or None.

    ⭐ The lips have been the same fault on both heads for four generations and neither fit was
    causing it. Boeser's rendered upper lip measures 18% taller than his portrait's and Makar's 22%
    SHORTER, with his philtrum 20% too long — opposite errors on two men, which rules out the shape
    fit and rules out the projection, because both would err in one direction. What they share is
    the 2009 artist's painted mouth line, which every stage defers to (`aper_m` holds it out of the
    corrections) and which sits wherever it sat on the base head. On base 138 that is a little high
    for Boeser and on 3040 a little low for Makar, so each man's upper lip is measured from the
    wrong place. It is the brow-height fault exactly, one feature down, and it takes the same
    treatment: measure where the line landed in the render, and slide the ink until it is right.

    Reported as the drop from the nose base (2) to the lip line (13) over the face height, so it is
    scale-free and signed — larger means the mouth sits lower on the face.
    """
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    fh = float(np.hypot(*(P[10] - P[152])))
    if fh < 20:
        return None
    return float((P[13][1] - P[2][1]) / fh)


def _lip_height(img, lm=None):
    """Vermilion height over the face height, UPPER and LOWER separately. -> (up, lo) or None.

    ⚠ Separately, and that is not fussiness. The first version returned one number for both lips
    and drove one scale about the mouth line, which shrinks the ink above and below it together —
    so it bought Boeser's upper lip (+18.5% of face height over his portrait down to +7.6%) by
    driving his lower lip from +9.7% to -5.4% and Makar's from -11.2% to -15.9%. The two lips are
    independently wrong on both men, in opposite directions on one of them, so one gain cannot fix
    them and a gain that tries will always pay for one with the other.

    A companion to `_mouth_line` and the other half of the same fault. The slide fixes WHERE the
    mouth is; this is HOW THICK it is, and Boeser needs the second without the first — his line
    lands right and his upper lip still renders 14% taller than his portrait's.

    ⚠ It is a MAP measurement even though it sounds like a geometric one, and that is the whole
    reason it can be corrected here. On a closed mouth mediapipe finds the vermilion border by
    COLOUR: landmark 0 sits on the painted edge between lip and skin, and 13 on the painted line
    between the lips. Neither is a silhouette and neither is a crease the mesh carries — measured,
    sliding the ink ten texels moved the mesh not at all and moved this reading by the full ten. So
    a lip that renders too thick is ink that is painted too tall, and it scales about its own line.

    ⚠ This divides by FACE HEIGHT and takes the vertical drop only; the QA sheet that scores the
    finished head divides the full 0-13 distance by EYE WIDTH. Checked directly rather than assumed,
    because a term disagreeing with its own score was already the fault once in this loop: on gen
    22's Boeser the two read +17.7% and +14.5% over the same portrait, and that gap is exactly the
    faceheight-over-eyewidth ratio moving 2.035 -> 1.979 between photo and render. They agree. So
    when this term reports converged and the sheet reports thick, the disagreement is in TIME, not
    in definition — this is read near the top of a pass and the region level and detail terms run
    after it, so the last thing to touch the mouth on any pass is never this one.
    """
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    fh = float(np.hypot(*(P[10] - P[152])))
    if fh < 20:
        return None
    up = abs(P[13][1] - P[0][1]) / fh          # painted lip/skin edge down to the painted line
    lo = abs(P[17][1] - P[14][1]) / fh         # …and the line down to the lower edge
    return (float(up), float(lo)) if min(up, lo) > 1e-4 else None


def _hair_ab(img, lm=None):
    """Hair-box mean (a, b) minus the face's, the QA's hair COLOUR figure. -> (da, db) or None."""
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    ew = float(np.hypot(*(P[263] - P[33])))
    if ew < 8:
        return None
    lab = cv2.cvtColor(a, cv2.COLOR_RGB2LAB).astype(np.float32) - np.float32([0, 128, 128])
    face = np.zeros(a.shape[:2], np.uint8)
    cv2.fillPoly(face, [np.round(P[list(QA_OVAL)]).astype(np.int32)], 255)
    fm = face > 0
    c = 0.5 * (P[33] + P[263])
    x0, y0, x1, y1 = (int(c[0] + QA_HAIR_BOX[0] * ew), int(c[1] + QA_HAIR_BOX[1] * ew),
                      int(c[0] + QA_HAIR_BOX[2] * ew), int(c[1] + QA_HAIR_BOX[3] * ew))
    m = np.zeros(a.shape[:2], bool)
    m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    m &= ~fm
    m &= a.sum(2) > 40
    if m.sum() < 200 or fm.sum() < 200:
        return None
    return tuple(float(lab[..., i][m].mean() - lab[..., i][fm].mean()) for i in (1, 2))


def _hair_tex(img, lm=None):
    """The QA's hair TEXTURE figure — the spread of L finer than 8 px inside the hair box."""
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    ew = float(np.hypot(*(P[263] - P[33])))
    if ew < 8:
        return None
    L = cv2.cvtColor(a, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    face = np.zeros(L.shape, np.uint8)
    cv2.fillPoly(face, [np.round(P[list(QA_OVAL)]).astype(np.int32)], 255)
    c = 0.5 * (P[33] + P[263])
    x0, y0, x1, y1 = (int(c[0] + QA_HAIR_BOX[0] * ew), int(c[1] + QA_HAIR_BOX[1] * ew),
                      int(c[0] + QA_HAIR_BOX[2] * ew), int(c[1] + QA_HAIR_BOX[3] * ew))
    m = np.zeros(L.shape, bool)
    m[max(0, y0):max(0, y1), max(0, x0):max(0, x1)] = True
    m &= face == 0
    m &= a.sum(2) > 40
    if m.sum() < 200:
        return None
    # the QA works in a frame where the outer eye corners are 400 px apart; a sigma means the same
    # thing in two images only if they are at the same scale, so state it in eye-widths.
    sig = 8.0 * ew / 400.0
    return float((L - cv2.GaussianBlur(L, (0, 0), max(sig, 0.6)))[m].std()) / 2.55


def _eye_dl(img, lm=None):
    """The scored EYE box's mean L minus the face mean, in real Lab L. -> float, or None.

    ⭐ The eye box is the only scored region that has never had a correction loop, and it is the
    worst region on both heads. The reason it was left out was a wrong diagnosis: the eyeball sheet
    is a shared asset the builder does not author, so the box looked unreachable. Measured 2x2 —
    our map / stock map crossed with our geometry / stock geometry — says otherwise:

        Makar   stock map, stock geom   eyes 48.0   face 64.0   dL -15.9
                OUR map,   stock geom   eyes 50.1   face 75.7   dL -25.6
                stock map, OUR geom     eyes 42.6   face 63.8   dL -21.3
                OUR map,   OUR geom     eyes 44.2   face 75.9   dL -31.7   (portrait: -22.8)

    Both halves own a piece and neither is architectural. The map half is the shared globe failing
    to follow an 11.7 L lift of the skin around it; the geometry half is 5.5 L of extra shadow from
    a socket the aperture loop deliberately tightened. Neither is fixable where it arises — but the
    LID SKIN is ours, it is most of the box's area, and lifting it moves the reading. So the box
    gets a loop like every other region, closed on the render, and the cap is what keeps it honest.
    """
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    L = cv2.cvtColor(a, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    lit = a.sum(2) > 40
    face = np.zeros(L.shape, np.uint8)
    cv2.fillPoly(face, [np.round(P[list(QA_OVAL)]).astype(np.int32)], 255)
    fm = (face > 0) & lit
    p = np.zeros(L.shape, np.uint8)
    cv2.fillPoly(p, [np.round(P[list(L_EYE) + list(R_EYE)]).astype(np.int32)], 255)
    m = (p > 0) & lit
    if fm.sum() < 500 or m.sum() < 200:
        return None
    return float(L[m].mean() - L[fm].mean()) / 2.55


def _region_dl(img, lm=None):
    """Each scored skin region's mean L minus the face mean, in real Lab L. -> dict, or None.

    The verification pass's own statistic, so the difference between this on a render and this on a
    photograph is a number the builder can act on directly instead of one a human has to interpret.
    """
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    L = cv2.cvtColor(a, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    face = np.zeros(L.shape, np.uint8)
    cv2.fillPoly(face, [np.round(P[list(QA_OVAL)]).astype(np.int32)], 255)
    lit = a.sum(2) > 40
    fm = (face > 0) & lit
    if fm.sum() < 500:
        return None
    fL = float(L[fm].mean())
    out = {}
    for name, idx in QA_SKIN_REGIONS.items():
        p = np.zeros(L.shape, np.uint8)
        cv2.fillPoly(p, [np.round(P[list(idx)]).astype(np.int32)], 255)
        m = (p > 0) & lit
        if m.sum() > 200:
            out[name] = float(L[m].mean() - fL) / 2.55
    return out or None


def _skin_tone(img, lm=None):
    """The face oval's ABSOLUTE mean Lab, in real L and centred a/b. -> (L, a, b) or None.

    ⭐ The blind spot every other figure in this file shares. `_region_dl` and the whole verification
    pass report each region as a DELTA against the face mean, so a head whose skin is uniformly
    thirteen L too dark scores perfectly in every region and still does not look like a living man.
    That is not hypothetical: Makar's render measured 62.7 L against his portrait's 76.0, 3.6 too
    green and 4.8 too blue, with every relative region inside tolerance.

    The cause is upstream, in the `albedo level` stage: it matches the projected map to the 2009 BASE
    HEAD's own map (`_lab(base_np)`), not to the photograph. Base head 138 happens to be about the
    right tone for Boeser; base head 3040 is thirteen L darker than Makar. So the level a head lands
    on is an accident of which stock head it was built from. Measure it here and close it in the
    render loop, where the renderer's own exposure is already in the number.
    """
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    lab = cv2.cvtColor(a, cv2.COLOR_RGB2LAB).astype(np.float32)
    face = np.zeros(a.shape[:2], np.uint8)
    cv2.fillPoly(face, [np.round(P[list(QA_OVAL)]).astype(np.int32)], 255)
    m = (face > 0) & (a.sum(2) > 40)
    if m.sum() < 500:
        return None
    return (float(lab[..., 0][m].mean()) / 2.55,
            float(lab[..., 1][m].mean()) - 128.0,
            float(lab[..., 2][m].mean()) - 128.0)


def _region_ab(img, lm=None):
    """Each scored region's mean (a, b) minus the face's. -> {name: (da, db)}, or None.

    ⭐ The axis nothing in this builder has ever corrected. Level has a loop, texture has a loop,
    hair has both plus a chroma term of its own — and the FACE's chroma has never been closed
    against anything. It shows up exactly where a person's colour lives: Makar's portrait puts his
    nose +3.2 a against his face and the render puts it -3.2, a swing of six and a half in the
    wrong DIRECTION, and Boeser's portrait puts his nose at +5.2 where the render says +0.9. A red
    nose and a warm mouth against cooler brows and temples are most of what separates skin from
    plastic, and a face that is uniformly the right colour but flat across it still reads dead —
    which is why the render's chroma spread across the face measures 3.2 against a portrait's 4.7.
    """
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    lab = cv2.cvtColor(a, cv2.COLOR_RGB2LAB).astype(np.float32)
    face = np.zeros(a.shape[:2], np.uint8)
    cv2.fillPoly(face, [np.round(P[list(QA_OVAL)]).astype(np.int32)], 255)
    lit = a.sum(2) > 40
    fm = (face > 0) & lit
    if fm.sum() < 500:
        return None
    fa, fb = float(lab[..., 1][fm].mean()), float(lab[..., 2][fm].mean())
    out = {}
    for name, idx in QA_SKIN_REGIONS.items():
        p = np.zeros(a.shape[:2], np.uint8)
        cv2.fillPoly(p, [np.round(P[list(idx)]).astype(np.int32)], 255)
        m = (p > 0) & lit
        if m.sum() > 200:
            out[name] = (float(lab[..., 1][m].mean()) - fa, float(lab[..., 2][m].mean()) - fb)
    return out or None


def _region_tex(img, lm=None):
    """Each scored skin region's TEXTURE figure — the spread of L finer than 8 px. -> dict, or None.

    The companion to `_region_dl`, and the same argument for its existence: the map is not the thing
    being scored. Measured on the hair, a map that carried 68% of the portrait's spread rendered at
    49% of it, because rasterising a 512-texel map onto a head a few hundred pixels tall is itself a
    low-pass — and a different one for every fitted head. Rather than guess at that loss, measure it.
    """
    import cv2
    a = np.asarray(img.convert("RGB"), np.uint8)
    if lm is None:
        try:
            lm = np.asarray(landmarks(img), np.float64)[:, :2]
        except Exception:
            return None
    P = np.asarray(lm, np.float64)[:, :2]
    ew = float(np.hypot(*(P[263] - P[33])))
    if ew < 8:
        return None
    L = cv2.cvtColor(a, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    # the verification pass works in a frame where the outer eye corners are 400 px apart; its 8-px
    # sigma means the same thing in another image only when it is restated in eye-widths.
    hp = L - cv2.GaussianBlur(L, (0, 0), max(8.0 * ew / 400.0, 0.6))
    lit = a.sum(2) > 40
    out = {}
    for name, idx in QA_SKIN_REGIONS.items():
        p = np.zeros(L.shape, np.uint8)
        cv2.fillPoly(p, [np.round(P[list(idx)]).astype(np.int32)], 255)
        m = (p > 0) & lit
        if m.sum() > 200:
            out[name] = float(hp[m].std()) / 2.55
    return out or None


UP_LID_RINGS = ((33, 246, 161, 160, 159, 158, 157, 173, 133),
                (263, 466, 388, 387, 386, 385, 384, 398, 362))
LO_LID_RINGS = ((33, 7, 163, 144, 145, 153, 154, 155, 133),
                (263, 249, 390, 373, 374, 380, 381, 382, 362))


def _eye_close_fields(base_id, game_dir, positions, t_lm, w, h):
    """What the aperture stage needs to close an eye: which vertices are lid, which are globe, and
    which way is 'shut'. -> (wUpper, wLower, axis, [globe vertex groups]) or None.

    The lids are found through the UV, which is the one frame in which a mediapipe landmark and a
    vertex mean the same thing. The axis is the line from the upper lid's centroid to the lower's,
    so it is this eye's own closing direction and not an assumption about which way is down.
    """
    try:
        import cv2
        try:
            from . import char_model as C
        except ImportError:
            import char_model as C
        nm = HEAD_FMT.format(base_id)
        b = C.blob(False, game_dir, nm)
        sc = C.build_scene(b, C.scan_models(b, nm)[0], asset=nm, game_dir=game_dir, eyes=1)
        P = np.asarray(positions, np.float64)
        uv = np.asarray(sc["uv"], np.float64)
        if len(uv) != len(P):
            return None
        px = np.clip((uv[:, 0] * w).astype(int), 0, w - 1)
        py = np.clip((uv[:, 1] * h).astype(int), 0, h - 1)
        skin = np.unique(sc["tri"][sc["mat"] == 0]) if len(sc.get("mat", ())) else None
        if skin is None or not len(skin):
            return None
        L = np.asarray(t_lm, np.float64)[:, :2]
        ew = float(np.linalg.norm(L[133] - L[33]))
        if ew < 8:
            return None

        def dist(rings):
            m = np.full((h, w), 255, np.uint8)
            for r in rings:
                cv2.polylines(m, [np.round(L[list(r)]).astype(np.int32)], False, 0, 1)
            return cv2.distanceTransform(m, cv2.DIST_L2, 3)[py, px]

        dU, dL = dist(UP_LID_RINGS), dist(LO_LID_RINGS)
        rad = 0.22 * ew
        keep = np.zeros(len(P), bool)
        keep[skin] = True
        wU = np.where(keep & (dU < dL), np.exp(-0.5 * (dU / rad) ** 2), 0.0)
        wL = np.where(keep & (dL <= dU), np.exp(-0.5 * (dL / rad) ** 2), 0.0)
        if wU.sum() < 1e-3 or wL.sum() < 1e-3:
            return None
        cU = (P * wU[:, None]).sum(0) / wU.sum()
        cLo = (P * wL[:, None]).sum(0) / wL.sum()
        ax = cLo - cU
        n = float(np.linalg.norm(ax))
        if n < 1e-6:
            return None
        groups = [np.asarray(list(g), int) for g in (sc.get("eye_verts") or []) if len(g)]
        return wU.astype(np.float64), wL.astype(np.float64), ax / n, groups
    except Exception:
        return None


def _render_head(base_id, game_dir, color_img, nrm_img, positions, w=1000, h=1300):
    """Render the finished maps on the fitted mesh, frontally, the way the QA renders them.

    Returns a PIL image or None. Everything about it is best-effort: a build must not fail because
    a preview renderer was unavailable, so every path out of here is a None rather than a raise.
    """
    try:
        try:
            from . import char_model as C, arena_preview as AP
        except ImportError:
            import char_model as C
            import arena_preview as AP
        nm = HEAD_FMT.format(base_id)
        b = C.blob(False, game_dir, nm)
        sc = C.build_scene(b, C.scan_models(b, nm)[0], asset=nm, game_dir=game_dir, eyes=1)
        sc["pos"] = np.asarray(positions, np.float32)
        sc["nrm"] = C._vertex_normals(sc["pos"].astype(np.float64), sc["tri"]).astype(np.float32)
        sc["vcol"] = C.lambert(sc["nrm"], len(sc["pos"]))
        col = np.asarray(color_img.convert("RGB"), np.uint8)
        nrm = np.asarray(nrm_img.convert("RGB"), np.uint8) if nrm_img is not None else None
        for mid in list(sc["tex"]):
            if mid != sc.get("eye_mat"):
                sc["tex"][mid] = col
                if nrm is not None:
                    sc["ntex"][mid] = nrm
        sc["tan"] = C.tangents(sc["pos"], sc["uv"], sc["tri"], sc["nrm"])
        C.seat_eyes(sc)
        C.head_light(sc, "game")
        C.light_eyes(sc)
        return AP.render(sc, w, h, 3.14, 0.0, 1.45).convert("RGB")
    except Exception:
        return None


def _lash_depth(img, lm):
    """How far below the face's mean L the upper lash line sits, in Lab L, on this photograph.

    Returns a negative number, or 0.0 if it cannot be read. Walks outward from the lid line in
    eye-widths exactly as the probe did, and takes the trough — not the value AT the landmarks,
    which straddles the edge and reads half as deep.
    """
    import cv2
    try:                                            # same dual import the projection stage uses
        from . import face_shape as FS
    except ImportError:
        import face_shape as FS
    a = np.asarray(img.convert("RGB"), np.uint8)
    L = cv2.cvtColor(a, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32) / 2.55
    face = np.zeros(L.shape, np.uint8)
    cv2.fillConvexPoly(face, cv2.convexHull(np.round(lm[:FS.N_SKIN]).astype(np.int32)), 255)
    if (face > 0).sum() < 500:
        return 0.0
    fL = float(L[face > 0].mean())
    best = 0.0
    for up, lo in ((UP_LID_L, LO_LID_L), (UP_LID_R, LO_LID_R)):
        A, B = lm[list(up)].astype(np.float64), lm[list(lo)].astype(np.float64)
        w = float(np.linalg.norm(A[-1] - A[0]))
        if w < 8:
            continue
        d = A - 0.5 * (A + B)
        d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
        prof = []
        for h in np.linspace(-0.06, 0.22, 29):
            p = A + d * (h * w)
            xs = np.clip(p[:, 0].round().astype(int), 0, L.shape[1] - 1)
            ys = np.clip(p[:, 1].round().astype(int), 0, L.shape[0] - 1)
            prof.append(float(L[ys, xs].mean()))
        best = min(best, min(prof) - fL)
    return best
# the brows, each as a closed loop: lower edge outward, upper edge back
BROW_L = [70, 63, 105, 66, 107, 55, 65, 52, 53, 46]
BROW_R = [300, 293, 334, 296, 336, 285, 295, 282, 283, 276]

_DET = None
_SEG = None


def _cv2():
    import cv2
    return cv2


def _detector():
    global _DET
    if _DET is None:
        from mediapipe.tasks.python import BaseOptions, vision
        model = resources.data_path("face_landmarker.task")
        if not model.exists():
            raise RuntimeError(f"face landmark model missing: {model}")
        _DET = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model)),
            output_facial_transformation_matrixes=True, num_faces=1))
    return _DET


def _segmenter():
    global _SEG
    if _SEG is None:
        from mediapipe.tasks.python import BaseOptions, vision
        model = resources.data_path("selfie_multiclass.tflite")
        if not model.exists():
            raise RuntimeError(f"segmentation model missing: {model}")
        _SEG = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=str(model)), output_category_mask=True))
    return _SEG


# selfie_multiclass class ids
SEG_BG, SEG_HAIR, SEG_BODY, SEG_FACE, SEG_CLOTHES, SEG_OTHER = range(6)

# Intermediates from the last build, kept only so a diagnostic can look at them without the
# pipeline having to hand them back through its return value. Nothing reads this in the app.
_DBG = {}


def segment(img):
    """Per-pixel class map for a photograph: hair / face skin / body skin / clothing / background.

    This is the one thing the landmarks cannot tell us. A face fit knows where a face is; it has
    no opinion at all about where the hair ends and the arena begins, and the builder used to
    substitute an ellipse fitted to the face oval for that opinion. The face oval stops at the
    forehead, so the ellipse stopped just past the hairline and half the hair in every reference
    fell outside it (48% of the segmented hair pixels kept on the Boeser set, 56% on Makar) — and
    the half it cut was the crown, the temples and the swept sides, the half no other source can
    supply. Those came from the base head, recoloured, however much hair the references showed.

    It also answers headwear directly, which two image statistics could not: the cap in his set
    comes back as CLOTHING covering the scalp, 1.3% hair, rather than as an outlier in some
    brightness distribution that a brightly-lit blond head can also produce.
    """
    import mediapipe as mp
    from PIL import Image
    if not isinstance(img, Image.Image):
        img = Image.open(img)
    a = np.ascontiguousarray(np.array(img.convert("RGB")))
    r = _segmenter().segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=a))
    return np.squeeze(r.category_mask.numpy_view()).astype(np.uint8)


def landmarks(img, raw=False):
    """478 (x, y) landmarks in PIXELS for a PIL image. Raises if no face is found.

    raw=True instead returns {'norm': 478x3 normalised xyz, 'matrix': 4x4 head pose} — what
    face_shape needs to fuse several photographs of the same head into one 3D cloud."""
    import mediapipe as mp
    from PIL import Image
    if not isinstance(img, Image.Image):
        img = Image.open(img)
    img = img.convert("RGB")
    # Retry upscaled when the detector comes up empty. It is trained on square crops a few hundred
    # pixels across and its recall falls off both below that and at extreme yaw, and the two
    # compound: the single best Boeser reference — a near-full-profile bench shot, 669x582, the only
    # one that shows his jawline and the whole sweep of his hair — was rejected outright at native
    # size and detects cleanly at 2x. A reference that never reaches the pipeline is the most
    # expensive kind of failure there is, because nothing downstream can tell it was ever there.
    # The landmarks come back NORMALISED, so a scaled copy needs no correction on the way out.
    for k in (1, 2, 3):
        q = img if k == 1 else img.resize((img.width * k, img.height * k), Image.LANCZOS)
        r = _detector().detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                        data=np.ascontiguousarray(np.array(q))))
        if r.face_landmarks:
            break
    if not r.face_landmarks:
        raise ValueError("no face detected in that image")
    lm = r.face_landmarks[0]
    if raw:
        mats = getattr(r, "facial_transformation_matrixes", None)
        return {"norm": np.array([[l.x, l.y, l.z] for l in lm], np.float32),
                "matrix": np.array(mats[0]) if mats else np.eye(4)}
    return np.array([[l.x * img.width, l.y * img.height] for l in lm], np.float32)


# ── geometry helpers ─────────────────────────────────────────────────────────
def _hull(pts, scale=1.0):
    c = pts.mean(0)
    return np.int32(_cv2().convexHull(np.float32(c + (pts - c) * scale)))


def _ring(pts, idx, scale):
    """The face oval pushed out from its centroid — control points OUTSIDE the face so the warp has
    a defined domain past the jaw and brow and the blend seam never lands on an undefined triangle."""
    c = pts[list(idx)].mean(0)
    return c + (pts[list(idx)] - c) * scale


def _procrustes(src, dst):
    """src carried onto dst by the best similarity transform (scale + rotation + translation)."""
    sc, dc = src.mean(0), dst.mean(0)
    s0, d0 = src - sc, dst - dc
    U, _S, Vt = np.linalg.svd(s0.T @ d0)
    R = (U @ Vt).T
    scale = (d0 * (s0 @ R.T)).sum() / max((s0 ** 2).sum(), 1e-9)
    return (src - sc) @ R.T * scale + dc


def piecewise_warp(src_img, src_pts, dst_pts, out_wh):
    """Warp src_img so src_pts land on dst_pts. Returns (RGB array, coverage mask)."""
    cv2 = _cv2()
    from scipy.spatial import Delaunay
    from PIL import Image
    src = np.array(src_img.convert("RGB")) if isinstance(src_img, Image.Image) else src_img
    W, H = out_wh
    out = np.zeros((H, W, 3), np.uint8)
    cov = np.zeros((H, W), np.uint8)
    for a, b, c in Delaunay(dst_pts).simplices:
        d = np.float32([dst_pts[a], dst_pts[b], dst_pts[c]])
        s = np.float32([src_pts[a], src_pts[b], src_pts[c]])
        x, y, w, h = cv2.boundingRect(d)
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
        if x1 <= x0 or y1 <= y0:
            continue
        M = cv2.getAffineTransform(s, d - np.float32([x0, y0]))
        patch = cv2.warpAffine(src, M, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REFLECT)
        m = np.zeros((y1 - y0, x1 - x0), np.uint8)
        cv2.fillConvexPoly(m, np.int32(d - np.float32([x0, y0])), 255, cv2.LINE_AA)
        sl = (slice(y0, y1), slice(x0, x1))
        out[sl][m > 0] = patch[m > 0]
        cov[sl][m > 0] = 255
    return out, cov


def face_mask(pts, wh, oval_scale=1.05, feather=12, eye_feather=3, keep_eyes=True):
    """Feathered face-oval blend mask with the eye sockets punched back OUT.

    The sockets are punched AFTER the blur, not before: holes that small do not survive a blur at
    face-seam sigma, and the symptom is the photo's open eyes pasted over the base map's empty
    sockets — which the game then draws a separate eyeball in front of."""
    cv2 = _cv2()
    W, H = wh
    m = np.zeros((H, W), np.uint8)
    cv2.fillConvexPoly(m, _hull(pts[list(FACE_OVAL)], oval_scale), 255)
    m = cv2.GaussianBlur(m, (0, 0), feather).astype(np.float32) / 255.0
    if keep_eyes:
        for eye in (L_EYE, R_EYE):
            hole = np.zeros((H, W), np.uint8)
            cv2.fillConvexPoly(hole, _hull(pts[list(eye)], 1.30), 255)
            m *= 1.0 - cv2.GaussianBlur(hole, (0, 0), eye_feather).astype(np.float32) / 255.0
    return m


# ── colour helpers (LAB: L = lightness, a/b = chroma) ────────────────────────
def _lab(img):
    return _cv2().cvtColor(img, _cv2().COLOR_RGB2LAB).astype(np.float32)


def _unlab(lab):
    return _cv2().cvtColor(np.clip(lab, 0, 255).astype(np.uint8), _cv2().COLOR_LAB2RGB)


def _soft_lift(L, add, knee=LIFT_KNEE):
    """Additive shift of an L channel that does NOT flatten the highlights. Both arguments are on
    the 0..255 scale OpenCV's LAB uses, so a real L of 80 is 204 here.

    ⭐ MEASURED FAULT, fixed here. Every level term in this file used to end in `np.clip(L + d, 0,
    255)`, and on a light head that ceiling is not a safety rail, it is the answer. A blond buzz cut
    asks the hair level stage for a large positive shift — Pettersson's scalp measured +12.8 L
    open-loop before the closed loop had said anything at all — and a plain clip prints every texel
    the shift pushes over 255 as the SAME 255. The scalp came out at L 85.8 with p95 97.3: not
    bright hair, a flat white plate with the grain, the key from above and the strand detail all
    quantised away, because the top of the distribution is where all three of those live.

    The region mean lands on its target either way, which is why no check downstream ever caught
    this: a mean cannot see that the spread above it has been eaten.

    So compress instead of clipping. Below the knee nothing moves at all — the shift is exactly the
    additive one the caller asked for, and the crater notes elsewhere in this file about gradients
    inside a mask still hold. Above it the remaining headroom is spent through a tanh, which is
    monotone and asymptotic, so the ordering and the relative spacing of the highlights survive and
    nothing ever actually reaches the ceiling."""
    y = np.asarray(L, np.float32) + np.asarray(add, np.float32)
    top = 255.0 - float(knee)
    over = y > knee
    if top > 1.0 and np.any(over):
        y = np.where(over, knee + top * np.tanh((y - knee) / top), y).astype(np.float32)
    return np.clip(y, 0.0, 255.0)


def _soft_unlift(L, add, knee=LIFT_KNEE):
    """The exact inverse of `_soft_lift`, so a level step can be BACKED OUT of a map that other
    stages have written to since — undoing through the same mask rather than by restoring a copy.
    Only inexact where `_soft_lift` hit the 0 floor, which a positive lift never does."""
    x = np.asarray(L, np.float32)
    a = np.asarray(add, np.float32)
    top = 255.0 - float(knee)
    y = x
    over = x > knee
    if top > 1.0 and np.any(over):
        y = np.where(over, knee + top * np.arctanh(np.clip((x - knee) / top, 0.0, 0.999)),
                     x).astype(np.float32)
    # ⚠ arctanh runs away near the ceiling — a texel some later stage pushed to 254 inverts to 400,
    # and after the subtraction it clips back to white. That printed as a hard white BAND along the
    # hairline, at exactly the texels the lift had put closest to the top. Undoing a lift can only
    # ever move a texel DOWN by at most what was put in, so say so: it bounds the runaway without
    # touching the well-conditioned interior, where the inverse is exact.
    return np.clip(y - a, np.minimum(x, x - a), np.maximum(x, x - a))


def _structure_tensor(L, rho):
    """The smoothed outer product of the gradient. Its major eigenvector says which way an edge
    faces and its eigenvalue split says how strongly the neighbourhood agrees on that - which is
    the only thing that separates a head of hair from noise of the same contrast."""
    cv2 = _cv2()
    gx = cv2.Sobel(L, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(L, cv2.CV_32F, 0, 1, ksize=3)
    return (cv2.GaussianBlur(gx * gx, (0, 0), rho), cv2.GaussianBlur(gy * gy, (0, 0), rho),
            cv2.GaussianBlur(gx * gy, (0, 0), rho))


def _coherence(L, rho=4.0):
    """(l1 - l2) / (l1 + l2): 0 is isotropic mush, 1 is perfectly combed."""
    Jxx, Jyy, Jxy = _structure_tensor(L, rho)
    return np.sqrt((Jxx - Jyy) ** 2 + 4 * Jxy * Jxy) / np.maximum(Jxx + Jyy, 1e-6)


def _flow(L, rho=4.0):
    """Unit vector ALONG the strand - the structure tensor's MINOR eigenvector, since a strand is
    the direction in which the image changes least."""
    Jxx, Jyy, Jxy = _structure_tensor(L, rho)
    t = 0.5 * np.arctan2(2 * Jxy, Jxx - Jyy) + np.pi / 2
    return np.cos(t).astype(np.float32), np.sin(t).astype(np.float32)


def _lic(x, vx, vy, taps=4, step=1.0):
    """Line integral convolution: average `x` along its own flow field. Streaks a field that is
    oriented but incoherent into continuous lines without adding anything to it."""
    cv2 = _cv2()
    ys, xs = np.mgrid[0:x.shape[0], 0:x.shape[1]].astype(np.float32)
    acc, wsum = x.copy(), 1.0
    px, py, nx, ny = xs.copy(), ys.copy(), xs.copy(), ys.copy()
    for i in range(1, taps + 1):
        w = float(np.exp(-0.5 * (i / (taps * 0.6)) ** 2))
        px += vx * step
        py += vy * step
        nx -= vx * step
        ny -= vy * step
        acc += w * (cv2.remap(x, px, py, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
                    + cv2.remap(x, nx, ny, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT))
        wsum += 2 * w
    return acc / wsum


def _mean_lab(img, mask):
    sel = mask > 0.5
    return _lab(img)[sel].reshape(-1, 3).mean(0) if sel.sum() else np.zeros(3, np.float32)


def _lab_shift(img, mask, d):
    lab = _lab(img)
    for ch in range(3):
        lab[..., ch] += d[ch] * mask
    return _unlab(lab)


def relight(warped, base, mask, delight=0.85, sigma=40):
    """Sit the photo at the base map's exposure while keeping the player's complexion. The shipped
    maps are near-flat de-lit albedo and a headshot is studio-lit, so L is matched outright; a/b are
    matched only partway (0.6 of the photo kept) or everyone ends up the base head's skin tone.

    `delight` additionally replaces the photo's LOW-FREQUENCY lightness with the base map's. A global
    mean/std match cannot remove a studio key light — it only recentres it — so the forehead and nose
    highlights survive and read as blown-out plastic once the map is lit again in-engine. Matching the
    blurred L instead keeps only the photo's fine detail (pores, stubble, creases) and takes the broad
    shading from the de-lit base, which is the thing that was actually authored for this lighting.
    """
    cv2 = _cv2()
    w, b = _lab(warped), _lab(base)
    sel = mask > 0.5
    if sel.sum() < 100:
        return warped
    out = w.copy()
    for ch, keep in ((0, 0.0), (1, 0.6), (2, 0.6)):
        ws, bs = w[..., ch][sel], b[..., ch][sel]
        out[..., ch] = ((w[..., ch] - ws.mean()) * (bs.std() / max(ws.std(), 1e-3)) + bs.mean()) \
            * (1 - keep) + w[..., ch] * keep
    if delight > 0:
        lo_w = cv2.GaussianBlur(out[..., 0], (0, 0), sigma)
        lo_b = cv2.GaussianBlur(b[..., 0], (0, 0), sigma)
        out[..., 0] += (lo_b - lo_w) * delight
    return _unlab(out)


def skin_hair_masks(base, face_m):
    """(skin, hair) over the whole base map. The unwrap puts hair in the top corners and down the
    sides and skin everywhere else, and the shipped hair is always markedly darker than the shipped
    skin, so a lightness split keyed off the face's own median separates them without hand-painting."""
    cv2 = _cv2()
    lab = _lab(base)
    L, a = lab[..., 0], lab[..., 1]
    ref = np.percentile(L[face_m > 0.5], 50) if (face_m > 0.5).sum() else 170.0
    hair = ((L < ref * 0.72) & (a > 118)).astype(np.float32)
    hair = cv2.GaussianBlur(cv2.morphologyEx(hair, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)),
                            (0, 0), 4)
    hair = np.clip(hair * (1 - face_m), 0, 1)
    skin = cv2.GaussianBlur(np.clip((L > ref * 0.62).astype(np.float32) - hair, 0, 1), (0, 0), 6)
    return skin, hair


# ── the haircut library ──────────────────────────────────────────────────────
# The fill below already works from an exemplar rather than from noise: nobody photographs the back
# of a head, so the crown and the nape take the BASE head's own painted hair and only have its
# colour moved onto the measured one. That is the right idea and it was only ever given one
# exemplar — the head the player happens to sit on. Head 138's artist painted a short, high,
# receding cut, so every player built on 138 got that cut whatever their photographs showed: bare
# temples, exposed ears, and a fill region carrying somebody else's parting.
#
# 447 heads ship, every one of them hand-painted by the artists in this identical unwrap. So the
# question is not "synthesise hair" but "which shipped head is already wearing this haircut", and
# that is a lookup, not a model. Scored against the segmenter's own hair vote — which is in UV and
# registered, so the two masks are directly comparable — head 138 places 116th of 447 for Boeser at
# IoU 0.715 against the best match's 0.815.
#
# The scan decodes every head, so it is cached the way the facial-hair catalogue is: keyed on the
# asset set, because these are shipped assets and the launcher never rewrites a head it is not
# editing.
HAIR_CACHE_VERSION = 1
_HAIRCUTS: dict = {}


def _haircut_cache_path():
    import os
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or ""
    base = Path(appdata) / "NHL2K10 Mod Launcher" if appdata else Path.home()
    base.mkdir(parents=True, exist_ok=True)
    return base / "haircut_catalogue.npz"


def haircut_catalogue(game_dir=None, log=None, force=False):
    """{'ids', 'hair' (packed bits, one 512x512 mask per head), 'strand'} over every shipped head.

    Roughly nine minutes cold, because each head is a separate archive decode; then it is a file.
    """
    cv2 = _cv2()
    key = str(game_dir)
    if not force and key in _HAIRCUTS:
        return _HAIRCUTS[key]
    try:
        from .char_model import head_ids
    except ImportError:
        from char_model import head_ids
    ids = list(head_ids(game_dir))
    p = _haircut_cache_path()
    if not force and p.exists():
        try:
            z = np.load(p)
            if int(z["version"]) == HAIR_CACHE_VERSION and list(z["ids"]) == ids:
                out = {"ids": np.asarray(z["ids"]), "hair": np.asarray(z["hair"]),
                       "strand": np.asarray(z["strand"])}
                _HAIRCUTS[key] = out
                return out
        except Exception:
            pass
    if log:
        log(f"  haircut catalogue: reading {len(ids)} shipped heads, once — this takes a few "
            f"minutes and is then cached")
    Z = np.zeros((UV, UV), np.float32)
    keep, packed, strand = [], [], []
    for hid in ids:
        try:
            a = np.asarray(base_maps(hid, game_dir)["color"].convert("RGB"), np.uint8)
        except Exception:
            continue
        if a.shape[:2] != (UV, UV):
            a = cv2.resize(a, (UV, UV), interpolation=cv2.INTER_AREA)
        _, h = skin_hair_masks(a, Z)
        m = h > 0.5
        L = _lab(a)[..., 0]
        keep.append(hid)
        packed.append(np.packbits(m))
        strand.append(float(np.abs(cv2.GaussianBlur(L, (0, 0), 0.8)
                                   - cv2.GaussianBlur(L, (0, 0), 3.0))[m].mean()) if m.any() else 0.0)
    out = {"ids": np.asarray(keep, np.int32), "hair": np.asarray(packed, np.uint8),
           "strand": np.asarray(strand, np.float32)}
    try:
        np.savez_compressed(p, version=HAIR_CACHE_VERSION, ids=np.asarray(ids, np.int32), **out)
    except Exception:
        pass
    _HAIRCUTS[key] = out
    return out


def haircut_donor(want, conf, base_id, game_dir=None, log=None):
    """The shipped head whose painted haircut best matches what the photographs saw.

    `want` is the segmenter's hair vote in UV and `conf` says where it may be believed. Scored on
    IoU over the believable texels, with the exemplar's own strand energy as the tiebreak — a cut
    that matches the silhouette but is painted as a flat mass is no use as an exemplar. Returns the
    base head unless another head is CLEARLY better, so this can only ever help a build that the
    base was already wrong for.
    """
    try:
        cat = haircut_catalogue(game_dir, log=log)
    except Exception:
        return int(base_id), None
    ok = conf > 0.5
    tgt = (want > 0.5) & ok
    if int(ok.sum()) < 20000 or int(tgt.sum()) < 4000:      # not enough seen to judge a cut by
        return int(base_id), None
    n = UV * UV
    best, bscore, bs_base = int(base_id), -1.0, -1.0
    for i, hid in enumerate(cat["ids"]):
        m = np.unpackbits(cat["hair"][i])[:n].astype(bool).reshape(UV, UV)
        inter = float((m & tgt).sum())
        union = float(((m | tgt) & ok).sum())
        s = inter / max(union, 1.0) + 0.010 * float(min(cat["strand"][i], 9.0))
        if int(hid) == int(base_id):
            bs_base = s
        if s > bscore:
            bscore, best = s, int(hid)
    if bscore < bs_base + 0.02:
        return int(base_id), None
    try:
        img = base_maps(best, game_dir)["color"].convert("RGB")
    except Exception:
        return int(base_id), None
    if log:
        log(f"  haircut: head {best} is wearing this player's cut more closely than the base head "
            f"{int(base_id)} ({bscore:.3f} against {bs_base:.3f}) - taking the fill's hair from it")
    return best, np.asarray(img, np.uint8)


def hair_color(photo, p_lm):
    """Mean colour of the band just above the hairline in the reference — the player's hair. The
    brightest 30% is dropped so background and rim light don't wash it out."""
    ph = np.array(photo.convert("RGB"))
    top = p_lm[list(FACE_OVAL)][:, 1].min()
    x0, x1 = int(p_lm[:, 0].min()), int(p_lm[:, 0].max())
    y1 = int(max(top - (p_lm[[10]][:, 1].mean() - top) * 0.05, 0))
    y0 = int(max(y1 - (p_lm[:, 1].max() - top) * 0.32, 0))
    band = ph[y0:y1, x0:x1].reshape(-1, 3).astype(np.float32)
    if len(band) < 50:
        return None
    return band[band.mean(1) < np.percentile(band.mean(1), 70)].mean(0)


# ── the build ────────────────────────────────────────────────────────────────
SEAM_SIGMA = 2.0                     # texels the seam correction is spread over
SEAM_PASSES = 3                      # diffusion passes; see heal_seams
SEAM_MAX_ANGLE = 30.0                # a pair whose two lips face further apart than this is not a
                                     # seam, it is two surfaces passing close by; see _seam_pairs


@functools.lru_cache(maxsize=8)
def _seam_pairs(base_id, game_dir=None, size=UV):
    """Texels that are the SAME point on the head but far apart on the sheet. -> (sel, me, pair, ys, xs)

    An unwrap has to cut the surface open to lay it flat, and this head's cut runs down the back of
    the skull. The two lips of that cut are neighbours on the head and strangers on the sheet, so
    nothing in the projection makes them agree — each is filled from whichever photograph happened to
    see it — and they end up painted differently. That difference is the hard vertical line down the
    back of every head this builds.

    Found by 3D position rather than by hunting for edges in the image, so it cannot mistake a real
    dark strand of hair for a seam: for each used texel, the nearest texels in SPACE are checked, and
    any that lands on the same point of the skull (within 0.12 cm) while sitting more than 12 texels
    away on the sheet is its opposite lip.

    Proximity alone is not enough, though, because two surfaces can be close together without being
    the same surface. On this head that catches the MOUTH: the upper and lower lip are a hair apart
    in space and half the sheet apart in UV, so they pair up and a heal would average them into each
    other and wipe out the lip line. They give themselves away by facing opposite directions, so the
    pair is kept only when the two lips of the cut agree on which way the surface points — measured,
    that drops 27% of the raw pairs, and the ones it drops are concentrated exactly where the mouth
    sits on the sheet.
    """
    from scipy.spatial import cKDTree
    pos, nrm, msk, _model = uv_geometry(base_id, game_dir, size=size)
    sel = msk > 0
    yy, xx = np.mgrid[0:size, 0:size]
    ys, xs = yy[sel], xx[sel]
    P3 = pos[sel]
    uv = np.column_stack([xs, ys]).astype(np.float64)
    d, i = cKDTree(P3).query(P3, k=12)
    cand = (np.linalg.norm(uv[i] - uv[:, None, :], axis=2) > 12) & (d < 0.12)
    has = cand.any(1)
    me = np.nonzero(has)[0]
    pair = i[me, np.argmax(cand[me], 1)]
    N = nrm[sel]
    N = N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)
    keep = (N[me] * N[pair]).sum(1) > np.cos(np.radians(SEAM_MAX_ANGLE))
    return sel, me[keep], pair[keep], ys, xs


def heal_seams(img, base_id, game_dir=None, sigma=SEAM_SIGMA, passes=SEAM_PASSES):
    """Make both lips of the unwrap's cut agree, without leaving a new edge where the repair ends.

    Snapping the two lips to their average closes the line but leaves a one-texel cliff between each
    lip and its own neighbour, which is a thinner seam rather than no seam. So the correction is
    treated as a field: the per-texel delta is planted on the seam texels, diffused inward by a
    NORMALISED blur (normalised so it falls off smoothly instead of dragging in unpainted texels),
    and added — then repeated, because one pass only pulls each lip part of the way.

    sigma 2.0 x 3 comes from a sweep on head 138 scored two ways at once, since the seam step alone
    cannot tell a closed seam from a displaced one: seam step fell 8.9 -> 3.5 L mean and 34.1 -> 15.0
    at p99, while the gradient at p99 in a band around the seam — where a displaced cliff would show
    — went DOWN, 56.3 to 55.5. Tighter sigma scores better on the seam and worse on the gradient,
    which is exactly the trade that says it is moving the edge rather than removing it.

    This applies to the NORMAL map too, which was not the plan. The worry was that a normal map's
    texels are directions in a per-texel tangent frame and the frames on the two lips of a cut need
    not agree, so averaging raw RGB across one is not obviously the same statement as averaging
    colour. Measured on head 138 it is: with the pairs gated as _seam_pairs gates them, the two lips'
    tangent directions sit a median 17 deg apart, small enough that the average still points at the
    surface. Its step is worth closing on its own terms — 10.2 mean out of 255, as big as the colour
    step standing next to it, and a normal step is a shading step.

    HONEST LIMIT, because the numbers here are easy to over-read. What this fixes is the maps: on a
    head 138 build the colour step falls 12.0 -> 3.1 and the normal step 10.2 -> 7.3. What it does
    NOT fix is the line still visible down the back of the head in the launcher's preview, which was
    the reason for writing it. That line was chased to ground and it is not in the maps: rendering
    the same head with a flat grey texture and no normal map at all puts the step at 0.00 L, with a
    perturbation-free neutral normal map at 3.03 L, and with the real one at 7.96 L — and healing,
    padding the unwrap's border, or planting a correction field 160 texels wide all leave the
    rendered step at 8.0-8.2 L, unmoved. Something in how the preview rasterizer carries the frame
    across the wrap at u = 1 accounts for it, not the texels, and that is not fixed here.
    """
    import cv2
    from PIL import Image
    try:
        sel, me, pair, ys, xs = _seam_pairs(base_id, game_dir)
    except Exception:
        return img                       # no geometry -> nothing to heal, leave the map untouched
    if not len(me):
        return img
    size = sel.shape[0]
    src = img.convert("RGB")
    A = np.asarray(src if src.size == (size, size) else src.resize((size, size)), np.float64)
    k = max(int(sigma * 4) | 1, 3)
    for _ in range(int(passes)):
        C = A[sel]
        tgt = 0.5 * (C[me] + C[pair])
        D = np.zeros((size, size, 3))
        W = np.zeros((size, size))
        for idx in (me, pair):
            np.add.at(D, (ys[idx], xs[idx]), tgt - C[idx])
            np.add.at(W, (ys[idx], xs[idx]), 1.0)
        Db = cv2.GaussianBlur(D, (k, k), sigma)
        Wb = cv2.GaussianBlur(W, (k, k), sigma)
        A = np.clip(A + Db / np.maximum(Wb, 1e-6)[..., None] * (Wb > 1e-4)[..., None], 0, 255)
    out = Image.fromarray(A.astype(np.uint8))
    return out if out.size == img.size else out.resize(img.size)


def base_maps(base_id, game_dir=None):
    """{'color','normal','occlusion'} -> PIL RGB, decoded from a shipped head."""
    nm = HEAD_FMT.format(base_id)
    return {r["label"]: A.decode_record(nm, r, game_dir).convert("RGB")
            for r in A.list_textures(nm, game_dir)}


# ── multi-view projection ────────────────────────────────────────────────────
def uv_geometry(head_id, game_dir=None, size=UV, positions=None):
    """(position, normal, mask, model) rasterised into UV space from the head's face island.

    A texel of the colour map is a point on the head, and this says WHICH point and which way it
    faces. That turns texturing into projection: transform a texel's 3D position into a photograph
    and read the pixel. Everything the ring-extrapolated 2D warp could only guess at — ears, temples,
    the sides of the neck, the scalp — is then sampled from an actual photograph, and the normal
    decides which photograph gets a say.

    positions overrides the mesh vertices (pass face_shape's fitted ones so this describes the head
    you are actually building, not the base head).
    """
    try:
        from . import char_model as C
    except ImportError:
        import char_model as C
    asset = HEAD_FMT.format(int(head_id))
    b = C.blob(True, game_dir, asset)
    m = C.scan_models(b, asset)[0]
    M = C.read_model(b, m)
    if positions is not None:
        M = dict(M, pos=np.asarray(positions, np.float32))
    p = next(q for q in M["parts"] if q["mat"] == 0)
    lo, hi = p["first_vtx"], p["first_vtx"] + p["n_vtx"]
    P = M["pos"][lo:hi].astype(np.float64)
    uv = M["uv"][lo:hi].astype(np.float64)
    T = p["tris_idx"].reshape(-1, 3).astype(np.int64) - lo

    N = np.zeros_like(P)                              # recompute: the shipped normals belong to the
    fn = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])   # UNFITTED mesh
    for k in range(3):
        np.add.at(N, T[:, k], fn)
    N /= np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-12)

    cv2 = _cv2()
    pos = np.zeros((size, size, 3), np.float32)
    nrm = np.zeros((size, size, 3), np.float32)
    msk = np.zeros((size, size), np.uint8)
    px = np.column_stack([uv[:, 0] * (size - 1), uv[:, 1] * (size - 1)])
    # Barycentric, NOT flat-filled. A constant position per triangle makes every texel of that
    # triangle sample the same pixel of the photograph, and the map comes out visibly faceted.
    for a, b_, c in T:
        A, B_, Cc = px[a], px[b_], px[c]
        x0, x1 = int(np.floor(min(A[0], B_[0], Cc[0]))), int(np.ceil(max(A[0], B_[0], Cc[0])))
        y0, y1 = int(np.floor(min(A[1], B_[1], Cc[1]))), int(np.ceil(max(A[1], B_[1], Cc[1])))
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, size - 1), min(y1, size - 1)
        if x1 < x0 or y1 < y0:
            continue
        det = (B_[1] - Cc[1]) * (A[0] - Cc[0]) + (Cc[0] - B_[0]) * (A[1] - Cc[1])
        if abs(det) < 1e-9:
            continue
        yy, xx = np.mgrid[y0:y1 + 1, x0:x1 + 1].astype(np.float64)
        l0 = ((B_[1] - Cc[1]) * (xx - Cc[0]) + (Cc[0] - B_[0]) * (yy - Cc[1])) / det
        l1 = ((Cc[1] - A[1]) * (xx - Cc[0]) + (A[0] - Cc[0]) * (yy - Cc[1])) / det
        l2 = 1.0 - l0 - l1
        ins = (l0 >= -0.002) & (l1 >= -0.002) & (l2 >= -0.002)
        if not ins.any():
            continue
        L = np.dstack([l0, l1, l2])[ins]
        pos[y0:y1 + 1, x0:x1 + 1][ins] = L @ np.array([P[a], P[b_], P[c]])
        nrm[y0:y1 + 1, x0:x1 + 1][ins] = L @ np.array([N[a], N[b_], N[c]])
        msk[y0:y1 + 1, x0:x1 + 1][ins] = 255
    # Close the seams the rasteriser leaves — but NOT with cv2.dilate. Dilate takes the per-channel
    # MAX over the window, and these are SIGNED vector fields lying on a background of zeros, so a
    # boundary texel comes back as whichever channel happened to win against that zero. Measured on
    # head 138: the two columns just inside the map's u=0 and u=1 edges came out as (0,0,0) or a
    # pure (0,1,0) where every neighbour reads nz = -0.93. Those two columns ARE the back-of-head
    # centre line — the head unwrap is a cylinder cut down the middle of the back — so the artifact
    # drew a bright parting down the back of every head: the hair's crown key fired on a texel that
    # actually faces the nape, and the projection sampled a "position" that is not on the head.
    #
    # Average the valid neighbours instead — sum(x*m)/sum(m), so texels outside the island abstain
    # rather than vote zero. Correct for a position and correct for a direction, where a max is
    # neither.
    k = np.ones((3, 3), np.float32)
    for _ in range(2):
        hole = msk == 0
        if not hole.any():
            break
        vm = (msk > 0).astype(np.float32)
        w = cv2.filter2D(vm, -1, k, borderType=cv2.BORDER_CONSTANT)
        take = hole & (w > 0.5)
        for arr in (pos, nrm):
            s = cv2.filter2D(arr * vm[..., None], -1, k, borderType=cv2.BORDER_CONSTANT)
            arr[take] = (s / np.maximum(w, 1e-6)[..., None])[take]
        msk = np.where(take, np.uint8(255), msk)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=2, keepdims=True), 1e-12)
    return pos, nrm, msk > 0, M


_SCALP_SHELL = 16384                    # facial_hair's slot bit for the scalp shell


def _has_scalp_shell(head_id, game_dir=None):
    """Does this head carry hair as GEOMETRY above the skull, or only as paint on it?

    ⭐ MEASURED, and it settles a fault that survived three rebuilds of one head.

    The hair-level machinery in `build_multi` aims the albedo so that the RENDER, not the map,
    matches the hair-minus-face the photographs measure. That is right, and on a head with a scalp
    shell it works: the shell is hair-shaped geometry, the renderer lights it as hair, and the
    albedo is the only free variable left. HAIR_RENDER_SHADE is the measured size of the gap.

    On a head with NO shell the same reasoning inverts. There is nothing above the skull, so the
    region the probe calls hair is the top of the skull, lit as skull. Measured on slot 319 with a
    flat grey albedo, the renderer puts that region 15.8 L UNDER the face — not because of hair, but
    because the crown turns away from the key. And the target, for a blond buzz cut, runs the other
    way: his own portrait puts his scalp 4.1 L ABOVE his face, because a shaved blond head catches
    the light his face does not. So the loop is asked to close twenty L by paint, on a surface where
    a unit of albedo buys 0.24 of a unit of what it is measuring — and the only way to get there is
    to paint the scalp white. That is exactly what shipped: a chalk plate with the base head's
    strand relief showing through it.

    Twenty L of shading is not a colour error and cannot be corrected with colour. On these heads
    the honest albedo is the de-lit one the photographs give, and the residual belongs to the
    renderer. So: report which kind of head this is, and let the level stages stand down.
    """
    try:
        from . import char_model as C, facial_hair as FH
    except ImportError:
        import char_model as C, facial_hair as FH
    try:
        a = C.HEAD_FMT.format(int(head_id))
        b = C.blob(False, game_dir, a)
        M = C.read_model(b, C.scan_models(b, a)[0])
        return _SCALP_SHELL in set(FH.hair_slots(M))
    except Exception:
        return True                     # unknown: behave the way this file always has


def mesh_occlusion(M, pos_uv, nrm_uv, uv_mask, strength=1.0, coarse=128, log=None):
    """Ambient occlusion computed from the FITTED mesh, rasterised into UV space. -> float HxW, 0..1.

    WHY THIS EXISTS. The occlusion map was passed straight through from the base head, unchanged,
    while the mesh underneath it was being reshaped by face_shape and the colour map rebuilt from
    photographs. So the one channel that describes where the head is CONCAVE described a different
    head. It cost nothing while the preview light pointed down the lens — a head-on light has almost
    no occlusion to reveal — and the moment the preview got a raking key (char_model.HEAD_LIGHT) the
    gap showed: under the jaw the shipped map reads 202 against 251 at mid-cheek, a gentle ambient
    falloff where a mandible should have a crease, and the terminator was doing all the work.

    HOW. Not ray tracing. The occluders are the mesh's own vertices, each standing for its share of
    the surface — a disc of area A (a third of each adjacent triangle, the usual split) facing along
    the vertex normal. A texel at p with normal n sums the form factor of every such disc:

        occ = SUM  A * max(0, n.v) * max(0, -nq.v) / (pi * d^2 + A)      v = (q - p)/d

    which is Bunnell's point-cloud occlusion. Both cosines matter and neither is optional: the first
    is why a texel is not occluded by something lying in its own plane, the second is why the far
    side of the skull — which faces away — contributes nothing, so no distance cutoff has to be
    guessed at. Single bounce, so concavities come out a little too dark; that is the direction to
    err in for a crease and the normalisation below takes the level back out.

    It runs on a `coarse` grid and is scaled up. AO is a low-frequency quantity by construction and
    the fine detail in this channel — nostrils, the lip line, the ear's inner folds — is the
    artist's, already in the shipped map, and is preserved by multiplying rather than replacing.

    WHAT COUNTS AS AN OCCLUDER. Every permanent part of the head asset, which crucially includes the
    shoulders and the collar yoke: most of what darkens a neck is the body it sits on. The hair
    shells do NOT count. They are optional geometry the build turns on and off (facial_hair), and a
    beard's shadow baked into the AO of a head that is then rendered clean-shaven is a smear under
    the chin with nothing casting it.

    NORMALISED, NOT ABSOLUTE. An exposed texel should come out at 1.0 and leave the shipped map
    alone; only the concave part of this is wanted. So the open-surface level is measured off the
    map itself (the 90th percentile over the island) and divided out. That also makes the result
    independent of how many vertices the head happens to have.
    """
    cv2 = _cv2()
    try:
        from . import facial_hair as FH
    except ImportError:
        import facial_hair as FH

    P = np.asarray(M["pos"], np.float64)
    shells = set(FH.hair_slots(M))
    tris = [p["tris_idx"].reshape(-1, 3) for p in M["parts"]
            if len(p["tris_idx"]) and int(p["lod"]) not in shells]
    if not tris:
        return np.ones(uv_mask.shape, np.float32)
    T = np.concatenate(tris).astype(np.int64)

    e1, e2 = P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]]
    fn = np.cross(e1, e2)
    fa = 0.5 * np.linalg.norm(fn, axis=1)
    A = np.zeros(len(P))
    N = np.zeros_like(P)
    for k in range(3):
        np.add.at(A, T[:, k], fa / 3.0)
        np.add.at(N, T[:, k], fn)
    keep = A > 1e-9
    Q, Aq = P[keep], A[keep]
    Nq = N[keep] / np.maximum(np.linalg.norm(N[keep], axis=1, keepdims=True), 1e-12)

    # the receivers: the UV island, decimated
    s = max(1, int(round(uv_mask.shape[0] / float(coarse))))
    pc = pos_uv[::s, ::s].reshape(-1, 3).astype(np.float64)
    nc = nrm_uv[::s, ::s].reshape(-1, 3).astype(np.float64)
    mc = uv_mask[::s, ::s].reshape(-1)
    ch, cw = uv_mask[::s, ::s].shape
    occ = np.zeros(len(pc))
    idx = np.flatnonzero(mc)
    # Lift the receiver off its own surface, or the discs it is made of occlude it: at d -> 0 the
    # form factor goes to A/(pi*d^2 + A) -> 1 and every texel comes out black.
    eps = 0.02 * float(np.sqrt(np.median(Aq)))
    for a in range(0, len(idx), 512):                    # chunked: the full outer product is ~1 GB
        j = idx[a:a + 512]
        v = Q[None, :, :] - (pc[j] + nc[j] * eps)[:, None, :]
        d2 = np.einsum("ijk,ijk->ij", v, v) + 1e-9
        inv = 1.0 / np.sqrt(d2)
        cp = np.maximum(np.einsum("ijk,ik->ij", v, nc[j]) * inv, 0.0)
        cq = np.maximum(-np.einsum("ijk,jk->ij", v, Nq) * inv, 0.0)
        occ[j] = (Aq[None, :] * cp * cq / (np.pi * d2 + Aq[None, :])).sum(1)

    ao = np.exp(-strength * occ).reshape(ch, cw).astype(np.float32)
    ref = float(np.percentile(ao.reshape(-1)[mc], 90)) if mc.any() else 1.0
    ao = np.clip(ao / max(ref, 1e-6), 0.0, 1.0)
    ao[~mc.reshape(ch, cw)] = 1.0
    ao = cv2.resize(ao, uv_mask.shape[::-1], interpolation=cv2.INTER_CUBIC)
    ao = cv2.GaussianBlur(ao, (0, 0), max(1.5, 0.6 * s))
    ao = np.where(uv_mask, np.clip(ao, 0.0, 1.0), 1.0).astype(np.float32)
    if log:
        log(f"  geometric AO: {len(Q)} occluder discs, "
            f"darkest {100 * float(ao[uv_mask].min()):.0f}% of open surface")
    return ao


def _beard_weight(rgb, lm, face, drop=10.0, span=25.0):
    """Where a photograph's LOWER FACE is materially darker than its upper face — i.e. the beard.

    Deliberately not a segmentation: MediaPipe labels a beard SEG_FACE, correctly, so the class map
    cannot see one. What separates a beard from a shadow is not its shape but WHERE it is allowed to
    be — beards grow below the nose and shadows do not care — so the zone comes from the landmarks
    and only the darkness is measured. `drop`/`span` in L: 10 below the upper-face level counts for
    nothing, 35 below counts fully, so an evenly lit clean-shaven jaw scores zero and this becomes a
    no-op for the players who do not have one.

    Returns a 0..1 weight, blurred, for `_flatten` to exclude from its lighting estimate.
    """
    cv2 = _cv2()
    p = np.asarray(lm, np.float32).reshape(-1, 2)
    L = _lab(rgb)[..., 0]
    nose, chin, top = p[1][1], p[152][1], p[10][1]
    h = max(abs(chin - top), 1e-3)
    yy = np.arange(L.shape[0], dtype=np.float32)[:, None] * np.ones((1, L.shape[1]), np.float32)
    # From above the nose down, carried a little past the chin so a beard under the jaw is included.
    # It has to START high: a jaw rises to its hinge, and the beard rises with it into the sideburn,
    # which sits level with the eyes — a zone opening at the nose misses that whole back stretch. The
    # darkness term below is what keeps the cheeks out, so the zone can afford to be generous.
    zone = np.clip((yy - (nose - 0.30 * h)) / (0.12 * h), 0.0, 1.0) * np.clip(
        (chin + 0.18 * h - yy) / (0.12 * h), 0.0, 1.0)
    # The REFERENCE band stays at the nose, though. Raising it with the zone put it on the forehead,
    # where hair hangs into the hull and reads dark, which drags `ref` down and so weakens the very
    # exclusion it feeds — measured as 4.2 L of beard lost before the blend even ran. Mid-face is the
    # right place to ask what unshadowed skin looks like.
    up = face * np.clip((nose - yy) / (0.10 * h), 0.0, 1.0)
    ref = float(np.median(L[up > 0.5])) if (up > 0.5).any() else float(np.median(L))
    dark = np.clip((ref - L - drop) / span, 0.0, 1.0)
    return cv2.GaussianBlur((zone * dark * face).astype(np.float32), (0, 0), 0.02 * h + 1.0)


# ── features a view can FAIL TO SEE ──────────────────────────────────────────────────────────────
# A beard and a pair of eyebrows are PRESENCE, not tone. Every other disagreement between two
# photographs of one man is lighting — that is the premise the consensus de-light is built on — but
# these two features break it, because a view turned far enough simply does not resolve them.
# Measured on the Pettersson pair: the frontal shot puts his brows 12.4 L under the upper face and
# his lower face 10.4 L under it; the 0.80-yaw shot scores 2.7 and 6.7 for the same man on the same
# day. Averaged, the brow arrives at about 7.5 — and the consensus pass gets there first, scoring
# the frontal shot's brow as a broad dark departure, i.e. as a SHADOW, and subtracting it.
# So measure per view how much of each feature that view actually resolves, and let that field both
# exempt the feature from the consensus and decide the blend. This is the same argument the hair
# fusion already makes one material up: fuse by evidence, never by average.
# ⚠ BOTH DEFAULT TO OFF, and the reasoning above is kept only because the MEASUREMENT is worth
# having, not because it worked. Built twice and measured both times against the averaged build:
#   coverage 46.9% (unmasked)  brow -7.5 -> -5.5 L,  beard -3.9 -> -3.1 L
#   coverage 12.4% (masked)    brow -7.5 -> -5.1 L,  beard -3.9 -> -2.7 L
# Fixing the obvious implementation bug made it WORSE, which is the signature of a wrong diagnosis
# rather than a wrong constant. The brows really are too light — the frontal photograph puts them
# at -12.4 L and the old build lands at -7.5 — but the blend is not where that is being lost, and
# `comb_brows` is the lever that is actually aimed at it. Do not re-enable these without a
# measured brow/beard delta showing an improvement.
FEATURE_SHARP = 0.0                  # softmax sharpness on feature evidence; 0 disables the biasing
FEATURE_KEEP = 0.0                   # how much of the consensus correction a fully-seen feature ducks

# What DID work: hand the brow / lash / beard back the darkness `flatten` takes off them. 0 disables.
FEATURE_RESTORE = 1.0
BROW_R_LM = (70, 63, 105, 66, 107, 55, 65, 52, 53, 46)
BROW_L_LM = (300, 293, 334, 296, 336, 285, 295, 282, 283, 276)
LASH_R_LM = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
LASH_L_LM = (362, 382, 381, 380, 374, 373, 390, 249, 263, 466, 388, 387, 386, 385, 384, 398)


def _brow_weight(rgb, lm, face, drop=6.0, span=20.0):
    """The eyebrow twin of `_beard_weight`: zone from the landmarks, weight from the darkness.

    Same reasoning and the same failure mode — the segmenter has no eyebrow class, and a brow is
    separated from a shadow by WHERE it is allowed to be. `drop`/`span` are lower than the beard's
    because a brow is a smaller, denser mark: 6 L under the forehead already counts for something.
    """
    cv2 = _cv2()
    p = np.asarray(lm, np.float32).reshape(-1, 2)
    L = _lab(rgb)[..., 0]
    h = max(abs(p[152][1] - p[10][1]), 1e-3)
    zone = np.zeros(L.shape[:2], np.float32)
    for idx in ((70, 63, 105, 66, 107, 55, 65, 52, 53, 46),
                (300, 293, 334, 296, 336, 285, 295, 282, 283, 276)):
        cv2.fillPoly(zone, [p[list(idx)].astype(np.int32)], 1.0)
    # Generous, like the beard zone: the darkness term is what keeps skin out, and a brow drawn
    # tight to its landmarks loses the soft outer third that is most of what reads as a brow.
    zone = np.clip(cv2.GaussianBlur(zone, (0, 0), 0.03 * h + 1.0) * 2.2, 0.0, 1.0)
    # Reference is the FOREHEAD — above the brows, below where hair hangs in.
    yy = np.arange(L.shape[0], dtype=np.float32)[:, None] * np.ones((1, L.shape[1]), np.float32)
    brow_y = float(np.median(p[[70, 63, 105, 300, 293, 334]][:, 1]))
    up = face * np.clip((brow_y - 0.04 * h - yy) / (0.06 * h), 0.0, 1.0) \
              * np.clip((yy - (p[10][1] + 0.06 * h)) / (0.06 * h), 0.0, 1.0)
    ref = float(np.median(L[up > 0.5])) if (up > 0.5).sum() > 50 else float(np.median(L))
    dark = np.clip((ref - L - drop) / span, 0.0, 1.0)
    return cv2.GaussianBlur((zone * dark * face).astype(np.float32), (0, 0), 0.015 * h + 1.0)


def _feature_evidence(rgb, lm, face):
    """How much of the presence-features (beard, brows) THIS view resolves. -> 0..1, image space."""
    return np.maximum(_beard_weight(rgb, lm, face), _brow_weight(rgb, lm, face))


BROW_CAP = 3.0                       # ceiling on the brow restore; the target is the base head's own
BROW_COMB = 1.0                      # how much of the strand band is streaked along its flow


GRAIN_CAP = 2.5                      # ceiling on the brow STRAND restore; the target is the photo's


def comb_brows(color, base_np, t_lm, cap=BROW_CAP, comb=BROW_COMB, rho=3.0, taps=3,
               grain_target=None, body_target=None):
    """Give the eyebrows the body the base head's have. -> the map, HxWx3 float.

    Two other suspects were measured and cleared first, because a brow that reads as a smudge above
    the eye has more than one plausible cause and only one of them was real.

      * the FLATTEN stage. It arrives out of the blend 27.0 L under the forehead, which is where the
        references put it, and leaves flatten at 13.8 — a stage eating half a brow is exactly the
        beard bug one feature up, so the brows were given the beard's exemption from the lighting
        vote. It moved 13.8 to 14.0. Nothing. What flatten takes out over a brow is the
        photographs' own brow-ridge shading, which is lighting and has no business in an albedo map.
      * the nine views DISAGREEING about where the brow is. Averaging photographs that each put the
        bar a few texels apart would cost a border without costing darkness, which is the symptom.
        Built the same head from one reference to check: the single-view brows measure a border
        gradient of 74/74 and 57/81 against the nine-view blend's 81/172. Multi-view is not the
        problem, it is carrying the brow better than any single photograph does.

    What is left is not a fault in a stage at all. A 512-texel photograph of a real eyebrow simply
    is soft — Boeser's own references measure 94 to 161 — and the brow the 2009 artist drew on this
    unwrap measures 180, because it is drawn rather than photographed. That is the same argument
    this file already makes about the nose and the hair: the base head is the calibration, not
    because it is a better likeness of anybody but because it is what the other 446 heads look like,
    and a face carrying less than all of them reads as a blob standing among them however accurate
    its colour is.

    So measure the brow as its DEVIATION from the skin level under it, held out of its own estimate,
    and put back the deficit against the artist's. Thickness and darkness are the same field here —
    a thin faint bar and a thick dark one differ by exactly that one number — so restoring it does
    both, and it cannot move the brow or change its shape, only how much of it there is.

    A FIELD rather than one number, because the two brows are not equally soft: five of Boeser's
    nine photographs sit past -35 degrees of yaw and two past +22, so one brow is seen at a grazing
    angle in most of them and comes out the weaker side. Smoothed over 8 texels the ratio still
    tells the two sides apart while absorbing the few texels by which the artist's brow and the
    landmarks disagree on this unwrap — his left brow measures a 4 L step across the landmark hull
    against his right brow's 29, so a per-texel ratio would be comparing our brow with his forehead
    half the time.

    The strand band is also streaked along its own flow, the same LIC the hair comb uses, since a
    brow is combed hair and nine averaged views leave it oriented but not continuous.
    """
    cv2 = _cv2()
    H, W = color.shape[:2]
    z = np.zeros((H, W), np.float32)
    for bi in (BROW_L, BROW_R):
        p = np.zeros((H, W), np.uint8)
        cv2.fillConvexPoly(p, _hull(np.asarray(t_lm, np.float64)[:, :2][bi], 1.35), 255)
        z = np.maximum(z, cv2.GaussianBlur(p, (0, 0), 3.0).astype(np.float32) / 255.0)
    sel = z > 0.5
    if not sel.any():
        return color, None
    lab = _lab(np.clip(color, 0, 255).astype(np.uint8))
    L = lab[..., 0].astype(np.float32)
    bL = _lab(base_np)[..., 0].astype(np.float32)

    def dev(x):
        """x minus the skin level under it, with the brow zone held out of that level."""
        w = (1.0 - z).astype(np.float32)
        return x - cv2.GaussianBlur(x * w, (0, 0), 10.0) / np.maximum(
            cv2.GaussianBlur(w, (0, 0), 10.0), 1e-4)

    dv, db = dev(L), dev(bL)
    k = np.clip(cv2.GaussianBlur(np.abs(db), (0, 0), 8.0)
                / np.maximum(cv2.GaussianBlur(np.abs(dv), (0, 0), 8.0), 0.25), 1.0, cap)

    # ⭐ …and now say how much of that to spend, in the PHOTOGRAPH's units rather than the artist's.
    # Everything above is a good FIELD and a bad LEVEL. The field is right because the two brows are
    # unequally soft and the base head knows which parts of a brow are dense; the level is wrong
    # because it is the 2009 artist's, and a drawn brow is drawn as dark as it takes to read at 512
    # texels. Measured on the finished heads: Boeser's portrait puts his brows 4.8 L under his face
    # mean and the render put them 11.2 under — six L of over-darkening, the same fault in the same
    # shape as the feature restore had, aimed at the asset instead of at the man.
    #
    # So keep the shape and rescale it bodily until the brow's mean body is what this player's own
    # photographs measure. Same ratio-of-two-identical-measurements the strand restore uses. It is
    # allowed to come out BELOW 1 here — unlike the strand restore, which can only ever have lost
    # detail to a resample, this one is demonstrably capable of overshooting, and refusing to let it
    # come back down is exactly how it got to 11.2 in the first place. Floored well above zero all
    # the same: a brow talked out of existence is worse than a dark one.
    if body_target is not None:
        _neg = sel & (dv < 0)
        _ours = float(np.abs(dv[_neg]).mean()) if _neg.any() else 0.0
        _have = float(k[sel].mean())
        if _ours > 0.25 and _have > 1.0:
            want = float(np.clip(body_target / _ours, 0.35, cap))
            k = np.clip(1.0 + (k - 1.0) * ((want - 1.0) / (_have - 1.0)), 0.35, cap)
    vx, vy = _flow(L, rho=rho)
    hp = cv2.GaussianBlur(L, (0, 0), 0.8) - cv2.GaussianBlur(L, (0, 0), 3.0)
    add = SOFT_KNEE * np.tanh((dv * (k - 1.0) + comb * (_lic(hp, vx, vy, taps=taps) - hp))
                              / SOFT_KNEE)
    lab[..., 0] = np.clip(L + add * z, 0, 255)

    # ── and the STRANDS, against the photographs rather than against the artist ───────────────
    # Everything above restores the brow's BODY: how dark the bar is against the skin under it.
    # That is thickness and darkness in one number, and it is not the thing that makes a brow read
    # as an eyebrow rather than a smudge — a solid bar of exactly the right darkness still reads as
    # a smudge. The QA scores the other quantity, the strand band, and measured on both finished
    # heads it sat at 3.22 and 2.35 against portraits carrying 3.93 and 3.84.
    #
    # There is no artist target for this and there should not be: the base head's brow is DRAWN, so
    # its strand band is a hand-made suggestion of hair at whatever scale the 2009 texel budget
    # allowed. The photographs of this player's own brow are the only honest source, and by the time
    # they reach UV they have been through a similarity warp, a joint alignment and a resample,
    # each of which is a mild low-pass. Measure the loss and undo exactly it — a ratio of two
    # identically-computed numbers, one from the photograph and one from the map, so it carries no
    # units and no assumption; 1.0 when we are already there and never below it.
    if grain_target is not None:
        Lb = lab[..., 0].astype(np.float32)
        hp_b = Lb - cv2.GaussianBlur(Lb, (0, 0), 3.0)
        mine = float(hp_b[sel].std())
        gb = float(np.clip(grain_target / max(mine, 1e-6), 1.0, GRAIN_CAP))
        lab[..., 0] = np.clip(Lb + SOFT_KNEE * np.tanh(hp_b * (gb - 1.0) / SOFT_KNEE) * z, 0, 255)
    else:
        mine = gb = 0.0

    ours = float(np.abs(dv[sel & (dv < 0)]).mean()) if (sel & (dv < 0)).any() else 0.0
    his = float(np.abs(db[sel & (db < 0)]).mean()) if (sel & (db < 0)).any() else 0.0
    return _unlab(lab).astype(np.float32), (ours, his, float(k[sel].mean()), float(k[sel].max()),
                                            mine, gb, body_target or 0.0)


def _flatten(rgb, cov, sigma=45, beard=None):
    """Strip a photograph's own lighting: divide out its low-frequency luminance over the covered
    area. What survives is albedo plus fine detail, which is the only part that should be shared
    between two photos shot in different rooms.

    `cov` weights where the lighting is MEASURED, not where it is corrected — the correction is
    applied to the whole frame. So it wants to name one material and only that one. Hand it the face
    and it reads the light off skin; hand it the whole head and a dark head of hair looks to it like
    a shadow, which it then dutifully brightens until the player is grey on top.

    A BEARD IS THE SAME BUG, INSIDE THE FACE MASK. The warning above is about hair and stops at the
    hairline, but a beard is head hair growing on the face: it is dark, it is broad, and it sits
    squarely inside the hull this is handed. At sigma 45 the field follows it, so it is read as a
    shadow over the jaw and brightened back to cheek level. Measured on Boeser, whose photographs put
    the jaw 59 L below the cheek: the built map kept 38 and `facial_hair.measure` then read the
    result and called a full beard "stubble". Nothing downstream can recover it, because by then the
    beard is gone from the pixels. So take the same exemption the hair gets — dark texels in the
    lower face do not vote on the light. They are still CORRECTED by it, like every other texel;
    they just no longer get to say what it is."""
    cv2 = _cv2()
    lab = _lab(rgb)
    L = lab[..., 0]
    w = cov.astype(np.float32)
    if beard is not None:
        w = w * (1.0 - np.clip(beard, 0.0, 1.0))
    num = cv2.GaussianBlur(L * w, (0, 0), sigma)
    den = cv2.GaussianBlur(w, (0, 0), sigma)
    lo = num / np.maximum(den, 1e-4)
    ref = float(np.median(L[w > 0.5])) if (w > 0.5).any() else 128.0   # skin's level, not skin+beard
    lab[..., 0] = np.clip(L - lo + ref, 0, 255)
    return _unlab(lab)


def build(ref_path, base_id, game_dir=None, shape=0.35, oval_scale=1.05, feather=12,
          recolor_hair=True, match_skin=True, warp_detail=True, keep_eyes=True, delight=0.85):
    """Build the three head maps from one reference headshot.

    ref_path      frontal headshot (the flatter the lighting the better)
    base_id       shipped head whose mesh/hair/ears the new face rides on
    shape         0..1, how far to move features toward the player's own proportions (see module doc)
    delight       0..1, how much of the photo's broad studio shading to replace with the base map's
                  (see relight). 0 keeps the headshot's key light baked into the albedo.
    warp_detail   carry the base normal + occlusion through the SAME landmark warp, so their pores,
                  creases and cavity shading stay under the features they belong to once `shape` has
                  moved them. Costs nothing when shape == 0 (the warp is then the identity).

    Returns {'color','normal','occlusion': PIL RGB, 'masks': PIL RGB debug, 'landmarks': ...}."""
    from PIL import Image
    maps = base_maps(base_id, game_dir)
    base = maps["color"]
    base_np = np.array(base)

    photo = Image.open(ref_path)
    if photo.mode == "RGBA":                       # headshots are often cut out on transparency
        bg = Image.new("RGB", photo.size, (255, 255, 255))
        bg.paste(photo, mask=photo.getchannel("A"))
        photo = bg
    photo = photo.convert("RGB")

    p_lm, t_lm = landmarks(photo), landmarks(base)
    aim = t_lm if shape <= 0 else t_lm + (_procrustes(p_lm, t_lm) - t_lm) * shape
    src = np.vstack([p_lm, _ring(p_lm, FACE_OVAL, 1.18), _ring(p_lm, FACE_OVAL, 1.45)])
    dst = np.vstack([aim, _ring(aim, FACE_OVAL, 1.18), _ring(aim, FACE_OVAL, 1.45)])

    warped, cov = piecewise_warp(photo, src, dst, base.size)
    m = face_mask(aim, base.size, oval_scale, feather, keep_eyes=keep_eyes) * (cov > 0)
    warped = relight(warped, base_np, m, delight=delight)

    out = base_np.astype(np.float32)
    skin, hair = skin_hair_masks(base_np, m)
    if match_skin:          # carry the complexion out to ears/neck/scalp so the jaw seam disappears
        d = (_mean_lab(warped, m) - _mean_lab(base_np, m)) * 0.85
        out = _lab_shift(out.astype(np.uint8), np.clip(skin - m, 0, 1), d).astype(np.float32)
    if recolor_hair:
        hc = hair_color(photo, p_lm)
        if hc is not None:
            hl = _lab(np.uint8([[hc]]))[0, 0]
            out = _lab_shift(out.astype(np.uint8), hair, (hl - _mean_lab(base_np, hair)) * 0.8) \
                .astype(np.float32)
    color = (warped.astype(np.float32) * m[..., None] + out * (1 - m[..., None])).astype(np.uint8)

    res = {"color": Image.fromarray(color)}
    for label in ("normal", "occlusion"):
        img = maps.get(label)
        if img is None:
            continue
        if warp_detail and shape > 0:
            # self-warp: the base map carried from its OWN landmark positions to the ones the colour
            # map now uses. Skipping this leaves creases and cavity shading behind the features.
            w2, c2 = piecewise_warp(img, np.vstack([t_lm, _ring(t_lm, FACE_OVAL, 1.18),
                                                    _ring(t_lm, FACE_OVAL, 1.45)]), dst, img.size)
            keep = (c2 > 0).astype(np.float32)
            img = Image.fromarray((w2 * keep[..., None] +
                                   np.array(img) * (1 - keep[..., None])).astype(np.uint8))
        res[label] = img
    res["masks"] = Image.fromarray(np.uint8(np.dstack([m, skin, hair]) * 255))
    res["landmarks"] = {"photo": p_lm, "base": t_lm, "aim": aim}
    return res


def deshine(rgb, skin, strength=DESHINE, sigma=24.0, dl=6.0, dc=4.0, log=None):
    """Take the arena's SPECULAR VEIL back out of the albedo. -> float32 RGB, same shape.

    ⭐ WE ARE NOT SHOOTING CROSS-POLARIZED, AND EVERY ALBEDO MAP SO FAR HAS PAID FOR IT. Production
    facial capture puts a polarizer on the light and a crossed one on the lens for exactly one
    reason: to keep specular reflection OUT of the diffuse map, because the renderer is going to add
    its own specular on top and a highlight painted into the albedo is a highlight that does not
    move when the head turns. It is a shiny patch glued to the forehead, and it is a large part of
    why a face built from press photographs reads as plastic rather than skin.

    Our references are arena and press photographs — uncontrolled, un-polarized, lit by whatever the
    building had. The pipeline already de-lights by CONSENSUS between views, and that catches what
    the photographs DISAGREE about; it is structurally blind to a sheen that sits on the same brow
    in most photographs of the same man under the same kind of lighting, because agreement is what
    consensus takes for truth.

    So key on the physics instead. A specular reflection is the light's own colour laid on top of
    the skin, so it raises L and DILUTES chroma in the same texel. That pairing is the signature,
    and it is what separates a sheen from genuinely pale skin — pale skin is bright WITHOUT being
    desaturated relative to its neighbourhood. Estimate both locally over skin only, and remove just
    the component that is above the local level and below the local chroma at once, putting back the
    chroma the veil diluted (take the shine off without restoring the colour and you have traded a
    plastic face for a grey one).

    Judged against the artist's shipped map for the same head rather than against itself — the
    99th-percentile L of each region, i.e. how far the bright tail runs past his — at strength 0.6:

        Makar   nose +20.4 -> +12.9    jaw +7.1 -> +2.0    chin +11.0 -> +9.4   forehead +7.5 -> +6.3
        Boeser  nose  +4.3 ->  +0.0    jaw +1.6 -> -0.4    cheeks  +0.0 -> -3.9

    every region moved toward his figure, hardest where the gap was worst, with region medians
    moving under 1.2 L — it takes the tail off and leaves the skin tone alone. Boeser's cheeks are
    the one overshoot, and it is present at every strength, so it is the detector firing on real
    highlights in his photographs rather than a strength that is too high.

    ⚠ This is measured on skin. `skin` must exclude the eyes: a sclera is bright and neutral by
    nature and is exactly what this would eat.
    """
    import cv2
    lab = cv2.cvtColor(np.clip(rgb, 0, 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    L, A, B = lab[..., 0], lab[..., 1] - 128.0, lab[..., 2] - 128.0
    w = np.asarray(skin, np.float32)
    den = np.maximum(cv2.GaussianBlur(w, (0, 0), sigma), 1e-6)
    loc = lambda x: cv2.GaussianBlur(x * w, (0, 0), sigma) / den          # noqa: E731
    Lm = loc(L)
    Ch, Cm = np.hypot(A, B), loc(np.hypot(A, B))
    sheen = np.clip((L - Lm) / 2.55 / dl, 0, 1) * np.clip((Cm - Ch) / dc, 0, 1) * w
    dL = strength * sheen * (L - Lm)
    out = lab.copy()
    out[..., 0] = L - dL
    # ⚠ SCALE the chroma back, do not pull it toward the local mean. A veil is neutral light laid on
    # top, so it dilutes chroma MULTIPLICATIVELY — every texel under it, including its neighbours,
    # is desaturated by roughly the same factor. Pulling toward the local mean therefore restores
    # nothing (the mean is diluted too); it just smooths, and it measurably made things worse:
    # Makar's cheek chroma went 22.6 -> 20.4 against the artist's 24.4, i.e. further away. Taking
    # 1 - dL/L of the light out has to put 1/(1 - dL/L) of the colour back. Capped, because the
    # ratio runs away where the veil is near-total.
    g = np.clip(L / np.maximum(L - dL, 1e-3), 1.0, 1.0 + 0.5 * strength)
    out[..., 1] = A * g + 128.0
    out[..., 2] = B * g + 128.0
    if log:
        log(f"  de-shine: specular veil removed from {100 * float((sheen > 0.05).mean()):.1f}% of "
            f"the map, up to {float((strength * sheen * (L - Lm)).max()) / 2.55:.1f} L")
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB).astype(np.float32)


def gloss_alpha(color, base_color, log=None):
    """Rebuild the colour map's ALPHA channel the way the artist derived theirs. -> PIL RGBA.

    ⭐ THE CHANNEL WE HAVE BEEN THROWING AWAY. `base_maps` decodes the shipped head with a
    .convert("RGB") and `install` re-encodes with another one, so every head this tool has ever
    written went into the game with a flat alpha. That channel is not empty on the artists' heads
    and it is not a cutout — measured on two shipped heads it holds all 255 values with std ~110,
    and viewed as an image it is unmistakable: black over the hair, ~250 over open skin, dark in the
    brows, the lashes, the nostrils and the lip line. On a 2009 console skin shader that is a
    specular / gloss mask, and it has exactly the right shape to be one — skin takes a highlight,
    hair and ink do not. Flattening it asks the engine to put skin gloss on a man's hair.

    It can be synthesised rather than warped across, because the artists plainly derived it from the
    albedo themselves. Regressed against the colour map's own L on the shipped heads:

        head 3040   pearson +0.961      head 0138   pearson +0.959

    monotone, saturating at ~250-255 over skin and 0 below L~60, with the whole transition between
    L 83 and 155. So take the artist's OWN curve — the median alpha in each L decile of their map —
    and run our L through it. That reproduces their relationship exactly while following OUR
    hairline, OUR brows and OUR lip line rather than dragging theirs onto a face that no longer has
    them in the same place, which is the same argument the hairline vote upstream is built on.
    """
    import cv2
    from PIL import Image
    if base_color is None:
        return color
    try:
        b = np.asarray(base_color.convert("RGBA"))
    except Exception:
        return color
    al = b[..., 3].astype(np.float32)
    if float(al.std()) < 4.0:                       # a flat alpha carries nothing to copy
        return color
    Lb = cv2.cvtColor(b[..., :3], cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    # the artist's curve, as a monotone lookup: median alpha per L bin, gaps filled by interpolation
    # and forced non-decreasing so a sparse bin cannot put a step in it.
    edges = np.arange(0, 257, 8, np.float32)
    xs, ys = [], []
    for i in range(len(edges) - 1):
        m = (Lb >= edges[i]) & (Lb < edges[i + 1])
        if m.sum() >= 64:
            xs.append(0.5 * (edges[i] + edges[i + 1]))
            ys.append(float(np.median(al[m])))
    if len(xs) < 4:
        return color
    curve = np.interp(np.arange(256, dtype=np.float32), xs, np.maximum.accumulate(ys))
    a = np.asarray(color.convert("RGB"), np.uint8)
    Lo = cv2.cvtColor(a, cv2.COLOR_RGB2LAB)[..., 0]
    out = curve[Lo]
    # the curve is a per-texel lookup, so it prints the albedo's own noise into the mask; a gloss
    # mask is a broad property of the surface, not a per-texel one. One texel of blur, no more --
    # the lip line and the lash line have to stay crisp or the highlight walks over them.
    out = cv2.GaussianBlur(out, (0, 0), 1.0)
    if log:
        log(f"  gloss mask: rebuilt from the artist's own L->alpha curve "
            f"(mean {out.mean():.0f}, {100 * float((out < 128).mean()):.0f}% of the map matte)")
    return Image.fromarray(np.dstack([a, np.clip(out, 0, 255).astype(np.uint8)]))


def match_relief(nrm, base_nrm, weight, region=None, scales=(2.0, 6.0), cap=1.8, log=None):
    """Restore the MICRO-RELIEF the warp costs, to the artist's own measured figure. -> PIL RGB.

    ⭐ WHY. Everything written about real-time skin agrees on one point, and it is the one this
    pipeline was quietly failing: what separates skin from painted plastic is high-frequency NORMAL
    breakup — pores, follicles, the fine crepe at the eye corners — because that is what breaks a
    specular highlight into something alive instead of a smooth sweep across a mannequin. It is
    carried in the normal, not the albedo, and no amount of albedo work substitutes for it.

    We had been losing it. Measured over the scored skin regions of both heads, our finished normal
    against the artist's shipped one for the same head:

        head 3040   pore ~2 texels  7.79 -> 6.08  (-21.9%)   crease ~6 texels  11.07 -> 9.41 (-15.0%)
        head 0138   pore ~2 texels  7.14 -> 5.51  (-22.8%)   crease ~6 texels  11.39 -> 9.13 (-19.8%)

    A fifth of the pore relief, gone, on a 2009 asset we are supposed to be dragging forward. The
    cause is not mysterious: the base normal is resampled through the landmark warp and a warp is a
    low-pass wherever it stretches, and `detail_normal` then rebuilds relief from the COLOUR map,
    which has been through the same warp and the same multi-view blend.

    So close the gap the same way every other term in this file closes one — measure it and correct
    it, rather than picking a sharpening number that looks nice. Decompose both maps into the same
    two bands, take the ratio of their energies over the region we actually authored, and put back
    exactly the shortfall. It cannot invent structure: it only restores the AMPLITUDE of relief that
    survived, so it is bounded by `cap` against amplifying resampling noise in a region that has
    almost nothing left. Z is left alone — it is reconstructed from XY by the shader.

    ⚠ `region` is where the shortfall is MEASURED and `weight` is where the correction is APPLIED,
    and they are deliberately not the same mask. Measured over the whole authored area this term
    reported x1.00 on both heads and moved nothing, twice — because that area includes the HAIR,
    where our map is deliberately more energetic than the artist's (the strand-contrast and comb
    stages put it there on purpose). Hair surplus cancelled skin deficit and the ratio came out at
    unity while skin was still a fifth short. Measure on skin; apply everywhere we authored.
    """
    import cv2
    from PIL import Image
    if nrm is None or base_nrm is None:
        return nrm
    A = np.asarray(nrm.convert("RGB"), np.float32)
    B = np.asarray(base_nrm.convert("RGB").resize(nrm.size), np.float32)
    m = np.asarray(weight if region is None else region, np.float32) > 0.5
    if m.sum() < 500:
        return nrm
    W = np.asarray(weight, np.float32)
    out, said = A.copy(), []
    for lo, hi in zip((0.0,) + tuple(scales[:-1]), scales):
        def band(X):
            f = X if lo <= 0 else cv2.GaussianBlur(X, (0, 0), lo)
            return f - cv2.GaussianBlur(X, (0, 0), hi)
        tot = 1.0
        # TWICE, and measured each time rather than doubled blind. One pass asked x1.27 on head
        # 3040's pore band to close 21.9% and delivered 6.9% short, because the gain is applied
        # through `weight` — feathered to 0 at the edge of the authored region — while the shortfall
        # is measured over the whole of it. Re-measuring after the first pass asks for exactly what
        # is still missing, and the cap applies to the PRODUCT so this cannot creep past it.
        for _ in range(2):
            for ch in (0, 1):
                eb = float(np.sqrt((band(B[..., ch])[m] ** 2).mean()))
                ea = float(np.sqrt((band(out[..., ch])[m] ** 2).mean()))
                g = float(np.clip(eb / max(ea, 1e-6), 1.0, cap / tot))
                out[..., ch] += (g - 1.0) * band(out[..., ch]) * W
                if ch == 0:
                    tot *= g
        said.append(f"{hi:.0f}tx x{tot:.2f}")
    if log:
        log(f"  micro-relief: restored to the artist's own figure ({', '.join(said)})")
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def detail_normal(color, base_normal, weight, bump=1.0, sigma=2.0, hair=None, chroma_gate=True,
                  hair_relief=1.0,
                  scales=((1.0, 1.0, 1.0), (3.0, 0.6, 0.6), (8.0, 0.35, 0.35), (20.0, 0.0, 0.28))):
    """Photographic relief folded into the shipped normal map. -> uint8 HxWx3 RGB.

    THE CONVENTION IS MEASURED, NOT ASSUMED. A normal map has four plausible axis conventions and
    picking wrong makes every pore and wrinkle pop inward. The shipped map was correlated against
    the shipped occlusion map — AO is a clean proxy for concavity, dark = in a crease — over its
    strongest gradients:

        corr(nx, dAO/dx) = -0.374      corr(nx, dAO/dy) = +0.004
        corr(ny, dAO/dy) = +0.366      corr(ny, dAO/dx) = +0.015

    Cross terms at zero say the axes are not swapped; the equal-and-opposite diagonal says the two
    axes have opposite handedness against the image grid. So, with h a height field and x/y the
    image axes (y increasing DOWN), this game stores `nx = -dh/dx, ny = +dh/dy`. The colour map's
    own luminance gradients give the same two signs independently (-0.309 / +0.345).

    The height field is the colour map's high-frequency LIGHTNESS. That is sound here for the same
    reason the flatten pass is: everything broad has already been divided out of this map, so what
    is left at high frequency is stubble, pores, lip texture and hair strands — surface, not
    lighting. Three things this does that a plain lightness high-pass does not, and each of them is
    a separate reason the old map came out flat:

    PIGMENT IS NOT SHAPE. Height from lightness alone cannot tell a pit from a freckle, so every
    mole and blemish was embossed as geometry while genuine soft creases got the same treatment as
    a birthmark. Shading is achromatic — a shadow darkens L and leaves a and b where they were —
    but pigment moves colour: melanin swings b, blood swings a. So measure the chromatic content of
    each high-frequency feature and damp the height by it. What survives is the achromatic part of
    the residual, which is the part that is actually a surface.

    RELIEF LIVES AT MORE THAN ONE SCALE. One Gaussian at sigma 2 captures pores and nothing else;
    the nasolabial fold, the philtrum, the brow ridge and the tendons of the neck are all bigger
    than that and were simply absent. Summing several bands with falling weight puts them back
    without letting the broadest one, which is really shading, dominate.

    HAIR IS NOT SKIN. Strands are the finest and highest-contrast structure on the head and the
    single-scale version smoothed them into a shell. Hair gets its own gain and its own per-band
    amplitudes — the third number in `scales` — and a band of its own at 20 texels, which is the
    scale of a LOCK and is past anything skin needs. Measured relief over the hair island went from
    7.02 to 21.69 when hair first got its own gain, and the lock band is why the crown stopped
    reading as a smooth dome with strands drawn on it.

    `weight` is where the photographs are trusted. Inside it the shipped map contributes only its
    LOW frequencies and the photographs supply the detail; outside it the shipped map is untouched,
    which is what keeps the artist's ear, crown and nape intact.
    """
    cv2 = _cv2()
    bn = np.asarray(base_normal, np.float32) / 127.5 - 1.0
    bz = np.maximum(bn[..., 2], 0.25)
    sx, sy = -bn[..., 0] / bz, bn[..., 1] / bz          # the base map, as a slope field

    lab = _lab(np.clip(np.asarray(color, np.float32), 0, 255).astype(np.uint8))
    L = lab[..., 0]

    # how chromatic is the local high-frequency content? achromatic => surface, coloured => pigment
    if chroma_gate:
        ca = lab[..., 1] - cv2.GaussianBlur(lab[..., 1], (0, 0), sigma)
        cb = lab[..., 2] - cv2.GaussianBlur(lab[..., 2], (0, 0), sigma)
        cmag = cv2.GaussianBlur(np.hypot(ca, cb), (0, 0), 2.0)
        lmag = cv2.GaussianBlur(np.abs(L - cv2.GaussianBlur(L, (0, 0), sigma)), (0, 0), 2.0)
        shape_w = np.clip(1.0 - 1.4 * cmag / np.maximum(lmag + cmag, 1e-3), 0.15, 1.0)
    else:
        shape_w = np.ones_like(L)

    hairm = np.zeros_like(L) if hair is None else np.clip(np.asarray(hair, np.float32), 0, 1)

    # ⭐ HOW MUCH HAIR IS THERE? The bands below, and the boost at `k`, were all sized on heads with
    # a full head of hair, and they are the reason a lock reads as volume instead of paint. On a
    # BUZZ CUT every one of them is inventing: measured on Pettersson, the lock band alone carved
    # combed ridges the length of the skull into the normal map of a man who is shaved, and it was
    # the loudest fault on the finished head. `hair_relief` is the ratio of what this player's
    # photographs carry at lock scale to what this map carries there, so a shaved head asks for
    # nothing and a long-haired one is unchanged. It scales only the bands ABOVE the strand scale —
    # stubble is real and is what a buzz cut actually has — and the hair boost with them.
    hr = float(np.clip(hair_relief, 0.0, 2.0))

    gx = np.zeros_like(L)
    gy = np.zeros_like(L)
    for sg, amp, hamp in scales:
        hp = L - cv2.GaussianBlur(L, (0, 0), sg * sigma / 2.0)
        # Skin and hair get their own amplitude per band, because they carry shape at different
        # scales. Skin's are folds — the nasolabial, the philtrum, the brow — and they run out by
        # about eight texels; anything broader on skin is the room and gets no weight at all.
        #
        # Hair used to be damped to 0.35 of skin's weight on every band past the finest, on the
        # reasoning that a strand is a one-texel feature and the wide bands over hair are the
        # parting and the key light. Half of that was right and it left the hair reading as paint
        # on a dome: under a raking preview light (char_model.HEAD_LIGHT) the crown had strand
        # detail and no volume, because a lock of hair is fifteen to twenty-five texels across on
        # this map and NOTHING in the band set reached that far. The old damping was also measured
        # in a build where the hair in the colour map was mostly retinted base-map paint, whose
        # broad bands genuinely were nothing; the segmenter now resolves 76% of the hair island
        # from photographs, so those bands are lock shadow and a real parting. Hence the fourth
        # band, hair-only at 20 texels, and the mid bands restored to skin's weight.
        a = amp * (1.0 - hairm) + (hamp if sg <= 1.0 else hamp * hr) * hairm
        gx += a * cv2.Sobel(hp, cv2.CV_32F, 1, 0, ksize=3) / 8.0
        gy += a * cv2.Sobel(hp, cv2.CV_32F, 0, 1, ksize=3) / 8.0
    gx *= shape_w * (1.0 - hairm) + hairm      # the chroma gate is a SKIN argument; hair is pigment
    gy *= shape_w * (1.0 - hairm) + hairm      # AND shape at once, and damping it flattens strands

    # Calibrate against the ARTIST's own relief rather than inventing a gain: a photograph's
    # lightness is in no particular units, so the only non-arbitrary scale available is "as much
    # relief as this head already has". bump is then a readable multiple of that.
    w = np.clip(np.asarray(weight, np.float32), 0.0, 1.0)
    n = max(float(w.sum()), 1.0)
    rms_b = float(np.sqrt(((sx ** 2 + sy ** 2) * w).sum() / n))
    rms_d = float(np.sqrt(((gx ** 2 + gy ** 2) * w).sum() / n))
    k = bump * NORM_BOOST * rms_b / max(rms_d, 1e-9)
    k = k * (1.0 + 1.2 * hr * hairm)                    # strands read at roughly twice skin relief

    sx += (cv2.GaussianBlur(sx, (0, 0), sigma) - sx) * w + k * gx * w
    sy += (cv2.GaussianBlur(sy, (0, 0), sigma) - sy) * w + k * gy * w

    nz = 1.0 / np.sqrt(1.0 + sx * sx + sy * sy)
    out = np.dstack([-sx * nz, sy * nz, nz]) * 127.5 + 127.5
    return np.clip(out, 0, 255).astype(np.uint8)


def build_multi(ref_paths, base_id, game_dir=None, positions=None, sharp=2.0, delight=0.9,
                flatten=0.85, fill_hair=True, chroma=0.7, bump=0.5, ao=1.0, mode="ship",
                log=print):
    """Build the three maps from SEVERAL photographs — the whole map, not a face patch.

    ref_paths     any number of photos of the same head at any angles; the more yaw spread the more
                  of the head gets real pixels. Photos with no detectable face are skipped.
    mode          "ship" (default) runs the curated ladder the A/B renders picked — see SHIP_SKIP
                  for what is held out and why. "full" is the complete ladder including the
                  render-measure-correct loop, kept for measurement.
    positions     fitted vertex positions from face_shape (so visibility describes the built head)
    sharp         exponent on the visibility weight. Higher = each texel comes from fewer photos
                  (crisper, but the joins between photos get harder).
    flatten       0..1, how much of the photographs' own broad shading to divide out. The colour
                  map wants to be flat albedo: the head ships a normal map and an occlusion map and
                  the engine lights it from those, so any shading baked in here is lit twice.
    chroma        0..1, how hard to low-pass the map's colour. Skin's real colour varies slowly;
                  the blotches a photo carries are the room, not the face.
    ao            0..2, how hard the FITTED mesh's own ambient occlusion is multiplied into the
                  shipped occlusion map. 0 ships the base head's map unchanged, which is what this
                  did before it had one. See mesh_occlusion.

    This is projection, not warping. Each texel knows its 3D point on the head (uv_geometry); a
    similarity fit recovered from the landmarks puts that point in each photograph's pixels; the
    texel reads the pixel it lands on. So ears, temples, jaw sides and scalp are SAMPLED rather
    than extrapolated, which the ring-padded 2D warp could never do honestly. The rigid fit still
    leaves features a few pixels off, so each photo is first pre-warped by that residual and is
    then exact at every landmark.

    There is no face oval and no composite here. Every texel is resolved the same way, which is why
    there is no jaw seam to blend: nothing is being pasted onto anything. Beyond the cameras' reach
    the last projected colour is pushed outward, and only past THAT does the measured skin/hair
    colour take over, so the map never steps.
    """
    from PIL import Image
    try:
        from . import face_shape as FS
    except ImportError:
        import face_shape as FS
    cv2 = _cv2()
    _skip = SHIP_SKIP if mode == "ship" else frozenset()

    maps = base_maps(base_id, game_dir)
    base = maps["color"]
    base_np = np.array(base)
    W, H = base.size
    t_lm = landmarks(base)

    views = FS.read_refs(ref_paths)
    if not views:
        raise ValueError("no reference photo produced a face detection")
    log(f"  {len(views)} views: " + ", ".join(f"{v['path'].name} (yaw {v['yaw']:+.0f})"
                                              for v in views))

    pos_uv, nrm_uv, uv_mask, M = uv_geometry(base_id, game_dir, size=H, positions=positions)
    mesh_lm = FS.mesh_landmarks(
        M, np.column_stack([t_lm[:FS.N_SKIN, 0] / (W - 1), t_lm[:FS.N_SKIN, 1] / (H - 1)]))
    flat_pos = pos_uv.reshape(-1, 3).astype(np.float64)

    # ── which texels are hair, which are skin ─────────────────────────────────
    # From the BASE map, and legitimately so: hair is geometry here, the unwrap is shared by every
    # head, and so the hairline sits in the same place in the map whoever is wearing it. Anything on
    # top of the skull is hair too, whatever the base map's colours made of it: that is where the
    # mask is least reliable (the artist's key light bleaches the crown towards skin) and it is also
    # the one place no eye-level camera reaches. The forehead is safe — it does not face upwards.
    face_m = np.zeros((H, W), np.uint8)
    cv2.fillConvexPoly(face_m, _hull(t_lm[list(FACE_OVAL)], 1.0), 255)
    skin_m, hair_m = skin_hair_masks(base_np, face_m.astype(np.float32) / 255.0)
    up = np.clip((nrm_uv[..., 1] - 0.30) / 0.30, 0.0, 1.0) * uv_mask
    hair_m = np.maximum(hair_m, up).astype(np.float32)

    # ── the ears, which need their own rules ──────────────────────────────────
    # An ear is the one part of a head that a global similarity fit cannot register: it is a small,
    # deep, self-occluding shell that every camera sees at a grazing angle, so a 1 mm error in the
    # fit lands a whole helix in the wrong place. AVERAGING several such views is what produced the
    # ghost ear — a second translucent helix inside the first — because each view drew the ear a
    # little offset from the last. One clean ear from the best-placed camera beats a consensus of
    # six. `ear` marks the region; the projection loop sharpens the view weights inside it so the
    # best view wins outright, and the fill is told this is skin, not hair (the base head's dark
    # concha reads as hair to the mask and printed an olive blob in the ear bowl).
    # mediapipe has no ear landmarks, but it has the tragus (127/356) and the eye corner (33/263),
    # and the ear hangs off the tragus by a fixed fraction of that distance on every human head.
    # TWO radii, and they are not interchangeable. The tight one says "this is an ear, not hair" and
    # sharpens the view weights. The wide one is the region handed to the artist wholesale — and it
    # has to be wide, because the misregistered helices do not stay inside the ear: the worst ghost
    # was a whole second helix smeared DOWN AND FORWARD onto the cheek, well outside any ellipse
    # drawn around the ear itself. Handing over only the ear left that ghost sitting next to a clean
    # ear, which reads worse than either. Eating a little cheek and hairline costs nothing: the base
    # map is the same unwrap, so what arrives there is a cheek and a hairline.
    ear = np.zeros((H, W), np.float32)
    ear_zone = np.zeros((H, W), np.float32)
    for tr, ey in ((127, 33), (356, 263)):
        d = float(abs(t_lm[ey][0] - t_lm[tr][0])) or 50.0
        out = -1.0 if t_lm[tr][0] < t_lm[ey][0] else 1.0
        c = (int(t_lm[tr][0] + out * 0.55 * d), int(t_lm[tr][1] + 0.35 * d))
        e = np.zeros((H, W), np.uint8)
        cv2.ellipse(e, c, (int(0.95 * d), int(1.55 * d)), 0, 0, 360, 255, -1)
        ear = np.maximum(ear, cv2.GaussianBlur(e, (0, 0), 0.10 * d).astype(np.float32) / 255.0)
        z = np.zeros((H, W), np.uint8)
        cz = (int(t_lm[tr][0] + out * 0.45 * d), int(t_lm[tr][1] + 0.45 * d))
        cv2.ellipse(z, cz, (int(1.45 * d), int(2.05 * d)), 0, 0, 360, 255, -1)
        ear_zone = np.maximum(ear_zone,
                              cv2.GaussianBlur(z, (0, 0), 0.20 * d).astype(np.float32) / 255.0)
    ear *= uv_mask
    ear_zone *= uv_mask
    hair_m = hair_m * (1.0 - 0.9 * ear)

    # No photograph of a hockey player shows his bare chest: below the chin the camera sees a
    # collar, a jersey or a mic. Fade projection out over the neck so that region is filled from
    # the measured skin instead of having a shirt printed on it.
    dn = mesh_lm[152] - mesh_lm[10]                     # chin - forehead: the head's own "down"
    face_h = float(np.linalg.norm(dn))
    dn /= max(face_h, 1e-9)
    below = ((pos_uv.reshape(-1, 3) - mesh_lm[152]) @ dn).reshape(H, W)
    torso = np.clip((0.15 * face_h - below) / (0.25 * face_h), 0.0, 1.0)

    # ── nothing below the jaw is hair ─────────────────────────────────────────
    # `skin_hair_masks` splits on lightness, and the base map paints the throat dark because it is
    # in shadow under the chin — so the whole underside of the neck came back flagged as hair. The
    # fill retints skin and hair by two different shifts and mixes them by this mask, so wherever
    # the mask has an edge the fill prints that edge: a wedge with a hard diagonal border down the
    # left of the neck, which is the "lines on the neck" the user saw. (Measured on head 3040: 27
    # levels of high-frequency contrast against the stock map's 4 over the same box.) On this unwrap
    # the nape sits well above the chin line, so there is no hair below it to lose.
    hair_m = hair_m * np.clip((0.02 * face_h - below) / (0.10 * face_h), 0.0, 1.0)
    hairy = np.clip(hair_m, 0, 1).astype(np.float32)

    # ── the dark features are FEATURES, not shadows ───────────────────────────
    # THIS IS HALF OF WHY THE MOUTH LOOKED BROKEN, and no amount of blending was going to fix it.
    # The darkness gate below throws out any skin sample darker than the view's median skin
    # lightness less 46, which is the right instinct for a collar or a cast shadow. But the mouth
    # APERTURE is darker than that in every photograph ever taken, so it was rejected in all views
    # at once, `have` fell to zero along the lip line, and the outward push filled the hole with a
    # flat grey bar. The shipped 2009 map has a crisp dark mouth line; ours had a smear. The same
    # argument holds for the nostrils and the lash line.
    #
    # We know exactly where those are: the base map's own landmarks, in this unwrap, in texels. So
    # carve them out and let the gate speak everywhere else. This is not a loosened threshold — the
    # gate keeps full strength over the neck and jaw where it earns its keep — it is a statement
    # that three small regions of a face are legitimately black.
    LIP_RING = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267,
                0, 37, 39, 40, 185]
    EYE_L_RING = [33, 160, 158, 133, 153, 144]
    EYE_R_RING = [362, 385, 387, 263, 373, 380]
    NOSE_RING = [98, 97, 2, 326, 327]
    feat_m = np.zeros((H, W), np.float32)
    for ring, grow in ((LIP_RING, 3), (EYE_L_RING, 2), (EYE_R_RING, 2), (NOSE_RING, 3)):
        poly = np.round(t_lm[ring, :2]).astype(np.int32)
        c = poly.mean(0)
        poly = np.round(c + (poly - c) * (1.0 + grow / 10.0)).astype(np.int32)
        cv2.fillPoly(feat_m, [poly], 1.0)
    feat_m = np.clip(cv2.GaussianBlur(feat_m, (0, 0), 2.5), 0, 1)
    # The APERTURE is a narrower claim than the exemption above and it needs its own mask. Behind
    # the lips there is no skin to photograph: the mouth interior is separate geometry, and this
    # island of the map only ever carried the artist's dark line. Opening the darkness gate over it
    # does not recover a mouth, it admits whatever the cameras found in the gap — teeth, in the
    # smiling views — and the bar went from grey to white. Measured p99 gradient went UP while the
    # crop got visibly worse, which is the metric rewarding a hard edge rather than a mouth. So no
    # photograph speaks here at all. The shipped line is both crisp and correct, and the mouth is
    # the one part of a face where 2009 already had it right.
    INNER_RING = [78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308, 415, 310, 311, 312,
                  13, 82, 81, 80, 191]
    aper_m = np.zeros((H, W), np.float32)
    cv2.fillPoly(aper_m, [np.round(t_lm[INNER_RING, :2]).astype(np.int32)], 1.0)
    aper_m = np.clip(cv2.GaussianBlur(aper_m, (0, 0), 1.5), 0, 1)
    # The aperture above is drawn from the BASE head's landmarks, and the 2009 base head has its
    # mouth SHUT — so it is a slit two texels tall. That is the right shape for the base head and the
    # wrong shape for a photograph taken mid-word: an open mouth's teeth and gap cover the entire
    # vermilion band, land well outside the slit, and print as a grey bar across the lower lip with a
    # notch where a tooth edge fell. The guard was never wrong, it was just too small to catch what
    # was actually coming through. The whole lip region, for the per-view veto below.
    lip_m = np.zeros((H, W), np.float32)
    _lp = np.round(t_lm[LIP_RING, :2]).astype(np.float64)
    _lp = np.round(_lp.mean(0) + (_lp - _lp.mean(0)) * 1.30).astype(np.int32)
    cv2.fillPoly(lip_m, [_lp], 1.0)
    lip_m = np.clip(cv2.GaussianBlur(lip_m, (0, 0), 3.0), 0, 1)
    # And the aperture is not the landmarker's inner ring either. THE ARTIST DREW A TALLER LINE
    # THAN MEDIAPIPE FINDS. Row by row down the mouth on this unwrap, the base head's dark line runs
    # y=296..311 while the inner ring closes at 304 — so seven rows of it fell outside the guard,
    # took the projection instead, and came out at chroma 15 against the artist's 29. That is the
    # flat grey band under the lip, and it survived the open-mouth veto unchanged because the
    # closed-mouth views put a lip-contact shadow there too: a real shadow, in the wrong place, five
    # texels below where this head's mouth actually closes.
    # The base map knows where its own mouth is. Ask it: inside the lips, anything the artist drew
    # clearly darker than the lips around it IS the aperture. A measurement on the asset we are
    # already deferring to, rather than a landmark ring borrowed from a different face.
    _lipreg = np.zeros((H, W), np.uint8)
    cv2.fillPoly(_lipreg, [np.round(t_lm[LIP_RING, :2]).astype(np.int32)], 1)
    _bL = _lab(base_np)[..., 0]
    if _lipreg.sum() > 100:
        _line = ((_lipreg > 0) & (_bL < np.median(_bL[_lipreg > 0]) - 15.0)).astype(np.uint8)
        # A per-texel threshold leaves HOLES, and a hole in this guard is exactly what the user
        # reported. The artist painted a few texels of tooth-gap highlight into the line itself, so
        # nine columns of it (measured: x 242-250) read lighter than the threshold, dropped out of the
        # guard, and took the projection's teeth instead — a pale smudge at the bottom corner of the
        # mouth with the rest of the line correct on either side of it. A mouth line is CONTINUOUS;
        # close the mask along its own axis before trusting it, and the hole closes with it.
        _line = cv2.morphologyEx(_line, cv2.MORPH_CLOSE, np.ones((3, 21), np.uint8)) & (_lipreg > 0)
        aper_m = np.maximum(aper_m, np.clip(cv2.GaussianBlur(
            _line.astype(np.float32), (0, 0), 1.5), 0, 1))
    log(f"  mouth aperture: {int((aper_m > 0.5).sum())} texels held to the artist's own line")
    log(f"  dark-feature exemption: {100 * (feat_m > 0.5).mean():.1f}% of the map "
        f"(mouth, nostrils, lash line) freed from the darkness gate")

    acc = np.zeros((H, W, 3), np.float32)
    wacc = np.zeros((H, W, 1), np.float32)
    hair_acc = np.zeros((H, W), np.float32)            # the segmenter's hair vote, in UV
    hair_wt = np.zeros((H, W), np.float32)
    seg_acc = np.zeros((H, W, 6), np.float32)          # diagnostic only; see _DBG
    shot, capped, agape, hair_ab, hair_lp = [], [], [], [], []
    ev_uv = []                     # per view, in UV: how much of the beard/brows this view resolves
    hair_fine, skin_fine, skin_mid, skin_grain, brow_grain = [], [], [], [], []
    brow_body, hair_fine_sd, hair_lock = [], [], []
    # focus is relative to the best photograph THIS man has, not to an absolute number: sharpness in
    # absolute terms depends on his lighting and his skin as much as on the lens, and the question
    # the weight has to answer is only ever "which of these do I believe more". See SHARP_FLOOR.
    fbest = max([float(v.get("focus", 0.0)) for v in views] + [1e-6])
    log("  reference quality: " + ", ".join(
        f"{v['path'].name[:18]} {v['face_px']:.0f}px/focus {float(v.get('focus', 0.0)) / fbest:.2f}"
        for v in views))
    for v in views:
        # mesh -> this photograph. read_refs built lm3 in PIXELS, so the fit lands straight in
        # image coordinates: an orthographic camera recovered from the landmarks themselves.
        n = len(mesh_lm)
        srt = FS._similarity(mesh_lm, v["lm3"][:n].astype(np.float64))
        pl = FS._apply(mesh_lm, srt)[:, :2].astype(np.float32)   # where the mesh thinks they are

        # The fit is rigid, so features still sit a few pixels off. Correct with a SMOOTH residual
        # field — landmark offsets splatted and blurred (Shepard), damped where no landmark is near.
        # A piecewise-affine warp would do it too, but its outer triangles are huge and it facets
        # the whole surround; this stays smooth and simply fades to zero away from the face.
        iw, ih = v["img"].size
        res, den = np.zeros((ih, iw, 2), np.float32), np.zeros((ih, iw), np.float32)
        off = (pl - v["lm"][:n]).astype(np.float32)
        cx = np.clip(np.round(pl[:, 0]), 0, iw - 1).astype(int)
        cy = np.clip(np.round(pl[:, 1]), 0, ih - 1).astype(int)
        np.add.at(res, (cy, cx), off)
        np.add.at(den, (cy, cx), 1.0)
        # The landmarks are DENSE — 468 of them, roughly 10-15 px apart on a head this size — so the
        # field can afford to be tight. It used to be 0.16 of the face, which smeared each point's
        # correction over a third of the face and gave up on exactly the places that need it: the
        # nose flanks, the eye corners, the mouth. Off the face the confidence damp still fades it
        # to zero, so tightening it costs nothing at the edges.
        sig = max(RESIDUAL_SIGMA * v["face_px"], 4.0)
        res = cv2.GaussianBlur(res, (0, 0), sig)
        den = cv2.GaussianBlur(den, (0, 0), sig)
        conf = np.clip(den / max(float(den.max()) * 0.25, 1e-6), 0, 1)
        field = res / np.maximum(den, 1e-6)[..., None] * conf[..., None]

        head = np.zeros((ih, iw), np.uint8)            # FACE only — see _flatten on why not the hair
        cv2.fillConvexPoly(head, _hull(v["lm"], 1.0), 255)
        headf = head.astype(np.float32) / 255.0
        photo = _flatten(np.array(v["img"]), headf,
                         beard=_beard_weight(np.array(v["img"]), v["lm"], headf))
        # …and, in image space still, how much of the presence-features this view resolves. It has
        # to be built here, from the ORIGINAL photograph, because `_flatten` has already begun
        # normalising the very darkness the measurement is made of.
        ev_img = _feature_evidence(np.array(v["img"]), v["lm"], headf)

        # The photograph's own head region. The depth buffer below rejects texels that fall outside
        # OUR head, which is no help at all when our head is the bigger of the two: the crown sits
        # above his hair and reads the wall, and the map came out with a band of arena signage
        # printed across the top of the scalp. This is the other side of that test — the region the
        # CAMERA agrees is head.
        #
        # This used to be an ellipse fitted to the face oval and shrunk to 0.80 x 0.82 of it, and it
        # was throwing away half the head. Mediapipe's face oval tops out at mid-forehead, so the
        # shrunk ellipse cut just past the HAIRLINE: measured against the segmenter, it contained
        # 48% of the hair pixels in the Boeser references and 56% in the Makar ones. And it was the
        # wrong half — it kept the fringe, which several views resolve, and cut the crown, the
        # temples and the whole swept side, which is the half nothing downstream can recover. A full
        # profile that shows the entire sweep of his hair contributed none of it.
        #
        # Ask the segmenter instead, which has an opinion about hair and the ellipse never did.
        # Hair + face skin + body skin, so the neck comes with it; clothing and background do not.
        # Then keep only the component the face is in — one Boeser reference is a two-shot and the
        # other man's head would otherwise be projected onto this one — erode a little, because the
        # segmenter's boundary pixel is a blend of hair and arena, and feather what is left.
        cls = segment(v["img"])
        reg8 = ((cls == SEG_HAIR) | (cls == SEG_FACE) | (cls == SEG_BODY)).astype(np.uint8)
        nlab, lab = cv2.connectedComponents(reg8)
        if nlab > 2:
            lx = np.clip(np.round(v["lm"][:, 0]), 0, iw - 1).astype(int)
            ly = np.clip(np.round(v["lm"][:, 1]), 0, ih - 1).astype(int)
            hit = lab[ly, lx]
            hit = hit[hit > 0]
            if len(hit):
                reg8 = (lab == np.bincount(hit).argmax()).astype(np.uint8)
        er = max(1, int(round(0.015 * v["face_px"])))
        reg8 = cv2.erode(reg8, np.ones((3, 3), np.uint8), iterations=er)
        if reg8.sum() < 0.2 * v["face_px"] ** 2:         # segmenter found nothing usable
            reg8 = np.zeros((ih, iw), np.uint8)
            (ecx, ecy), (aw, ah), ang = cv2.fitEllipse(_hull(v["lm"], 1.0).astype(np.float32))
            cv2.ellipse(reg8, (int(ecx), int(ecy)), (int(aw * 0.95), int(ah * 1.05)),
                        ang, 0, 360, 1, -1)
        reg = cv2.GaussianBlur(reg8.astype(np.float32), (0, 0), max(2.0, 0.02 * v["face_px"]))

        # …and while the segmenter is here, ask it what is ON the head. See the headwear note below.
        brow = float(v["lm"][list(L_EYE) + list(R_EYE)][:, 1].min())
        yy, xx = np.ogrid[:ih, :iw]
        box = ((yy < brow) & (yy > brow - 1.2 * v["face_px"])
               & (xx > v["lm"][:, 0].min()) & (xx < v["lm"][:, 0].max()))
        capped.append(float((cls[box] == SEG_OTHER).mean()) if box.sum() > 100 else 0.0)

        # …and what COLOUR his hair is, straight off the photograph, while the segmenter's answer is
        # still in hand. The crown is filled from a tone read off the projection, and the projection
        # only resolves hair at the hairline and the fringe — shaded strands, at the bottom of their
        # own tone range, where chroma has collapsed towards neutral. This is the honest measurement
        # of the same thing: a whole head of hair, in the frame the camera saw it, before any of it
        # is thrown away by a visibility test. Hue only; the LEVEL still comes from the projection,
        # which is de-lit and consistent with the rest of the map.
        hm8 = cls == SEG_HAIR
        hair_ab.append(np.median(_lab(np.asarray(v["img"], np.uint8))[hm8], 0)[1:]
                       if hm8.sum() > 500 else None)
        # …and, from the same pixels, how much LIGHTNESS RANGE his hair has. Not its level — that is
        # the projection's job and it is de-lit — but the spread from the lit crown to the shadowed
        # side. A head of hair is a self-occluding mass; the p10-to-p90 span across it is the single
        # number that separates hair from a painted helmet, and the unwrap cannot invent it. Measured
        # here because this is the only place a whole head of hair exists as hair, before visibility,
        # projection and the flatten pass each take their share of it.
        # …and, with it, the median lightness of the FACE in the same photograph, because the hair's
        # level is only meaningful against it. A photograph's absolute L is set by its exposure; the
        # same head of hair reads 60 under a flash and 95 in daylight, and neither number belongs in
        # a de-lit albedo. What survives exposure is hair MINUS face — the fourth number here.
        _pl = _lab(np.asarray(v["img"], np.uint8))[..., 0]
        _fm8 = cls == SEG_FACE
        # …and the same subtraction again over the FRONT of the head only, and only in views that
        # are actually looking at the front. A head of hair is not one tone: it runs from a lit
        # crown to a nape no camera ever sees, and measured on these two players the whole-mask
        # median and the front-of-head one disagree by 19 L on Boeser and 1 L on Makar — his is a
        # pale cut whose sides fall into shadow while the crown stays lit, hers is not. Anchoring a
        # blonde's fringe on a statistic half-made of his own shadowed nape is what made one head 13
        # L too light and the other 5 L too dark off the same code.
        #
        # Region and reference are defined exactly as a viewer's own eye defines them and exactly as
        # the head is scored: a box 0.95 eye-widths either side of the eye centre and 0.70 to 1.55
        # above it, minus the face oval, minus background — against the median inside that same
        # oval. Not the segmenter's face class, which draws a different boundary, and not the
        # segmenter's face class, which draws a different boundary. Within the box, hair only, on
        # both this side and the map side — the box also catches forehead and temple, and the level
        # correction can only move hair, so measuring something it cannot move would make it miss by
        # however much of the box is skin. And only from views within 25 degrees of frontal, because
        # past that the box has swung round onto the side of the head and is measuring a different
        # part of the man.
        _ecp = 0.5 * (v["lm"][33] + v["lm"][263])
        _ewp = float(np.hypot(*(v["lm"][263] - v["lm"][33])))
        _ovl = np.zeros((ih, iw), np.uint8)
        cv2.fillConvexPoly(_ovl, _hull(v["lm"][list(FACE_OVAL)], 1.0), 255)
        _fbox = ((yy >= _ecp[1] - 1.55 * _ewp) & (yy <= _ecp[1] - 0.70 * _ewp)
                 & (np.abs(xx - _ecp[0]) <= 0.95 * _ewp)
                 & (_ovl == 0) & (cls != SEG_BG) & hm8)
        _ok_f = abs(float(v["yaw"])) <= 25.0 and _fbox.sum() > 300 and (_ovl > 0).sum() > 500
        hair_lp.append(np.append(np.append(np.percentile(_pl[hm8], [10, 50, 90]),
                                           float(np.median(_pl[_fm8]))
                                           if _fm8.sum() > 500 else np.nan),
                                 [float(np.median(_pl[_fbox])),
                                  float(np.median(_pl[_ovl > 0]))] if _ok_f else [np.nan, np.nan])
                       if hm8.sum() > 500 else None)

        # …and how much STRAND DETAIL that hair carries, at the map's own scale. The contrast step
        # below restores detail towards the ARTIST's base map, which is a 2009 asset whose hair is a
        # painted shell; on Makar the finished head reads 3.56 on the high-pass against 7.79 in his
        # portrait, and no amount of chasing the artist closes that, because the artist never had it.
        # The photograph does. Resample it so a face is as many pixels wide as it is texels wide in
        # the unwrap, and the two band-passes are then measuring the same physical wavelength.
        # Eroded hard first: the outer band of the segmenter's hair is half background, and an edge
        # is the loudest thing a high-pass can find.
        _hf = _hfs = _hlk = None
        _core = cv2.erode(hm8.astype(np.uint8), np.ones((3, 3), np.uint8),
                          iterations=max(1, int(round(0.02 * v["face_px"]))))
        if _core.sum() > 500:
            _s = min(max(float(t_lm[:, 0].max() - t_lm[:, 0].min())
                        / max(v["face_px"], 1e-6), 0.15), 4.0)
            _ps = cv2.resize(_pl, (0, 0), fx=_s, fy=_s, interpolation=cv2.INTER_AREA)
            _cs = cv2.resize(_core, (0, 0), fx=_s, fy=_s, interpolation=cv2.INTER_NEAREST) > 0
            if _cs.sum() > 200:
                _hf = float(np.abs(_bp(_ps.astype(np.float32), 0.8, 3.0))[_cs].mean())
                # …and the STANDARD DEVIATION of the same band, which is the statistic that is
                # actually scored. This file already made this exact correction for skin and the
                # note there states the reason: mean |energy| and std are not interchangeable, and
                # std is the one that weights a few strong strands over the flat ground between
                # them. Hair was left on the mean and it is the region with the largest remaining
                # texture deficit anywhere — Makar's finished head reads 3.73 where his portrait
                # reads 7.79 — so it gets the same correction.
                _hfs = float(_bp(_ps.astype(np.float32), 0.8, 3.0)[_cs].std())
                # ⭐ …and the SAME measurement at LOCK scale, which is the one nothing was making.
                # `detail_normal` carries a hair-only band at 20 texels because that is the width of
                # a lock, and it is what stops long hair reading as paint on a dome. A BUZZ CUT has
                # no locks: it has stubble at the strand scale and a bare skull at every scale above
                # it. Nothing measured that, so the lock band fired on every head alike and carved a
                # full head of combed hair into the normal map of a man who is shaved. Measured on
                # the photographs, in the same map texel units as everything else here, so the band
                # can be asked for in proportion to what this player actually has.
                _hlk = float(np.abs(_bp(_ps.astype(np.float32), 8.0, 25.0))[_cs].mean())
        hair_fine.append(_hf)
        hair_fine_sd.append(_hfs)
        hair_lock.append(_hlk)

        # The same argument for SKIN, and the same 1.2-3.0 texel band the contrast restore uses.
        # That step also aims at the 2009 base head, and measured on the finished heads every skin
        # region lands at roughly two thirds of the portrait: brows 3.93 -> 3.02 and 3.84 -> 2.11,
        # mouth 5.67 -> 3.00 and 7.22 -> 5.37, chin 2.89 -> 1.97 and 2.16 -> 1.71. One deficit with
        # one cause — the target.
        _sf = _sm = _sg = None
        _score = cv2.erode(_fm8.astype(np.uint8), np.ones((3, 3), np.uint8),
                           iterations=max(1, int(round(0.03 * v["face_px"]))))
        if _score.sum() > 500:
            _s2 = min(max(float(t_lm[:, 0].max() - t_lm[:, 0].min())
                        / max(v["face_px"], 1e-6), 0.15), 4.0)
            _ps2 = cv2.resize(_pl, (0, 0), fx=_s2, fy=_s2, interpolation=cv2.INTER_AREA)
            _cs2 = cv2.resize(_score, (0, 0), fx=_s2, fy=_s2,
                              interpolation=cv2.INTER_NEAREST) > 0
            if _cs2.sum() > 200:
                _sf = float(np.abs(_bp(_ps2.astype(np.float32), 1.2, 3.0))[_cs2].mean())
                # …and the band ABOVE it, which is the one the eye is actually complaining about.
                # 1.2-3.0 texels is pores and stubble roots. A brow, a lash line, a lip line and a
                # nostril are 3-12 texels wide on this unwrap, and NOTHING in this pipeline restores
                # that band — measured on the finished heads, brows read 3.05 against the portrait's
                # 3.93 and 2.14 against 3.84, the mouth 3.37 against 5.67, the eyes 9.10 against
                # 11.16. All four are features, not grain, and all four live here.
                _sm = float(np.abs(_bp(_ps2.astype(np.float32), 3.0, 12.0))[_cs2].mean())
                # …and BELOW 1.2 texels, which the fine restore deliberately excludes on the grounds
                # that the base head's finest band is DXT artifact and ours is sensor noise. That
                # reasoning was sound about the TARGET and wrong about the BAND. Measured three ways
                # over the whole face — everything finer than 3 texels, all on the map's own scale —
                # the portrait carries 3.28 and 3.32, our map 2.49 and 2.17, and our RENDER of that
                # map 2.50 and 2.61. The render is faithful; the map is 24-35% short, and since the
                # 1.2-3.0 band is already at parity the whole shortfall is here. With a photograph of
                # this player setting the target the DXT objection no longer applies.
                # …and the SAME statistic, over the SAME band, that the restore will compare its own
                # map against: the STANDARD DEVIATION of everything finer than three texels. Mean
                # |energy| and std are not interchangeable here and the difference is the whole
                # story. By mean, the photograph reads 2.21 and our map 3.12 — we look ahead. By std
                # the photograph reads 3.28 and our map 2.49, and it is std that weights the strong
                # local features over the flat ground between them. That is precisely the shape of
                # the fault: our foreheads and cheeks carry MORE grain than the portraits and our
                # brows, lash lines and lip lines carry markedly less. Two numbers cannot be
                # compared unless they are the same number.
                _sg = float((_ps2.astype(np.float32)
                             - cv2.GaussianBlur(_ps2.astype(np.float32), (0, 0), 3.0))[_cs2].std())
        # …and the SAME statistic over the BROWS alone, which everything above deliberately erodes
        # away. A brow is not skin — it is combed hair three or four texels wide, the strongest
        # local feature on the upper face, and the one the QA scores hardest — so a target averaged
        # over cheek and forehead cannot speak for it. Measured per stage, the projection arrives
        # carrying 2.61 against the photographs' own brows and every pass after it ADDS, ending at
        # 2.84: the deficit is already there the moment the photograph lands in UV, so no amount of
        # re-weighting the blend reaches it. Nine views were tried against one to be sure — 2.61
        # against 2.70, so multi-view is not the cause either.
        #
        # `comb_brows` below already restores the brow's BODY, its darkness against the skin under
        # it, and restores it against the base head. That is a different quantity from this one and
        # it is why the brows can measure right for thickness and still read as a smudge: nothing in
        # this pipeline has ever had a STRAND target for them, from any source.
        _bg = _bb = None
        if _sf is not None:
            _bmask = np.zeros(_ps2.shape[:2], np.uint8)
            _plm = np.asarray(v["lm"], np.float64)[:, :2] * _s2
            for _bi in (BROW_L, BROW_R):
                cv2.fillConvexPoly(_bmask, _hull(_plm[_bi], 1.15), 255)
            if int((_bmask > 0).sum()) > 100:
                _bg = float((_ps2.astype(np.float32)
                             - cv2.GaussianBlur(_ps2.astype(np.float32), (0, 0), 3.0)
                             )[_bmask > 0].std())
                # ⭐ …and the BODY: how far the brow sits below the skin under it. `comb_brows` restores
                # exactly this quantity and it aims at the BASE HEAD's, on the argument that a 512-texel
                # photograph of a brow is soft and the artist's drawn one is the calibration the other
                # 446 heads share. That argument is about SHARPNESS and the stage spends it on DARKNESS,
                # because in that stage thickness and darkness are one number. Measured on the finished
                # heads the bill comes in: Boeser's portrait puts his brows 4.8 L under his face mean and
                # the render puts them 11.2 under. Six L of over-darkening, aimed at a 2009 asset instead
                # of at the man — the same fault, in the same shape, as the feature restore had.
                #
                # So measure the honest target here, the way the strand target is measured: the same
                # operator comb_brows uses, on the same 1.35 hull, over a photograph resampled so a face
                # is as many pixels wide as it is texels wide in the unwrap. Two identical measurements,
                # so the ratio of them carries no units and no claim about the artist's level.
                _bh = np.zeros(_ps2.shape[:2], np.uint8)
                for _bi in (BROW_L, BROW_R):
                    cv2.fillConvexPoly(_bh, _hull(_plm[_bi], 1.35), 255)
                _z2 = cv2.GaussianBlur(_bh, (0, 0), 3.0).astype(np.float32) / 255.0
                _w2 = (1.0 - _z2).astype(np.float32)
                _pf = _ps2.astype(np.float32)
                _dv2 = _pf - cv2.GaussianBlur(_pf * _w2, (0, 0), 10.0) / np.maximum(
                    cv2.GaussianBlur(_w2, (0, 0), 10.0), 1e-4)
                _s2b = (_z2 > 0.5) & (_dv2 < 0)
                if int(_s2b.sum()) > 100:
                    _bb = float(np.abs(_dv2[_s2b]).mean())
        skin_fine.append(_sf)
        skin_mid.append(_sm if _sf is not None else None)
        skin_grain.append(_sg if _sf is not None else None)
        brow_grain.append(_bg)
        brow_body.append(_bb)

        # …and how far open is his mouth in this one. Inner-lip gap over mouth width: scale-free,
        # and it reads the aperture itself rather than the pout, so a thick lower lip does not
        # register as a jaw drop. Measured over both reference sets — 13 photographs — the two
        # populations do not overlap and there is nothing in between them: shut reads .002-.033 and
        # speaking reads .122-.221. 0.06 is simply the middle of that gap.
        agape.append(float(np.linalg.norm(v["lm"][13, :2] - v["lm"][14, :2])
                           / max(np.linalg.norm(v["lm"][61, :2] - v["lm"][291, :2]), 1e-6)))

        xy = FS._apply(flat_pos, srt)[:, :2].astype(np.float32).reshape(H, W, 2)
        fx = cv2.remap(field, xy[..., 0], xy[..., 1], cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REPLICATE)
        sx = np.ascontiguousarray(xy[..., 0] - fx[..., 0])
        sy = np.ascontiguousarray(xy[..., 1] - fx[..., 1])
        smp = cv2.remap(photo, sx, sy, cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
        inside = ((sx > 1) & (sx < iw - 2) & (sy > 1) & (sy < ih - 2)).astype(np.float32)

        d = np.array([0.0, 0.0, -1.0]) @ srt[1]        # camera axis, back in mesh space
        d /= max(np.linalg.norm(d), 1e-12)
        # a grazing texel is at the silhouette, where half its footprint is background — the source
        # of the dark rim that otherwise wraps the ears and temples. Cut it off well before 90deg.
        w = np.clip(((nrm_uv * d).sum(2) - 0.22) / 0.78, 0.0, 1.0) ** sharp
        # size, and — new — SHARPNESS, which size does not imply. See SHARP_FLOOR.
        foc = SHARP_FLOOR + (1.0 - SHARP_FLOOR) * (float(v.get("focus", 0.0)) / fbest) ** SHARP_RAMP
        w *= inside * uv_mask * min(v["face_px"] / 220.0, 1.4) * foc * torso

        # ── visibility: does the camera actually SEE this texel? ──────────────
        # Facing the camera is not the same as being visible. The ear sticks out, and the head
        # behind it faces the camera just as squarely — both project onto the same photo pixels, so
        # without a depth test the ear gets printed twice, once on itself and once on the skull
        # behind it. That is the doubled, stretched ear. Scatter every texel's distance along the
        # camera axis into a depth buffer, keep the nearest, and drop whatever loses.
        dep = (flat_pos @ d).astype(np.float32).reshape(H, W)
        ix = np.round(sx).astype(np.int32).ravel()
        iy = np.round(sy).astype(np.int32).ravel()
        okz = (uv_mask.ravel() & (ix >= 0) & (ix < iw) & (iy >= 0) & (iy < ih))
        zbuf = np.full((ih, iw), -1e9, np.float32)
        np.maximum.at(zbuf, (iy[okz], ix[okz]), dep.ravel()[okz])
        # Close the scatter's pinholes ONLY. A plain dilate also pushes the nearest surface over
        # pixels that already had a correct depth, so the nose tip ends up shadowing its own flanks
        # and the frontal view — the one view that should own the middle of the face — gets rejected
        # there. Every texel then falls back to an oblique view, which reads the nostril instead:
        # that was the dark streak running down the cheek.
        zfill = cv2.dilate(zbuf, np.ones((3, 3), np.float32))
        zbuf = np.where(zbuf > -1e8, zbuf, zfill)
        near = cv2.remap(zbuf, sx, sy, cv2.INTER_NEAREST, borderMode=cv2.BORDER_REPLICATE)
        behind = near - dep                            # >0 = something nearer covers this texel
        w *= np.clip((VIS_FAR - behind) / (VIS_FAR - VIS_NEAR), 0.0, 1.0)

        # The same buffer doubles as the head's OUTLINE in this photograph, which is the honest way
        # to keep the room out of the map: a texel that lands outside the silhouette is looking at
        # the wall behind him. Eroded, because the fit is a few pixels off and the outermost band is
        # exactly where a miss costs the most.
        silh = (zbuf > -1e8).astype(np.uint8)
        er = max(1, int(round(0.02 * v["face_px"])))
        silh = cv2.erode(silh, np.ones((3, 3), np.uint8), iterations=er)
        silh = cv2.GaussianBlur(silh.astype(np.float32), (0, 0), max(2.0, 0.01 * v["face_px"]))
        w *= cv2.remap(silh, sx, sy, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
        w *= cv2.remap(reg, sx, sy, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)

        # ── texel footprint ───────────────────────────────────────────────────
        # How much photograph does one texel actually get? The Jacobian of the sampling map says so.
        # Where it collapses, a whole row of texels is reading the same few pixels and the result is
        # a smear — the stretched band around an ear, and the streaks where the residual field
        # folds back on itself. Judge it against this view's own median rather than an absolute,
        # since it scales with how big the head is in the frame.
        ax, ay = np.gradient(sx, axis=1), np.gradient(sy, axis=1)
        bx, by = np.gradient(sx, axis=0), np.gradient(sy, axis=0)
        foot = np.abs(ax * by - ay * bx)
        sel0 = w > 0.45
        if sel0.sum() > 500:
            med = float(np.median(foot[sel0]))
            w *= np.clip(foot / max(FOOT_MIN * med, 1e-9), 0.0, 1.0)
        w = cv2.GaussianBlur(w, (0, 0), 3)             # soften the joins between photographs

        # Colour plausibility, as a BACKSTOP for whatever survives the silhouette — a mic, a hand,
        # a jersey collar inside the outline. Loose on purpose: the tight version cut the eyes and
        # the lips out of every view, and the fill then dragged their dark pixels down the cheeks
        # as streaks. Only genuinely off-band colour is dropped now.
        sel = w > 0.45
        if sel.sum() > 500:
            ls = _lab(np.clip(smp, 0, 255).astype(np.uint8))
            ma, mb = np.median(ls[sel][:, 1]), np.median(ls[sel][:, 2])
            dab = np.hypot(ls[..., 1] - ma, ls[..., 2] - mb)
            w *= np.clip(1.0 - (dab - 34.0) / 20.0, 0.0, 1.0)

            # Hair is dark, so a BRIGHT sample in a hair texel is the room behind him. This is the
            # one leak the silhouette test cannot see: it rejects texels that fall outside OUR
            # head's outline, and the crown falls inside it and still misses his — our head is the
            # taller of the two. A wall is close enough to skin in chroma to pass the gate above,
            # which is how a pale blue-grey cap ended up printed over the back of the skull. So
            # calibrate on the hair this view actually resolved and throw out whatever is far
            # brighter than that.
            hsel = sel & (hairy > 0.5)
            if hsel.sum() > 300:
                mL = float(np.median(ls[..., 0][hsel]))
                w *= 1.0 - hairy * np.clip((ls[..., 0] - (mL + 22.0)) / 18.0, 0.0, 1.0)

            # And the mirror of it for skin: a sample far DARKER than the skin this view resolved
            # is not skin. Under the jaw every photograph of a hockey player has a shirt collar, a
            # cast shadow and often a mic, all of them the same neutral grey the chroma gate above
            # lets through, and all of them with the hard straight edges that gate cannot see. That
            # is what printed the wedge under the left jaw — a straight crease at the collar and a
            # swoosh of shirt below it, strongest in the +45 and +27 views where the neck is most
            # foreshortened. Skin in shadow is genuinely darker than skin in light, so the
            # threshold is set well past any real shading and only opens on black.
            ssel = sel & (hairy < 0.5)
            if ssel.sum() > 300:
                sLm = float(np.median(ls[..., 0][ssel]))
                w *= 1.0 - (1.0 - hairy) * (1.0 - feat_m) * np.clip(
                    ((sLm - 46.0) - ls[..., 0]) / 20.0, 0.0, 1.0)

        # The colour gates above run AFTER the join-softening blur and they cut on image content,
        # so each of them puts its own hard edge back into the weight — a collar or a mic ends as a
        # pencil-thin line where the gate closed. Soften once more, at the end, so no weight this
        # view carries has an edge sharper than the blend can hide.
        w = cv2.GaussianBlur(w, (0, 0), 2.0)
        shot.append((smp, w))
        # The feature evidence has to travel through the SAME remap the photograph did, or it would
        # be compared against texels it does not describe.
        # ⚠ Masked by this view's own weight. BORDER_REPLICATE smears the edge value across
        # everything the warp does not reach, and an unmasked field measured 46.9% of the map as
        # "beard or brow" — which switched the consensus de-light off over half the head and cost
        # 2.0 L of brow instead of buying any. The evidence is only meaningful where the view is.
        ev_uv.append(cv2.remap(ev_img, sx, sy, cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
                     * np.clip(w / 0.35, 0.0, 1.0))

        # While this view's warp is still in hand, carry its HAIR LABELS through the very same
        # remap the photograph went through. See the hairline note after the loop: the base map
        # cannot say where THIS player's hairline is, and the segmenter can.
        hair_acc += cv2.remap((cls == SEG_HAIR).astype(np.float32), sx, sy, cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE) * w
        hair_wt += w
        for _c in range(6):                                    # diagnostic only; see _DBG
            seg_acc[..., _c] += cv2.remap((cls == _c).astype(np.float32), sx, sy,
                                          cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE) * w

    # ── headwear ──────────────────────────────────────────────────────────────
    # Hockey reference photos are full of caps, beanies and helmets, and a cap is the worst thing
    # that can happen to this build: it sits exactly where the hair goes, it is opaque, and the
    # crown is the region no other view can correct. Boeser's set had one white Canucks cap in six
    # photographs and it printed as a hard-edged pale plate with the logo blob still legible.
    #
    # No image statistic separates it. Measured on both reference sets before the segmenter was
    # available: the capped view's hair-band mean sits at dE 19 from the set's median while an
    # uncapped, rim-lit Makar view sits at 47, and band gradient energy puts the cap (0.30) inside
    # the range of ordinary hair (0.25-0.82). A UV-space consensus on hair lightness did separate
    # them, but only by 20 L against a 14 L false alarm — a bright blond in good light is nearly a
    # hat. The segmenter answers it outright, because "accessory" is a class it was trained on:
    # over the band above the brow, Boeser's cap reads 53% SEG_OTHER and every other reference in
    # both sets reads 0.0%. There is no threshold to tune between those; 15% is simply the middle
    # of nowhere.
    #
    # The WHOLE view goes, not just its scalp. Dropping only the hair region and keeping the face
    # was tried first, on the reasoning that a photograph of a man in a cap is still a photograph
    # of his face — and it measurably made Boeser worse than excluding the file by hand had: a cap
    # has a BRIM, the brim overhangs the forehead and temple, and those are skin in every mask this
    # file has. The pale plate and a smear of the cap's logo came straight back over the left
    # temple. Whatever is on the head is not confined to the part of the head we call hair.
    for i, (v, cf) in enumerate(zip(views, capped)):
        if cf > 0.15:
            log(f"  ! {v['path'].name}: {100 * cf:.0f}% of the crown is headwear, not hair "
                f"- dropping this view")
            smp, w = shot[i]
            shot[i] = (smp, w * 0.0)
    if all(w.max() <= 0 for _s, w in shot):
        raise ValueError("every reference photograph has headwear covering the head")

    # ── an open mouth ─────────────────────────────────────────────────────────
    # Unlike headwear, this costs only the LIPS. A photograph of a man talking is a perfectly good
    # photograph of his cheeks, his nose and his brow, and those are most of the face — so the veto
    # is the lip region alone, not the view. What it removes is the teeth: they arrive as a bright
    # neutral band, they sit outside the base head's slit-shaped aperture, and no downstream gate
    # can tell them from a pale lower lip because in a still photograph that is exactly what they
    # look like.
    for i, (v, gp) in enumerate(zip(views, agape)):
        if gp > 0.06 and shot[i][1].max() > 0:
            log(f"  ! {v['path'].name}: mouth is open (gap/width {gp:.3f}) "
                f"- vetoing this view over the lips only")
            smp, w = shot[i]
            shot[i] = (smp, w * (1.0 - lip_m))
    # If NOTHING is left over the lips, say so and hand the region to the base head rather than let
    # the outward push invent one. The aperture mask is already the "no photograph speaks here"
    # channel, so widening it to the whole lip region is the existing mechanism, not a new one.
    lip_have = max(float((w * lip_m).max()) for _s, w in shot)
    if lip_have < 0.10:
        log("  ! no reference photograph has a closed mouth - keeping the base head's lips")
        aper_m = np.maximum(aper_m, lip_m)
    # ── one white balance for the whole set ───────────────────────────────────
    # A locker-room photo is warm, an arena photo is cool, a studio headshot is neutral, and the
    # same face comes out a different colour in each. It is the same skin, so the difference is
    # lighting: carry every view's skin chroma onto the most frontal view's and the joins between
    # photographs stop being visible as colour steps.
    anchor = min(range(len(views)), key=lambda i: abs(views[i]["yaw"]))
    log(f"  white balance anchored on {views[anchor]['path'].name}")
    ref_lab = None
    for i, (smp, w) in enumerate(shot):
        sel = w > 0.45
        if sel.sum() < 500:
            continue
        lab = _lab(np.clip(smp, 0, 255).astype(np.uint8))
        med = np.median(lab[sel], 0)
        if i == anchor:
            ref_lab = med
    bal = []
    for i, (smp, w) in enumerate(shot):
        sel = w > 0.45
        if not (ref_lab is None or i == anchor or sel.sum() < 500):
            lab = _lab(np.clip(smp, 0, 255).astype(np.uint8))
            dl = ref_lab - np.median(lab[sel], 0)
            lab += np.float32([dl[0] * 0.75, dl[1], dl[2]])   # chroma fully, exposure mostly
            smp = _unlab(np.clip(lab, 0, 255)).astype(np.float32)
        bal.append(smp)

    # ── joint alignment: congeal the views onto their own consensus ───────────
    # MEASURED motivation. A frontal-only build resolves the vermilion border at a p99 gradient of
    # 32.5; the six-view blend of the same photographs resolves it at 14.6. Nothing about a mouth is
    # harder to photograph from six angles than from one — the detail is being averaged away because
    # the six projections do not land on the same texel. The landmark fit is a similarity plus a
    # Shepard-splatted residual over 468 points, which is smooth by construction and cannot follow a
    # lip border that moves a few texels between views.
    #
    # So stop trying to make the projection exact and CORRECT it after the fact, where the error is
    # actually visible: in UV space, between the views themselves. Each view is flowed onto the
    # current weighted consensus and the consensus is rebuilt. This is congealing, and it needs no
    # target — the mean of six misaligned faces is still a face, and flowing towards it removes the
    # disagreement rather than any one view's idea of the truth.
    #
    # Three guards, all load-bearing:
    #   * the flow is computed on HIGH-PASSED lightness, so a shadow that exists in one view cannot
    #     be mistaken for a displacement of the feature under it;
    #   * it is clamped to FLOW_MAX texels, because past that it is not registration error, it is
    #     the flow finding a different feature and dragging an eyebrow onto a hairline;
    #   * it is damped by BOTH the view's own weight and the consensus confidence, so a view that
    #     barely sees a texel is never asked where that texel should go.
    if len(bal) > 1 and FLOW_PASSES > 0 and hasattr(cv2, "DISOpticalFlow_create"):
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        bal0 = [b.copy() for b in bal]
        ws0 = [w.copy() for _, w in shot]
        ws = [w.copy() for w in ws0]
        fgy, fgx = np.mgrid[0:H, 0:W].astype(np.float32)
        F = [np.zeros((H, W, 2), np.float32) for _ in bal]

        def _hp(rgb):
            """Lighting-invariant edge image: a shadow in one view must not read as displacement."""
            g = cv2.cvtColor(np.clip(rgb, 0, 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
            g = g.astype(np.float32) - cv2.GaussianBlur(g.astype(np.float32), (0, 0), 8)
            return cv2.normalize(g, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

        moved = 0.0
        for _ in range(FLOW_PASSES):
            wsum = np.maximum(np.stack(ws).sum(0), 1e-6)
            cons = (np.stack(bal) * np.stack(ws)[..., None]).sum(0) / wsum[..., None]
            conf = np.clip(wsum / 0.05, 0, 1)
            cg = _hp(cons)
            for i in range(len(bal)):
                f = dis.calc(cg, _hp(bal[i]), None)
                # Compose into the running field and resample from the ORIGINAL every time. Warping
                # the already-warped image instead costs one bilinear filtering per pass, and that
                # blur is larger than the sharpness the alignment buys back — measured: two-pass
                # warp-the-warped came out BELOW the unaligned blend on mouth edge contrast.
                F[i] = F[i] + f
                mag = np.linalg.norm(F[i], axis=2, keepdims=True)
                F[i] *= np.clip(FLOW_MAX / np.maximum(mag, 1e-6), 0, 1)
                F[i] = cv2.GaussianBlur(F[i], (0, 0), 2.0)
                F[i] *= cv2.GaussianBlur(np.minimum(ws0[i] / 0.35, conf),
                                         (0, 0), 4).clip(0, 1)[..., None]
                moved = max(moved, float(np.linalg.norm(F[i], axis=2).max()))
                mx, my = fgx + F[i][..., 0], fgy + F[i][..., 1]
                bal[i] = cv2.remap(bal0[i], mx, my, cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
                ws[i] = cv2.remap(ws0[i], mx, my, cv2.INTER_LINEAR,
                                  borderMode=cv2.BORDER_REPLICATE)
        shot = [(bal[i], ws[i]) for i in range(len(bal))]
        # The evidence fields are per-texel statements about these images, so they have to be
        # dragged by the same displacement — otherwise a brow's evidence stays where the brow was.
        for i in range(len(ev_uv)):
            ev_uv[i] = cv2.remap(ev_uv[i], fgx + F[i][..., 0], fgy + F[i][..., 1],
                                 cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        log(f"  joint alignment: {FLOW_PASSES} passes, up to {moved:.1f} texels of view "
            f"disagreement taken out")

    # ── consensus de-lighting: the views have to AGREE about tone ─────────────
    # The white balance above is one number per photograph, so it cannot touch a shadow that only
    # exists in one of them. The nose flank is the case that matters: at -16 and -38 degrees the
    # nose casts onto its own side and the nostril darkens the same texels the studio shot sees as
    # plain skin, and since the flank genuinely faces sideways it is the OBLIQUE views that win the
    # visibility weight there. So the map inherited a dark tear running from the eye down the nose.
    #
    # Two photographs of one face disagree only about LIGHTING: the albedo underneath is the same
    # skin. So measure each view's low-frequency departure from the weighted consensus and take it
    # out. Where the views agree — real albedo, the lips, the brows, the stubble line — the
    # departure is zero and nothing moves; the correction lands exactly and only on the shadows.
    # It stays low-frequency, so every view keeps all of its own detail for the band blend below.
    # It runs over the colour channels too, and for the same reason: an arena's lighting puts a red
    # flush on whichever cheek faces it, and that flush is in one photograph and not the next.
    if len(bal) > 1:
        ws = np.stack([w for _, w in shot])
        labs = [_lab(np.clip(b, 0, 255).astype(np.uint8)) for b in bal]
        wsum = np.maximum(ws.sum(0), 1e-6)
        blur = [cv2.GaussianBlur(m, (0, 0), CONSENSUS_SIGMA) for m in ws]
        # ⚠ The premise above — "two photographs of one face disagree only about LIGHTING" — is
        # false over a beard and a pair of eyebrows, and only over those. They are PRESENCE, and a
        # view turned away does not resolve them, so the view that DOES resolve them registers as
        # the odd one out and its feature is subtracted as if it were a shadow. Measured on the
        # Pettersson pair: brows at -12.4 L frontal against -2.7 L at 0.80 yaw. Duck the correction
        # wherever any view has evidence; everywhere else this multiplies by 1 and nothing changes.
        ev_max = (np.max(np.stack(ev_uv), 0) if len(ev_uv) == len(bal)
                  else np.zeros(ws.shape[1:], np.float32))
        ev_max = cv2.GaussianBlur(np.clip(ev_max, 0, 1), (0, 0), 3.0)
        keep = (1.0 - FEATURE_KEEP * ev_max)
        dmax = 0.0
        for ch in (0, 1, 2):
            Cs = np.stack([l[..., ch] for l in labs])
            cons = (Cs * ws).sum(0) / wsum
            dmax = max(dmax, float(np.abs((Cs - cons) * (ws > 0.2)).max()))
            for i, l in enumerate(labs):
                lo = (cv2.GaussianBlur((Cs[i] - cons) * ws[i], (0, 0), CONSENSUS_SIGMA)
                      / np.maximum(blur[i], 1e-4))
                l[..., ch] = np.clip(l[..., ch] - lo * CONSENSUS * keep, 0, 255)
        bal = [_unlab(l).astype(np.float32) for l in labs]
        log(f"  consensus de-light: up to {dmax:.0f} of view-to-view disagreement removed"
            f" ({100 * float((ev_max > 0.15).mean()):.1f}% of the map held back as beard/brow)")

    # Over the presence-features, blend by EVIDENCE rather than by visibility. Visibility is the
    # right question for skin — which view resolves this texel best — and the wrong one for a brow,
    # where a view can see the texel perfectly well and still not contain the feature. Softmax so it
    # degrades gracefully: where the views agree the exponents cancel and this is the old mean, and
    # where one view alone carries the beard it takes the texel outright. Same shape as the fusion
    # the hair already uses one material up.
    if FEATURE_SHARP > 0 and len(ev_uv) == len(bal) and len(bal) > 1:
        _ev = np.clip(np.stack(ev_uv), 0, 1)
        _bias = np.exp(FEATURE_SHARP * (_ev - _ev.max(0, keepdims=True)))
        _wts = [w * _bias[i] for i, (_s, w) in enumerate(shot)]
        _lost = float(np.abs(np.stack(_wts) - np.stack([w for _s, w in shot])).max())
        log(f"  feature blend: beard/brow fused by evidence, up to {_lost:.2f} of view weight moved")
    else:
        _wts = [w for _s, w in shot]
    for smp, w in zip(bal, _wts):
        acc += smp * w[..., None]
        wacc += w[..., None]

    # Diagnostic only, and off unless something sets _DBG["box"] to a (x0, y0, x1, y1) texel window:
    # what did each individual view contribute there, and with how much weight. This is the only
    # place the per-view stack still exists — everything below is already the average — so a blend
    # artifact can be attributed to a NAMED photograph rather than guessed at.
    if _DBG.get("box"):
        bx0, by0, bx1, by1 = _DBG["box"]
        _DBG["boxrows"] = [
            (views[i]["path"].name, float(views[i]["yaw"]),
             float(w[by0:by1, bx0:bx1].mean()),
             float(cv2.cvtColor(np.clip(smp[by0:by1, bx0:bx1], 0, 255).astype(np.uint8),
                                cv2.COLOR_RGB2LAB)[..., 0].mean() / 2.55))
            for i, (smp, (_, w)) in enumerate(zip(bal, shot))]

    raw_have = np.clip(wacc[..., 0] / 0.02, 0, 1)      # confidence that a texel got real pixels
    have = cv2.GaussianBlur(raw_have, (0, 0), 6)
    proj = acc / np.maximum(wacc, 1e-6)

    # ── pinholes: a texel no view reached divides nothing by nothing ──────────
    # Where wacc is ~0 that division is 0/1e-6, i.e. BLACK, and normally that is harmless because
    # `have` is ~0 there too and the fill below replaces it. But `have` is blurred with sigma 6, so
    # a hole only a few texels across is voted "covered" by its neighbours and the black survives
    # every later stage. On head 3040 that printed as a dark blot on the nose bridge — 46 texels
    # where the same nostril shadow was rejected by all six views at once — plus hairline streaks
    # down the left of the neck where the silhouette erode cut a thin sliver out of every view.
    # A hole this small is fully described by the texels around it, so fill it from them: a
    # coverage-weighted blur, widening until it finds support, and left alone where there is none
    # (those are the genuinely unreached regions, and the fill below owns them).
    # ⭐ A RAMP, NOT A THRESHOLD — and that is the whole nose-bridge fix. See FILL_TRUST. The hard
    # `raw_have < 0.05` cut repaired the core of a coverage hole and left its rim, because a rim texel
    # has coverage that is small but not zero and so was believed at full strength. Confidence is
    # continuous, so the repair has to be too: regress each texel toward the coverage-weighted
    # estimate of its neighbours in inverse proportion to how much evidence it has of its own. At
    # full coverage this is the identity and nothing anywhere moves.
    starve = np.clip(1.0 - raw_have / FILL_TRUST, 0.0, 1.0) ** FILL_RAMP
    if float(starve.max()) > 0.0:
        fillp = proj
        for sig in (16.0, 5.0):
            den = cv2.GaussianBlur(wacc[..., 0], (0, 0), sig)[..., None]
            est = cv2.GaussianBlur(acc, (0, 0), sig) / np.maximum(den, 1e-6)
            c = np.clip(den / 0.02, 0.0, 1.0)
            fillp = est * c + fillp * (1 - c)
        proj = fillp * starve[..., None] + proj * (1.0 - starve[..., None])
        log(f"  thin coverage: {int(((starve > 0.5) & (have > 0.5)).sum()):,} barely-reached texels "
            f"inside the covered area regressed toward their surroundings "
            f"({int((starve > 0.01).sum()):,} touched at all)")

    # ── WHERE THE HAIRLINE IS, from the photographs ───────────────────────────
    # `skin_hair_masks` reads the hairline off the BASE map, on the stated assumption that the
    # unwrap is shared by every head so the hairline sits in the same place whoever is wearing it.
    # The unwrap is shared; the hairline is NOT. It is PAINTED, per head asset, and head 138's
    # artist painted a short, high, receding cut, so every head built on it inherited that cut no
    # matter whose photographs went in — the bald temples and the exposed ears in the render.
    #
    # The photographs know where THIS player's hairline is. Each view's hair labels came through
    # the identical warp as its pixels, so the vote is already in UV and already registered. Trust
    # it where the cameras reached, keep the base mask everywhere else. The below-jaw gate stays
    # multiplied in: the segmenter calls a beard hair and it is right, but the beard is not this
    # mass and the level, key and comb blocks downstream are not written for it.
    #
    # This has to run BEFORE the blend below, not after: the blend and the detail transfer both
    # ask whether a texel is hair, and answering that with the artist's hairline is what let the
    # temples come through as smeared skin in the first place.
    hair_seen = hair_acc / np.maximum(hair_wt, 1e-6)
    hair_ph = cv2.GaussianBlur(np.clip(hair_seen, 0, 1) * np.clip(hair_wt / 0.02, 0, 1), (0, 0), 4)
    hair_ph *= np.clip((0.02 * face_h - below) / (0.10 * face_h), 0.0, 1.0)
    # ⚠ Gating the photographic vote here is NOT the fix for the nose-bridge crater, which is what
    # this comment used to claim. Measured: gating it moved the pit 17.9 -> 18.2 L, i.e. nothing.
    # The glabella reads as hair on the BASE mask, from the lightness split, long before the
    # segmenter votes — head 3040's artist painted heavy brows and they bleed onto the bridge. See
    # `_hairlv` down in the loop, which is where the gate actually belongs and actually works.
    grew = float(((hair_ph > 0.5) & (hair_m <= 0.5)).sum())
    _DBG.update(vote=hair_ph.copy(), seen=hair_seen.copy(), wt=hair_wt.copy(),
                hair_before=hair_m.copy(), seg=seg_acc / np.maximum(hair_wt, 1e-6)[..., None])
    hair_m = np.maximum(hair_m, hair_ph).astype(np.float32)
    hairy = np.clip(hair_m, 0, 1).astype(np.float32)
    log(f"  hairline: the photographs put hair on {int(grew):,} texels the base head paints as "
        f"skin ({100.0 * grew / max(float(uv_mask.sum()), 1.0):.1f}% of the head) - the base map's "
        f"hairline is the artist's cut, not this player's")

    # ── two-band blend: average the tone, pick a winner for the detail ────────
    # Averaging views is right for colour and wrong for detail. Two photos are never registered to
    # the sub-pixel, so their pores, stubble and eyelashes land a texel or two apart and the mean of
    # them is mush — which is what makes a projected map read as unauthored no matter how clean the
    # colour is. So: keep the weighted average's LOW frequencies (smooth, seamless, no ghosting to
    # see at that scale) and take the HIGH frequencies from the single best-weighted view per texel.
    # Detail stays as sharp as the photograph it came from, and because only fine detail switches,
    # the boundaries where the winner changes are invisible.
    #
    # That is the TWO-BAND case of a Laplacian blend, and its blind spot is the middle: everything
    # from a pore up to a nasolabial fold came out of the mushy average, and that is the band the
    # eye actually reads as facial FORM. So run the pyramid properly (Burt & Adelson, as applied to
    # texture atlases by Allene/Pons/Keriven — blend each octave with the weights blurred to THAT
    # octave's scale) for the middle bands, and keep the gated winner-take-all transfer below for
    # the top. Measured over six variants, that pairing beat every other on all three axes at once:
    # mid-band energy 12.61 -> 13.06, and p99.9 gradient — the seam detector — 25.47 -> 23.95.
    # Replacing the top octave with a one-hot pick INSTEAD of the gated transfer restores detail but
    # puts the hard seams back that multi-band exists to prevent (p99.9 +30%).
    if len(bal) > 1 and BLEND_BANDS > 1:
        nb = BLEND_BANDS
        gp = [[b.astype(np.float32) for b in bal]]
        gw = [[np.maximum(w, 1e-8).astype(np.float32) for _, w in shot]]
        for _ in range(nb - 1):
            gp.append([cv2.pyrDown(x) for x in gp[-1]])
            gw.append([cv2.pyrDown(x) for x in gw[-1]])
        # HAIR CANNOT BE AVERAGED AT THESE SCALES. The averaging argument above holds for skin,
        # whose mid-band is form — a fold, a cheekbone — and is the same shape in every view. Hair's
        # mid-band is the LOCKS, and a lock is long and thin, so each view contributes its own copy
        # of it at its own angle; the mean of those cancels the orientation and keeps the energy.
        # That is what left the temples and the sides a brown wash with the right level, the right
        # spread (p05..p95 21/52/82/108/141 against the hand-corrected map's 14/46/78/105/133) and
        # no strands in it — every scalar matched and the picture still read as smeared.
        #
        # So over hair, fuse the middle octaves by DETAIL instead of by weight: softmax the views on
        # the magnitude of their own Laplacian coefficient, tempered by that octave's own mean
        # magnitude so it is scale-free, and still multiplied by the blend weight so a view that
        # barely saw a texel cannot win it. This is the focus-stacking rule, and it keeps whichever
        # photograph resolved the lock instead of averaging it against the ones that did not. Skin
        # is untouched — the mix is the hair mask itself, and softmax degrades to the plain weighted
        # mean where the views agree, so the octave boundaries stay invisible.
        out = None
        for lv in range(nb - 1, -1, -1):
            ws_ = np.stack(gw[lv])[..., None]
            tot = np.maximum(ws_.sum(0), 1e-8)
            if lv == nb - 1:                                  # coarsest: the tone everyone shares
                out = (np.stack(gp[lv]) * ws_).sum(0) / tot
                continue
            hs = np.stack([gp[lv][i] - cv2.pyrUp(gp[lv + 1][i], dstsize=gp[lv][i].shape[1::-1])
                           for i in range(len(bal))])
            band = (hs * ws_).sum(0) / tot
            hlv = cv2.resize(hairy, hs.shape[2:0:-1], interpolation=cv2.INTER_AREA)
            if float(hlv.max()) > 0.05:
                mag = np.abs(hs).mean(-1)
                T = max(float(mag.mean()), 1e-3)
                e = np.exp(np.clip(mag / T - (mag / T).max(0), -20, 0)) * ws_[..., 0]
                sharp = (hs * (e / np.maximum(e.sum(0), 1e-8))[..., None]).sum(0)
                band = band * (1 - hlv[..., None]) + sharp * hlv[..., None]
            out = cv2.pyrUp(out, dstsize=gp[lv][0].shape[1::-1]) + band
        # Only where the cameras actually reached: outside that the weights are all ~0 and the
        # pyramid is dividing noise by noise. The fill owns those texels anyway.
        keep = cv2.GaussianBlur((raw_have > 0.05).astype(np.float32), (0, 0), 2.0)[..., None]
        proj = np.clip(out, 0, 255) * keep + proj * (1 - keep)
        log(f"  multi-band blend: {nb} octaves")

    if len(bal) > 1 and DETAIL_SIGMA > 0:
        wstack = np.stack([w for _, w in shot])
        # PER-TEXEL ARGMAX IS THE OTHER HALF OF THE BROKEN MOUTH. `best` is meant to be "the view
        # that saw this properly", and the transfer below treats it as a single sharp photograph —
        # but taking the winner independently at every texel makes it a MOSAIC. Measured over the
        # mouth box on head 3040: six distinct views win inside it, so the lip line is assembled
        # from six slightly different placements of the lip, and the high-pass of that composite is
        # already smeared before any blending happens (p1 of -14.6 against -23.1 for a single view
        # over the same box). Raising the clamp cannot fix it and neither can aligning the views:
        # the damage is done in building `best` at all.
        #
        # So choose the label over a heavily SMOOTHED weight field. The winner then holds across a
        # whole region — the entire mouth comes from one photograph — and the few boundaries that
        # remain fall where the smoothed weights genuinely cross, which is away from features and
        # is exactly where the pyramid above can hide them. This is the cheap form of the graph-cut
        # labelling a real photogrammetry stitcher runs, and it buys most of what that buys. Note
        # it is invisible to every seam metric — it was found by counting distinct winners, not by
        # measuring the result.
        pick = np.argmax(np.stack([cv2.GaussianBlur(w, (0, 0), LABEL_SIGMA) for w in wstack]), 0)
        best = np.take_along_axis(np.stack(bal), pick[None, ..., None], 0)[0]
        # LIGHTNESS only, and clamped. Detail is a luminance phenomenon — pores, stubble, creases —
        # while an unclamped colour residual just reimports whatever hard edge the winning view had
        # at a hair or background boundary, which prints as dark streaks across the face.
        bl_ = _lab(np.clip(best, 0, 255).astype(np.uint8))[..., 0]
        hi_raw = bl_ - cv2.GaussianBlur(bl_, (0, 0), DETAIL_SIGMA)
        # A HARD CLIP HERE WAS FLATTENING EVERY REAL FEATURE. DETAIL_CLAMP exists to stop a hard
        # edge at a hair or background boundary from being reimported as a streak, and at ±14 it
        # does that — but it is also below every real feature on a face. Measured on head 3040 over
        # the mouth box: 4.7% of texels exceed 14 and the lip line itself reaches -65, so the clip
        # was flattening it to a fifth of its contrast. Over the cheek, 0.03% exceed it. That is the
        # whole symptom: skin came through untouched and every actual feature got limited.
        #
        # A soft knee separates the two jobs. Below ~20 it is essentially linear, so a lip line, a
        # nostril and an eye crease all pass at full strength; past that it compresses smoothly and
        # asymptotes, so a genuine outlier still cannot print a streak. No corner anywhere, which
        # matters because a clip's corner is itself an edge the eye finds.
        hi_p = SOFT_KNEE * np.tanh(hi_raw / SOFT_KNEE)
        # only where a view genuinely won: near-tied weights mean neither view is trustworthy there
        strong = np.clip((np.max(wstack, 0) - 0.25) / 0.35, 0.0, 1.0)
        lab = _lab(np.clip(proj, 0, 255).astype(np.uint8))
        lo_L = cv2.GaussianBlur(lab[..., 0], (0, 0), DETAIL_SIGMA)
        own = lab[..., 0] - lo_L                       # the average's own (mushier) detail
        lab[..., 0] = np.clip(lo_L + hi_p * strong + own * (1 - strong), 0, 255)
        # A vermilion border is a CHROMA step, not a lightness step, and lips sit at a scale the
        # pyramid above hands to the average. So run the same winner-take-all transfer over a and b.
        # Kneed harder than lightness because a colour excursion that escapes is a coloured blotch,
        # not a crease, and the eye forgives it far less.
        blab = _lab(np.clip(best, 0, 255).astype(np.uint8))
        for ch in (1, 2):
            hc_raw = blab[..., ch] - cv2.GaussianBlur(blab[..., ch], (0, 0), DETAIL_SIGMA)
            hp_c = CHROMA_KNEE * np.tanh(hc_raw / CHROMA_KNEE)
            lo_c = cv2.GaussianBlur(lab[..., ch], (0, 0), DETAIL_SIGMA)
            own_c = lab[..., ch] - lo_c
            lab[..., ch] = np.clip(lo_c + hp_c * strong + own_c * (1 - strong), 0, 255)
        proj = _unlab(lab).astype(np.float32)
    # ── symmetry fill ─────────────────────────────────────────────────────────
    # The unwrap is mirror-symmetric about the map's centre line: the two ears sit at matching
    # distances from the edges, the face is centred, the neck is centred. So anything no camera
    # reached — the wedge behind an ear, a patch of jaw the near side always occluded — has a real
    # measured twin on the other side of the map. Take it from there before resorting to smearing
    # the nearest colour outward. Faces are not perfectly symmetric, but a mirrored ear beats an
    # invented one, and the handover is gated on the twin being MUCH better covered than the hole.
    mir_p, mir_h = proj[:, ::-1], have[:, ::-1]
    take = np.clip((mir_h - have - 0.2) / 0.3, 0.0, 1.0) * np.clip((0.6 - have) / 0.4, 0.0, 1.0)
    take = cv2.GaussianBlur(take, (0, 0), 4)[..., None]
    proj = proj * (1 - take) + mir_p * take
    have = np.maximum(have, (mir_h * take[..., 0] * 0.9))
    log(f"  symmetry fill: {100 * (take[..., 0] > 0.5).mean():.0f}% of the map taken from its twin")

    # push the last projected colour outward so the fill is never a step: dilate under the mask,
    # blur, and let the low-confidence texels take the pushed value instead of a flat one
    # Seed from the CONFIDENT core, not the rim: the outermost projected texels are the grazing
    # ones that caught some background. Colour and weight are blurred TOGETHER and divided at the
    # end, so the spread stays a weighted average of real pixels and never re-admits empty ones.
    #
    # Skin and hair are pushed SEPARATELY. They meet at a hard edge that a 9-texel blur walks
    # straight across, and since the cameras cover the face far better than the crown, one shared
    # push means face colour floods the whole top of the map: the head came out bald and pink under
    # a grey smear. Each material now only ever spreads into itself.
    # How far the push may be trusted is a question about DISTANCE, and it has to be asked as one.
    # Judging it by the diffused mass instead — which is what this did — saturates at 1 a few texels
    # out and stays there, so the push was trusted across the entire map and the fill below was
    # never reached at all. The crown is 150 texels from the nearest photographed hair, and it was
    # being handed the smeared colour of his fringe: in profile that rendered as a bald cream cap.
    def _push(seed, near, fade):
        num, den = proj * seed[..., None], seed.copy()
        for _ in range(7):
            num = cv2.GaussianBlur(num, (0, 0), 9)
            den = cv2.GaussianBlur(den, (0, 0), 9)
        dist = cv2.distanceTransform((seed < 0.5).astype(np.uint8), cv2.DIST_L2, 3)
        return (num / np.maximum(den, 1e-6)[..., None],
                np.clip(1.0 - (dist - near) / fade, 0.0, 1.0))

    # Never let the push flood the mouth aperture. It sits in the middle of well-covered skin, so
    # `reach` is ~1 across it and it was taking the smeared average of the face around it — a flat
    # grey bar straight through the mouth. Keep it out of the push SEED so skin colour never spreads
    # in, and drop `reach` inside it below, so what the photographs did not supply falls through to
    # the base map's own mouth line instead of to grey.
    core = (have > 0.6).astype(np.float32) * (1.0 - aper_m)
    push_s, reach_s = _push(core * (1 - hairy), PUSH_NEAR, PUSH_FADE)
    push_h, reach_h = _push(core * hairy, HAIR_NEAR, HAIR_FADE)
    # and hair only keeps what it actually SAW. The top of the head is half-covered by cameras that
    # are all at eye level, and half-covered hair blends to a pale wash that renders as a bald spot.
    reach_h = np.minimum(reach_h, np.clip((cv2.GaussianBlur(have, (0, 0), 8) - 0.5) / 0.25, 0, 1))
    push = push_s * (1 - hairy)[..., None] + push_h * hairy[..., None]
    reach = reach_s * (1 - hairy) + reach_h * hairy
    # Hand over to the smeared version GRADUALLY. This used to be a hard `where(core)`, i.e. a step
    # at have = 0.6, and a step in a mask is a step in the map: it drew a scalloped grey cut-out
    # across the ear bowl, sharp-edged and obviously wrong, wherever coverage happened to cross that
    # line. Nothing that varies smoothly should be switched on a threshold.
    # Ramp it BELOW the old threshold, not across it: `push` is a heavy blur, so every texel that
    # takes some of it loses contrast, and centring the ramp on 0.6 handed half a dose of smear to
    # the whole transition band — the temples and the hairline went grey. Above 0.6 the measurement
    # still wins outright, exactly as before; the ramp only replaces the cliff underneath it.
    reach = reach * (1.0 - aper_m)
    t = np.clip((have - 0.30) / 0.30, 0.0, 1.0)
    t = (t * t * (3 - 2 * t))[..., None]
    proj = proj * t + push * (1 - t)
    log(f"  projected coverage: {100 * (have > 0.5).mean():.0f}% of the map, "
        f"{100 * (reach > 0.5).mean():.0f}% carried outward")

    # ── fill everything the cameras never saw, in the measured tones ──────────
    solid = have > 0.6
    skin_ref = solid & (skin_m > 0.5)
    skin_rgb = (np.median(proj[skin_ref].reshape(-1, 3), 0) if skin_ref.sum() > 200
                else np.array([200.0, 165.0, 145.0]))
    # The hair the fill paints has to be the SAME hair the projection painted, or the join shows —
    # the sides came out auburn off the photographs while the crown was filled from an independent
    # measurement in the frontal shot and came out light blond, and the two met in a visible line
    # across the top of the head. So measure it where the two actually meet: the hair the cameras
    # did see, as it ended up in the map. Only if none of it survived does it fall back to reading
    # the photograph directly.
    # …and measure it only where the ARTIST also calls it hair. `hairy` is deliberately generous —
    # it is widened by the upward-facing normals so the crown is covered — and generosity is right
    # for deciding what to PAINT and wrong for deciding what to MEASURE. On Boeser, whose usable
    # views are all near-frontal, the only hair the cameras resolved was the hairline and the
    # temples, where the generous mask takes forehead with it; the tone came out 114/90/86, which
    # is a complexion and not a hair colour, and the whole crown was painted in it. The base map
    # knows better: it was drawn on this unwrap by someone who could see the whole head, so ask it
    # where the hair unambiguously is and read the photographs only there.
    #
    # And if that leaves nothing, the honest conclusion is that these photographs never resolved any
    # hair, not that the mask's leftovers will do. Fall through to reading the band above the
    # hairline in the PHOTOGRAPH instead (the `else` below), which is at least a measurement of hair.
    # Widening the region until it produces a number is how the crown got painted in complexion.
    seen_ab = [v for v, (_s, w) in zip(hair_ab, shot) if v is not None and w.max() > 0]
    ab = np.median(np.stack(seen_ab), 0) if seen_ab else None
    seen_lp = [v for v, (_s, w) in zip(hair_lp, shot) if v is not None and w.max() > 0]
    hair_span = float(np.median([p[2] - p[0] for p in seen_lp])) if seen_lp else 0.0
    hair_med = float(np.median([p[1] for p in seen_lp])) if seen_lp else 0.0
    # ⭐ RELATIVE to the face in the same frame, not absolute — see the note where it is measured.
    _hrel = [p[1] - p[3] for p in seen_lp if len(p) > 3 and np.isfinite(p[3])]
    hair_rel = float(np.median(_hrel)) if _hrel else None
    # …and the front-of-head version, which is the one the level anchor uses; see the note there.
    _hrelf = [p[4] - p[5] for p in seen_lp
              if len(p) > 5 and np.isfinite(p[4]) and np.isfinite(p[5])]
    hair_rel_f = float(np.median(_hrelf)) if _hrelf else None
    _hfin = [v for v, (_s, w) in zip(hair_fine, shot) if v is not None and w.max() > 0]
    hair_fine_e = float(np.median(_hfin)) if _hfin else None
    # …summarised by the 80th percentile rather than the median, for the reason the brow strand
    # target already gives: hair photographed at a grazing angle is a smear, and the sharpest view
    # of a lock is the honest statement of how much strand it carries. A focus stack takes the
    # sharpest frame, not the average one.
    _hsd = [v for v, (_s, w) in zip(hair_fine_sd, shot) if v is not None and w.max() > 0]
    hair_fine_s = float(np.percentile(_hsd, 80)) if _hsd else None
    # Lock scale takes the MEDIAN, not the 80th percentile: the argument above is about resolving
    # fine strands, which a soft frame loses, and a lock is twenty texels across and survives any
    # frame that saw the head at all. A percentile here would let one grazing shot's shadow decide
    # that a shaved man has locks.
    _hlks = [v for v, (_s, w) in zip(hair_lock, shot) if v is not None and w.max() > 0]
    hair_lock_e = float(np.median(_hlks)) if _hlks else None
    _smid = [v for v, (_s, w) in zip(skin_mid, shot) if v is not None and w.max() > 0]
    skin_mid_e = float(np.median(_smid)) if _smid else None
    _sgr = [v for v, (_s, w) in zip(skin_grain, shot) if v is not None and w.max() > 0]
    skin_grain_e = float(np.median(_sgr)) if _sgr else None
    # …taken over the views that actually SAW the brow rather than all of them: a brow photographed
    # past 35 degrees of yaw is a foreshortened smear, and the median of nine views is dragged down
    # by the five of Boeser's that sit there. The MAX over the views that landed is the right
    # summary of "how much strand does a photograph of this man's eyebrow carry", for the same
    # reason a focus stack takes the sharpest frame rather than the average one.
    _bgr = [v for v, (_s, w) in zip(brow_grain, shot) if v is not None and w.max() > 0]
    brow_grain_e = float(np.percentile(_bgr, 80)) if _bgr else None
    # …and the brow BODY, over the same views and summarised the same way and for the same reason:
    # a brow seen past 35 degrees of yaw is foreshortened into the ridge above it and reads shallow.
    _bbd = [v for v, (_s, w) in zip(brow_body, shot) if v is not None and w.max() > 0]
    brow_body_e = float(np.percentile(_bbd, 80)) if _bbd else None
    blab = _lab(base_np)
    sL = float(np.median(blab[..., 0][skin_m > 0.5])) if (skin_m > 0.5).any() else 128.0
    hair_ref = solid & (hairy > 0.5) & (blab[..., 0] < sL - 25.0)
    src = "photographs, where the base map agrees it is hair"
    if fill_hair and hair_ref.sum() > 200:
        # Read the TONE off the dark half of that hair, not off its median. Two errors contaminate
        # this measurement and both push the same way: the skin/hair mask leaks forehead and temple
        # into the hair region, and hair photographs with a specular sheen along the part. Neither
        # can make hair read darker than it is. Taking the median therefore paints the crown and the
        # nape — the one region no camera reaches and nothing downstream can correct — several stops
        # light and pink. Measured on Boeser: the built crown came out RGB 127/96/86 against the
        # artist's 35/28/18, and the nape 134/104/94 against 57/44/32. A low percentile of lightness
        # throws the leak and the sheen away while keeping the hair's own hue. 35% and not lower so
        # a handful of shadowed texels cannot set the tone for the whole skull.
        # SPREAD is still measured on the full region below: the dark tail is truncated by
        # construction, so its MAD would understate the strand contrast it is there to carry.
        # …but LIGHTNESS ONLY. Both errors the low percentile defends against — mask leak and
        # specular sheen — are arguments about how BRIGHT a texel is, and neither says anything
        # about its hue. Taking the whole colour off the dark tail imports a third error instead,
        # and a systematic one: chroma compresses towards neutral as lightness falls, so the
        # darkest third of any hair region is also its greyest third. Measured on Boeser, whose
        # hair the references agree is blond at b* 11-21, the crown was filled at b* 7.8 — greyer
        # than the 2009 base head it replaced. That is the whole "sad, flat colour, not his blond"
        # complaint, and it is not a lighting or a blending fault, it is this percentile.
        # So: level off the dark tail, hue off the SEGMENTER, which saw whole heads of hair rather
        # than the shaded fringe the projection resolves. Taking the hue from the full projected
        # region instead was tried first and moved b* by 0.1 — the projection's hair is grey
        # everywhere, not just in its dark tail, because everywhere it reaches is hairline.
        # Median across views, so one wet-and-blue-lit arena shot cannot set his hair colour.
        plab = _lab(np.clip(proj, 0, 255).astype(np.uint8))
        hL = plab[..., 0]
        dark = hair_ref & (hL <= np.percentile(hL[hair_ref], 35))
        lvl = dark if dark.sum() > 200 else hair_ref
        hue = ab if ab is not None else np.array(
            [np.median(plab[..., 1][hair_ref]), np.median(plab[..., 2][hair_ref])], np.float32)
        hair_rgb = _unlab(np.array([[[np.median(hL[lvl]), hue[0], hue[1]]]], np.float32)
                          )[0, 0].astype(np.float32)
    else:
        hc = hair_color(views[0]["img"], views[0]["lm"]) if fill_hair else None
        hair_rgb = np.array(hc, np.float32) if hc is not None else skin_rgb * 0.5
        src = ("the band above the hairline - no photograph resolved hair on the unwrap"
               if hc is not None else "nothing: half the skin tone, as a last resort")

    # ── the fill is the BASE MAP, RETINTED ────────────────────────────────────
    # Nobody photographs the back of a player's head. The crown and the nape are the one part of the
    # map no reference can ever reach, and they are also where the skin/hair mask is least reliable:
    # the back of the skull unwraps to a strip at the map's right edge that the mask happily called
    # skin, so it filled with flat pink and rendered, in profile, as a bald cream cap.
    #
    # So do not ask the mask what to paint there. The base head already HAS an answer, drawn by the
    # artist who drew this unwrap — hair with strands and a parting on the crown, skin on the neck,
    # each in the right place because it is the same unwrap. Keep all of it and change only the
    # colour: shift its skin onto the skin we measured and its hair onto the hair we measured, with
    # the mask deciding only how much of each shift a texel gets. Where the mask is wrong the base's
    # own colour still carries the region, so being wrong costs a slight mistint instead of a bald
    # patch. Its baked lighting comes out in the flatten pass below, along with the photographs'.
    # and widen the mask for this purpose using the base map's own lightness: outside the face oval,
    # anything clearly darker than the base's skin is hair. The mask that comes off skin_hair_masks
    # is built for the face and stops caring at the map's outer strips, which is precisely where the
    # back of the skull lives — it was being retinted as skin, so the crown came out grey.
    # It is a GUESS, so it may only speak where there is nothing better. Ungated it also fired on the
    # temple — which is outside the face oval and is shaded darker than mid-cheek in the base map —
    # and flipped that whole wedge to "hair", with the base map's own shading step for an edge. The
    # fill barely showed it (reach is ~0.9 there) but the hair hue lock below is not gated on reach
    # at all, so it painted hair pigment straight onto lit skin: a pale grey-green wedge from the
    # hairline to the brow with a hard diagonal border. Where the cameras DID resolve a texel, their
    # answer stands; the widening only fills in behind them.
    # It carries the same above-the-chin gate as the mask itself, and for the same reason — under
    # the jaw every term of the widening fires at once (dark in the base map, outside the face oval,
    # unreached by any camera) and it would put the neck wedge straight back.
    # …and the hair half of it comes from whichever shipped head is wearing this player's CUT, not
    # from whichever head the player happens to sit on. See `haircut_donor`. Only the hair is taken:
    # the skin retint, the contrast reference and everything else downstream still work off the base
    # head, because those are about this map's own skin and the donor has nothing to say about it.
    donor_id, donor_np = haircut_donor(hair_seen, hair_wt / 0.02, base_id, game_dir, log=log)
    dlab = _lab(donor_np) if donor_np is not None else blab
    dhair = (skin_hair_masks(donor_np, face_m.astype(np.float32) / 255.0)[1] > 0.5) \
        if donor_np is not None else None
    hair_m = np.maximum(hair_m, np.clip((sL - dlab[..., 0] - 8.0) / 14.0, 0, 1)
                        * (1.0 - face_m.astype(np.float32) / 255.0)
                        * (1.0 - np.clip(reach, 0.0, 1.0))
                        * np.clip((0.02 * face_h - below) / (0.10 * face_h), 0.0, 1.0))
    hs, hh = hair_m <= 0.35, hair_m >= 0.65
    tgt_s = _lab(np.clip(skin_rgb, 0, 255).astype(np.uint8).reshape(1, 1, 3))[0, 0]
    tgt_h = _lab(np.clip(hair_rgb, 0, 255).astype(np.uint8).reshape(1, 1, 3))[0, 0]
    src_s = np.median(blab[hs], 0) if hs.sum() > 200 else tgt_s
    # Measure the hair source over texels the DONOR also calls hair: `hair_m` is our mask, and where
    # it and the donor disagree the donor is painting skin there, which would drag the source tone
    # towards a cheek and undo the retint.
    hsel_d = (hh & dhair) if dhair is not None and int((hh & dhair).sum()) > 200 else hh
    src_h = np.median(dlab[hsel_d], 0) if hsel_d.sum() > 200 else tgt_h
    # Hair gets a GAIN as well as a shift. A shift alone moves the base map's hair onto the
    # measured colour and leaves its contrast where the artist put it, so a player whose hair
    # photographs as high-contrast strands gets the base head's flat mass in the measured hue —
    # the crown reads as a painted helmet, and it reads that way exactly where nothing else can
    # correct it, because this is the region no camera saw. Matching the spread as well as the
    # centre carries the strand structure across. Measured with a MAD rather than a standard
    # deviation so the parting highlight, which is a handful of very bright texels, does not set
    # the scale for the whole crown. Lightness only: hair chroma spread is mostly sensor noise,
    # and gaining it up turns a brown crown speckled green and magenta.
    mad = lambda v: float(np.median(np.abs(v - np.median(v))) * 1.4826)   # noqa: E731
    gain = 1.0
    if fill_hair and hsel_d.sum() > 400 and hair_ref.sum() > 400:
        s_src = mad(dlab[..., 0][hsel_d])
        s_tgt = mad(_lab(np.clip(proj, 0, 255).astype(np.uint8))[..., 0][hair_ref])
        if s_src > 1.0:
            gain = float(np.clip(s_tgt / s_src, 0.6, 2.2))
    # Harden the mix before it is used as a blend weight. `hair_m` is a soft evidence field, and over
    # the side of the head it sits around 0.8 — which reads as "almost certainly hair" and is then
    # spent mixing a fifth of retinted SKIN into it. Skin is retinted to L 190 and hair to L 50, so a
    # fifth of the wrong one is a thirty-L error, and it is the whole of why the side hair came out
    # at 106 against the artist's 57 and the user's 59: the two references disagree about the crown
    # but they agree exactly here. A soft mask is the right way to DECIDE and the wrong way to MIX.
    # The knee keeps a genuine transition band around the hairline, where mixing is what is wanted.
    hm = np.clip((hair_m - 0.25) / 0.5, 0, 1)[..., None]
    lab_skin = blab + (tgt_s - src_s)
    # ⭐ MEASURED FAULT, fixed here, and it printed as the most visible thing on a finished head.
    #
    # The retint above is a SHIFT: it moves the base map onto the measured skin tone and keeps every
    # relative level the artist painted. That is right almost everywhere, and wrong on exactly the
    # texels the hairline vote just grew. A BALD base head - and the library has plenty, they are the
    # ones worth picking for a buzz cut - has a lit crown painted into its map, several L above its
    # own face. Shift that and it stays several L above the face; mix it under the hair fill at the
    # hairline, where `hm` has not yet switched over, and the result is a bright arc running from the
    # temple back over the ear. On slot 319 it was the single loudest fault on the head, and it
    # survived every downstream stage because every one of them holds the hair region out.
    #
    # Where the photographs say hair, the base map's level is a different head's scalp highlight and
    # carries no information about this player. Take it out - feathered by the same vote, so nothing
    # moves where the vote is weak, and only ever downward, so a base head that paints its crown
    # DARKER than its face keeps that and is not brightened into a halo instead.
    _fsel = (face_m > 127) & (hair_m < 0.2)
    if int(_fsel.sum()) > 400:
        _ex = np.maximum(lab_skin[..., 0] - float(np.median(lab_skin[..., 0][_fsel])), 0.0)
        _cut = np.clip(hair_ph, 0, 1) * _ex
        lab_skin[..., 0] = lab_skin[..., 0] - _cut
        if float(_cut.max()) > 1.0:
            log(f"  hairline level: the base head paints a lit scalp up to "
                f"{float(_cut.max()) / 2.55:.1f} L over its own face, under where the photographs "
                f"put this player's hair - levelled, it was printing as a bright arc at the hairline")
    lab_hair = tgt_h + (dlab - src_h) * np.array([gain, 1.0, 1.0], np.float32)
    fill = _unlab(np.clip(lab_skin * (1 - hm) + lab_hair * hm, 0, 255)).astype(np.float32)
    if fill_hair:
        log("  hair fill: tone %d/%d/%d, " % tuple(np.round(hair_rgb).astype(int)[:3])
            + f"contrast x{gain:.2f}, over {100 * (hm[..., 0] > 0.5).mean():.0f}% of the map, "
            + f"structure from head {donor_id}; "
            f"tone read off {src}")
    # and the mouth interior keeps the artist's own pigment. The retint above shifts every texel
    # onto the measured SKIN, which is right for a cheek and wrong for a mouth interior — it lifted
    # the shipped dark red towards pale skin and the line came back grey. A mouth interior is dark
    # red on everybody; it is not a place the player's complexion belongs.
    fill = fill * (1 - aper_m[..., None]) + base_np.astype(np.float32) * aper_m[..., None]

    # --- diagnostic probe: how much beard, and how much BROW, survives each stage (in L) ---
    _JAW = [172, 136, 150, 149, 176, 148, 152, 377, 400, 378, 379, 365, 397, 288, 361, 323]
    _CHK = [117, 118, 119, 100, 142, 36, 205, 346, 347, 348, 329, 371, 266, 425]
    _BRW = BROW_L + BROW_R
    _FHD = [67, 109, 10, 338, 297, 104, 69, 108, 151, 337, 299, 333]
    _lmp = np.asarray(t_lm, np.float32).reshape(-1, 2).astype(int)

    # …and the HAIR, in the region and against the reference the QA actually scores it on, so a
    # map-space number can be set beside a render-space one and the difference attributed. The QA
    # takes a box 0.95 eye-widths either side of the eye centre and 0.70..1.55 above it, drops
    # anything inside the face oval, and reports the mean L there minus the mean L over the oval.
    # Reproduced here rather than approximated: "the hair is 13 L too light" is only actionable if
    # both numbers mean the same thing, and a median over the whole hair mass does not — the mass
    # runs from a lit crown to a nape the camera never sees, while the QA sees fringe and temple.
    _EW = float(np.hypot(*(_lmp[263] - _lmp[33])))
    _ec = 0.5 * (_lmp[33] + _lmp[263])
    _facem = np.zeros(uv_mask.shape, np.uint8)
    cv2.fillConvexPoly(_facem, _hull(np.asarray(t_lm, np.float64)[list(FACE_OVAL)], 1.0), 255)
    _hairm = np.zeros(uv_mask.shape, np.uint8)
    cv2.rectangle(_hairm, (int(_ec[0] - 0.95 * _EW), int(_ec[1] - 1.55 * _EW)),
                  (int(_ec[0] + 0.95 * _EW), int(_ec[1] - 0.70 * _EW)), 255, -1)
    _hairm = (_hairm > 0) & (_facem == 0) & uv_mask
    _facem = (_facem > 0) & uv_mask

    _guard = {"keep": None, "cost": None, "declined": 0}

    def _bar_cost(c):
        """How far this map sits OUTSIDE the shipped-albedo distribution, in L-ish units. Zero
        means it looks like something a 2K artist would have painted.

        ⚠ The masks here MUST be the ones the bar was measured with — skin_hair_masks over the
        whole map, segmenting the candidate by its own colour, exactly as the 433-head scan did.
        Measuring skin over the face-oval landmarks instead reads several L higher, and a cost
        function on a different footing from its own bar cannot see the drift it exists to catch:
        with the oval it scored the pre-loop map 19.3 when the bar scored it 0, and then waved
        through the accepted steps that walked skin from L 66.3 to 74.5.
        """
        u8 = np.clip(c, 0, 255).astype(np.uint8)
        _sm, _hm = skin_hair_masks(u8, np.zeros(u8.shape[:2], np.float32))
        _hc = np.clip(_hm, 0, 1)
        skin, hair = (1.0 - _hc) > 0.6, _hc > 0.6
        Lb = _lab(u8)[..., 0].astype(np.float32) / 2.55
        if skin.sum() < 2000:
            return 0.0
        sL = float(np.median(Lb[skin]))
        cost = max(0.0, sL - BAR_SKIN_HI) + max(0.0, BAR_SKIN_LO - sL)
        if hair.sum() > 2000:
            cost += 2.0 * max(0.0, (float(np.median(Lb[hair])) - sL) - BAR_HAIR_DL_MAX)
        cost += 150.0 * max(0.0, float((Lb > 92.0).mean()) - BAR_BLOWN_MAX)
        return cost

    def _probe(tag, c):
        # ⭐ TAP. Every probed stage can hand its map out, which is what makes "how much of the 2009
        # look is the correction stack rather than the fit" a measurable question instead of an
        # argument. The from-scratch proto showed photographic skin using the SAME projection this
        # builder already runs — so the difference is not the sampling, it is the twenty stages
        # after it. Set _DBG["capture"] to a tag (or "*" for all) and the map at that point comes
        # back in _DBG["captured"], ready to be written into the artist's UV and rendered.
        _cap = _DBG.get("capture")
        if _cap is not None and (_cap == "*" or _cap == tag):
            _DBG.setdefault("captured", {})[tag] = np.clip(c, 0, 255).astype(np.uint8).copy()
        Lp = _lab(np.clip(c, 0, 255).astype(np.uint8))[..., 0]

        def g(idx, r=5):
            return float(np.median([np.median(Lp[max(0, _lmp[i][1] - r):_lmp[i][1] + r,
                                                 max(0, _lmp[i][0] - r):_lmp[i][0] + r])
                                    for i in idx]))
        _DBG.setdefault("probe", []).append((tag, round(g(_JAW) - g(_CHK), 1)))
        # Diagnostic only, off unless _DBG["box"] is set: min/mean L inside one texel window, per
        # stage. The per-view dump above proved the nose-bridge blob is NOT in the blend — all six
        # of Makar's photographs land L 85-87 there against a nose median of 79.6 — so whatever
        # digs it to L 42 runs after the blend, and this says WHICH STAGE.
        if _DBG.get("box"):
            _bx0, _by0, _bx1, _by1 = _DBG["box"]
            _b = Lp[_by0:_by1, _bx0:_bx1] / 2.55
            # PIT DEPTH, not level. A stage that darkens the whole head lowers the box too and is
            # not digging a hole; what makes a hole is the box falling relative to the skin AROUND
            # it. So carry the nose's own median alongside and report the difference.
            _nm = _DBG.get("boxref")
            if _nm is None:
                _nm = np.zeros(Lp.shape, np.uint8)
                cv2.fillPoly(_nm, [np.round(np.asarray(_lmp, np.float64)[
                    list(QA_SKIN_REGIONS["nose"])]).astype(np.int32)], 255)
                _nm = _DBG["boxref"] = _nm > 0
            _ref = float(np.median(Lp[_nm])) / 2.55
            _DBG.setdefault("boxL", []).append((tag, round(float(_b.min()), 1),
                                                round(float(_b.mean()), 1), round(_ref, 1),
                                                round(_ref - float(_b.mean()), 1)))
        _DBG.setdefault("brow", []).append((tag, round(g(_FHD, 4) - g(_BRW, 3), 1)))
        # …and how much TEXTURE the brows carry, in the band and by the statistic the QA scores
        # them on: the standard deviation of everything finer than three map texels. Per stage,
        # because "the brows are soft" is not one fault — it is whichever stage takes it out.
        _bm = np.zeros(Lp.shape, np.uint8)
        for _i in _BRW:
            cv2.circle(_bm, (int(_lmp[_i][0]), int(_lmp[_i][1])), 6, 255, -1)
        _hp = Lp - cv2.GaussianBlur(Lp, (0, 0), 3.0)
        _DBG.setdefault("browE", []).append(
            (tag, round(float(_hp[_bm > 0].std()) / 2.55, 2)))
        # hair, the QA's way: level against the face, and strand energy at the QA's own scale.
        if _hairm.any():
            _hph = Lp - cv2.GaussianBlur(Lp, (0, 0), 3.0)
            _DBG.setdefault("hairQ", []).append(
                (tag, round(float(Lp[_hairm].mean() - Lp[_facem].mean()) / 2.55, 1),
                 round(float(_hph[_hairm].std()) / 2.55, 2)))
        # …and the BACK-OF-HEAD SEAM. The head unwrap is a cylinder cut down the centre-back, so
        # that line is the map's u=0 and u=1 edges; a bright stripe there draws a parting on the
        # back of every head. Reported as how far columns 4-5 in from each edge stand above their
        # own neighbours, over the crown-to-nape band.
        # Compared only against texels the mesh actually SAMPLES: columns 0-3 and 508-511 lie
        # outside the island, so averaging them in measures the gap between skin and unused filler
        # rather than a step in the map. Split hair band from neck band — they fail separately.
        for _nm, _lo, _hi in (("seam", 0.08, 0.45), ("seam_neck", 0.45, 0.60)):
            _b = Lp[int(_lo * H):int(_hi * H)]
            _DBG.setdefault(_nm, []).append(
                (tag, round(float(_b[:, 4:6].mean() - _b[:, 6:10].mean()), 1),
                 round(float(_b[:, -6:-4].mean() - _b[:, -10:-6].mean()), 1)))

        # ── ⭐ THE SHIPPED-ALBEDO GATE ────────────────────────────────────────
        # Every stage up to and including de-shine measured INSIDE the shipped distribution on a
        # blond head (skin L 66, hair dL -24, nothing blown) and every step of the closed loop
        # below drove it OUT — skin to L 83.5, and 12.4% of the map blown against a shipped
        # library maximum of 0.1%. The cause is structural, not a tuning miss: the loop reads its
        # targets off LIT photographs and writes them into a DE-LIT albedo, so it is forever
        # chasing a studio key light that the game will add again at render time.
        #
        # So the loop no longer gets the last word. A step that moves the map further outside what
        # a 2K artist would have painted is declined and the previous map stands. This is a whole
        # rollback to the last accepted state, NOT the partial unwind that printed a hard band
        # once before — the difference is that nothing has been layered on top yet.
        if tag.startswith('loop-'):
            _c = _bar_cost(c)
            if _guard["keep"] is not None and _c > _guard["cost"] + BAR_TOL:
                _guard["declined"] += 1
                # ASCII only: the launcher's console is cp1252 and a decorative glyph here would
                # take the whole build down on the first decline, which is exactly when it fires.
                log(f"  [declined] {tag} would take the map outside the shipped-albedo "
                    f"distribution (cost {_guard['cost']:.1f} -> {_c:.1f})")
                return _guard["keep"]
            _guard["keep"] = np.array(c, np.float32); _guard["cost"] = _c
        else:
            # pre-loop stages are the known-good baseline the loop is measured against
            _guard["keep"] = np.array(c, np.float32); _guard["cost"] = _bar_cost(c)
        return c

    _probe('proj', proj)
    _probe('fill', fill)
    color = proj * reach[..., None] + fill * (1 - reach[..., None])
    _probe('blend+fill', color)

    # ── ⭐ the hair takes the DONOR's structure OUTRIGHT (ship mode) ──────────
    # The fill above already carries the haircut donor's texels — an artist's coherent locks,
    # retinted to the measured tone with the measured spread — but only where no camera reached.
    # Where cameras DID reach, the projection wins, and the projection's hair is the fault the
    # user keeps reporting: a lock is a long thin thing, each view contributes its own copy at its
    # own angle, and averaging cancels the ORIENTATION while preserving the ENERGY, so resolved
    # hair arrives as incoherent bright/dark streaks. The comb below can reorder speckle; it
    # cannot reconcile two references wearing two different cuts, which is what Pettersson's set
    # actually contains. Every one of the 447 shipped heads wears PAINTED hair on this unwrap —
    # so in ship mode the donor's paint is the structure everywhere the mask is confidently hair,
    # and the photographs keep only what they are good for there: tone, spread and the hairline.
    # The transition band (hair_m 0.35..0.65) still blends, so the hairline stays photographic.
    if mode == "ship" and HAIR_FROM_DONOR and fill_hair and donor_np is not None:
        hm_c = np.clip((np.clip(hair_m, 0, 1) - 0.35) / 0.30, 0.0, 1.0)
        color = color * (1 - hm_c[..., None]) + fill * hm_c[..., None]
        log(f"  hair structure: head {donor_id}'s paint over "
            f"{100 * float((hm_c > 0.5).mean()):.0f}% of the map - the projection keeps the "
            f"hairline band and hands the mass to the donor")

    # ── the hair keeps its STRUCTURE and takes the measured HUE ───────────────
    # Retinting the fill fixed the crown and left the rest of the head grey, because the crown is
    # not most of the hair: `reach` is high over the fringe, the part and the temples, so those
    # texels are the PROJECTION, and the projection's hair is the problem. Everywhere a camera
    # resolves hair on this unwrap is a hairline — strands in their own shadow, at the bottom of
    # their tone range, where chroma has collapsed. Measured after retinting the fill alone: the
    # crown target moved to b* 18 and the scalp still read 9.2 against the references' 11-21.
    #
    # So state it once, over all of it. The segmenter measured his hair colour on whole heads in
    # six photographs; that is a better answer than a fringe, and it is the same answer the fill
    # already uses. Chroma only, as ONE shift, so every strand, parting and highlight the
    # photographs resolved survives untouched and only the colour of the hair becomes his.
    if fill_hair and ab is not None:
        hm_ = np.clip(hair_m, 0, 1)
        sel_h = hm_ > 0.5
        if sel_h.sum() > 400:
            clab = _lab(np.clip(color, 0, 255).astype(np.uint8))
            d_ab = ab - np.median(clab[sel_h], 0)[1:]
            clab[..., 1] += d_ab[0] * hm_
            clab[..., 2] += d_ab[1] * hm_
            color = _unlab(np.clip(clab, 0, 255)).astype(np.float32)
            log(f"  hair hue lock: a{d_ab[0]:+.1f} b{d_ab[1]:+.1f} onto the colour the segmenter "
                f"measured over {len(seen_ab)} whole heads of hair")


    # ── the ears keep the ARTIST's structure and take only the player's tone ──
    # Everything else in this file projects photographs onto geometry, and for a cheek or a brow
    # that is right. An ear is different in kind: it is a 5 mm-deep self-occluding shell that every
    # camera sees at a grazing angle, so a global similarity fit — which is all the photographs can
    # give — cannot register it to better than a few millimetres. Averaging views then stacks six
    # slightly displaced helices into a translucent mess, and picking one view instead just prints
    # whatever hair happened to hang over the ear that day. Gate-tuning does not fix either: the
    # information is not in the references.
    #
    # It is in the BASE MAP. The artist drew this ear for this mesh on the shared unwrap, in perfect
    # registration, with the fold structure a photograph at 45 degrees can never resolve. So swap
    # bands: the ear's high frequencies (helix, antihelix, tragus, the shadow in the bowl) come from
    # the artist, its low frequencies (skin tone, how it sits against this player's hairline) from
    # the measurement. This is how the published head pipelines handle ears too, and for the same
    # reason — it is the one part of a head where the template beats the camera.
    # The split has to be BROAD — an ear's forms are 50-odd texels across, so a small-radius split
    # keeps only the fine folds and throws the ear itself away, leaving a flat pink patch. And it
    # runs in Lab: lightness carries every bit of the ear's structure, colour carries none of it, so
    # the artist supplies L and the measurement supplies the hue outright.
    # Keeping only the artist's LIGHTNESS and taking chroma from a blur of our own map was half a
    # measure: it left the ghost's chroma in place and it left the ghost entirely alone outside the
    # tight ellipse. The whole zone is now the artist's pixels, shifted bodily onto our colour — a
    # low-frequency difference, measured at the same broad radius on ALL THREE channels, so every
    # structure the artist drew survives and only the tone becomes this player's. It is the same
    # thing a retoucher does by hand: take the existing asset's ear and colour-match it.
    if ear_zone.max() > 0:
        e = ear_zone[..., None]
        cl, bl = _lab(np.clip(color, 0, 255).astype(np.uint8)), _lab(base_np)
        s = 25.0
        adj = cv2.GaussianBlur(cl - bl, (0, 0), s)
        color = _unlab(np.clip(cl * (1 - e) + (bl + adj) * e, 0, 255)).astype(np.float32)

    # Below the collar the map is never seen except as a sliver of neck under the chin, and every
    # photograph there is jersey, collar or microphone, so the projection there is worthless.
    # Settling it onto ONE flat colour is worse than worthless: `torso` is a ramp over a quarter of
    # the face height, so its top edge printed a hard horizontal band straight across the throat
    # where real shading stopped and flat pink started, and below it the chest went dead. Settle onto
    # the measured skin, as before — the band was never the flat colour, it was the RAMP. `torso`
    # falls from 1 to 0 over a quarter of the face height and is a hard function of a plane, so its
    # edge printed a line straight across the throat. Stretch it to half the face height and blur it,
    # and the same flat colour arrives invisibly. (Settling onto `fill` instead was tried and is
    # wrong: the base map's chest is much paler than its face, so the throat lit up.)
    # and it has to be COMPLETE before that cut, not start at it. `torso` drives the projection
    # weight to zero at 0.15 face-heights below the chin, so `proj` steps there no matter what; the
    # settle can only hide the step by already being at full strength when it arrives. Ramping from
    # the step downwards (the obvious reading) leaves the step at full contrast and merely fades it
    # afterwards. Finish at 0.14 instead, and the same base-map bright streak along the collarbone —
    # which `fill` carries and every later stage preserved — goes with it.
    settle = np.clip((below + 0.02 * face_h) / (0.16 * face_h), 0.0, 1.0)
    settle = cv2.GaussianBlur(settle.astype(np.float32), (0, 0), 10)
    color += (skin_rgb[None, None, :] - color) * settle[..., None]
    _probe('settle', color)

    # ── flatten the map's own shading: an albedo, not a photograph ────────────
    # This used to lay the BASE map's low-frequency lightness over ours, on the theory that its
    # shading was authored for this engine. It is — for the base head's face. Ours is a different
    # shape with its features in different places, so the base's dark eye sockets landed under our
    # eyes and its chin shadow landed on our jaw: the dark streaks down the cheeks were entirely
    # this. Nothing should bake lighting into the colour map at all — the head already ships a
    # normal map and an occlusion map, and the engine lights it from those. So the broad shading
    # that the photographs brought with them gets divided out and the map is left as flat albedo.
    _pre_flatten = color.copy()             # for FEATURE_RESTORE, below the flatten block
    if flatten > 0:
        lab = _lab(np.clip(color, 0, 255).astype(np.uint8))
        L = lab[..., 0]
        # measured on SKIN, applied everywhere: a lighting field belongs to the room, not to the
        # material, so hair must not be allowed to vote on it and must not be flattened towards it.
        m = (np.clip(reach, 0.0, 1.0) * np.clip(skin_m, 0.0, 1.0)).astype(np.float32)
        ref = float(np.median(L[m > 0.5])) if (m > 0.5).any() else 128.0
        # ...and neither may a BEARD, for the same reason and by the same rule. `_flatten` already
        # holds facial hair out of the per-photograph estimate; this is the map-wide pass and it
        # would otherwise put back over the whole jaw at sigma 90 what that one no longer takes out
        # per view. The zone is the landmarks' (below the nose), the weight is the darkness — see
        # `_beard_weight`. Excluded from the VOTE only: the estimate then interpolates the jaw's
        # lighting from the cheeks around it, which is what a light actually does, and the beard's
        # own darkness survives instead of being read as the shadow it is sitting in.
        # `below` runs along the head's own DOWN from landmark 152, the point of the chin, so the
        # chin is 0 and everything above it is NEGATIVE. Measured on Boeser's jaw landmarks, the
        # contour climbs from -0.00 at the chin to -0.51 back at the ear, because a jaw is not level
        # — it rises to the hinge, and a beard rises with it into the sideburn. An upper edge at
        # -0.40 therefore cut the back half of the beard out of its own exemption entirely (landmarks
        # 361 and 323 scored 0.00) while the front, where the beard is thickest, was fine. Carry the
        # zone to -0.62 and let the darkness term do the discriminating, which is what it is for: a
        # cheek at L 184 scores zero on darkness no matter how generous the zone is.
        bz = np.clip((below + 0.62 * face_h) / (0.14 * face_h), 0.0, 1.0) * np.clip(
            (0.30 * face_h - below) / (0.15 * face_h), 0.0, 1.0)
        # Two smaller leaks, both measured at texels where zone and darkness were already 1.0 and
        # the weight still came out 0.38-0.64. `* m` was one: it scales the exemption by the very
        # mask it is meant to open, so the thin-coverage rim of the jaw — exactly the rim the sigma
        # 90 blur reaches across — was under-excluded. The sigma 6 blur was the other; it is there to
        # keep the edge soft, not to erode the middle, so soften it and restore the plateau. What a
        # field this broad needs is the beard fully OUT with a margin: a half-excluded ring still
        # pulls it down.
        dark = np.clip((ref - L - 4.0) / 16.0, 0.0, 1.0)
        beard_m = cv2.GaussianBlur((bz * dark).astype(np.float32), (0, 0), 4.0)
        beard_m = np.clip(beard_m * 1.6, 0.0, 1.0)
        m = m * (1.0 - beard_m)
        # NOT the brows, though, and it was worth proving rather than assuming. A brow is the same
        # KIND of thing as a beard — dark broad hair sitting on skin — so the obvious move is to
        # give it the same exemption from the lighting vote. Tried: the brow left this stage at 14.0
        # L under the forehead instead of 13.8. Nothing. The brow is not lost to the vote, it is
        # lost to the correction, and correctly so: what this stage removes over the brow is the
        # photographs' own brow-ridge shading, which is lighting and does not belong in an albedo
        # map. The brow that reaches the end of the build is 21.8 L under the forehead against the
        # 2009 artist's 14.5 on the same measure, so it is not short of darkness at all.
        ref = float(np.median(L[m > 0.5])) if (m > 0.5).any() else ref
        # The estimate has to DEGRADE to "no correction", never collapse to zero. A weighted blur
        # divided by its own weight is only meaningful where there is weight; on the crown, which is
        # neither skin nor reached, the numerator and denominator both go to nothing and the ratio
        # comes out near zero — so `ref - lo_c` became a flat +150 on lightness and printed a pale
        # grey cap over the back of the skull. That was the "cap": not one stray photo pixel, but
        # this line. So estimate at two scales and fall back — fine where the support is dense, broad
        # where it is thin, and `ref` (i.e. leave the texel alone) where there is none at all.
        lo_c = np.full_like(L, ref)
        for sig, need in ((90.0, 0.02), (25.0, 0.05)):
            den = cv2.GaussianBlur(m, (0, 0), sig)
            est = cv2.GaussianBlur(L * m, (0, 0), sig) / np.maximum(den, 1e-4)
            c = np.clip(den / need, 0.0, 1.0)
            lo_c = est * c + lo_c * (1 - c)
        _DBG.update(fl_lo=lo_c.copy(), fl_m=m.copy(), fl_beard=beard_m.copy(),
                    fl_ref=np.float32(ref), fl_skin=np.clip(skin_m, 0, 1).astype(np.float32))
        lab[..., 0] = np.clip(L + (ref - lo_c) * flatten, 0, 255)
        color = _unlab(lab).astype(np.float32)
    # ── give the features back what flatten took ──────────────────────────────
    # `flatten` estimates the photographs' lighting at sigma 90/25 and removes it, and it already
    # holds a beard exemption. Measured across the stage anyway, on the Pettersson build, in L
    # against the upper face: brow -8.2 -> -4.7, beard -7.8 -> -5.5, eye -18.4 -> -12.2. It is
    # taking 43% of the eyebrows, 30% of the beard and 34% of the eye. Some of that genuinely is
    # brow-RIDGE shading and belongs to the light — but a brow, a lash line and a stubble field are
    # albedo, and no lighting estimate at that radius can tell them apart from the shadow they sit
    # in, which is why the existing exemption is a mask and not a subtraction.
    #
    # So restore, over those features only, and only DOWNWARD: take the pre-flatten lightness where
    # it is darker. Downward-only is the whole trick — a highlight on the brow ridge is exactly the
    # thing flatten is right about and it is never given back, while the mark itself is.
    if flatten > 0 and FEATURE_RESTORE > 0:
        _fm = np.zeros(color.shape[:2], np.float32)
        for _idx in (BROW_R_LM, BROW_L_LM, LASH_R_LM, LASH_L_LM):
            cv2.fillPoly(_fm, [np.asarray(t_lm, np.float32)[list(_idx)].astype(np.int32)], 1.0)
        _fm = np.clip(cv2.GaussianBlur(_fm, (0, 0), 2.5) * 1.5, 0.0, 1.0)
        if "beard_m" in dir():                   # only exists when the flatten block actually ran
            _fm = np.maximum(_fm, np.clip(beard_m, 0.0, 1.0))
        _cl, _pl = _lab(np.clip(color, 0, 255).astype(np.uint8)), \
                   _lab(np.clip(_pre_flatten, 0, 255).astype(np.uint8))
        _give = np.minimum(_pl[..., 0] - _cl[..., 0], 0.0) * _fm * FEATURE_RESTORE
        _cl[..., 0] = np.clip(_cl[..., 0] + _give, 0, 255)
        color = _unlab(_cl).astype(np.float32)
        log(f"  feature restore: up to {abs(float(_give.min())) / 2.55:.1f} L of brow/lash/beard "
            f"given back over {100 * float((_fm > 0.3).mean()):.1f}% of the map")
    _probe('flatten', color)
    if delight > 0:                                    # hold the map at the base map's exposure
        lab, bl = _lab(np.clip(color, 0, 255).astype(np.uint8)), _lab(base_np)
        lab[..., 0] += float(np.median(bl[..., 0]) - np.median(lab[..., 0])) * delight
        color = _unlab(np.clip(lab, 0, 255)).astype(np.float32)

    # ── chroma cleanup ────────────────────────────────────────────────────────
    # A photograph carries the room's colour casts as blotches — a red flush from arena lighting on
    # one cheek, a green bounce off the boards on a temple. Skin's real colour varies slowly; only
    # its LIGHTNESS carries the detail. Low-pass the chroma and keep every bit of the luminance, and
    # the map stops looking like a photo of a face in a room and starts looking painted.
    if chroma > 0:
        lab = _lab(np.clip(color, 0, 255).astype(np.uint8))
        _lab0 = lab.copy()
        for ch in (1, 2):
            sm = cv2.GaussianBlur(lab[..., ch], (0, 0), 9)
            lab[..., ch] += (sm - lab[..., ch]) * chroma
        # …but not over the mouth line. That argument holds for a cheek and fails completely for the
        # aperture: the artist's line is seven texels tall and the blur that answers it is 9, so the
        # low-pass reads a genuine feature as a blotch and averages it into the lip around it.
        # Measured column by column across the line on the last build, with the aperture guard
        # already holding the composite at 0.99: stock 28-32 chroma, built 18-19, flat across all
        # 83 columns — the guard was working and this ran afterwards and undid it. The mouth is not
        # the room's colour cast, it is the one place on this map where chroma IS the detail.
        lab[..., 1:3] += (_lab0[..., 1:3] - lab[..., 1:3]) * aper_m[..., None]
        del _lab0
        # Hair gets the stronger version of the same argument. It is one material with one pigment;
        # everything that varies across it is lightness — strand, parting, the shadow under the
        # fringe. Whatever hue variation is left is the room: skylight on the crown reads blue, and a
        # 9-texel blur cannot fix it because the crown's neighbours are blue too. So hold the whole
        # head of hair at the hue measured where the cameras actually resolved it, and let lightness
        # carry all the structure. This is the single biggest thing that stops the map reading as a
        # photograph of hair and starts it reading as painted hair.
        hs = np.clip(hair_m, 0, 1)[..., None]
        ref_h = hair_ref & (np.abs(lab[..., 1] - 128) + np.abs(lab[..., 2] - 128) < 60)
        if ref_h.sum() > 200:
            hue = np.median(lab[..., 1:3][ref_h].reshape(-1, 2), 0)
            lab[..., 1:3] += (hue[None, None, :] - lab[..., 1:3]) * (0.85 * hs)
        color = _unlab(np.clip(lab, 0, 255)).astype(np.float32)

    # ── meet the body at the tone the body was authored against ───────────────
    # The chest, shoulders and back of the neck are the BODY model — a different asset, textured
    # elsewhere, and painted to match THIS head's stock map. The stock map's collar tone is
    # therefore not a suggestion, it is the boundary condition. Our neck arrives from the
    # photographs instead and lands somewhere else entirely (measured on head 3040: 21-28 levels
    # darker and redder than stock at the collar), which is why the neck read as a different colour
    # from the shoulders it blends into. `settle` above does not help — it settles onto the
    # MEASURED skin, i.e. flatly onto the wrong tone.
    # So carry the map back onto the stock map over the neck: zero at the jaw, complete by the
    # collar (the band sits at 0.5-0.8 face-heights below the chin — measured, not guessed).
    # Nothing is lost: below the throat the map is already flat and no photograph reaches it, so
    # there is no detail to protect, only a tone to match. The ramp spans most of the neck and is
    # blurred, so the two tones meet as a gradient rather than the step the user saw.
    # THIS HAS TO RUN LAST. Put it before `flatten`/`delight` and they re-normalise the map's
    # lightness over the graft and undo it — tried, and the collar came back within 3 levels of
    # where it started.
    collar = np.clip((below - 0.12 * face_h) / (0.40 * face_h), 0.0, 1.0)
    collar = collar * collar * (3 - 2 * collar)                          # smoothstep
    collar = cv2.GaussianBlur(collar.astype(np.float32), (0, 0), 8)
    # The variable is still needed below as a REGION mask even when the graft is off, so the switch
    # only skips the blend. Measured inside the face oval the graft costs nothing (hi-freq 1.66 ->
    # 1.65, L spread 8.33 -> 8.32) — all of its apparent detail cost is the neck, which is exactly
    # the part it is supposed to replace. Kept switchable only so that can be re-checked by eye.
    if not SKIP_COLLAR_GRAFT:
        color += (base_np.astype(np.float32) - color) * collar[..., None]
    _probe('collar', color)

    # ── put back the contrast the BLEND spent ─────────────────────────────────
    # Every stage between the photographs and here trades sharpness for seamlessness, and each trade
    # is individually correct: the weights are blurred twice so joins do not print, the views are
    # averaged so no single camera's noise wins, the top octave is transferred through a soft knee so
    # a hair boundary cannot streak. What none of them can see is the TOTAL. Measured on the shipped
    # Boeser against the 2009 head 138 it replaced, over identical UV boxes: nostrils 9.41 -> 4.73,
    # brows 7.37 -> 5.66, cheek 3.69 -> 2.92. Half the nose gone, and the nostril openings 18 L
    # lighter with it — which is the "the nose looks all kinds of messed up" report, and it is not a
    # modelling fault. The fitted nose is MORE prominent than the base head's (protrusion 5.36 cm
    # against 4.79) and the paint sits on it to within 4 texels; it is just soft.
    #
    # The base head is the calibration. It is not a better photograph of anybody, but it is what 446
    # shipped heads look like, drawn on this exact unwrap for this exact engine, and a face that
    # carries less texture than all of them reads as a smooth blob standing among them no matter how
    # accurate its colour is. So measure both maps' high-frequency energy over the same skin and
    # restore the deficit. Lightness only, through the same soft knee the detail transfer uses, so a
    # seam or a stray edge compresses instead of amplifying — the point is to recover pores and
    # nostrils, not to sharpen mistakes.
    #
    # This has to run BEFORE the grain synthesis below, which measures its sigma off resolved skin —
    # otherwise the invented grain in the fill is calibrated to the soft version and the two halves
    # of the map step apart again, which is the exact thing that block exists to prevent.
    # ONE NUMBER FOR THE WHOLE MAP IS NOT ENOUGH, and the reason is the shape of the loss. A single
    # gain measured over skin came out at x1.22 and moved the cheek 2.92 -> 3.40 while the nostrils,
    # the deepest hole, went 4.73 -> 5.37 against the base head's 9.41. The deficit is not uniform:
    # it is concentrated wherever the views disagreed most and every knee, gate and blur in the
    # pipeline bit at once, which is precisely the dark features — nostril, lash line, lip line.
    # So make the gain a FIELD, the local ratio of the two energies, smoothed far wider than the
    # detail it corrects so it carries no structure of its own and cannot print an edge.
    mlab = _lab(np.clip(color, 0, 255).astype(np.uint8))
    mL = mlab[..., 0]
    fine = (raw_have > 0.7) & (skin_m > 0.5) & (hairy < 0.3) & (aper_m < 0.2)
    # held out of ship mode: the A/B renders read both restore bands as part of the overcooking —
    # see SHIP_SKIP. The flatten's own feature restore already gives the marks back.
    if "contrast-restore" not in _skip and fine.sum() > 500:
        # BAND-LIMITED, and both sides of the ratio. A plain high-pass runs all the way down to the
        # single texel, and at that scale neither map is carrying anything worth restoring: the
        # target `base_np` has been through DXT, so its finest band is block artifact, and `mL` at
        # that scale is the reference photographs' sensor and JPEG noise. Measured over the base's
        # own skin mask, the pixel band (finer than 1.2 texels) stands at 2.65 against a structure
        # band of 2.17 — more than half of what this ratio was chasing was compression, and the
        # gain, up to x3, was spending itself amplifying grain to reach it. That is the "choppy"
        # report, and it is why the build reads noisier than the base map on smooth forehead and
        # cheek while still reading softer than it on the nostrils. A pore, a stubble root and a
        # nostril edge all live between 1.2 and 3 texels on a 512 map, so measure and restore
        # exactly that band and leave the noise floor of both maps out of the argument.
        hp_now = _bp(mL, 1.2, 3.0)
        bL = _lab(base_np)[..., 0]
        hp_base = _bp(bL, 1.2, 3.0)
        e_now = cv2.GaussianBlur(np.abs(hp_now), (0, 0), 12.0)
        e_base = cv2.GaussianBlur(np.abs(hp_base), (0, 0), 12.0)
        # The ceiling is 3, not 2. It is not a sharpening amount — the target is the BASE head's own
        # local energy, so the gain can never carry the map past the asset we are matching, and a cap
        # only stops places from REACHING it. At 2 the field saturated: the nostrils came back to
        # 5.53 against stock's 9.41 and the forehead to 3.09 against 5.74, both of them pinned at the
        # cap, while the cheek — which was never far off — landed at 3.58 against 3.69 on its own.
        # ⭐ …and the base head only sets the SHAPE of the target, not its level. Matching a 2009
        # asset is the wrong ambition when a photograph of this player's own skin is in hand, and
        # measured, every skin region on both finished heads landed at about two thirds of its
        # portrait: brows 3.05 against 3.93 and 2.14 against 3.84, the mouth 3.37 against 5.67, the
        # eyes 9.10 against 11.16. Over the whole face, everything finer than three texels stands at
        # 2.49 and 2.17 against the portraits' 3.28 and 3.32 — and the RENDER of that map measures
        # 2.50 and 2.61, so the loss is in the map and not in the rasteriser or the lighting.
        #
        # So keep `e_base` — it is a FIELD, and it knows a nostril needs more than a forehead, which
        # no scalar can — and rescale it bodily by how far short of the photograph this map actually
        # is. A ratio of two identically-measured numbers, so it carries no units and no assumption
        # about what the base head was worth; 1.0 when we are already there, and never below it.
        want_s = e_base
        if skin_grain_e is not None:
            _mine = float((mL - cv2.GaussianBlur(mL, (0, 0), 3.0))[fine].std())
            want_s = e_base * max(1.0, min(skin_grain_e / max(_mine, 1e-6), 2.5))
        g = np.clip(want_s / np.maximum(e_now, 0.25), 1.0, 4.0)
        add = SOFT_KNEE * np.tanh(hp_now * (g - 1.0) / SOFT_KNEE)
        # Applied wherever the cameras reached — the measuring mask above is skin-only because a
        # gain read off hair would be read off strands, but the nostril openings and the lash
        # line are exactly what needs restoring and the mask calls both of them dark-not-skin.
        zone = cv2.GaussianBlur(np.clip(raw_have / 0.7, 0, 1) * (1.0 - aper_m), (0, 0), 3)
        mlab[..., 0] = np.clip(mL + add * zone, 0, 255)
        color = _unlab(mlab).astype(np.float32)
        log(f"  contrast restore: the map carries "
            f"{float((mL - cv2.GaussianBlur(mL, (0, 0), 3.0))[fine].std()):.2f} of energy finer "
            f"than three texels against the photographs' "
            f"{skin_grain_e if skin_grain_e is None else round(skin_grain_e, 2)} over the same "
            f"skin, on the base head's own {hp_base[fine].std():.2f}-shaped field - "
            f"x{g[fine].mean():.2f} put back on average, x{g[zone > 0.5].max():.2f} at the worst")

        # ── and the SAME argument one octave up, where the features live ─────────────────────────
        # Everything above operates between 1.2 and 3.0 texels, which is pores and stubble roots. The
        # things a viewer names when a head reads soft — the brow, the lash line, the lip line, the
        # nostril, the nasolabial — are three to twelve texels wide on this unwrap, and no step in
        # this file has ever touched that band. Measured on the finished heads against their own
        # portraits, that is exactly where the deficit is: brows 3.05 against 3.93 and 2.14 against
        # 3.84, the mouth 3.37 against 5.67, the eyes 9.10 against 11.16, while the fine band was
        # already within a few percent. Same construction as above so it inherits the same
        # protections: a smoothed local ratio, a soft knee, and a lower ceiling because at this scale
        # a runaway gain does not read as sharp, it reads as blotchy.
        if skin_mid_e is not None:
            m2 = _lab(np.clip(color, 0, 255).astype(np.uint8))
            mid_now = _bp(m2[..., 0].astype(np.float32), 3.0, 12.0)
            e_mid = cv2.GaussianBlur(np.abs(mid_now), (0, 0), 20.0)
            # ⭐ …including, and this was missing, the FIELD. This gain was a scalar over a local
            # energy, and a scalar over a local energy is not a restore, it is a leveller: it is
            # largest exactly where the map has least and falls to 1.0 exactly where the map has
            # most, so it drives every locality toward one uniform energy. On a face that is
            # backwards. Measured on the finished renders against the portraits, in this very band,
            # the ratio model/photo came out forehead 1.72 and 1.42, cheeks 1.32 and 1.82 — pumped
            # past the photograph — while brows 0.85/0.70, eyes 0.75/0.68, mouth 0.69/0.78, chin
            # 0.77/0.69 and jaw 0.84/0.72 were all left short. Two different faces, same split, and
            # the mean over skin was 0.99 and 0.97: the LEVEL was already right and every point of
            # the error was this redistribution. A photograph's texture is not spread evenly — on
            # Boeser the forehead carries 0.85 in this band and the mouth 5.90 — and the cap at 2.0
            # was being spent on the forehead while the mouth got nothing.
            #
            # So shape it the way the octave below is shaped, and for the same stated reason: the
            # base head knows a lip line needs more than a forehead. Take its own 3-12 texel field
            # as the shape and rescale it bodily by how far short of the photograph this map is,
            # which is the same ratio-of-two-identical-measurements the fine band uses — no units,
            # no claim that a 2009 asset's LEVEL is the right one, 1.0 when we are already there.
            base_mid = _bp(bL, 3.0, 12.0)
            e_base_mid = cv2.GaussianBlur(np.abs(base_mid), (0, 0), 20.0)
            want_mid = e_base_mid * max(1.0, min(
                skin_mid_e / max(float(mid_now[fine].std()), 1e-6), 2.5))
            # ⚠ Floored at 1.0, and it was worth trying not to be. The rendered forehead and cheeks
            # carry 1.3-1.8x the portraits' feature-band texture while the features carry 0.6-0.9,
            # so letting this gain attenuate looked like the way to fix the distribution. Measured,
            # a floor of 0.6 moved the rendered forehead by 0.00 and cost Makar real detail — chin
            # 0.64 -> 0.51, jaw 0.70 -> 0.61, mouth 0.78 -> 0.71. Both halves of that are explained
            # by a shading-only render (uniform albedo, flat normal): the forehead measures 0.11
            # there and the chin 0.15, so their texture really is albedo and really is downstream of
            # this stage — the level pass and the grain synthesis both run after it — while the eyes
            # measure 7.17 of the portrait's 9.29 and the jaw 1.80 of 2.44, which is to say most of
            # what the QA reads in those regions is the socket and the mandible catching the light
            # and no albedo stage can add to it or take from it. Attenuating here spends itself on
            # the wrong regions in both directions. Leave it a restore.
            g2 = np.clip(want_mid / np.maximum(e_mid, 0.25), 1.0, 2.0)
            m2[..., 0] = np.clip(m2[..., 0] + SOFT_KNEE * np.tanh(mid_now * (g2 - 1.0) / SOFT_KNEE)
                                 * zone, 0, 255)
            color = _unlab(m2).astype(np.float32)
            log(f"  feature restore: the 3-12 texel band carries {mid_now[fine].std():.2f} against "
                f"the {skin_mid_e:.2f} the photographs measure over the same skin - "
                f"x{float(g2[fine].mean()):.2f} put back on average")

    _probe('contrast-restore', color)

    # ── the HAIR gets the same argument, and needs it more ───────────────────
    # The restore above is skin-only on purpose: a gain measured over hair is measured over strands,
    # and a local ratio would find a huge deficit wherever strands blurred and sharpen that patch to
    # a wire brush with its own outline. But leaving hair out entirely leaves it wherever the blend
    # dropped it, and the blend costs hair more than skin — a strand is a one-texel feature and every
    # octave of the pyramid is an average. Measured against head 138 over the same UV boxes: the side
    # of the head carried 7.33 of detail against the artist's 10.43 and the crown 8.49 against 9.17.
    #
    # And the LEVEL was already right, which is what makes this the whole of the hair fault. Measured
    # as a low-frequency field over the whole map, our hair sits 62.6 L above the base head's and a
    # hand-corrected map's sits 64.6 above it — the same hair, the same brightness, within two L.
    # What separates them is that theirs has strands in it and ours does not: over the base's hair
    # mask their lightness runs a MAD of 51.9 against our 29.7 at the same mean. Hair that is the
    # right colour and the right brightness with no shadow between the strands is exactly "a sad,
    # flat colour that doesn't even look like hair", and it is a CONTRAST fault, not a tone fault —
    # every earlier attempt to fix it by moving the tone was aimed at the wrong statistic.
    #
    # Two corrections to the obvious form of that, both measured, both the opposite of what I first
    # wrote. A scale ladder over the hair, ours against the base and against the hand-corrected map:
    #
    #                       band 0.8-3   3-8    8-20
    #   whole hair, ours          6.94  6.05    6.97
    #   whole hair, base          6.69  5.64    7.98
    #   whole hair, hand-corrected 8.35  6.58    7.22
    #   side hair only, ours      6.05  5.11    5.16
    #   side hair only, base      8.10  5.77    5.68
    #   side hair only, corrected 9.58  5.67    6.45
    #
    # First: the BASE IS THE WRONG YARDSTICK for hair. Averaged over the whole hair mask ours
    # already carries MORE fine energy than the 2009 paint does (6.94 against 6.69), so a gain of
    # base/ours clamps to 1.00 and the block does nothing — which is exactly what it logged. The
    # standard being asked for is the hand-corrected map, and that sits about a fifth above BOTH of
    # them at the strand scale. So aim at the base times HAIR_DETAIL, not at the base.
    #
    # Second: A FIELD, not a scalar, and for once for the same reason as on skin. The deficit is not
    # spread evenly — over the top of the head we are level with the artist, and down the SIDES,
    # where the cameras see hair at a grazing angle and the projection smears it along the strand,
    # we carry 6.05 against his 9.58. One number averaged over both regions is a number that fits
    # neither. The local energy ratio finds the sides on its own.
    # ── the hair keeps its own tonal RANGE ────────────────────────────────────
    # The strand contrast above is a fine-scale statement and it is not the whole fault. Measured on
    # the finished map, the crown box reads 104.5 and the side box 106.7 — two L apart. The
    # hand-corrected map runs 113.8 and 58.7, fifty-five apart, and the shipped 2009 paint runs 41
    # and 57. Every reference has a mass with a lit top and a dark side; ours has a uniform sheet in
    # the right colour, which is exactly the "sad flat colour that doesn't look like hair".
    #
    # The earlier reading that our hair matched the references "within 2 L" was an average over both
    # boxes at once, where being too dark on the crown and far too bright at the side cancel. Levels
    # measured over a region that contains its own opposite are not measurements.
    #
    # The target cannot come from the base map: its crown is DARKER than its sides, the reverse of
    # both the photographs and the user's map, because the 2009 artist painted a dark cap. It comes
    # from the photographs, where the segmenter has a whole head of hair and its p10-to-p90 span is
    # a direct measurement of the thing that is missing. Applied to the BROAD component only, about
    # the hair's own median, so the level stays where the projection and the hue lock put it and only
    # the spread is restored — and gated to a stretch, never a squeeze, because no camera angle can
    # manufacture range and finding too much of it is not a fault worth correcting.
    # …and the SHAPE of that range cannot come from our own map by stretching it. Stretching is
    # rank-preserving: it can only make texels that are already the dark ones darker. Measured, the
    # first version of this did exactly what that predicts — the crown went 104.5 -> 112.4 against
    # the artist's 113.8 and landed, and the side did not move at all, 106.7 -> 106.1 against 58.7.
    # Our side hair is not ranked dark, because the projection samples it from frontal photographs
    # where that hair is on the silhouette and rim-lit, and it is faithful to those pixels. The
    # references disagree with the photograph and agree with each other to within one L.
    #
    # They are not being unfaithful, they are baking OCCLUSION. Hair is a self-shadowing mass: the
    # crown is open to the sky and the sides and nape are buried against the head, and a low-poly
    # shell with a hair-coloured normal map cannot produce that in engine, so both artists put it in
    # the albedo. That is geometry, and this file already computes it — mesh_occlusion, from the
    # fitted mesh, the same term the occlusion map gets. So drive the range with the AO and let the
    # photographs set only its SIZE: the amplitude is whatever makes the hair's own span match the
    # p10-to-p90 the segmenter measured across whole heads. The constant is measured, not tuned, and
    # the pattern is derived rather than assumed.
    # The mesh's own AO is not that driver, and one build settled it: over the hair its whole p10-to
    # -p90 spread is NINE PERCENT, and skewed, so the crown sits at the median and nearly every other
    # texel below it. Scaled to a 117 L span that saturates into a flat darkening — measured, the
    # crown went 112.3 -> 104.6, away from the artist's 113.8, and the side moved 106.1 -> 100.1,
    # nowhere near 58.7. A head shell simply is not a self-occluding form; the hair is, and the hair
    # is not in the geometry.
    #
    # What both artists actually painted is a KEY FROM ABOVE. The crown faces the light, the sides
    # face across it and the nape faces away, and that is in the geometry — as the surface normal
    # against the head's own up, which this file already derives from the chin-to-forehead landmarks
    # rather than assuming a world axis. Soft-kneed rather than clipped, because the cap is reached
    # on a real head of hair and a hard clip would flatten the crown into a plate at the ceiling.
    hs_m = np.clip(hair_m, 0, 1)
    hs_sel = hs_m > 0.5
    # ⭐ Does this head have hair GEOMETRY, or is its hair painted onto the skull? The library holds
    # both: most heads carry a scalp shell (`facial_hair` bit 16384) that the renderer lights as its
    # own form, and the rest - the buzz cuts - have nothing above the skull at all. Everything below
    # that reasons about hair level was measured on the first kind, and on the second kind it is
    # aiming at something paint cannot reach. See `_hair_shell` for what that costs.
    _hair_shell = _has_scalp_shell(base_id, game_dir)
    # what the OPEN-LOOP hair level step below put in, kept so the closed loop can hand it back if
    # it turns out the albedo has no authority over this head's hair at all. See `_hask`.
    _hopen_add = None
    if hair_span > 1.0 and hs_sel.sum() > 400:
        rlab0 = _lab(np.clip(color, 0, 255).astype(np.uint8))
        # The key is median-preserving, so it can only ever set the SPREAD; the mass also has to sit
        # at the right level, and with the spread restored the mistake became legible — crown 131.6
        # against the artist's 113.8 and side 94.2 against 58.7, the same 25 L too bright at both
        # ends. The level cannot come from the base head, whose hair belongs to a different player
        # and is nearly black. It comes from where the span came from: a whole head of this player's
        # hair, in the photographs, as the segmenter found it.
        #
        # ⭐ MEASURED FAULT, fixed here. That target used to be the photographs' ABSOLUTE median L.
        # A photograph's absolute L is its exposure, and this map is a de-lit albedo, so the two are
        # not the same quantity and matching them is a coincidence at best. Measured on the finished
        # heads against their own portraits, in a render: Boeser's hair sits 23.2 L below his face
        # where the photograph says 8.3 — 15 L too dark, comfortably the largest colour fault on the
        # head, and it survives every downstream step because nothing after this asks about level.
        # What is exposure-free, and what the eye actually reads, is hair MINUS face. Anchor that.
        _skin_sel = (np.clip(skin_m, 0, 1) > 0.5) & (hs_m < 0.2)
        # ⭐ TWO MEASURED FAULTS, both fixed here, and they had been cancelling each other unevenly:
        # one head came out 13 L too light and the other 5 L too dark off this same code.
        #
        # FIRST, the REGION. `hair_rel` above is the median over everything the segmenter calls hair
        # in a photograph, and it was matched against the median over the whole UV hair mask. Neither
        # is the hair anyone looks at. A head of hair runs from a lit crown to a nape no camera in
        # any reference set has ever seen, and the two ends are far apart — the renderer alone
        # spreads them 55 L. So the whole-mask median is dominated on the map side by fill and
        # occlusion over the back of the head, and on the photograph side by whichever of the sides
        # happened to be turned away. Measured, the whole-mask and front-of-head statistics disagree
        # by 19 L on Boeser and 1 L on Makar. Use the front of the head on both sides instead:
        # `hair_rel_f`, and the map's own hair inside the same box.
        #
        # SECOND, the LIGHT. This target is read off a lit photograph and written into a de-lit
        # albedo that the renderer is about to light again. With no hair information in the map at
        # all, the renderer already puts this region 12 L under the face — see HAIR_RENDER_SHADE,
        # measured on both heads. An albedo that also carries those 12 L ships them twice, and the
        # finished head renders that much too dark. Aim the albedo 12 L high so the RENDER lands on
        # the photograph, which is the only place the comparison is meaningful.
        _seen = have > 0.5
        _hf = hs_sel & _hairm & _seen                  # the map's hair, on the front of the head
        _ff = _facem & _seen
        if hair_rel_f is not None and _hf.sum() > 400 and _ff.sum() > 400:
            tgt = (float(np.median(rlab0[..., 0][_ff])) + hair_rel_f
                   + (HAIR_RENDER_SHADE * 2.55 if _hair_shell else 0.0))
            _from = float(np.median(rlab0[..., 0][_hf]))
        elif hair_rel is not None and _skin_sel.sum() > 400:
            tgt = float(np.median(rlab0[..., 0][_skin_sel])) + hair_rel
            _from = float(np.median(rlab0[..., 0][hs_sel]))
        else:
            tgt = hair_med                       # no face in frame; the old absolute anchor
            _from = float(np.median(rlab0[..., 0][hs_sel]))
        d_hm = float(np.clip(tgt - _from, -HAIR_LEVEL_CAP, HAIR_LEVEL_CAP))
        # ⭐ MEASURED. On a dark-haired head this target sits well down the range and the cap is the
        # only thing that ever binds. On a BLOND head it does not: his hair is about as light as his
        # face, so `hair_rel_f` is near zero and the whole ask is the HAIR_RENDER_SHADE aim - which
        # lands the target above the face, near white. Spent open-loop that put the scalp against
        # `_soft_lift`'s knee, where the closed loop below could no longer move it, and the head
        # rendered with a chalk plate for a crown.
        #
        # The aim is also worth far less than it costs. Measured on the render, a unit of albedo over
        # the hair buys about 0.24 of a unit of hair-minus-face, because it lifts the forehead inside
        # the same box: the 12 L aim buys under 3 L of what it is aiming at, and spends 12 L of the
        # only headroom the closed loop has. So let it ask, but never past the knee - leave the map
        # room to be corrected against an actual render rather than against this estimate.
        _room = LIFT_KNEE - float(np.percentile(rlab0[..., 0][hs_sel], 90))
        if d_hm > _room:
            log(f"  hair level: the anchor asked for {d_hm / 2.55:+.1f} L but his hair is light "
                f"enough that only {max(0.0, _room) / 2.55:+.1f} fits under the highlight knee - "
                f"the render check below gets the rest")
            d_hm = float(max(0.0, _room))
        if abs(d_hm) >= HAIR_LEVEL_CAP - 1e-3:
            log(f"  ! hair level: the anchor asked for {tgt - _from:+.0f} L and the cap allowed "
                f"{d_hm:+.0f} - the cap is setting this head's hair tone, not bounding it")
        _hopen_add = d_hm * cv2.GaussianBlur(hs_m, (0, 0), 3)
        rlab0[..., 0] = _soft_lift(rlab0[..., 0], _hopen_add)
        color = _unlab(rlab0).astype(np.float32)
        log(f"  hair level: the front of the head sits {abs(d_hm) / 2.55:.1f} L "
            f"{'below' if d_hm > 0 else 'above'} where it must for the RENDER to match the "
            f"{'?' if hair_rel_f is None else f'{hair_rel_f / 2.55:.1f}'} L under his own face the "
            f"photographs measure there - corrected")

        up = -(nrm_uv * dn).sum(2)                     # +1 crown, 0 sides, -1 under the fringe
        u10, u50, u90 = np.percentile(up[hs_sel], [10, 50, 90])
        # ⭐ THE CAP WAS SETTING THE ANSWER, again — the very failure the note above HAIR_OCC_MAX
        # describes, just moved rather than removed. `k` is chosen so that a LINEAR key would span
        # `hair_span`, and the tanh is then applied on top, so the delivered span is always short and
        # short by a different amount for every head: measured, Boeser's 124 L span and Makar's 90 L
        # both came out as ±43 and ±40, indistinguishable. That is why their crowns then read 9 L too
        # dark and 11 L too light against the same photographs off one shared level anchor — the
        # level was fine and the SPREAD was flat.
        #
        # So solve for the gain instead of assuming it: bisect `k` until the delivered p10-to-p90,
        # tanh and all, IS the span the photographs measured. The ceiling stays, at a multiple of the
        # span rather than a constant, so it does what a soft knee is for — trimming the few texels
        # past the ends — and never what it was doing, which is deciding where the ends are.
        # ⭐ …and the SPAN was being paid for twice, which is the same mistake as the level above and
        # a much more expensive one. `hair_span` is a photograph's p10-to-p90 across a head of hair,
        # and a head of hair is a self-occluding mass, so nearly all of that number is the LIGHT: a
        # lit crown against a side that is turned away. It was being written into the albedo, keyed
        # off the surface normal — which is to say, painted on as if it were tone — and then the
        # renderer lit the head again on top of it.
        #
        # Measured, with a flat albedo and nothing else changed, the renderer already spreads this
        # hair 54.1 L on Boeser and 56.9 on Makar. The photographs ask for 48.6 and 35.3. The
        # renderer is not short of span; it delivers MORE than either photograph, and every level of
        # key on top was surplus. On Boeser that surplus was +82 on the crown, which is what put his
        # hair BRIGHTER than his own face in the finished map.
        #
        # So ask only for the deficit. On both reference heads the deficit is zero and this block
        # does nothing, which is the correct amount of nothing; it stays rather than being deleted
        # because a reference set shot flat really could measure a wider span than the light gives,
        # and because the deficit is the honest statement of what an albedo owes here.
        want_span = max(0.0, hair_span - HAIR_RENDER_SPAN * 2.55)
        occ_max = max(HAIR_OCC_MAX, 1.5 * want_span)

        def _span(kk):
            d = occ_max * np.tanh((up[hs_sel] - u50) * kk / occ_max)
            p = np.percentile(d, [10, 90])
            return float(p[1] - p[0])

        lo, hi = 0.0, float(np.clip(want_span / max(u90 - u10, 1e-3), 1e-3, 400.0))
        while _span(hi) < want_span and hi < 1e5:       # the tanh eats it; ask for more gain
            hi *= 2.0
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if _span(mid) < want_span:
                lo = mid
            else:
                hi = mid
        k = 0.5 * (lo + hi)
        d_hair = occ_max * np.tanh((up - u50) * k / occ_max)
        rlab = _lab(np.clip(color, 0, 255).astype(np.uint8))
        rlab[..., 0] = np.clip(rlab[..., 0] + d_hair * cv2.GaussianBlur(hs_m, (0, 0), 3), 0, 255)
        color = _unlab(rlab).astype(np.float32)
        log(f"  hair key: the photographs span {hair_span / 2.55:.0f} L crown to side and the "
            f"renderer already gives {HAIR_RENDER_SPAN:.0f} of it, so the albedo owes "
            f"{want_span / 2.55:.0f}"
            + (" - nothing to add" if want_span <= 0 else
               f" (delivered {_span(k) / 2.55:.0f}), that is "
               f"{abs(float(d_hair[hs_sel].min())) / 2.55:.0f} L off the sides and "
               f"{float(d_hair[hs_sel].max()) / 2.55:+.0f} on the crown"))
    _probe('hair-key', color)

    HAIR_DETAIL = 1.25
    hsel = (np.clip(hairy, 0, 1) > 0.5) & (raw_have > 0.3)
    if hsel.sum() > 500:
        hlab = _lab(np.clip(color, 0, 255).astype(np.uint8))
        hL = hlab[..., 0]
        hp_h, hp_hb = _bp(hL, 0.8, 3.0), _bp(_lab(base_np)[..., 0], 0.8, 3.0)
        eh_now = cv2.GaussianBlur(np.abs(hp_h), (0, 0), 12.0)
        eh_base = cv2.GaussianBlur(np.abs(hp_hb), (0, 0), 12.0)
        # Ceiling raised to 3.5 for the same reason as HAIR_OCC_MAX: the field was pinned at the old
        # 2.5 down the whole side of the head, where the strand detail still reads 8.3 against the
        # hand-corrected map's 11.2. A clamp that is reached everywhere it matters is not a clamp.
        # ⭐ The artist is a FLOOR, not the target. `eh_base` is the 2009 head's own hair, which is a
        # painted shell — on Makar the finished map reads 3.56 on this band where his portrait reads
        # 7.79, and chasing the artist can never close that because the artist never had it. Where a
        # photograph of THIS player's hair was measurable at the map's scale, aim at that instead and
        # keep the artist only as the fallback. The 3.5 clamp still stands, so a wildly-lit reference
        # cannot run away with it, and `SOFT_KNEE` still keeps the result off the rails.
        # ⭐ MEASURED FAULT, fixed here. Everything below was written for heads whose hair came out
        # too FLAT, so every term is a floor and the gain is clamped at a minimum of 1.0 — this
        # stage could add strand energy and could never take any away. That is a safe assumption
        # only while the player's hair is longer than whatever the base map paints, and it fails
        # hard the other way: on a buzz cut the map arrives carrying a donor's full head of hair
        # (the haircut fill takes structure from whichever shipped head matches best, and none of
        # the 447 is shaved), the photographs measure a third of that, and the clamp refuses to
        # give any of it back. Measured on Pettersson: 4.68 of strand on the map against 3.14 in
        # his photographs, and the stage still reported "x1.97 put back".
        #
        # Where the photographs measured this player's own hair they are the TARGET, in both
        # directions. The artist's map stays the floor only when no photograph could see the hair.
        want = eh_base * HAIR_DETAIL
        _glo = 1.0
        if hair_fine_e is not None:
            want = np.full_like(eh_now, float(hair_fine_e))
            _glo = HAIR_STRAND_FLOOR
        # …and the same field, rescaled bodily until its SPREAD is the photograph's. `hair_fine_e`
        # above is a mean and it is compared against a local mean field, which is self-consistent
        # and measures the wrong thing; asking a uniform scalar to be the floor of a field also
        # levels the hair the way the feature restore was levelling the face. Take the field's own
        # shape — the base map knows a parting carries more than a nape — and scale it by a ratio of
        # two identically-computed standard deviations, one from the photograph and one from this
        # map, over the same band and at the same scale. No units, 1.0 when we are already there.
        if hair_fine_s is not None and hair_fine_e is None:
            # only when the energy target is absent: taking the greater of the two lets the spread
            # estimate veto a reduction the energy estimate correctly asked for, which is how the
            # buzz cut kept its donor's strands even after the target was measured.
            _mine_s = float(hp_h[hsel].std())
            _r = float(np.clip(hair_fine_s / max(_mine_s, 1e-6), 1.0, 3.5))
            want = np.maximum(want, eh_now * _r)
        gh = np.clip(want / np.maximum(eh_now, 0.25), _glo, 3.5)
        hz = cv2.GaussianBlur(np.clip(hair_m, 0, 1), (0, 0), 3)
        hlab[..., 0] = np.clip(hL + SOFT_KNEE * np.tanh(hp_h * (gh - 1.0) / SOFT_KNEE) * hz, 0, 255)
        color = _unlab(hlab).astype(np.float32)
        e_h, e_hb = float(np.abs(hp_h[hsel]).mean()), float(np.abs(hp_hb[hsel]).mean())
        log(f"  hair contrast: strands carry {e_h:.2f} against the artist's {e_hb:.2f} and the "
            f"photographs' {hair_fine_e if hair_fine_e is None else round(hair_fine_e, 2)} over the "
            f"same hair - x{float(gh[hsel].mean()):.2f} put back on average, "
            f"x{float(np.percentile(gh[hsel], 98)):.2f} down the flattest sides"
            + (f"; spread {float(hp_h[hsel].std()):.2f} against the photographs' "
               f"{hair_fine_s:.2f}" if hair_fine_s is not None else ""))

        # ── and now COMB it ──────────────────────────────────────────────────────────────────
        # Everything above sets how much detail the hair carries. None of it sets whether that
        # detail LINES UP, and down the sides of the head it does not: measured against the
        # hand-corrected map, the level is right (the mass sits 85 L below his own skin against
        # its 79), the crown-to-side form is right (54 L against 58) and the strand energy is
        # within 7% - yet the side box reads 0.659 on the structure tensor's coherence where the
        # hand-corrected map reads 0.799 and even the artist's own stock head reads 0.741. Ours is
        # the only one of the three that is MUSH there, and it is mush at the right brightness with
        # the right contrast, which is why every scalar said the hair was finished and it still
        # looked choppy.
        #
        # Multi-view blending is what does it. A lock of hair is a long thin thing, so each view
        # contributes its own copy at its own angle, and averaging them cancels the ORIENTATION
        # while preserving the ENERGY. The sides get the worst of it because that is where the most
        # views overlap at the most grazing angles.
        #
        # So put the orientation back rather than more contrast: smooth the fine band ALONG its own
        # dominant direction and leave it untouched across, which turns speckle into strands without
        # inventing a single one. Energy is renormalised back to what it was, so this only ever
        # REORDERS detail. Strength is the coherence deficit against the artist's own hair in the
        # same texels - a region already combed is left alone, and a base head whose hair happens to
        # be mush cannot ask for more of it.
        hL2 = _lab(np.clip(color, 0, 255).astype(np.uint8))
        fine = _bp(hL2[..., 0], 0.8, 3.0)
        vx, vy = _flow(cv2.GaussianBlur(hL2[..., 0], (0, 0), 0.8))
        streak = _lic(fine, vx, vy)
        streak = streak * np.clip(cv2.GaussianBlur(np.abs(fine), (0, 0), 12.0)
                                  / np.maximum(cv2.GaussianBlur(np.abs(streak), (0, 0), 12.0),
                                               1e-3), 1.0, 4.0)
        c_now = _coherence(hL2[..., 0])
        need = np.clip((_coherence(_lab(base_np)[..., 0]) - c_now) / 0.15, 0, 1)
        wc = cv2.GaussianBlur(np.clip(hair_m, 0, 1) * need, (0, 0), 3)
        hL2[..., 0] = np.clip(hL2[..., 0] - fine * wc + streak * wc, 0, 255)
        color = _unlab(hL2).astype(np.float32)
        log(f"  hair comb: strands line up {float(c_now[hsel].mean()):.2f} against the artist's "
            f"{float(_coherence(_lab(base_np)[..., 0])[hsel].mean()):.2f} over the same hair - "
            f"combed along their own direction over "
            f"{float((wc > 0.05).mean() * 100):.0f}% of the map")
    _probe('hair-comb', color)

    # Texels outside the UV island are never sampled by the mesh, but they ARE averaged into the mip
    # chain and bleed back in at distance, and they had been left as whatever the accumulator last
    # held — a grey band along the bottom edge. Give them the retinted base map like everything else
    # unreachable here.
    # ── the filled regions need GRAIN, or the join reads as a splice ───────────
    # MEASURED, and not what it looks like. Down the centre of the map the mean lightness runs
    # CONTINUOUSLY from jaw to chest — there is no colour step at the neck at all, and the largest
    # steps in the whole column are the map border and the hairline. What steps is TEXTURE:
    # high-frequency energy is 3.6 over the photographed jaw and 0.57 twenty texels below it, a
    # six-fold drop, because the fill is the base map's smooth paint and carries no skin at all.
    # That discontinuity in the STATISTICS is what the eye reads as two images spliced together,
    # and no amount of colour matching touches it.
    #
    # So measure the grain where the photographs actually resolved skin — its magnitude and its
    # scale, both taken from this face and not from a constant — and synthesize the same thing
    # wherever the fill took over. It is not detail and it is not pretending to be: it carries no
    # information about the player. It is there so the transition from measured to invented has no
    # signature, which is exactly the job a film grain does over a matte painting.
    clab = _lab(np.clip(color, 0, 255).astype(np.uint8))
    cL = clab[..., 0]
    seen = (raw_have > 0.7) & (skin_m > 0.5) & (hairy < 0.3)
    # held out of ship mode ("sanding a chest is what choppy looks like at arm's length") — the
    # A/B renders preferred the un-grained flatten, and the collar graft covers the splice.
    if "grain" not in _skip and seen.sum() > 500:
        sd = float((cL - cv2.GaussianBlur(cL, (0, 0), 3.0))[seen].std())
        rng = np.random.RandomState(12345)              # deterministic: same photos, same map
        n = rng.randn(H, W).astype(np.float32)
        n = cv2.GaussianBlur(n, (0, 0), 0.8) - cv2.GaussianBlur(n, (0, 0), 3.0)
        n *= sd / max(float(n.std()), 1e-6)
        # Only where the map is NOT already carrying measured grain, and only on skin: hair has its
        # own structure and the fill there is a retinted photograph of hair, not blank paint. Keyed
        # off `settle` as well as coverage — `settle` replaces everything below the chin with one
        # flat colour, and THAT plate is the actual splice. Note this block has to sit at the very
        # end of the pipeline, past settle and past the flatten pass: an earlier placement measured
        # as having literally no effect, because settle simply overwrote it.
        need = np.maximum(np.clip(1.0 - raw_have / 0.7, 0.0, 1.0), settle)
        need = cv2.GaussianBlur(need * np.clip(skin_m, 0, 1) * (1.0 - hairy), (0, 0), 6)
        # …but ONE amplitude for the whole fill is wrong, and the chest proves it. `sd` is measured
        # on photographed FACE skin, which is the busiest skin on the head; the fill it is sprayed
        # into runs from the jaw — where that is the right answer, because the splice is there — all
        # the way down over a chest and shoulders that the artist painted almost perfectly smooth.
        # Measured over the shoulder box: the build carried 4.72 of detail and 32.8 of pixel-scale
        # energy against the shipped head's 0.89 and 7.19, and a hand-corrected map's 1.48 and 11.2.
        # Five times too much grain, on the one part of the map where a splice cannot happen because
        # no photograph reaches within two hundred texels of it. Sanding a chest is what "choppy"
        # looks like at arm's length.
        #
        # So ask the base map how much texture belongs at each texel, exactly as the contrast
        # restore asks it how much detail belongs. Relative to what the base carries on the skin the
        # cameras DID resolve, so the number is a ratio of like to like and the jaw still gets its
        # full dose — the join keeps its cover, and the chest stops being sanded.
        _bL = _lab(base_np)[..., 0]
        e_b = cv2.GaussianBlur(np.abs(_bL - cv2.GaussianBlur(_bL, (0, 0), 3.0)), (0, 0), 12.0)
        ref_e = float(np.median(e_b[seen])) if seen.any() else 0.0
        amp = np.clip(e_b / ref_e, 0.0, 1.0) if ref_e > 1e-3 else np.ones_like(e_b)
        clab[..., 0] = np.clip(cL + n * need * amp, 0, 255)
        color = _unlab(clab).astype(np.float32)
        log(f"  grain synthesis: sigma {sd:.2f} measured on resolved skin, carried into "
            f"{100 * (need > 0.3).mean():.0f}% of the map at x{float(amp[need > 0.3].mean()):.2f} "
            f"of that on average - scaled to the texture the base map carries texel by texel")

    _probe('grain', color)
    # ── a photograph is an EXPOSURE; this map has to be an ALBEDO ─────────────
    # Nothing anywhere else in this file ever sets the LEVEL. White balance fixes the colour cast,
    # the consensus de-light removes view-to-view disagreement, and the flatten pass takes out
    # shading that varies ACROSS the face — but all three are differential, and every one of them is
    # satisfied by a face that is uniformly too dark, because a constant offset disagrees with
    # nothing. Photographs are uniformly too dark for a reason that has nothing to do with this
    # pipeline: a camera meters the whole frame, so a face under arena light lands near mid-grey,
    # while an albedo is what the surface reflects BEFORE the renderer lights it and sits far higher.
    #
    # Measured on the shipped Boeser against the head 138 it replaces, over identical UV boxes:
    # cheek 167.7 against 189.4, forehead 169.6 against 189.1 — and the NECK, which is fill and
    # therefore base-derived, matching at 166.4 against 170.2. That is the whole fault in one line:
    # in our map the face is the same lightness as the neck, and in every shipped head, and in a
    # hand-corrected one, it is twenty-odd L brighter. A face no brighter than the throat under it
    # is what "muddy" reads as, at any resolution and under any light.
    #
    # IT HAS TO BE LAST. Stated where `proj` is finalised instead, it measured the projection as 8 L
    # ABOVE the base and duly darkened it — correctly, for that stage — and the finished map still
    # came out 23 L below, because the flatten, the de-light, the settle plate and the geometric AO
    # between there and here remove about thirty L between them and none of them is answerable for
    # the total. Same lesson as the mouth line: a level is not a stage, it is a property of the
    # finished map, so measure it on the finished map.
    #
    # Weighted by where the CAMERAS reached, because that is precisely the region with the exposure
    # problem — the fill is base-derived and already correct, and the collar has just been grafted
    # from the base map and must not be pulled off it again. Lightness only: complexion lives in a*
    # and b* and is not touched. Capped, because this corrects a systematic exposure offset and is
    # not a licence to repaint one player in another's complexion.
    # …and measured only where BOTH maps say the texel is plain lit skin. This mask alone is not
    # enough and the difference image says so plainly: over its 108,777 texels the base-minus-built
    # lightness runs p25 -8, p50 +3, p75 +14, because it contains a beard, two nostrils, a lash
    # line and two temples alongside the forehead. Those are not level information — the beard is
    # dark in our map because he has one and the base head does not, and averaging that against a
    # forehead gives +3 where the forehead alone gives +17 and the cheek +16. A brow is not evidence
    # about exposure. So drop, from both sides independently, everything either map draws darker
    # than its own skin, and compare what is left: forehead, cheeks, nose bridge.
    # A single number is not enough, and the measurements say so. With the level match in, the
    # forehead came up to 176.6 against the artist's 189.1 — but the real fault the boxes exposed is
    # not the offset, it is the RANGE. Base head 138 runs 145.7 at the nose and 189.1 at the
    # forehead: 43 L of modelling. The user's own map runs 31. Ours ran 13. A face that carries a
    # thirteen-L spread from nose to brow is not dark, it is FLAT, and no scalar can un-flatten it —
    # brightening it by the forehead's deficit merely makes a flat face a lighter flat face.
    #
    # So two terms, both low-frequency, neither touching a*/b*:
    #   LEVEL      one number, measured over the BRIGHT half of plain skin. The bright half is the
    #              part of the face that is actually lit in both maps, which is what an exposure
    #              offset is a statement about; including the shaded half drags the estimate toward
    #              zero for the same reason the beard did.
    #   MODELLING  what is left after the level, blurred wide (sigma 25, so it carries no feature of
    #              its own) with the mask in the denominator so texels outside it neither vote nor
    #              dilute, and applied ONLY WHERE NEGATIVE.
    #
    # Darkening-only is the safety property, not a detail. Our map is too flat, so what it is short
    # of is its own shadows — the nose side, the socket, the jaw under-plane. Lightening a region
    # relative to its neighbours is never what "too flat" calls for, and here it is precisely what
    # would erase Boeser's beard, which the base head does not have and which the field would
    # therefore read as a 20 L deficit and dutifully bleach.
    sel_lvl = (raw_have > 0.7) & (skin_m > 0.5) & (hairy < 0.3) & (aper_m < 0.2)
    if "albedo-level" in _skip:
        # ── ⭐ the SHIP level: one curve onto the shipped distribution ────────
        # The block below anchors the level to the BASE head, and that anchor is the measured
        # fault the shipped-albedo bar exists to catch (see line ~617): a 2009 head's exposure is
        # not this player's albedo. What IS a defensible anchor is the library itself — 433 parsed
        # heads put skin at a median 63.5 L — so take the map's own skin median there with a single
        # gamma (a gamma, not a gain, so shadows keep their depth relative to the face) and
        # compress the top through a tanh knee so nothing blows. This is the exact curve the A/B
        # render validated over the raw flatten and over the full ladder.
        # Weighted OFF over the collar graft — the neck was just carried onto the body asset's
        # tone and re-levelling it would undo the graft — and off the mouth interior, which is the
        # artist's ink and no one's complexion.
        flab = _lab(np.clip(color, 0, 255).astype(np.uint8)).astype(np.float32)
        _L100 = flab[..., 0] / 2.55
        _ssel = sel_lvl if sel_lvl.sum() > 500 else \
            ((np.clip(skin_m, 0, 1) > 0.6) & (np.clip(hair_m, 0, 1) < 0.4) & uv_mask)
        if int(_ssel.sum()) > 500:
            _sL = float(np.median(_L100[_ssel]))
            _g = np.log(ALBEDO_SKIN_L / 100.0) / np.log(max(_sL, 1.0) / 100.0)
            _Lg = 100.0 * np.power(np.clip(_L100, 0.0, 100.0) / 100.0, _g)
            _kn = ALBEDO_KNEE_L * 0.85
            _hi = _Lg > _kn
            _Lg[_hi] = _kn + (ALBEDO_KNEE_L - _kn) * np.tanh((_Lg[_hi] - _kn)
                                                             / (ALBEDO_KNEE_L - _kn))
            _wlv = (1.0 - collar) * (1.0 - aper_m)
            flab[..., 0] = np.clip((_L100 + (_Lg - _L100) * _wlv) * 2.55, 0, 255)
            color = _unlab(flab).astype(np.float32)
            log(f"  albedo level: skin median {_sL:.1f} L gamma-curved onto the shipped "
                f"library's {ALBEDO_SKIN_L:.1f} (433 heads), knee at {ALBEDO_KNEE_L:.0f}")
    elif sel_lvl.sum() > 500:
        flab = _lab(np.clip(color, 0, 255).astype(np.uint8))
        _oL, _bL2 = flab[..., 0], _lab(base_np)[..., 0]
        plain = (sel_lvl & (_bL2 > np.median(_bL2[sel_lvl]) - 10.0)
                 & (_oL > np.median(_oL[sel_lvl]) - 10.0))
        if plain.sum() < 500:
            plain = sel_lvl
        bright = (plain & (_bL2 > np.percentile(_bL2[plain], 60))
                  & (_oL > np.percentile(_oL[plain], 60)))
        if bright.sum() < 500:
            bright = plain
        d0 = float(np.clip(np.median(_bL2[bright] - _oL[bright]), -LEVEL_CAP, LEVEL_CAP))

        m_lvl = (np.clip(raw_have / 0.7, 0, 1) * np.clip(skin_m, 0, 1)
                 * (1.0 - np.clip(hairy, 0, 1)) * (1.0 - aper_m) * (1.0 - collar))
        w_lvl = cv2.GaussianBlur(m_lvl, (0, 0), 8)
        # masked wide blur: sum(x*m)/sum(m), so unmasked texels abstain rather than vote zero
        res = (_bL2 - _oL - d0) * m_lvl
        num = cv2.GaussianBlur(res, (0, 0), 25.0)
        den = cv2.GaussianBlur(m_lvl, (0, 0), 25.0)
        # At 0.6 the face was still flat — nose 169.2 against forehead 182.1, thirteen L, where the
        # artist's own head runs twenty-four and the hand-corrected map thirty-one. There is nothing
        # to hold back for: the field only ever darkens, and it only darkens where the artist drew
        # shadow that our map does not have, so the full measurement is the right amount of it. The
        # beard is safe by construction and not by tuning — ours is DARKER there than the base, so
        # the residual is positive and clips to zero before it can touch it.
        model = np.minimum(np.where(den > 0.02, num / np.maximum(den, 1e-3), 0.0), 0.0)

        # The level has to be measured AFTER the modelling, not beside it. A strictly non-positive
        # field can only ever take lightness away, so a level fixed before it lands short by whatever
        # the field removes — measured: d0 came out 13.0 and the forehead moved 3.8. So apply the
        # modelling, then ask the finished lit skin the same question again and settle the offset on
        # the answer. Two passes, and the lit skin lands where the base head's does by construction.
        mod = model * w_lvl
        d_lvl = float(np.clip(np.median(_bL2[bright] - (_oL + mod)[bright]), -LEVEL_CAP, LEVEL_CAP))
        flab[..., 0] = np.clip(flab[..., 0] + mod + d_lvl * w_lvl, 0, 255)
        color = _unlab(flab).astype(np.float32)
        log(f"  albedo level: lit skin sits {abs(d_lvl):.1f} L "
            f"{'below' if d_lvl > 0 else 'above'} the base head's own"
            f"{' (CAPPED)' if abs(d_lvl) >= LEVEL_CAP - 0.05 else ''}; modelling puts back up to "
            f"{abs(float(model.min())):.1f} L of shadow the flatten took out")

    _probe('albedo-level', color)
    color = np.where(uv_mask[..., None], color, fill)
    _probe('final-mask', color)

    # ── the brows get the base head's body back ──────────────────────────────
    # BEFORE the socket punch below, so the normal map inherits the restored brow too — a brow is
    # relief as much as it is colour, and the map this builder hands the engine is flat albedo.
    if "brows" not in _skip:
        color, _bi = comb_brows(color, base_np, t_lm, grain_target=brow_grain_e,
                                body_target=brow_body_e)
        if _bi:
            log(f"  brows: deviation from the skin under them {_bi[0] / 2.55:.1f} L against the "
                f"photographs' {_bi[6] / 2.55:.1f} and the base head's {_bi[1] / 2.55:.1f} - "
                f"x{_bi[2]:.2f} applied on average, x{_bi[3]:.2f} on the weaker side")
            if brow_grain_e is not None:
                log(f"  brow strands: the map carries {_bi[4] / 2.55:.2f} against the "
                    f"{brow_grain_e / 2.55:.2f} the photographs measure over this player's own "
                    f"brows - x{_bi[5]:.2f} put back"
                    f"{' (CAPPED)' if _bi[5] >= GRAIN_CAP - 1e-3 else ''}")
    _probe('brows', color)

    # ── close the cylinder ────────────────────────────────────────────────────
    # The unwrap is a cylinder cut down the centre-back, so the island's first columns and its last
    # columns are THE SAME LINE on the head — texels that meet each other on the model, drawn a map
    # apart. Nothing above knows that. Every stage here works in map space, where the two edges are
    # as far from each other as texels can get, so each drifts on its own and the difference renders
    # as a step down the back of the neck.
    #
    # Measured, ours already wraps tighter than the artist's own map — 4.8 L against his 11.3 over
    # Boeser's hair — and the faint line left on the render is his, visible on the stock head at the
    # same strength. But matching the shipped asset is not the standard being aimed at here, and the
    # constraint is free: the two edges are known to be equal, so split whatever difference is left
    # between them and ramp it out over 32 texels, which is wide enough that the correction itself
    # has no edge. Smoothed down the seam first — the per-row difference carries the two edges' own
    # grain, and feeding that back in would print a copy of it on both sides.
    #
    # Colour only. The normal map's two edges disagree by the same 3.9 against the artist's 3.3, but
    # that disagreement is largely REAL: the tangent frame flips sides across the cut, so equal
    # surfaces do not have equal tangent-space normals there, and forcing them together would bend
    # the shading rather than heal it.
    _e = np.flatnonzero(uv_mask.any(0))
    if _e.size > 64:
        c0, c1 = int(_e[0]), int(_e[-1])
        FEATHER = 32
        # Only rows where the island actually reaches BOTH edges have two edges to reconcile; at the
        # very top and bottom of the map it does not, and a difference measured there is a
        # difference between two pieces of fill that never touch on the model.
        both = (uv_mask[:, c0:c0 + 2].any(1) & uv_mask[:, c1 - 1:c1 + 1].any(1)).astype(np.float32)
        d = (color[:, c0:c0 + 2].mean(1) - color[:, c1 - 1:c1 + 1].mean(1)) * both[:, None]
        d = cv2.GaussianBlur(d.reshape(-1, 1, 3), (0, 0), 4.0).reshape(-1, 1, 3)
        ramp = 0.5 * (1.0 + np.cos(np.pi * np.arange(FEATHER, dtype=np.float32) / FEATHER))
        color[:, c0:c0 + FEATHER] -= 0.5 * d * ramp[None, :, None]
        color[:, c1 - FEATHER + 1:c1 + 1] += 0.5 * d * ramp[::-1][None, :, None]
        color = np.clip(color, 0, 255)
        log(f"  wrap: the map's two back-centre edges disagreed by "
            f"{float(np.abs(d).mean()):.1f} levels on average, {float(np.abs(d).max()):.0f} at the "
            f"worst - split between them and ramped out over {FEATHER} texels, so the cylinder "
            f"closes")
    _probe('wrap', color)

    # the eye sockets are empty on purpose (the eyeball is separate geometry drawn in front)
    eye = np.zeros((H, W), np.float32)
    eye_n = np.zeros((H, W), np.float32)
    for e in (L_EYE, R_EYE):
        hole = np.zeros((H, W), np.uint8)
        cv2.fillConvexPoly(hole, _hull(t_lm[list(e)], 1.35), 255)
        eye = np.maximum(eye, cv2.GaussianBlur(hole, (0, 0), 3).astype(np.float32) / 255.0)
        # …and a TIGHTER one for the normal map. That punch is generous for a colour reason: a
        # photographed eyeball pasted onto lid skin is the worst artefact this builder can make, so
        # the hull is grown a third and feathered wide to be certain none of it lands. But it is
        # grown onto real skin, and measured on the base map it sits at 0.81 opacity right on the
        # lid line and is still 0.53 a tenth of an eye-width out — which is exactly where the
        # supratarsal fold lives. Sampled outward from the upper lid in Boeser's references the
        # skin climbs out of the lash line between 0.05 and 0.15 eye-widths and the crease is the
        # inflection in that climb, so the wide punch was deleting the fold from both maps and
        # handing back the 2009 artist's flat lid. Relief has no colour to leak: a normal a texel
        # out of place is a slightly wrong bump, not a stray eyeball. So the normal map gets a
        # punch that stops at the aperture (0.65 on the lid line, 0.16 at h=0.10) and keeps the
        # socket interior, which genuinely has no surface, still covered.
        tight = np.zeros((H, W), np.uint8)
        cv2.fillConvexPoly(tight, _hull(t_lm[list(e)], 1.05), 255)
        eye_n = np.maximum(eye_n, cv2.GaussianBlur(tight, (0, 0), 2).astype(np.float32) / 255.0)
    # keep the photographic lids for the normal map before the colour hands them to the artist
    color_n = color * (1 - eye_n[..., None]) + base_np.astype(np.float32) * eye_n[..., None]
    color = color * (1 - eye[..., None]) + base_np.astype(np.float32) * eye[..., None]
    # ⭐ THE PROBE LIST USED TO STOP AT `wrap`, ABOVE, AND EVERYTHING FROM HERE DOWN WAS UNMEASURED.
    # That blind spot cost most of a diagnosis: the nose-bridge hole traced clean through all
    # fourteen probed stages — pit depth never past 5.2 L, and 5.2 at `wrap` — while the finished
    # file read 27. It is dug by one of the four stages below, and none of them was being watched.
    _probe('eye-punch', color)

    # and so is the mouth line, for the same reason and by the same means. Handing it to the base map
    # at the composite is not enough: measured on the last build, with the guard sitting at 0.99 over
    # the artist's line, the finished map still read chroma 18 there against the base map's 29 — a
    # dozen later stages each take a small, individually-defensible bite out of a feature seven texels
    # tall, and the sum of them is the flat grey bar the user saw under the lip. There is nothing to
    # negotiate here: behind the lips the mesh has no skin, no photograph reaches it, and the 2009
    # artist's dark line is the only correct answer. So state it last, where nothing can undo it.
    color = color * (1 - aper_m[..., None]) + base_np.astype(np.float32) * aper_m[..., None]
    color_n = color_n * (1 - aper_m[..., None]) + base_np.astype(np.float32) * aper_m[..., None]
    _probe('mouth-line', color)

    # ── the lash line, stated last, for the same reason as the mouth line ─────
    # The eye punch above hands the whole lid back to the 2009 base map so that no photographed
    # eyeball can land on skin. That is the right trade, but it throws away the darkest and sharpest
    # thing the photographs have anywhere near the eye, and a lash line is not decoration: it is
    # what tells a viewer — and mediapipe — where the eye ENDS. Without one the boundary is found
    # wherever the shading happens to fall away, which is why the rendered aperture measured +13.5%
    # and +16.9% against the portrait while the fitted MESH's own lid landmarks measured -17%. The
    # geometry was never the problem. Two independent checks say the same: sliding the globe half a
    # centimetre through the socket moves the measured opening 2-3 points, so the globe is not the
    # aperture either; and merely re-running the lighting on unchanged geometry moved it TEN points.
    #
    # So it is drawn rather than sampled — a curve along the upper lid, at a width measured to be the
    # same on four eyes across two men and a depth measured on this face's own anchor portrait. Being
    # synthesised is the point: there is no photograph in it, so the eyeball it cannot leak.
    #
    # Upper lid only. That is what was measured; the lower lash is real but much weaker, and this
    # file has no business inventing a number for it.
    lash_dL = _lash_depth(views[anchor]["img"], views[anchor]["lm"])
    _lashw = None
    if lash_dL < -2.0:
        ew = float(np.linalg.norm(t_lm[133] - t_lm[33]))
        line = np.full((H, W), 255, np.uint8)
        for up in (UP_LID_L, UP_LID_R):
            cv2.polylines(line, [np.round(t_lm[list(up)]).astype(np.int32)], False, 0, 1)
        # the trough is centred ON the lid line and symmetric about it — the lash overhangs inward
        # as far as it stands proud outward — so a plain gaussian on distance is the right shape.
        sig = max(LASH_WIDTH_EW * ew / 2.355, 0.8)
        wl = np.exp(-0.5 * (cv2.distanceTransform(line, cv2.DIST_L2, 3) / sig) ** 2)
        lab_L = cv2.cvtColor(np.clip(color, 0, 255).astype(np.uint8),
                             cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32) / 2.55
        sel = skin_m > 0.5
        fL = float(lab_L[sel].mean()) if sel.sum() > 500 else float(lab_L.mean())
        k = float(np.clip((fL + lash_dL) / max(fL, 1e-6), 0.05, 1.0))
        color = color * (1.0 - wl[..., None] * (1.0 - k))
        color_n = color_n * (1.0 - wl[..., None] * (1.0 - k))
        _lashw = wl
        log(f"  lash line: {lash_dL:+.1f} L below a face mean of {fL:.0f}, "
            f"{LASH_WIDTH_EW:.2f} eye-widths wide ({2.355 * sig:.1f} texels)")
    else:
        log("  ! lash line: no trough found on the anchor portrait; left to the base map")
    _probe('lash', color)

    # ── take the arena's shine back out of the albedo ─────────────────────────
    # Last thing before the normal is derived, so the normal is built from de-shined colour too: a
    # specular veil is not relief and has no business being read as any. Skin only — the eyes are
    # excluded because a sclera is bright and neutral by nature and is exactly what this eats, and
    # hair because a highlight on hair is one we WANT to keep. See deshine().
    #
    # ⚠ BOTH MAPS. `color_n` is a private copy that exists only to derive the normal; the map that
    # actually ships is `color`. Wired to `color_n` alone — which is how this first went in — the
    # veil came out of the relief and stayed in the albedo, and the finished head measured exactly
    # as glossy as before while the log cheerfully reported the correction. The dry run was right;
    # the wiring was wrong.
    if DESHINE > 0:
        _skin = cv2.GaussianBlur(np.clip(reach, 0, 1) * uv_mask.astype(np.float32)
                                 * (1.0 - eye_n) * (1.0 - np.clip(hairy, 0, 1)), (0, 0), 4.0)
        color_n = deshine(color_n, _skin)
        color = deshine(color, _skin, log=log)
        _probe('de-shine', color)

    # ── the normal map, from the same photographs ─────────────────────────────
    # The colour map is deliberately flat albedo now, so all the relief the references carry has to
    # arrive through here or it is simply lost. Trust it exactly where the colour is trusted: the
    # ear zone and the eye sockets are the artist's in both maps, and so is everything the cameras
    # never reached.
    nrm_out = base_nrm = maps.get("normal")
    wn = (np.clip(reach, 0, 1) * uv_mask.astype(np.float32)
          * (1.0 - ear_zone) * (1.0 - eye_n))
    # Feather the trust boundary. `wn` is a product of hard-ish masks and it was printing a
    # RECTANGULAR edge across the top of the normal map — the relief simply stopped along a straight
    # line, which no face does.
    wn = cv2.GaussianBlur(wn, (0, 0), 5.0)
    # ⭐ THE HAIR MASK THE NORMAL MAP SEES IS HARDENED, and this is the "indented forehead" fix
    # (ablate.png, 2026-08-17: swapping in the artist's normal smoothed the forehead completely,
    # so the fault was OURS). `hairy` is the photographic hairline vote blurred at sigma 4, so a
    # wide skirt of forehead SKIN under the hairline reads 0.05..0.35 hair. For colour blending
    # that skirt is exactly right. Inside detail_normal it is poison, three ways at once: the
    # 8- and 20-texel hair bands get a fraction of their amplitude ON SKIN, the chroma gate is
    # fractionally bypassed (pigment embossed as geometry), and the strand gain `k` is boosted up
    # to 1.3x — so the hairline's own shadow was carved into the forehead as combed relief. Skin
    # under the hairline is skin: below 0.45 on the vote it now gets full skin treatment.
    hair_n = np.clip((np.clip(hairy, 0, 1) - 0.45) / 0.25, 0.0, 1.0)
    if bump > 0 and nrm_out is not None:
        # How much lock-scale structure does this player's hair actually have, against what this map
        # is carrying there? See `hair_relief` in detail_normal — the answer is what stops a shaved
        # head being given a full head of combed hair in relief.
        _hrel = 1.0
        _hsel = np.clip(hairy, 0, 1) > 0.5
        if hair_lock_e is not None and int(_hsel.sum()) > 500:
            _mine_l = float(np.abs(_bp(_lab(np.clip(color_n, 0, 255).astype(np.uint8))[..., 0],
                                       8.0, 25.0))[_hsel].mean())
            _hrel = float(np.clip(hair_lock_e / max(_mine_l, 1e-6), 0.0, 1.5))
            log(f"  hair relief: his photographs carry {hair_lock_e:.2f} of lock-scale structure "
                f"where this map carries {_mine_l:.2f} - the normal map's lock band runs at "
                f"x{_hrel:.2f}"
                + (" (a shaved head gets no locks)" if _hrel < 0.5 else ""))
        nrm_out = Image.fromarray(detail_normal(color_n, nrm_out, wn, bump=bump, hair=hair_n,
                                                hair_relief=_hrel))
        log(f"  normal map: photographic relief over {100 * (wn > 0.5).mean():.0f}% of the map")
        # ⭐ THE ARTIST'S SHIPPED MAP, decoded fresh — NOT `base_nrm`. `maps["normal"]` has already
        # been through the landmark warp by this point, and the warp is exactly the low-pass we are
        # trying to undo, so measuring against it measures our relief against the damage and asks
        # for a gain of x1.00. That is what gen 25 did: it logged "restored" on both heads and moved
        # nothing, and the deficit re-measured against the real shipped map was still -21%.
        # measured on SKIN (hair excluded — see match_relief), applied over everything we authored
        nrm_out = match_relief(nrm_out, base_maps(base_id, game_dir).get("normal"), wn,
                               region=wn * (1.0 - np.clip(hairy, 0, 1)), log=log)

    # ── and now CHECK the hair against the render, instead of predicting it ───
    # ⭐ The hair level stage above aims the albedo high by HAIR_RENDER_SHADE so that the RENDER, not
    # the map, lands on the photograph. That is the right target and the constant is the wrong way to
    # hit it. 12 L was measured on two heads with a flat albedo, and what the renderer actually takes
    # off a head of hair depends on the DONOR's hair geometry — how the crown curves, how far the
    # fringe overhangs, which is different for every cut. Measured on the finished heads the residual
    # came out -4.5 L on Boeser and +4.4 on Makar: the same size, opposite signs, off one shared
    # constant. No constant can be right for both, so stop guessing at it.
    #
    # The renderer is right here and a render costs a few seconds. So render the finished maps on the
    # fitted mesh, measure the hair with the same box the verification pass uses, and put the residual
    # back into the albedo. One step lands it, because a diffuse term moves a rendered L very nearly
    # one-for-one with the albedo L under it; the second pass is there to confirm rather than to
    # converge, and it stops as soon as the residual is inside HAIR_LOOP_TOL.
    #
    # Best-effort throughout: no build may fail because a preview renderer was unavailable, and the
    # correction is capped, because a loop that can move a head's hair by an unbounded amount on the
    # strength of one automatic landmark detection is a loop that can also ruin it.
    # ⚠ The target is a MEDIAN over the near-frontal references, read off the ORIGINAL files, and
    # both halves of that were learned the hard way. Read off `views[i]["img"]` it is wrong, because
    # by this point in the build those arrays have been white-balanced and de-lit against each other
    # and no longer carry the tone anyone will be comparing against: Boeser's anchor measured -8.4
    # there against -14.3 in the file it came from. And read off ONE photograph it is a lighting
    # measurement wearing a material's clothes — the very trap the hair level and hair key notes
    # above both describe. Measured across the reference sets, hair-minus-face on this box runs from
    # +6.0 to -14.3 on Boeser and -2.2 to -33.9 on Makar, and even his TWO frontal portraits disagree
    # by 6.5 L. A single view cannot be trusted with a 10 L correction. Turned views are excluded
    # rather than averaged in, because past ~20 degrees this box stops being the top of the head.
    #
    # ⚠ But a MEDIAN over near-frontal views is only robust when there are enough of them to have a
    # middle. With two it is an average, and averaging two disagreeing lights is not a light anybody
    # stood in: Makar's two frontals put his chin at -15.5 and -2.2 L, the average of -8.9 was aimed
    # at, hit, and scored 6 L wrong against the portrait. So for LEVELS take the most frontal view —
    # the portrait, the one image the head is actually judged against, and the one the user asked for
    # by name — and keep the median only as a fallback when it cannot be read.
    #
    # ⭐ TEXTURE COMES FROM THE SAME ANCHOR, and this cost a generation to learn. It was taking the
    # HIGH WATER MARK across the frontal views on the reasoning that a photograph can only ever
    # under-report detail. It cannot only under-report it: a 1280-wide press photo of Makar reads
    # 3.69 on the cheeks where his portrait reads 0.93, because sharpening and JPEG ringing are
    # detail too, and the head built to the high water mark came out with FOUR TIMES the cheek grain
    # the portrait has. Worse, mixing anchors is incoherent on its own — his levels were being aimed
    # at one photograph and his grain at another, so the head matched neither. One photograph is the
    # target for everything a photograph can say.
    _eyepos = None
    _hcand, _hxcand, _rcand, _tcand = [], [], [], []
    _byaw, _hbest, _rbest, _abest, _apbest, _bgbest = 1e9, None, None, None, None, None
    _hxbest, _tbest, _sbest, _mlbest, _rabest = None, None, None, None, None
    _lhbest, _eybest = None, None
    for _v in views:
        _yaw = abs(float(_v.get("yaw", 90.0)))
        if _yaw > 20.0:
            continue
        try:
            _vi = Image.open(_v["path"]).convert("RGB")
        except Exception:
            continue
        _hv, _hx, _rv = _hair_dl(_vi), _hair_tex(_vi), _region_dl(_vi)
        _tv = _region_tex(_vi)
        if _hv is not None:
            _hcand.append(_hv)
        if _hx is not None:
            _hxcand.append(_hx)
        if _rv is not None:
            _rcand.append(_rv)
        if _tv is not None:
            _tcand.append(_tv)
        if _yaw < _byaw and (_hv is not None or _rv is not None):
            _byaw, _hbest, _rbest = _yaw, _hv, _rv
            _abest, _apbest = _hair_ab(_vi), _aperture(_vi)
            _bgbest = _brow_gap(_vi)
            _hxbest, _tbest = _hx, _tv
            _sbest = _skin_tone(_vi)
            _mlbest = _mouth_line(_vi)
            _lhbest = _lip_height(_vi)
            _eybest = _eye_dl(_vi)
            _rabest = _region_ab(_vi)
    _htgt = _hbest if _hbest is not None else (
        float(np.median(_hcand)) if _hcand
        else _hair_dl(views[anchor]["img"], views[anchor]["lm"]))
    _hxtgt = _hxbest if _hxbest is not None else (
        float(np.percentile(_hxcand, 80)) if _hxcand else None)
    _rtgt = dict(_rbest) if _rbest else (
        {k: float(np.median([r[k] for r in _rcand if k in r]))
         for k in QA_SKIN_REGIONS if any(k in r for r in _rcand)} if _rcand else {})
    _rabtgt = dict(_rabest) if _rabest else {}
    _ttgt = dict(_tbest) if _tbest else (
        {k: float(np.median([t[k] for t in _tcand if k in t]))
         for k in QA_SKIN_REGIONS if any(k in t for t in _tcand)} if _tcand else {})

    # …and the loop itself is held out of ship mode. The shipped-albedo gate caught the structural
    # fault (targets read off LIT photographs, written into a DE-LIT albedo) and declined the worst
    # steps, but the A/B still preferred the pre-loop map — the gate can refuse a step, it cannot
    # refuse the ambition. mode="full" keeps it for measurement.
    if positions is not None and "loop" not in _skip:
        # the UV footprint of each scored region, feathered, so a correction lands as a broad wash
        # rather than as a polygon. Hair, the eye punch and the mouth line are held out: the first
        # has its own term below, and the other two are deliberately the artist's and must stay so.
        # ⭐ HARDENED, for the level holdouts only. `hairy` carries the photographic hairline vote,
        # which is blurred at sigma 4 before it is used, so around every hair edge there is a wide
        # skirt of texels reading 0.05-0.35 hair. For blending and for the detail transfer that
        # skirt is exactly right — it is what stops the hairline printing as a cut-out. As the
        # holdout for a LEVEL shift it is a slow poison: the loop lifts the face by ten or fifteen
        # L and every texel in the skirt gets a different fraction of that lift, so the correction
        # arrives as a gradient wherever the mask ramps. That is the crater at the glabella (fixed
        # at source above, in the vote), and it is also the pale band across the top of the
        # forehead under the hairline that shows on both men in gen 26 while every region score
        # sits in tolerance — a region mean cannot see a gradient inside itself.
        #
        # So for the holdouts: send the skirt to zero and steepen what is left. The boundary that
        # results is crisp, which is safe here and nowhere else, because it lands on the hairline
        # where there is a real edge in the albedo already to hide it.
        # ⭐ AND NOTHING AT OR BELOW THE GLABELLA IS HAIR, FOR LEVEL PURPOSES. This is the
        # nose-bridge crater, and it is not a texture fault at all: the map is clean when it is
        # written — the pit never passes 5.7 L through eighteen probed stages — and the hole is dug
        # afterwards, here, by the loop, through this mask.
        #
        # Measured at the box: `hairy` reads 0.48 on the bridge between the brows. That one number
        # digs the crater twice over. The hair LEVEL term darkens through `_hw`, so the bridge gets
        # half of every hair darkening; and the skin lift is withheld in proportion, so it gets 56%
        # of a correction the nose around it gets in full. The trace has the skin-tone stage moving
        # the nose +11.3 L and the box +6.6 — a ratio of 0.58, which is this mask and nothing else.
        # Over six passes the two compound into an 18 L step with a hard edge.
        #
        # The 0.48 comes from the BASE mask's lightness split, not from the segmenter: head 3040's
        # artist painted heavy brows and they bleed onto the bridge. So gate it low, at the glabella
        # rather than at the brow, ramped over a tenth of the face — a real hairline sits a quarter
        # of the face height higher, so the scalp loses nothing, and the beard is already handled by
        # the jaw gate. Eyebrows keep their own level term; they never relied on this one.
        #
        # ⚠ LEVEL ONLY. `hairy` itself is untouched, because the blend and the detail transfer both
        # want the soft, brow-inclusive version: it is what stops the hairline printing as a cut-out.
        _hairlv = np.clip(hairy, 0, 1) * np.clip(
            (-((pos_uv.reshape(-1, 3) - mesh_lm[9]) @ dn).reshape(H, W)) / (0.10 * face_h), 0.0, 1.0)
        _hardh = np.clip((_hairlv - 0.35) / 0.30, 0, 1).astype(np.float32)
        _keep = ((1.0 - _hardh) * (1.0 - np.clip(eye, 0, 1))
                 * (1.0 - np.clip(aper_m, 0, 1))).astype(np.float32)
        # …and a second copy for the two ADDITIVE terms — level and chroma — which the mouth line
        # and the sockets must ride along with rather than be held out of. Additive shifts preserve
        # relief, so what makes them safe on the artist's ink is the same thing that makes holding
        # the artist's ink out of the multiplicative detail term necessary.
        _keeplv = (1.0 - _hardh).astype(np.float32)
        _rmask, _rlvl = {}, {}
        for _name, _idx in QA_SKIN_REGIONS.items():
            _p = np.zeros((H, W), np.uint8)
            cv2.fillPoly(_p, [np.round(np.asarray(t_lm, np.float64)[list(_idx), :2]
                                       ).astype(np.int32)], 255)
            _f = cv2.GaussianBlur(_p.astype(np.float32) / 255.0, (0, 0), 10.0)
            _rmask[_name] = _f * _keep
            _rlvl[_name] = _f * _keeplv
            if _DBG.get("box"):                     # diagnostic: what weight does the box get?
                _bx = _DBG["box"]
                _DBG.setdefault("boxw", {})[_name] = (
                    float(_f[_bx[1]:_bx[3], _bx[0]:_bx[2]].mean()),
                    float(_rlvl[_name][_bx[1]:_bx[3], _bx[0]:_bx[2]].mean()),
                    float(_f[_f > 0.5].mean()) if (_f > 0.5).any() else 0.0,
                    float((_f * _keeplv)[_f > 0.5].mean()) if (_f > 0.5).any() else 0.0)
        # the LID SKIN — the eye box minus the aperture. See `_eye_dl`: the box's deficit is part
        # shared globe and part socket shadow, neither of which the map owns, and the lid is the
        # part of it that is ours. Held out of the aperture on purpose: painting inside it either
        # does nothing (the globe occludes it) or shows as a bright rim where it does not.
        _ep = np.zeros((H, W), np.uint8)
        for _eidx in (L_EYE, R_EYE):
            cv2.fillPoly(_ep, [np.round(np.asarray(t_lm, np.float64)[list(_eidx), :2]
                                        ).astype(np.int32)], 255)
        _emask = cv2.GaussianBlur(_ep.astype(np.float32) / 255.0, (0, 0), 6.0)
        _emask *= (1.0 - np.clip(aper_m, 0, 1)).astype(np.float32) * _keeplv
        # the mouth's own footprint, which unlike every `_rmask` entry deliberately INCLUDES the
        # aperture: the thing being moved IS the artist's drawn line, so holding it out would leave
        # the slide moving the skin around a line that stayed where it was.
        _mp = np.zeros((H, W), np.uint8)
        cv2.fillPoly(_mp, [np.round(np.asarray(t_lm, np.float64)[
            list(QA_SKIN_REGIONS["mouth"]), :2]).astype(np.int32)], 255)
        _mmask = cv2.GaussianBlur(np.maximum(_mp.astype(np.float32) / 255.0,
                                             np.clip(aper_m, 0, 1).astype(np.float32)),
                                  (0, 0), 8.0)
        _mmask *= (1.0 - np.clip(hairy, 0, 1)).astype(np.float32)
        # the hair LEVEL weight — glabella-gated for the same reason `_hardh` is. This is the half
        # of the crater that darkens rather than the half that withholds.
        _hw = cv2.GaussianBlur(_hairlv.astype(np.float32), (0, 0), 3.0)
        _moved = 0.0
        # ⭐ the hair level term's AUTHORITY, and it is the reason a buzz cut used to come out as a
        # chalk cap. `_hair_dl` measures a box above the brow, which on a head with no hair SHELL is
        # the bare crown, and the renderer lights the crown at a grazing angle — so the reading is
        # set by geometry there and albedo barely reaches it. Measured on Pettersson: the loop asked
        # for +10.2 L, put it in, and the render moved 0.6. It then spent the rest of HAIR_LOOP_CAP
        # on the same unreachable target, leaving ~+27 L of albedo on a scalp nobody could see the
        # effect of from the front and everybody could see from every other angle.
        #
        # So require the term to EARN its next step: if a push did not arrive in the reading, it had
        # no authority over this head's hair, and the honest move is to take it back out and stop
        # rather than to keep buying more of it. Backed out through `_soft_unlift` and the same mask,
        # because by the time this is known the map has been written to by half a dozen other stages
        # and restoring a snapshot would undo those too.
        # a head with no scalp shell starts with this term already stood down — see `_has_scalp_shell`
        _hask, _hwas, _hdead, _hput = 0.0, None, not _hair_shell, None
        if _hdead:
            log("  hair level: this head wears its hair as paint on the skull, not as geometry, so "
                "the render's hair-minus-face is shading and is left to the renderer")
        _rmoved = {k: 0.0 for k in QA_SKIN_REGIONS}
        _rlast = {k: 0.0 for k in QA_SKIN_REGIONS}
        _amoved = {k: [0.0, 0.0] for k in QA_SKIN_REGIONS}
        _rrelax = {k: 1.0 for k in QA_SKIN_REGIONS}
        # ⭐ How much of an L pushed into the MAP actually arrives in the RENDER, per region. It
        # starts at the 2.0 every term here used to assume and then measures itself, because that
        # assumption is only true for some regions. Probed directly — paint a known +8 L over one
        # region's map footprint, render, read it back — the nose takes 57.5%/56.7% (so 2.0 is
        # right for it), the eyebrows take 35.3%/28.5%, and the eye box takes 8.9%/2.7%. At 2.0
        # against a real 0.32, Boeser's brows asked +9.8 then +3.2, exhausted the 13.0 cumulative
        # cap, and arrived 1.4 L further on, still -9.4 against a portrait's -1.0. The estimate is
        # the secant one — what the last pass asked of the map over what the render then moved.
        _rgain = {k: 2.0 for k in QA_SKIN_REGIONS}
        _rasked = {}                # region -> (reading before, L actually pushed into the map)
        _rfirst = {}                # region -> its reading on the first pass; see LEVEL_LOOP_CAP
        # the detail term's half of the same two ideas as _rgain/_rfirst above: how much of a
        # multiplicative grain request survives into the render, and where the region STARTED, so
        # TEX_LOOP_CAP is spent on displacement rather than on requests. See the detail block.
        _tgain = {k: 1.0 for k in QA_SKIN_REGIONS}
        _tasked = {}                # region -> (reading before, multiplier actually applied)
        _tfirst = {}                # region -> its detail figure on the first pass
        _hxmoved = 1.0
        _lhscaled, _lhlast, _lhrelax = [1.0, 1.0], [0.0, 0.0], [1.0, 1.0]
        _eylifted = 0.0
        _smoved = [0.0, 0.0, 0.0]
        _abmoved = [0.0, 0.0]
        _bgmoved = 0.0
        _mlmoved = 0.0
        # the aperture stage is the one that moves GEOMETRY rather than albedo, so it works on its
        # own copy and the corrected mesh goes back to the caller in the result.
        _pos = np.asarray(positions, np.float64).copy()
        _eyf = _eye_close_fields(base_id, game_dir, _pos, t_lm, W, H)
        _eyeshrunk, _eyepinched = 1.0, 0.0
        # ⭐ The aperture term's own damping, and it is damping against LATENCY rather than against
        # ringing — which is what the previous version got wrong. It is the only stage here that
        # moves GEOMETRY, and geometry answers late. The gen 22 trace, in full:
        #
        #   pass 1  reads +17%  ->  globe x0.90, lids +0.12 cm
        #   pass 2  reads  +5%  ->  globe x0.96, lids +0.05 cm
        #   pass 3  reads  +5%  ->  globe x0.96, lids +0.05 cm     <- pass 2 had not landed yet
        #   pass 6  reads  -5%
        #
        # Pass 1 behaved perfectly: a 10% shrink took 12 of its 17 points. Then passes 2 and 3 read
        # the SAME +5% and each spent a 4% correction on it, because the first one's effect had not
        # reached the render by the time the second one measured; the 8% duly arrived and carried
        # the eye 10 points past its target, where it sat with no pass left to notice. A sign-flip
        # relaxation cannot see any of that -- the sign never flips until the damage is done.
        #
        # So the rule is: do not act again until the last action has been OBSERVED. Remember what
        # the last step was meant to remove, and if the reading has not moved by a useful fraction
        # of it, spend this pass waiting instead of stacking a second correction on top of a first
        # one that is still in flight. Ringing is handled too, but it is the lesser fault here.
        _aprelax, _aplast = 1.0, 0.0
        _apwant, _apwas = 0.0, None
        for _it in range(LOOP_PASSES):
            _r = _render_head(base_id, game_dir,
                              Image.fromarray(np.clip(color, 0, 255).astype(np.uint8)),
                              nrm_out, _pos)
            _rlm = None
            if _r is not None:
                try:
                    _rlm = np.asarray(landmarks(_r), np.float64)[:, :2]
                except Exception:
                    _rlm = None
            if _rlm is None:
                log("  ! render check: could not render or could not find a face in the render; "
                    "the open-loop constants stand")
                break

            # ── the hair's LEVEL ────────────────────────────────────────────
            _got = _hair_dl(_r, _rlm)
            _done = True
            if _got is not None and _htgt is not None and not _hdead:
                # ⭐ MEASURED, and it replaced a WRONG diagnosis that shipped a visible fault.
                #
                # This block used to ask a different question - did the last step ARRIVE in the
                # render? - and when it had not, it declared the term dead and BACKED OUT everything
                # both it and the open-loop step had spent. On a light-haired head that undo printed
                # a hard bright band along the hairline, because the level step is not the last thing
                # written to these texels: de-shine and the hair contrast/comb stages run in between,
                # and unwinding a level underneath them does not commute.
                #
                # It was also answering the wrong question. Measured directly on a buzzed head, by
                # painting a known step onto the hair mask over a flat grey base and reading the
                # render back: +10 L of albedo moves the rendered hair +5.6 L, +20 moves it +11.0.
                # The albedo has ordinary authority, about 0.55 L per L. What had actually happened
                # is that the map was already against `_soft_lift`'s knee, so the step never reached
                # the albedo at all, let alone the render. That is a FULL map, not a dead term, and
                # the fix is to stop asking - not to claw back a spend that did land.
                if _hwas is not None and abs(_hask) > 0.5 and _hput is not None:
                    if _hput < 0.25 * abs(_hask):
                        _hdead = True
                        log(f"  hair check {_it + 1}: asked the albedo for {_hask:+.1f} L and only "
                            f"{_hput:.1f} L fit - his hair is already as light as a map can be "
                            f"painted, so the remaining {_htgt - _got:+.1f} L is the renderer's "
                            f"shading and is left alone")
                _d = 0.0 if _hdead else float(
                    np.clip(_htgt - _got, -HAIR_LOOP_CAP - _moved, HAIR_LOOP_CAP - _moved))
                _ok = _hdead or abs(_htgt - _got) <= HAIR_LOOP_TOL or abs(_d) < 1e-3
                if not _ok:
                    _done = False
                    # over-relaxed: measured, one unit of albedo moves the rendered hair about half
                    # a unit, because the box also contains texels the correction does not reach.
                    _rl = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    _b4 = _rl[..., 0].copy()
                    _rl[..., 0] = _soft_lift(_rl[..., 0], 2.0 * _d * 2.55 * _hw)
                    # how much of the ask the map could actually hold, in the same units as `_d`
                    _sel = _hw > 0.25
                    _hput = (float(np.abs(_rl[..., 0] - _b4)[_sel].mean()) / (2.0 * 2.55)
                             if _sel.any() else abs(_d))
                    color = _unlab(_rl).astype(np.float32)
                    color = _probe('loop-region-L', color)
                    _hwas, _hask = _got, _d
                    _moved += _d
                    log(f"  hair check {_it + 1}: the render puts his hair {_got:+.1f} L against "
                        f"his face where the portrait puts it {_htgt:+.1f} - {_d:+.1f} L "
                        f"into the albedo" + (f" ({_hput:.1f} fit)" if _hput < abs(_d) - 0.5 else ""))

            # ── how far the eyes read as OPEN ───────────────────────────────
            # The one thing on either head the user named unprompted, and the one item here that is
            # NOT albedo. Three mechanisms were tried against the render and measured:
            #   · deepening the drawn upper lash 55% moved it 0.0 points;
            #   · drawing a lower lash line as well, to 50%, bought 4;
            #   · pinching the lid rings 0.18 cm together bought 4 and then stalled —
            # because `seat_eyes` pushes any lid vertex that penetrates the eyeball straight back
            # out, so the GLOBE's silhouette is the floor on how shut an eye can be. Boeser's base
            # head reads +16.6/+20.8 before any fit is applied and Makar's fitted head reads -2/+4,
            # so this is one particular head's socket being wrong for one particular man, not a
            # systematic bias — which is exactly the kind of thing a per-head loop should catch.
            #
            # Shrink the globe AND pinch the lids, together. Shrinking alone was tried in an earlier
            # session and hollowed the eye out (a third of the opening went black, because a small
            # ball no longer reaches the lid line and behind it there is nothing); paired with the
            # pinch the lids follow it down and the fill holds — measured across the ladder, black
            # went 57% -> 55% and white 20% -> 20% while the opening came from +18/+26 to +4/+7.
            _apg = _aperture(_r, _rlm)
            if _apg is not None and _apbest is not None and _eyf is not None:
                _erel = float(np.mean([(_apg[_i] - _apbest[_i]) / max(_apbest[_i], 1e-6)
                                       for _i in (0, 1)]))
                # Two-sided. See EYE_GROW_CEIL: an eye that has been closed too far is exactly as
                # wrong as one left too open, and it is the more expensive of the two, because a
                # shrunken globe pays for the overshoot in eye-box darkness as well as in shape.
                # has the last step arrived? It was aimed at removing `_apwant`, so the reading
                # should have moved by about that much; if it has moved less than a third of it,
                # the geometry is still catching up and a second correction now would double-count.
                _apsaw = (_apwas is None or abs(_apwant) < 1e-6
                          or abs(_erel - _apwas) >= 0.33 * abs(_apwant))
                if _aplast * _erel < 0:
                    _aprelax = max(_aprelax * 0.5, 0.25)
                _aplast = _erel
                _s = float(np.clip(0.80 * _aprelax * _erel, -0.10, 0.10))
                _s = float(np.clip(_s, 1.0 - EYE_GROW_CEIL / max(_eyeshrunk, 1e-6),
                                   max(_eyeshrunk - EYE_SHRINK_FLOOR, 0.0)))
                _t = float(np.clip(1.00 * _aprelax * _erel, -0.12, 0.12))
                _t = float(np.clip(_t, -EYE_SPREAD_CAP - _eyepinched,
                                   EYE_PINCH_CAP - _eyepinched))
                if abs(_erel) > 0.04 and not _apsaw:
                    _done = False       # still converging, just not by acting this pass
                    log(f"  eye opening {_it + 1}: the render reads "
                        f"{100 * _apg[0]:.1f}/{100 * _apg[1]:.1f}% of face height against the "
                        f"portrait's {100 * _apbest[0]:.1f}/{100 * _apbest[1]:.1f} "
                        f"({100 * _erel:+.0f}%) - holding: the last correction has moved it only "
                        f"{100 * abs(_erel - _apwas):.1f} of the {100 * abs(_apwant):.1f} points "
                        f"it was worth, so it has not landed yet")
                elif abs(_erel) > 0.04 and (abs(_s) > 1e-3 or abs(_t) > 1e-3):
                    _done = False
                    _apwant, _apwas = _erel, _erel
                    _wU, _wL, _ax, _grp = _eyf
                    _pos = np.asarray(_pos, np.float64)
                    _pos = _pos + (_wU[:, None] * _t) * _ax - (_wL[:, None] * _t) * _ax
                    for _g in _grp:
                        _c = _pos[_g].mean(0)
                        _pos[_g] = _c + (_pos[_g] - _c) * (1.0 - _s)
                    _eyeshrunk *= (1.0 - _s)
                    _eyepinched += _t
                    _eyepos = _pos
                    log(f"  eye opening {_it + 1}: the render reads "
                        f"{_apg[0] * 100:.1f}/{_apg[1] * 100:.1f}% of face height open against the "
                        f"portrait's {_apbest[0] * 100:.1f}/{_apbest[1] * 100:.1f} "
                        f"({_erel * 100:+.0f}%) - globe x{1.0 - _s:.2f}, lids {_t:+.2f} cm together")

            # ── and where the BROWS sit above them ──────────────────────────
            # Boeser's rendered brows stand 14% and 19% further above his eyes than his portrait's
            # do, on a brow whose SPAN is right to 0.3% — so it is not the brow's size or the fit,
            # it is where the ink was laid down. Slide it, in the map, along the same feathered
            # footprint the level pass uses, and let the render say when it has arrived. Capped at
            # about two per cent of a face height, which is a couple of millimetres on a man: past
            # that a brow stops being repositioned and starts being smeared into the lid.
            _bgg = _brow_gap(_r, _rlm)
            if _bgg is not None and _bgbest is not None:
                _bge = float(np.mean([(_bgg[_i] - _bgbest[_i]) / max(_bgbest[_i], 1e-6)
                                      for _i in (0, 1)]))
                _fhuv = float(np.hypot(*(np.asarray(t_lm, np.float64)[10, :2]
                                         - np.asarray(t_lm, np.float64)[152, :2])))
                _dy = _bge * float(np.mean(_bgbest)) * _fhuv          # texels, +ve = brow too high
                _dy = float(np.clip(_dy, -BROW_SHIFT_CAP * _fhuv - _bgmoved,
                                    BROW_SHIFT_CAP * _fhuv - _bgmoved))
                if abs(_bge) > 0.04 and abs(_dy) > 0.4:
                    _done = False
                    _yy, _xx = np.mgrid[0:H, 0:W].astype(np.float32)
                    _sh = cv2.remap(np.clip(color, 0, 255).astype(np.float32), _xx,
                                    (_yy - _dy).astype(np.float32),
                                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                    _bw = np.clip(_rmask["eyebrows"] / max(_rmask["eyebrows"].max(), 1e-6),
                                  0, 1)[..., None]
                    color = color * (1.0 - _bw) + _sh * _bw
                    color = _probe('loop-brow-shift', color)
                    _bgmoved += _dy
                    log(f"  brow height {_it + 1}: the render puts them "
                        f"{_bgg[0] * 100:.1f}/{_bgg[1] * 100:.1f}% of face height above the eye "
                        f"against the portrait's {_bgbest[0] * 100:.1f}/{_bgbest[1] * 100:.1f} "
                        f"({_bge * 100:+.0f}%) - {_dy:+.1f} texels")

            # ── and where the MOUTH LINE sits, the same way again ───────────
            # See `_mouth_line`. Boeser's upper lip renders 18% taller than his portrait's and
            # Makar's 22% shorter with a 20% philtrum — opposite signs on two men, so it is neither
            # fit nor projection, it is the 2009 artist's painted line sitting where it sat on
            # whichever base head each was built from. Slide it, measure, repeat. Capped at about
            # three per cent of a face height: past that a mouth stops being repositioned and starts
            # being smeared over the chin.
            # …and HOW THICK the lips are, in the same remap. One resample, not two: running a slide
            # and a scale as separate `remap` calls would put the mouth through bilinear
            # interpolation twice a pass and twelve times over a build, and each one is a small blur
            # of the sharpest ink on the head.
            _mlg = _mouth_line(_r, _rlm)
            _lhg = _lip_height(_r, _rlm)
            if (_mlg is not None and _mlbest is not None) or (
                    _lhg is not None and _lhbest is not None):
                _fhuv2 = float(np.hypot(*(np.asarray(t_lm, np.float64)[10, :2]
                                          - np.asarray(t_lm, np.float64)[152, :2])))
                _mdy, _msay = 0.0, []
                if _mlg is not None and _mlbest is not None:
                    # over-relaxed 2.0, like every other term that pushes the map and reads the
                    # render: about half of what is asked arrives. Makar's slide asked -3.2, -3.6,
                    # -1.8, -2.3 on four consecutive passes and never converged, spending its whole
                    # allowance and four extra resamples of the lip band getting a third of the way.
                    _mdy = 2.0 * (_mlg - _mlbest) * _fhuv2   # texels, +ve = mouth sits too low
                    _mdy = float(np.clip(_mdy, -MOUTH_SHIFT_CAP * _fhuv2 - _mlmoved,
                                         MOUTH_SHIFT_CAP * _fhuv2 - _mlmoved))
                    if abs(_mlg - _mlbest) <= 0.004 or abs(_mdy) <= 0.8:
                        _mdy = 0.0
                    else:
                        _msay.append(f"sits at {_mlg * 100:.1f}% of face height below the nose "
                                     f"against {_mlbest * 100:.1f} ({-_mdy:+.1f} texels)")
                _mk = [1.0, 1.0]                       # upper lip, lower lip
                if _lhg is not None and _lhbest is not None:
                    for _s in (0, 1):
                        if _lhg[_s] <= 1e-4:
                            continue
                        _k = float(np.clip(_lhbest[_s] / _lhg[_s],
                                           1.0 / LIP_SCALE_CAP, LIP_SCALE_CAP))
                        # Relaxed on a sign flip, exactly as the region level term is, and for the
                        # same measured reason: Makar's lip rang x0.90, x1.04, x1.05, x1.13, x1.11
                        # — driven from 7.9% of face height through the 7.2 target down to 6.3 and
                        # then hauled back — because a scale about a line re-samples ink a previous
                        # pass already re-sampled, so the reading lags what the map now holds.
                        if (_k - 1.0) * _lhlast[_s] < 0:
                            # floor 0.25, not 0.5 — at 0.5 the relaxation can only ever halve ONCE,
                            # and once was not enough: Boeser's upper lip rang x0.77, x1.09, x0.94
                            # and finished 13.1% thick, having flipped sign on every pass it took.
                            _lhrelax[_s] = max(_lhrelax[_s] * 0.5, 0.25)
                        _lhlast[_s] = _k - 1.0
                        # ⭐ NOT over-relaxed, unlike every other term in this loop, and the reason
                        # is in this function's own docstring: _lip_height is a MAP measurement.
                        # Sliding the ink ten texels moves the reading by the full ten, so the
                        # transfer here is ~1 and a 2.0 gain is not over-relaxation, it is a
                        # guaranteed factor-of-two overshoot. Measured: Makar's lower lip read 5.1%
                        # of face height against 4.3 — a ratio of 0.843, which asked as x0.686,
                        # clipped to x0.74 — and the finished head scored his lower lip 20.4% too
                        # THIN. Asked at the ratio it would have landed. The other terms need a
                        # gain because a map push arrives at a third to a half; this one does not.
                        _k = 1.0 + (_k - 1.0) * _lhrelax[_s]
                        _k = float(np.clip(_k, 1.0 / (LIP_SCALE_CAP * _lhscaled[_s]),
                                           LIP_SCALE_CAP / _lhscaled[_s]))
                        if abs(_k - 1.0) <= 0.05:
                            continue
                        _mk[_s] = _k
                        _msay.append(f"{('upper', 'lower')[_s]} lip reads "
                                     f"{_lhg[_s] * 100:.1f}% of face height against "
                                     f"{_lhbest[_s] * 100:.1f} (x{_k:.2f})")
                if _msay:
                    _done = False
                    # Everything about the mouth line's own row, so no scale can walk the mouth up
                    # or down the face and undo the slide that just placed it — and the two lips
                    # get their own gain either side of that row, in ONE resample. Running them as
                    # separate remap calls would put the sharpest ink on the head through bilinear
                    # interpolation twice a pass and twelve times over a build.
                    _y0 = float(np.asarray(t_lm, np.float64)[13, 1]) - _mlmoved
                    _yy2, _xx2 = np.mgrid[0:H, 0:W].astype(np.float32)
                    _rel = _yy2 - _y0 + _mdy
                    _kf = np.where(_rel < 0, max(_mk[0], 1e-3), max(_mk[1], 1e-3)).astype(np.float32)
                    _src = _y0 + _rel / _kf
                    # ⚠ CUBIC, not LINEAR, and the wider dead zones above exist for the same
                    # reason. mediapipe finds a closed mouth's vermilion border by COLOUR, so the
                    # thing this loop measures is the sharpness of a painted edge — and every
                    # resample softens it. Measured: Makar's lower lip read 5.1, 3.8, 3.8, 3.4
                    # across four passes while every gain in those passes asked it to grow. The
                    # correction was not failing to fire; it was being outrun by its own blur.
                    _msh = cv2.remap(np.clip(color, 0, 255).astype(np.float32), _xx2,
                                     _src.astype(np.float32),
                                     cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
                    _mw = np.clip(_mmask / max(_mmask.max(), 1e-6), 0, 1)[..., None]
                    color = color * (1.0 - _mw) + _msh * _mw
                    color = _probe('loop-mouth-shift', color)
                    _mlmoved += _mdy
                    for _s in (0, 1):
                        _lhscaled[_s] *= _mk[_s]
                    log(f"  mouth line {_it + 1}: the render's mouth " + "; ".join(_msay))

            # ── the hair's COLOUR, the same way ─────────────────────────────
            # Boeser's rendered hair sits 11 points off his face's a where the portrait puts it 5.7 —
            # too grey by half. Level was being corrected and chroma was not, and hair chroma is not
            # a free parameter: brown hair that renders grey stops reading as his hair at all.
            _gab = _hair_ab(_r, _rlm)
            if _gab is not None and _abest is not None:
                _dab = [float(np.clip(_abest[_i] - _gab[_i], -HAIR_AB_CAP - _abmoved[_i],
                                      HAIR_AB_CAP - _abmoved[_i])) for _i in (0, 1)]
                if max(abs(_abest[_i] - _gab[_i]) for _i in (0, 1)) > 0.6 and any(
                        abs(_x) > 1e-3 for _x in _dab):
                    _done = False
                    _al = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    for _i in (0, 1):
                        _al[..., _i + 1] = np.clip(_al[..., _i + 1] + 1.4 * _dab[_i] * _hw, 0, 255)
                        _abmoved[_i] += _dab[_i]
                    color = _unlab(_al).astype(np.float32)
                    color = _probe('loop-ab', color)
                    log(f"  hair colour {_it + 1}: the render's hair is "
                        f"({_gab[0]:+.1f},{_gab[1]:+.1f}) off his face against the portrait's "
                        f"({_abest[0]:+.1f},{_abest[1]:+.1f}) - "
                        f"({_dab[0]:+.1f},{_dab[1]:+.1f}) into the albedo")

            # ── the hair's STRAND TEXTURE, the same way ─────────────────────
            # ⭐ The map is not the thing being scored and for strand detail the gap between the two
            # is large: measured, Makar's map carries 68% of his portrait's spread in this band and
            # his RENDER carries 49% of it. Rasterising a 512-texel map onto a head that occupies a
            # few hundred pixels is itself a low-pass, and it is a different low-pass for every
            # haircut. So close it here, where it can be seen, rather than by guessing at a constant.
            _gx = _hair_tex(_r, _rlm)
            if _gx is not None and _hxtgt is not None and _gx > 0.05:
                _g = float(np.clip(_hxtgt / _gx, 0.7, 3.0))
                # 3.0 was binding on Makar — his hair needed x1.98 then x1.41 and the third pass got
                # clipped to x1.07 by the ceiling rather than by having arrived.
                _g = float(np.clip(_g, 0.7 / _hxmoved, 4.5 / _hxmoved))
                if abs(_g - 1.0) > 0.04:
                    _done = False
                    _hl = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    _hb = _bp(_hl[..., 0], 0.8, 6.0)
                    _hl[..., 0] = np.clip(_hl[..., 0] + SOFT_KNEE * np.tanh(
                        _hb * (_g - 1.0) / SOFT_KNEE) * _hw, 0, 255)
                    color = _unlab(_hl).astype(np.float32)
                    color = _probe('loop-hair-L', color)
                    _hxmoved *= _g
                    log(f"  strand check {_it + 1}: the render's hair carries {_gx:.2f} of strand "
                        f"against the photographs' {_hxtgt:.2f} - x{_g:.2f} into the albedo")

            # ── the face's own ABSOLUTE tone — is that skin at all ──────────
            # ⭐ Nothing anywhere else in this builder scores this, and it is the largest single
            # fault either head has: Makar's render measures 62.7 L against his portrait's 76.0,
            # 3.6 too green and 4.8 too blue, while EVERY relative region sits inside tolerance.
            # It can do both at once because every other figure here is a delta against the face
            # mean, and a uniformly wrong face cancels out of all of them.
            #
            # It is wrong because the `albedo level` stage above matches the map to the 2009 BASE
            # HEAD's map rather than to the photograph, so the tone a player lands on is an accident
            # of which stock head he was built from — 138 is about right for Boeser, 3040 is thirteen
            # L under Makar. This overrides that, from the render, where the renderer's own exposure
            # is already in the measurement.
            #
            # Runs BEFORE the region term deliberately, and does not fight it: the region term is
            # zero-sum by construction (it subtracts its own weighted mean back off), so it can move
            # level BETWEEN regions but never move the face as a whole. This is the only stage that
            # moves the face as a whole, and the two are therefore orthogonal.
            _sg = _skin_tone(_r, _rlm)
            if _sg is not None and _sbest is not None:
                _sd = [float(np.clip(_sbest[_i] - _sg[_i],
                                     -TONE_LOOP_CAP[_i] - _smoved[_i],
                                     TONE_LOOP_CAP[_i] - _smoved[_i])) for _i in range(3)]
                if (max(abs(_sbest[_i] - _sg[_i]) for _i in range(3)) > TONE_LOOP_TOL
                        and any(abs(_x) > 1e-3 for _x in _sd)):
                    _done = False
                    # 1.5, not the region term's 2.0: this one moves the WHOLE face, so an overshoot
                    # here is a visibly wrong man rather than a slightly wrong chin. Under-relaxed
                    # and iterated is the safe direction when there are six passes to spend.
                    # ⚠ NOT through `_keep`, and this is the one stage where that is right. Every
                    # other term redistributes level between parts of the face, so it must stay off
                    # the sockets and the mouth line, which are deliberately the 2009 artist's. This
                    # one says the whole man is the wrong colour, and a socket on a face that is ten
                    # L too dark is ten L too dark as well. Gen 13 proved it by omission: Makar's
                    # skin came up 12.6 L, his socket did not follow, and the eye box went from
                    # -19.6 to -32.3 against his portrait's -22.8 — a region that got WORSE purely
                    # because everything around it got right. The shift is additive, so relief and
                    # contrast inside the socket and along the lip line survive it untouched. Hair
                    # is still held out: it has its own level term and would be corrected twice.
                    _sw = (np.clip(np.maximum(np.maximum(skin_m, eye), aper_m), 0, 1)
                           * (1.0 - _hardh)).astype(np.float32)
                    _sl = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    _sl[..., 0] = np.clip(_sl[..., 0] + 1.5 * _sd[0] * 2.55 * _sw, 0, 255)
                    for _i in (1, 2):
                        _sl[..., _i] = np.clip(_sl[..., _i] + 1.5 * _sd[_i] * _sw, 0, 255)
                    color = _unlab(_sl).astype(np.float32)
                    color = _probe('loop-skin-tone', color)
                    for _i in range(3):
                        _smoved[_i] += _sd[_i]
                    log(f"  skin tone {_it + 1}: the render's face is "
                        f"L{_sg[0]:.1f} a{_sg[1]:+.1f} b{_sg[2]:+.1f} against the portrait's "
                        f"L{_sbest[0]:.1f} a{_sbest[1]:+.1f} b{_sbest[2]:+.1f} - "
                        f"({_sd[0]:+.1f},{_sd[1]:+.1f},{_sd[2]:+.1f}) into the albedo")

            # ── and every scored SKIN region's level ────────────────────────
            # ⭐ Every one of these was being set open-loop and every one of them was out, in the
            # same direction on both heads: the render put Boeser's brows 11.0 L under his face
            # where his portrait puts them 4.8, his chin 8.1 under against 11.6, his jaw 0.8 against
            # 3.8. The MAP was right — the brow stage measures its own deviation at 10.1 against the
            # photographs' 9.2 — and the renderer's own shading is the difference. A brow sits under
            # a ridge and a chin sits under a lip; no albedo stage can know how much that costs on
            # this particular fitted head, and it does not have to, because the render is right here.
            #
            # Zero-sum by construction: the area-weighted mean correction is subtracted back off, so
            # this only ever REDISTRIBUTES level between regions and can never walk the face's own
            # tone away from the skin match that set it. Capped per region, and applied through a
            # 10-texel feather, because these are broad washes and a visible seam between two of
            # them would be a worse fault than the levels they are fixing.
            # ⚠ …through `_rlvl`, not `_rmask` — the level term is the one correction the artist's
            # mouth line must RIDE ALONG WITH. Held out of it, Makar's lip line stayed where the
            # 2009 artist left it while the face around it came up 12.6 L, and the contrast across
            # it climbed every pass: his mouth box's texture went 5.70 -> 9.20 -> 9.59 -> 10.16
            # against a target of 6.90 while the detail term was CUTTING it x0.69, because the thing
            # generating the reading was the one thing the cut could not reach. A level shift is
            # additive, so the line's own relief and shape — which are the artist's and stay his —
            # survive it exactly.
            _rgot = _region_dl(_r, _rlm)
            # read the detail figures HERE as well as in the detail term below, off the same
            # render, because where the level push is placed depends on them — see `_over`.
            _tnow = _region_tex(_r, _rlm)
            if _rgot and _rtgt:
                _fld = np.zeros((H, W), np.float32)
                _wsum = np.zeros((H, W), np.float32)
                _said = []
                for _name in QA_SKIN_REGIONS:
                    if _name not in _rgot or _name not in _rtgt:
                        continue
                    _e = _rtgt[_name] - _rgot[_name]
                    # ⭐ The allowance is spent by MOVEMENT, not by asking. It used to accumulate
                    # each request, which is a different quantity whenever the request does not
                    # arrive in full — and on a low-transfer region it never does. Boeser's brows
                    # asked +9.8 then +3.2, hit the 13.0 ceiling, and were locked out of passes 3-6
                    # having moved the render 2.4 L. Measuring displacement from the first reading
                    # instead of summing intentions costs nothing on a region that converges (it
                    # arrives, so the two agree) and stops one from being retired mid-correction.
                    _rfirst.setdefault(_name, _rgot[_name])
                    _rmoved[_name] = _rgot[_name] - _rfirst[_name]
                    _dd = float(np.clip(_e, -LEVEL_LOOP_CAP - _rmoved[_name],
                                        LEVEL_LOOP_CAP - _rmoved[_name]))
                    if abs(_e) <= LEVEL_LOOP_TOL or abs(_dd) < 1e-3:
                        continue
                    # ⭐ Adaptive relaxation, per region, and the mouth is why. The 2.0 here is an
                    # over-relaxation: measured, one L of albedo moves the rendered region about
                    # half an L, because the feathered footprint reaches texels the render does not
                    # show. That factor is a per-REGION property, though, and 2.0 is too much for a
                    # small one under a hard edge — Makar's mouth rang, -9.4, -14.1, -7.0, -9.4,
                    # spending four of six passes going nowhere and leaving the region's texture
                    # figure at 10.53 against a target of 7.22 purely from the edges it kept
                    # redrawing. A sign flip means the last step overshot, so halve that region's
                    # step and let it settle. Costs nothing on a region that was converging, since
                    # a converging region never flips.
                    if _rlast[_name] * _dd < 0:
                        _rrelax[_name] = max(_rrelax[_name] * 0.5, 0.5)
                    _rlast[_name] = _dd
                    # …and the size of that over-relaxation is itself measured, per region and per
                    # head — see `_rgain`. What the last pass put into the map is known exactly;
                    # what the render then did with it is the reading in hand. Only believe a step
                    # big enough to read (a tenth of an L of response is noise, not a transfer
                    # ratio), and blend rather than jump, so one noisy pass cannot set the gain.
                    _prev = _rasked.get(_name)
                    if _prev is not None and abs(_prev[1]) > 0.5:
                        _tr = (_rgot[_name] - _prev[0]) / _prev[1]
                        if _tr > 0.08:
                            _rgain[_name] = 0.5 * _rgain[_name] + 0.5 * float(
                                np.clip(1.0 / _tr, 1.2, 4.0))
                    _push = _rgain[_name] * _rrelax[_name] * _dd
                    _rasked[_name] = (_rgot[_name], _push)
                    # ⭐ WHERE in the footprint the lift goes, not only how much of it there is.
                    # Measured on the finished brows: a push spread flat over the footprint
                    # transfers 32.9% (Boeser) and 30.2% (Makar) into the render, while the SAME
                    # total ink weighted toward that footprint's dark texels transfers 47.1% and
                    # 46.4% — because a brow box's mean is owned by its strands, and lifting the
                    # skin between them cannot move a mean it does not hold.
                    #
                    # It is not free: the weighting also flattens the box's spread by about a
                    # quarter (-25.0%/-29.0%), which is the region's texture score. So it is spent
                    # only in proportion to how far that region's detail is ALREADY over target,
                    # which makes the level and detail terms one correction instead of two pulling
                    # opposite ways — Boeser's brow carries 3.41 against a portrait's 3.20 and can
                    # afford to lose contrast, Makar's carries 3.08 against 3.84 and cannot. Lifts
                    # only: on a region being darkened this would deepen the strands instead.
                    _wf = _rlvl[_name]
                    _over = 0.0
                    if _tnow and _name in _tnow and _ttgt.get(_name, 0.0) > 0.05:
                        _over = float(np.clip(_tnow[_name] / _ttgt[_name] - 1.0, 0.0, 1.0))
                    # ⛔ …and never on the MOUTH, whatever its detail figure says. The weighting was
                    # measured and validated on EYEBROWS, where a box's mean genuinely is owned by
                    # strand ink and lifting the strands is the only way to move it. The mouth's
                    # dark texels are not strands, they are a BOUNDARY, and brightening a boundary
                    # moves where it is found rather than how bright it is. Rebuilding _wd on
                    # _rmask (which holds the artist's aperture line out) fixed half of this —
                    # Boeser's upper lip came +17.3% -> +12.3% — but only half, because _rmask
                    # protects the ~1,400-texel line and not the vermilion body around it, and the
                    # lift still lands there. Gen 23 scored Makar's mouth grain 11.23 against his
                    # portrait's 7.22, climbing on every pass while the detail term asked x0.66 to
                    # cut it, and his lips 17-20% too thin.
                    if _over > 0.05 and _dd > 0 and _name != "mouth":
                        _in = _wf > 0.2 * max(float(_wf.max()), 1e-6)
                        if int(_in.sum()) > 200:
                            _Lm = _lab(np.clip(color, 0, 255).astype(np.uint8))[..., 0]
                            _hi = float(np.percentile(_Lm[_in], 85))
                            _lo = float(np.percentile(_Lm[_in], 5))
                            _dk = np.clip((_hi - _Lm) / max(_hi - _lo, 1e-6), 0.0, 1.0)
                            # ⚠ built on _rmask, NOT _rlvl — the artist's mouth line and the eye
                            # punch are held out. Weighting a lift toward a region's darkest texels
                            # is a CONTRAST operation however additively it is applied, and that is
                            # exactly what _keep exists to keep off the artist's ink. Measured the
                            # hard way: with this built on _rlvl, Boeser's upper lip read +5.4%,
                            # then +13.1%, then +17.3% over the three generations after the
                            # weighting went in, while the lip term was asking x0.77 to SHRINK it —
                            # the lift was brightening the vermilion border, and a softer border is
                            # a border the colour-based landmarker places further out. Makar's
                            # mouth grain climbing 9.47 -> 10.25 is the same thing seen sideways.
                            _wd = _rmask[_name] * _dk
                            _wd *= float(_wf.sum()) / max(float(_wd.sum()), 1e-6)
                            _wf = _wf * (1.0 - _over) + _wd * _over
                    _fld += _wf * _push
                    _wsum += _rlvl[_name]
                    _rmoved[_name] += _dd
                    _said.append(f"{_name} {_rgot[_name]:+.1f} vs {_rtgt[_name]:+.1f} "
                                 f"({_dd:+.1f} at x{_rgain[_name]:.1f})")
                if _said:
                    _done = False
                    _fld /= np.maximum(_wsum, 1.0)
                    # ⭐ The DC removal is confined to the footprint that ASKED for something, and
                    # this is a measured bug fix, not tidiness. It exists to stop the region loop
                    # from walking the whole face's tone (the absolute tone term owns that), and it
                    # used to subtract the mean over ALL skin and then apply the result over all
                    # skin — so every region that asked for a lift paid for it by pushing a small
                    # NEGATIVE shift onto every part of the face that had asked for nothing. The
                    # eye box is the largest such part and it is not in QA_SKIN_REGIONS at all, so
                    # it absorbed that residual every pass with nothing to push back: measured,
                    # Makar's box went -22.1 -> -32.6 across a single pass in which the eye term
                    # had just lifted his lids +3.8. The lids were being darkened by the mouth.
                    _sk = (np.clip(skin_m, 0, 1) * _keeplv
                           * np.clip(_wsum, 0, 1)).astype(np.float32)
                    if _sk.sum() > 500:
                        _fld -= float((_fld * _sk).sum() / _sk.sum())
                    _fld *= _sk
                    _rl2 = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    _rl2[..., 0] = np.clip(_rl2[..., 0] + _fld * 2.55, 0, 255)
                    color = _unlab(_rl2).astype(np.float32)
                    color = _probe('loop-region-L2', color)
                    log(f"  region check {_it + 1}: " + ", ".join(_said))

            # ── the EYE box, the one scored region that never had a loop ──────
            _eyg = _eye_dl(_r, _rlm)
            if _eyg is not None and _eybest is not None:
                _ee = _eybest - _eyg
                _ed = float(np.clip(_ee, -EYE_LIFT_CAP - _eylifted, EYE_LIFT_CAP - _eylifted))
                if abs(_ee) > LEVEL_LOOP_TOL and abs(_ed) > 1e-3 and _emask.max() > 1e-6:
                    _done = False
                    # gain 1.0, not the region term's 2.0: only part of this box is skin the map
                    # owns, so a step sized as if all of it responded would overshoot the lid by
                    # roughly the globe's share of the area and then have to walk it back.
                    _ef = _emask / max(_emask.max(), 1e-6) * (_ed * 2.55)
                    _el = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    _el[..., 0] = np.clip(_el[..., 0] + _ef, 0, 255)
                    color = _unlab(_el).astype(np.float32)
                    color = _probe('loop-eye-L', color)
                    _eylifted += _ed
                    log(f"  eye box {_it + 1}: renders {_eyg:+.1f} against {_eybest:+.1f} "
                        f"({_ed:+.1f} on the lids, {_eylifted:+.1f} of {EYE_LIFT_CAP:.0f} spent)")

            # ── and every scored region's CHROMA, which nothing has ever done ──
            # See `_region_ab`. Level, texture and hair all have loops and the face's own colour
            # never had one: Makar's portrait puts his nose +3.2 a against his face and the render
            # put it -3.2, and Boeser's +5.2 against a rendered +0.9. Uniformly-right-but-flat is
            # what a mannequin is, and it is measurable — the render's chroma spread across the face
            # reads 3.2 where the portraits read 4.7. Same shape as the level term in every respect:
            # zero-sum, capped per region, through the same feathered footprint, and applied to the
            # artist's ink as well since an additive shift cannot disturb it.
            _agot = _region_ab(_r, _rlm)
            if _agot and _rabtgt:
                _af = [np.zeros((H, W), np.float32), np.zeros((H, W), np.float32)]
                _aw = np.zeros((H, W), np.float32)
                _asaid = []
                for _name in QA_SKIN_REGIONS:
                    if _name not in _agot or _name not in _rabtgt:
                        continue
                    _de = [_rabtgt[_name][_i] - _agot[_name][_i] for _i in (0, 1)]
                    _dc = [float(np.clip(_de[_i], -AB_LOOP_CAP - _amoved[_name][_i],
                                         AB_LOOP_CAP - _amoved[_name][_i])) for _i in (0, 1)]
                    if max(abs(_x) for _x in _de) <= AB_LOOP_TOL or all(
                            abs(_x) < 1e-3 for _x in _dc):
                        continue
                    for _i in (0, 1):
                        # 2.0, not 1.5, and it is the same over-relaxation the level term uses for
                        # the same measured reason — the probe above puts chroma transfer at ~50%,
                        # so a step sized at the error arrives at half the error.
                        _af[_i] += _rlvl[_name] * (2.0 * _dc[_i])
                        _amoved[_name][_i] += _dc[_i]
                    _aw += _rlvl[_name]
                    _asaid.append(f"{_name} ({_agot[_name][0]:+.1f},{_agot[_name][1]:+.1f}) vs "
                                  f"({_rabtgt[_name][0]:+.1f},{_rabtgt[_name][1]:+.1f})")
                if _asaid:
                    _done = False
                    # confined to the footprint that asked, for the same reason the level field is
                    _sk2 = (np.clip(skin_m, 0, 1) * _keeplv
                            * np.clip(_aw, 0, 1)).astype(np.float32)
                    _al2 = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    for _i in (0, 1):
                        _af[_i] /= np.maximum(_aw, 1.0)
                        if _sk2.sum() > 500:
                            _af[_i] -= float((_af[_i] * _sk2).sum() / _sk2.sum())
                        _al2[..., _i + 1] = np.clip(_al2[..., _i + 1] + _af[_i] * _sk2, 0, 255)
                    color = _unlab(_al2).astype(np.float32)
                    color = _probe('loop-ab2', color)
                    log(f"  chroma check {_it + 1}: " + ", ".join(_asaid))

            # ── and every scored region's TEXTURE, by the same argument ─────
            # The deficits left after the level loop were all detail: Boeser's rendered mouth carried
            # 3.66 of spread against his portrait's 5.67, his chin 2.04 against 2.89; Makar's brows
            # 2.71 against 3.84. The map is not what is scored, and the render is a low-pass on it —
            # measured at ~30% on the hair — so ask the render how much detail actually survived and
            # put back what did not, region by region, instead of guessing a single map-side gain.
            #
            # ⚠ Capped hard. Some of what these boxes measure is SHADING and not albedo at all: on a
            # uniform-albedo, flat-normal render the eye region already carries 65-77% of the
            # portrait's figure and the jaw 40-59%, so for those a gain can chase a number it will
            # never reach and only make the map noisy. TEX_LOOP_CAP is what stops it.
            #
            # It cuts as well as adds, and it has to: the projection can hand a region MORE grain
            # than the man has, off a sharpened press photo, and until this gain was allowed below
            # one there was nothing anywhere in the builder that could take grain back out again.
            _tgot = _region_tex(_r, _rlm)
            if _tgot and _ttgt:
                _gf = np.zeros((H, W), np.float32)
                _gw = np.zeros((H, W), np.float32)
                _tsaid = []
                for _name in QA_SKIN_REGIONS:
                    if _name not in _tgot or _name not in _ttgt or _tgot[_name] < 0.05:
                        continue
                    _gg = float(np.clip(_ttgt[_name] / _tgot[_name],
                                        1.0 / TEX_CUT_CAP, TEX_LOOP_CAP))
                    # ⭐ Ask for what ARRIVES, not for the ratio, and spend the allowance on
                    # MOVEMENT rather than on intention — the two faults the level term above had,
                    # both of them still here because raising TEX_LOOP_CAP to 3.0 looked like it
                    # had addressed the symptom. It had not: Makar's jaw asked x1.27, x1.39, x1.26,
                    # x1.28 over four passes, a cumulative 2.85 that never reached the 3.0 ceiling,
                    # so the ceiling was never what was stopping it. What the render did over those
                    # same four passes was 2.79 -> 3.21 -> 3.53 -> 3.49 against a target of 4.46:
                    # x1.25 delivered for x2.85 asked, and the last request moved it BACKWARDS.
                    #
                    # So estimate the transfer the same secant way, in log space because this gain
                    # is multiplicative, and let a region that answers at a third ask three times
                    # as loudly. Only believe a request big enough to read, blend rather than jump,
                    # and floor the exponent at 1.0 so this can only ever ask for MORE than the
                    # naive ratio, never less — a region whose grain is largely SHADING (the eye
                    # box carries 65-77% of the portrait's figure on a flat-normal render, the jaw
                    # 40-59%) will report a hopeless transfer forever, and the honest answer there
                    # is TEX_LOOP_CAP stopping it, not this term quietly giving up early.
                    _prev = _tasked.get(_name)
                    if _prev is not None and abs(float(np.log(_prev[1]))) > 0.05:
                        _tr = float(np.log(max(_tgot[_name], 1e-6) / max(_prev[0], 1e-6))
                                    / np.log(_prev[1]))
                        if _tr > 0.08:
                            _tgain[_name] = 0.5 * _tgain[_name] + 0.5 * float(
                                np.clip(1.0 / _tr, 1.0, 3.0))
                    _gg = float(np.clip(float(np.exp(np.log(max(_gg, 1e-6)) * _tgain[_name])),
                                        1.0 / TEX_CUT_CAP, TEX_LOOP_CAP))
                    _tfirst.setdefault(_name, _tgot[_name])
                    _tmv = _tgot[_name] / max(_tfirst[_name], 1e-6)
                    _gg = float(np.clip(_gg, 1.0 / (TEX_CUT_CAP * _tmv), TEX_LOOP_CAP / _tmv))
                    # ⭐ Detail DEFERS to level, because on a region made of ink the two are not
                    # independent: adding contrast to a brow deepens its strands, and the strands
                    # own the box's mean, so this term was handing back what the level term had
                    # just won. Measured on Boeser's eyebrows — the level term moved the render
                    # -10.8 -> -8.4 across two passes and was then locked out, while this one asked
                    # x1.09, x1.14, x1.10, x1.12 on each of the four passes after it and the box
                    # finished at -9.2, exactly where it began. A region that is not yet the right
                    # BRIGHTNESS has no business being given texture. Ramped rather than gated, so
                    # a region does not lurch into having grain the moment it crosses a threshold.
                    if _rgot and _rtgt and _name in _rgot and _name in _rtgt:
                        _lerr = abs(_rtgt[_name] - _rgot[_name])
                        _gg = 1.0 + (_gg - 1.0) * float(np.clip((5.0 - _lerr) / 3.5, 0.0, 1.0))
                    if abs(_gg - 1.0) <= TEX_LOOP_TOL:
                        continue
                    _gf += _rmask[_name] * (_gg - 1.0)
                    _gw += _rmask[_name]
                    _tasked[_name] = (_tgot[_name], _gg)
                    _tsaid.append(f"{_name} {_tgot[_name]:.2f} vs {_ttgt[_name]:.2f} "
                                  f"(x{_gg:.2f} at ^{_tgain[_name]:.1f})")
                if _tsaid:
                    _done = False
                    _gf /= np.maximum(_gw, 1.0)
                    _gf *= (np.clip(skin_m, 0, 1) * _keep).astype(np.float32)
                    _tl = _lab(np.clip(color, 0, 255).astype(np.uint8))
                    # ⭐ A NARROW band, not the 0.8-6.0 one the hair uses, and this is the whole
                    # difference between skin and blotch. The measurement this gain chases is a
                    # single number — the spread of L finer than 8 px — and blotches satisfy it just
                    # as well as pores do. Amplifying through a six-texel band therefore paid for the
                    # number with exactly the wrong energy: it multiplied whatever was already there,
                    # which on a projected map is the projection's own seams, the source JPEG's
                    # ringing and the symmetry fill's mismatch, and the cheeks and forehead came out
                    # matching their figure and reading as blotchy. Pores, stubble and skin grain all
                    # live under about two and a half texels; a blotch does not. Take the same number
                    # from the finer half of the band and the render reads as skin instead.
                    _tb = _bp(_tl[..., 0], 0.8, SKIN_DETAIL_SIGMA)
                    # …and a ceiling on the ENERGY, independent of the ratio being chased. The gain
                    # is a ratio and a ratio has no idea how big the thing it is multiplying is: a
                    # region that starts with a seam through it gets that seam multiplied by the same
                    # x2.2 as a region that starts clean. Past a few L of added swing on one texel it
                    # has stopped being detail whatever the ratio says.
                    _add = np.clip(SOFT_KNEE * np.tanh(_tb * _gf / SOFT_KNEE),
                                   -DETAIL_ADD_CAP * 2.55, DETAIL_ADD_CAP * 2.55)
                    _tl[..., 0] = np.clip(_tl[..., 0] + _add, 0, 255)
                    color = _unlab(_tl).astype(np.float32)
                    color = _probe('loop-tone', color)
                    log(f"  detail check {_it + 1}: " + ", ".join(_tsaid))

            if _done:
                log(f"  render check: everything inside tolerance after {_it + 1} pass(es)")
                break

    # ── the occlusion map, from the fitted GEOMETRY ───────────────────────────
    # The shipped map describes the base head's concavities and this is no longer the base head, so
    # fold in the AO the fitted mesh actually implies (mesh_occlusion). MULTIPLIED, not replaced:
    # the artist's map carries fine cavity detail — nostrils, the lip line, the folds inside the ear
    # — that no vertex-density AO reaches, and the geometric term carries the broad concavities that
    # move when the head is reshaped, chiefly the mandible and the neck under the chin.
    occ_out = maps.get("occlusion")
    if occ_out is not None and ao > 0:
        gao = mesh_occlusion(M, pos_uv, nrm_uv, uv_mask, strength=ao, log=log)
        # ⭐ …minus its noise floor. mesh_occlusion normalises by the island's 90th percentile, so
        # nearly every texel darkens a LITTLE, and on the convex forehead and temples that printed
        # as a grey smear the artist's map does not have — the ablation renders put part of the
        # "indented forehead" report here. Divide the deadband back out: occlusion shallower than
        # AO_DEADBAND returns to open-surface, and a real crease (the mandible measured ~20%)
        # keeps nearly all of its depth.
        gao = np.minimum(gao / (1.0 - AO_DEADBAND), 1.0)
        # …but NOT in the eye sockets. The socket is the deepest concavity on the head and the mesh
        # AO reads it as one, so it came out at 70 against the artist's 123 — a 43% darkening of the
        # one region that has no business being dark, because in the game an EYEBALL fills that hole
        # and the socket is never seen empty. The geometry the AO integrates is not the geometry the
        # player looks at. Every other stage already treats the sockets as the artist's — the colour
        # is restored from the base map there and the normal is excluded from the projection — so
        # this is that same statement, applied to the third map.
        gao = 1.0 + (gao - 1.0) * (1.0 - eye)
        oa = np.asarray(occ_out, np.float32)
        occ_out = Image.fromarray(np.clip(oa * gao[..., None], 0, 255).astype(np.uint8))

    # Close the unwrap's cut, LAST — every stage above paints into UV without knowing that the two
    # lips of the cut are the same place on the head, so each one adds its own contribution to the
    # step and only the finished map has all of it. All three maps: the normal map's step is a
    # shading step and measured as large as the colour one, so leaving it out leaves the line.
    col_out = heal_seams(Image.fromarray(np.clip(color, 0, 255).astype(np.uint8)), base_id, game_dir)
    if nrm_out is not None:
        nrm_out = heal_seams(nrm_out.convert("RGB"), base_id, game_dir).convert(nrm_out.mode)
    if occ_out is not None:
        occ_out = heal_seams(occ_out.convert("RGB"), base_id, game_dir).convert(occ_out.mode)

    res = {"color": col_out,
           "normal": nrm_out, "occlusion": occ_out,
           "masks": Image.fromarray(np.uint8(np.dstack([have, skin_m, hair_m]) * 255)),
           "coverage": have,
           # everything an intensity control needs to re-derive the normal on its own, without
           # re-running the projection: detail_normal(res["color"], res["base_normal"],
           # res["normal_weight"], bump=<slider>, hair=res["hair_weight"]) is the whole re-bake.
           # `hair_weight` has to travel with it or the slider rebuilds a map with the strand gain
           # missing, which is visibly flatter than the one the build produced. It is the HARDENED
           # mask (`hair_n`), the same one the build used — handing the slider the soft vote would
           # put the forehead relief bug straight back on every re-bake.
           "base_normal": base_nrm, "normal_weight": wn.astype(np.float32),
           "hair_weight": hair_n.astype(np.float32),
           # the 468 landmarks in MAP pixels — facial_hair.measure() reads the beard region off
           # the finished map with these, so it does not have to re-detect anything
           "landmarks": np.asarray(t_lm, np.float32)}
    # ⚠ The aperture stage moves the MESH, so the positions that come out of here are not always the
    # ones that went in and the caller has to write THESE, not the fit's. Absent when nothing moved,
    # so a caller that does `positions = res.get("positions", positions)` is always correct.
    if _eyepos is not None:
        res["positions"] = np.asarray(_eyepos, np.float32)
    return res


# ── slots + install ──────────────────────────────────────────────────────────
def free_slots(ros_path=None, game_dir=None):
    """Head ids that exist as an asset but NO player points at — safe to overwrite. Without a roster
    this can't be answered (usage lives in Roster.ROS), so it returns []."""
    try:
        from .char_model import head_ids
        from .player_assign import PlayerTable
    except ImportError:
        from char_model import head_ids
        from player_assign import PlayerTable
    have = set(head_ids(game_dir))
    if not ros_path:
        return []
    used = set(PlayerTable(ros_path).head_usage())
    return sorted(have - used)


def install(head_id, maps, game_dir, log=print, only=None):
    """Write the built maps into player_head_id_<head_id>.iff, in place.

    `only` limits which of color/normal/occlusion are written. Every write is same-dimension and
    same-format by construction: the three surfaces exactly fill the 983,040-byte VRAM blob with no
    slack, so growing or format-upgrading any of them would push the loader's placement cursor and
    desync the two after it."""
    import tempfile
    nm = HEAD_FMT.format(head_id)
    recs = {r["label"]: r for r in A.list_textures(nm, game_dir)}
    edits, tmp = [], Path(tempfile.mkdtemp(prefix="n2k_face_"))
    for label, rec in recs.items():
        if (only and label not in only) or label not in maps:
            continue
        p = tmp / f"{label}.png"
        img = maps[label].convert("RGB").resize((rec["w"], rec["h"]))
        if label == "color":
            img = gloss_alpha(img, A.decode_record(nm, rec, game_dir), log=log)
        img.save(p)
        edits.append({**rec, "path": str(p)})
    if not edits:
        raise ValueError("nothing to install")
    log(f"  installing {len(edits)} map(s) into {nm}: {', '.join(e['label'] for e in edits)}")
    return A.replace_many(nm, edits, Path(game_dir), log=log, prefer_lossless=False)


def assign(ros_path, row, head_id, game_dir=None, log=print, backup=True):
    """Point one roster row at `head_id` and save. Writes are in place (file size never changes)."""
    try:
        from .player_assign import PlayerTable
    except ImportError:
        from player_assign import PlayerTable
    t = PlayerTable(ros_path)
    first, last = t.name(row)
    was = t.head(row)
    t.set_head(row, head_id, validate=True, game_dir=game_dir)
    t.save(backup=backup)
    log(f"  {first} {last} (row {row}): head {was} -> {head_id}")
    return {"row": row, "name": f"{first} {last}", "was": was, "now": head_id}
