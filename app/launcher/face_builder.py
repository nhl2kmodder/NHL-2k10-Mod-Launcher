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
# Scale, in texels, over which the view weights are smoothed before the detail donor is chosen. Large
# on purpose: the point is that a whole feature comes from ONE photograph.
LABEL_SIGMA = 25.0
# Joint-alignment passes, and the largest displacement in texels that is still credible as
# registration error rather than the flow having found a different feature entirely.
FLOW_PASSES, FLOW_MAX = 2, 5.0
# Relief multiplier for the rebuilt normal layer, over the artist's own measured relief.
NORM_BOOST = 2.2

# mediapipe 478-point face-mesh index sets (the canonical topology, stable across versions).
FACE_OVAL = (10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378,
             400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54,
             103, 67, 109)
L_EYE = (33, 7, 163, 144, 145, 153, 154, 155, 133, 173, 157, 158, 159, 160, 161, 246)
R_EYE = (263, 249, 390, 373, 374, 380, 381, 382, 362, 398, 384, 385, 386, 387, 388, 466)

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
    hole = (msk == 0).astype(np.uint8)                # close the seams the rasteriser leaves
    k = np.ones((3, 3), np.uint8)
    for _ in range(2):
        for arr in (pos, nrm):
            grown = cv2.dilate(arr, k)
            arr[hole > 0] = grown[hole > 0]
        grown_m = cv2.dilate(msk, k)
        msk = np.where(hole > 0, grown_m, msk)
        hole = (msk == 0).astype(np.uint8)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=2, keepdims=True), 1e-12)
    return pos, nrm, msk > 0, M


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


def _flatten(rgb, cov, sigma=45):
    """Strip a photograph's own lighting: divide out its low-frequency luminance over the covered
    area. What survives is albedo plus fine detail, which is the only part that should be shared
    between two photos shot in different rooms.

    `cov` weights where the lighting is MEASURED, not where it is corrected — the correction is
    applied to the whole frame. So it wants to name one material and only that one. Hand it the face
    and it reads the light off skin; hand it the whole head and a dark head of hair looks to it like
    a shadow, which it then dutifully brightens until the player is grey on top."""
    cv2 = _cv2()
    lab = _lab(rgb)
    L = lab[..., 0]
    w = cov.astype(np.float32)
    num = cv2.GaussianBlur(L * w, (0, 0), sigma)
    den = cv2.GaussianBlur(w, (0, 0), sigma)
    lo = num / np.maximum(den, 1e-4)
    ref = float(np.median(L[cov > 0.5])) if (cov > 0.5).any() else 128.0
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


def detail_normal(color, base_normal, weight, bump=1.0, sigma=2.0, hair=None, chroma_gate=True,
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
        a = amp * (1.0 - hairm) + hamp * hairm
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
    k = k * (1.0 + 1.2 * hairm)                         # strands read at roughly twice skin relief

    sx += (cv2.GaussianBlur(sx, (0, 0), sigma) - sx) * w + k * gx * w
    sy += (cv2.GaussianBlur(sy, (0, 0), sigma) - sy) * w + k * gy * w

    nz = 1.0 / np.sqrt(1.0 + sx * sx + sy * sy)
    out = np.dstack([-sx * nz, sy * nz, nz]) * 127.5 + 127.5
    return np.clip(out, 0, 255).astype(np.uint8)


def build_multi(ref_paths, base_id, game_dir=None, positions=None, sharp=2.0, delight=0.9,
                flatten=0.85, fill_hair=True, chroma=0.7, bump=0.5, ao=1.0, log=print):
    """Build the three maps from SEVERAL photographs — the whole map, not a face patch.

    ref_paths     any number of photos of the same head at any angles; the more yaw spread the more
                  of the head gets real pixels. Photos with no detectable face are skipped.
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
    log(f"  dark-feature exemption: {100 * (feat_m > 0.5).mean():.1f}% of the map "
        f"(mouth, nostrils, lash line) freed from the darkness gate")

    acc = np.zeros((H, W, 3), np.float32)
    wacc = np.zeros((H, W, 1), np.float32)
    shot, capped = [], []
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
        photo = _flatten(np.array(v["img"]), head.astype(np.float32) / 255.0)

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
        w *= inside * uv_mask * min(v["face_px"] / 220.0, 1.4) * torso

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
        dmax = 0.0
        for ch in (0, 1, 2):
            Cs = np.stack([l[..., ch] for l in labs])
            cons = (Cs * ws).sum(0) / wsum
            dmax = max(dmax, float(np.abs((Cs - cons) * (ws > 0.2)).max()))
            for i, l in enumerate(labs):
                lo = (cv2.GaussianBlur((Cs[i] - cons) * ws[i], (0, 0), CONSENSUS_SIGMA)
                      / np.maximum(blur[i], 1e-4))
                l[..., ch] = np.clip(l[..., ch] - lo * CONSENSUS, 0, 255)
        bal = [_unlab(l).astype(np.float32) for l in labs]
        log(f"  consensus de-light: up to {dmax:.0f} of view-to-view disagreement removed")

    for smp, (_, w) in zip(bal, shot):
        acc += smp * w[..., None]
        wacc += w[..., None]

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
    thin = raw_have < 0.05
    if thin.any():
        fillp = proj
        for sig in (16.0, 5.0):
            den = cv2.GaussianBlur(wacc[..., 0], (0, 0), sig)[..., None]
            est = cv2.GaussianBlur(acc, (0, 0), sig) / np.maximum(den, 1e-6)
            c = np.clip(den / 0.02, 0.0, 1.0)
            fillp = est * c + fillp * (1 - c)
        proj = np.where(thin[..., None], fillp, proj)
        log(f"  pinholes: {int((thin & (have > 0.5)).sum()):,} unreached texels inside the "
            f"covered area filled from their surroundings")

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
        out = None
        for lv in range(nb - 1, -1, -1):
            ws_ = np.stack(gw[lv])[..., None]
            tot = np.maximum(ws_.sum(0), 1e-8)
            if lv == nb - 1:                                  # coarsest: the tone everyone shares
                out = (np.stack(gp[lv]) * ws_).sum(0) / tot
                continue
            hs = [gp[lv][i] - cv2.pyrUp(gp[lv + 1][i], dstsize=gp[lv][i].shape[1::-1])
                  for i in range(len(bal))]
            band = (np.stack(hs) * ws_).sum(0) / tot
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
        hL = _lab(np.clip(proj, 0, 255).astype(np.uint8))[..., 0]
        dark = hair_ref & (hL <= np.percentile(hL[hair_ref], 35))
        hair_rgb = np.median(proj[dark if dark.sum() > 200 else hair_ref].reshape(-1, 3), 0)
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
    hair_m = np.maximum(hair_m, np.clip((sL - blab[..., 0] - 8.0) / 14.0, 0, 1)
                        * (1.0 - face_m.astype(np.float32) / 255.0)
                        * (1.0 - np.clip(reach, 0.0, 1.0))
                        * np.clip((0.02 * face_h - below) / (0.10 * face_h), 0.0, 1.0))
    hs, hh = hair_m <= 0.35, hair_m >= 0.65
    tgt_s = _lab(np.clip(skin_rgb, 0, 255).astype(np.uint8).reshape(1, 1, 3))[0, 0]
    tgt_h = _lab(np.clip(hair_rgb, 0, 255).astype(np.uint8).reshape(1, 1, 3))[0, 0]
    src_s = np.median(blab[hs], 0) if hs.sum() > 200 else tgt_s
    src_h = np.median(blab[hh], 0) if hh.sum() > 200 else tgt_h
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
    if fill_hair and hh.sum() > 400 and hair_ref.sum() > 400:
        s_src = mad(blab[..., 0][hh])
        s_tgt = mad(_lab(np.clip(proj, 0, 255).astype(np.uint8))[..., 0][hair_ref])
        if s_src > 1.0:
            gain = float(np.clip(s_tgt / s_src, 0.6, 2.2))
    hm = hair_m[..., None]
    lab_skin = blab + (tgt_s - src_s)
    lab_hair = tgt_h + (blab - src_h) * np.array([gain, 1.0, 1.0], np.float32)
    fill = _unlab(np.clip(lab_skin * (1 - hm) + lab_hair * hm, 0, 255)).astype(np.float32)
    if fill_hair:
        log("  hair fill: tone %d/%d/%d, " % tuple(np.round(hair_rgb).astype(int)[:3])
            + f"contrast x{gain:.2f}, over {100 * (hm[..., 0] > 0.5).mean():.0f}% of the map; "
            f"tone read off {src}")
    # and the mouth interior keeps the artist's own pigment. The retint above shifts every texel
    # onto the measured SKIN, which is right for a cheek and wrong for a mouth interior — it lifted
    # the shipped dark red towards pale skin and the line came back grey. A mouth interior is dark
    # red on everybody; it is not a place the player's complexion belongs.
    fill = fill * (1 - aper_m[..., None]) + base_np.astype(np.float32) * aper_m[..., None]
    color = proj * reach[..., None] + fill * (1 - reach[..., None])

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

    # ── flatten the map's own shading: an albedo, not a photograph ────────────
    # This used to lay the BASE map's low-frequency lightness over ours, on the theory that its
    # shading was authored for this engine. It is — for the base head's face. Ours is a different
    # shape with its features in different places, so the base's dark eye sockets landed under our
    # eyes and its chin shadow landed on our jaw: the dark streaks down the cheeks were entirely
    # this. Nothing should bake lighting into the colour map at all — the head already ships a
    # normal map and an occlusion map, and the engine lights it from those. So the broad shading
    # that the photographs brought with them gets divided out and the map is left as flat albedo.
    if flatten > 0:
        lab = _lab(np.clip(color, 0, 255).astype(np.uint8))
        L = lab[..., 0]
        # measured on SKIN, applied everywhere: a lighting field belongs to the room, not to the
        # material, so hair must not be allowed to vote on it and must not be flattened towards it.
        m = (np.clip(reach, 0.0, 1.0) * np.clip(skin_m, 0.0, 1.0)).astype(np.float32)
        ref = float(np.median(L[m > 0.5])) if (m > 0.5).any() else 128.0
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
        lab[..., 0] = np.clip(L + (ref - lo_c) * flatten, 0, 255)
        color = _unlab(lab).astype(np.float32)
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
        for ch in (1, 2):
            sm = cv2.GaussianBlur(lab[..., ch], (0, 0), 9)
            lab[..., ch] += (sm - lab[..., ch]) * chroma
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
    color += (base_np.astype(np.float32) - color) * collar[..., None]

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
    if seen.sum() > 500:
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
        clab[..., 0] = np.clip(cL + n * need, 0, 255)
        color = _unlab(clab).astype(np.float32)
        log(f"  grain synthesis: sigma {sd:.2f} measured on resolved skin, carried into "
            f"{100 * (need > 0.3).mean():.0f}% of the map")

    color = np.where(uv_mask[..., None], color, fill)

    # the eye sockets are empty on purpose (the eyeball is separate geometry drawn in front)
    eye = np.zeros((H, W), np.float32)
    for e in (L_EYE, R_EYE):
        hole = np.zeros((H, W), np.uint8)
        cv2.fillConvexPoly(hole, _hull(t_lm[list(e)], 1.35), 255)
        eye = np.maximum(eye, cv2.GaussianBlur(hole, (0, 0), 3).astype(np.float32) / 255.0)
    color = color * (1 - eye[..., None]) + base_np.astype(np.float32) * eye[..., None]

    # ── the normal map, from the same photographs ─────────────────────────────
    # The colour map is deliberately flat albedo now, so all the relief the references carry has to
    # arrive through here or it is simply lost. Trust it exactly where the colour is trusted: the
    # ear zone and the eye sockets are the artist's in both maps, and so is everything the cameras
    # never reached.
    nrm_out = base_nrm = maps.get("normal")
    wn = (np.clip(reach, 0, 1) * uv_mask.astype(np.float32)
          * (1.0 - ear_zone) * (1.0 - eye))
    # Feather the trust boundary. `wn` is a product of hard-ish masks and it was printing a
    # RECTANGULAR edge across the top of the normal map — the relief simply stopped along a straight
    # line, which no face does.
    wn = cv2.GaussianBlur(wn, (0, 0), 5.0)
    if bump > 0 and nrm_out is not None:
        nrm_out = Image.fromarray(detail_normal(color, nrm_out, wn, bump=bump, hair=hairy))
        log(f"  normal map: photographic relief over {100 * (wn > 0.5).mean():.0f}% of the map")

    # ── the occlusion map, from the fitted GEOMETRY ───────────────────────────
    # The shipped map describes the base head's concavities and this is no longer the base head, so
    # fold in the AO the fitted mesh actually implies (mesh_occlusion). MULTIPLIED, not replaced:
    # the artist's map carries fine cavity detail — nostrils, the lip line, the folds inside the ear
    # — that no vertex-density AO reaches, and the geometric term carries the broad concavities that
    # move when the head is reshaped, chiefly the mandible and the neck under the chin.
    occ_out = maps.get("occlusion")
    if occ_out is not None and ao > 0:
        gao = mesh_occlusion(M, pos_uv, nrm_uv, uv_mask, strength=ao, log=log)
        oa = np.asarray(occ_out, np.float32)
        occ_out = Image.fromarray(np.clip(oa * gao[..., None], 0, 255).astype(np.uint8))

    res = {"color": Image.fromarray(np.clip(color, 0, 255).astype(np.uint8)),
           "normal": nrm_out, "occlusion": occ_out,
           "masks": Image.fromarray(np.uint8(np.dstack([have, skin_m, hair_m]) * 255)),
           "coverage": have,
           # everything an intensity control needs to re-derive the normal on its own, without
           # re-running the projection: detail_normal(res["color"], res["base_normal"],
           # res["normal_weight"], bump=<slider>, hair=res["hair_weight"]) is the whole re-bake.
           # `hair_weight` has to travel with it or the slider rebuilds a map with the strand gain
           # missing, which is visibly flatter than the one the build produced.
           "base_normal": base_nrm, "normal_weight": wn.astype(np.float32),
           "hair_weight": hairy.astype(np.float32),
           # the 468 landmarks in MAP pixels — facial_hair.measure() reads the beard region off
           # the finished map with these, so it does not have to re-detect anything
           "landmarks": np.asarray(t_lm, np.float32)}
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
        maps[label].convert("RGB").resize((rec["w"], rec["h"])).save(p)
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
