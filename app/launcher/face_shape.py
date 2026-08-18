"""Give a player head its own GEOMETRY, fitted from reference photographs.

face_builder.py paints a head; this file re-shapes one. The two halves share the same enabling
fact and are meant to run together: `face_builder.landmarks()` finds mediapipe's 478-point face
mesh ON THE GAME'S OWN UV COLOUR MAP, which pins every landmark to a UV coordinate — and a UV
coordinate is a point on the mesh. That closes the loop:

    photo  --mediapipe-->  478 landmarks in 3D          (what the player's head IS)
    UV map --mediapipe-->  478 landmarks in UV
    UV     --barycentric-> 478 points on the game mesh  (what the game's head IS)

Subtract, and you have a per-landmark 3D displacement measured in the mesh's own centimetres.
Spread it over the rest of the vertices and the head changes shape.

WHY MULTIPLE REFERENCES MATTER. A single frontal headshot pins X and Y well and Z barely at all —
mediapipe's depth on a front-on face is mostly its learned average face. Give it the same head at
several yaws and the depths disagree in an informative way: the network sees the real nose
projection, brow ridge and chin in the turned views. `fuse_shape()` rigidly aligns every view into
one frame and takes a visibility-weighted average, so each landmark's depth comes mostly from the
views that could actually SEE it. That is the difference between a head that is the base mesh with
a new face painted on and a head with the player's own profile.

WHAT MOVES. The displacement field is evaluated in 3D (normalised Gaussian weights over the
landmark cloud), so every part of the asset moves together — face island, eyeballs, brows, lashes,
mouth bag, hair cards. Nothing is re-topologised: vertex count, triangle list, UVs, skin weights
and bone indices are all untouched, which is what keeps the head animating and lets the write go
back in place with no size change. Displacement falls off with distance from the landmark cloud, so
the back of the skull, the neck and the chest stay exactly where the artist put them.

LIMITS worth knowing: mediapipe's mesh has no ears and stops at the hairline, so ear shape, skull
depth and hair volume are inherited from the base head — those are texture-side jobs. And the
positions repack to snorm16 against the model's own ModelPosScaleAndOffset, so a displacement that
leaves the original packing box is clamped (char_model warns).
"""
from __future__ import annotations

import numpy as np
from pathlib import Path

from . import char_model as C
from . import face_builder as FB

# mediapipe's last ten points are the two irises — they sit on the eyeball, not the skin, and the
# eyeball is separate geometry with its own centre. Using them as skin correspondences drags the
# lids around. Everything before 468 is skin.
N_SKIN = 468


# ───────────────────────────── references ─────────────────────────────
def read_refs(paths, min_face_px=90):
    """[{path, img, lm, lm3, yaw, pitch, roll, face_px}] for every photo with a detectable face.

    lm  = 478x2 pixel landmarks (what face_builder warps with)
    lm3 = 478x3 with z put in the SAME units as x. mediapipe normalises x and y by width and
          height independently but z by width, so the three axes only agree once y is multiplied
          by the aspect ratio — skipping that shears every fit on a non-square photo.

    focus = scale-free sharpness: the standard deviation of the face crop's own high-pass, banded
          at a FIXED FRACTION of the face rather than a fixed pixel radius, so it measures "detail
          per face" and a larger photograph of the same softness does not score higher for being
          larger. See face_builder's SHARP_FLOOR for why it exists and what it is worth.
    """
    import cv2
    from PIL import Image
    out = []
    for p in sorted(Path(pp) for pp in paths):
        img = Image.open(p)
        if img.mode == "RGBA":                          # headshots are often cut out on alpha
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.getchannel("A"))
            img = bg
        img = img.convert("RGB")
        try:
            res = FB.landmarks(img, raw=True)
        except ValueError:
            continue
        lm, mat = res["norm"], res["matrix"]
        W, H = img.size
        px = np.column_stack([lm[:, 0] * W, lm[:, 1] * H]).astype(np.float32)
        if np.ptp(px[:, 0]) < min_face_px:
            continue
        lm3 = np.column_stack([lm[:, 0] * W, lm[:, 1] * H, lm[:, 2] * W]).astype(np.float32)
        R = mat[:3, :3]
        fpx = float(np.ptp(px[:, 0]))
        x0, y0 = np.maximum(np.floor(px.min(0)).astype(int), 0)
        x1, y1 = np.minimum(np.ceil(px.max(0)).astype(int), [W, H])
        crop = cv2.cvtColor(np.asarray(img, np.uint8)[y0:y1, x0:x1],
                            cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
        sig = max(fpx / 110.0, 0.8)
        focus = float((crop - cv2.GaussianBlur(crop, (0, 0), sig)).std()) if crop.size else 0.0
        out.append(dict(path=p, img=img, lm=px, lm3=lm3, focus=focus,
                        yaw=float(np.degrees(np.arctan2(-R[2, 0], np.hypot(R[2, 1], R[2, 2])))),
                        pitch=float(np.degrees(np.arctan2(R[2, 1], R[2, 2]))),
                        roll=float(np.degrees(np.arctan2(R[1, 0], R[0, 0]))),
                        face_px=fpx))
    return out


def _similarity(src, dst, w=None):
    """Least-squares scale+rotation+translation taking src onto dst (optionally weighted)."""
    w = np.ones(len(src)) if w is None else np.asarray(w, np.float64)
    w = w / w.sum()
    sc, dc = (src * w[:, None]).sum(0), (dst * w[:, None]).sum(0)
    s0, d0 = src - sc, dst - dc
    H = (s0 * w[:, None]).T @ d0
    U, _S, Vt = np.linalg.svd(H)
    R = (U @ Vt).T
    if np.linalg.det(R) < 0:                            # never let the fit mirror the face
        Vt[-1] *= -1
        R = (U @ Vt).T
    scale = (w[:, None] * d0 * (s0 @ R.T)).sum() / max((w[:, None] * s0 ** 2).sum(), 1e-12)
    return scale, R, dc - scale * (sc @ R.T)


def _apply(P, srt):
    scale, R, t = srt
    return P @ R.T * scale + t


# ⭐ How far mediapipe's 3-D landmarks may be trusted as a function of how far the head is turned.
#
# Its 3-D output is a weak-perspective reconstruction whose depth channel is loosely constrained, and
# off-axis it does not degrade gracefully: it INFLATES the face laterally, worst in the eye region.
# Measured on both reference sets, the inner-canthus span over face height, against the portrait's
# own 2-D truth (Boeser 0.188, Makar 0.225):
#
#   yaw     -1     -0    +25    -37    -38    -47    -56          |  Makar  +1    -16    +27    +37    +45
#   3-D  0.185  0.182  0.214  0.228  0.235  0.242  0.274          |      0.216  0.226  0.218  0.253  0.262
#
# Frontal is exact on both men; 56 degrees is +48%. The outer span does the same thing more mildly,
# so it is not one scale factor that could be divided out — the eye region spreads faster than the
# face around it.
#
# So a turned view must not be allowed to re-measure something a frontal view already saw. It is
# still the ONLY evidence for what the frontal cannot see — the side of the jaw, the cheek's
# curvature away from camera — and that is what the exponent below is for; see the note in the fuse.
YAW_TRUST_POW = 3.0

# …and how far to go beyond discounting, to the flat rule that where the PORTRAIT can see a thing,
# the portrait decides it outright. Re-weighting alone cannot finish this job and the reason is not
# subtle: Boeser's reference set is seven turned photographs against two frontal ones, so even at a
# yaw exponent of 6 the turned views still carry a third of the vote and still drag his inner-canthus
# span 10% wide. Trading the variance of one photograph for the removal of a measured 26% bias is a
# good trade, and it is the same rule that already governs eye opening.
#
# Applied to the anchor's IN-PLANE axes only, and gated on visibility rather than on squareness.
#
# The first attempt scaled it by how squarely the anchor faced each landmark, and it moved the eye
# opening nicely and the inner-canthus span not at all — 13.7% wide at every setting from 0 to 1.
# The reason is that squareness answers the wrong question. The inner corner of an eye sits in a
# socket whose local surface runs edge-on to a frontal camera, so its squareness is ~0, and yet a
# frontal portrait measures where that corner IS about as well as it measures anything. What a
# frontal photograph is bad at is DEPTH, which is also exactly where mediapipe's reconstruction is
# weak, and depth error is what leaks sideways into the lateral inflation once the views are
# rigidly aligned to each other.
#
# So: take the two in-plane axes of the anchor's camera from the anchor, leave the depth axis to the
# fusion — that is what the turned views are actually for — and gate the whole thing on whether the
# landmark is front-facing at all, so the far side of the head is untouched.
FRONT_AUTHORITY = 1.0


def cloud_normals(P, k=14):
    """Outward unit surface normal at each landmark, by local PCA over its k nearest neighbours.

    The obvious cheap proxy — the radial direction from the cloud's centroid — is what this replaces,
    and it is not a small improvement. That proxy is only as good as the head is spherical, and it
    degrades to noise wherever a landmark sits NEAR the centroid, because there the position vector
    is short and its direction is dominated by whatever the neighbours happen to do. On a face-mesh
    cloud the centroid lands just behind the nose, so the region where the proxy is worthless is the
    eye region — which is precisely the region where the multi-view fusion needed to know the answer.
    A local plane fit has no such failure: it reads the surface the neighbours lie on regardless of
    where that surface sits relative to the middle of the head.
    """
    P = np.asarray(P, np.float64)
    d2 = _cdist2(P, P)
    idx = np.argsort(d2, axis=1)[:, :k]
    Q = P[idx] - P[idx].mean(1, keepdims=True)          # (n, k, 3), each neighbourhood centred
    # smallest singular direction of the neighbourhood = the plane's normal
    N = np.linalg.svd(Q, full_matrices=False)[2][:, 2, :]
    # A plane fit gives a normal up to sign, and the sign has to come from somewhere. "Away from the
    # cloud's centroid" is the obvious choice and it is wrong here for the same reason the radial
    # proxy was: this cloud is a height field over the front of a face, not a closed surface around a
    # middle, so for a landmark on the brow or the chin the centroid direction is nearly along the
    # surface and the dot product that picks the sign is decided by rounding. Measured, that flipped
    # 79% of them backwards. Orient against the face's own overall plane instead — every landmark on
    # a face mesh is within ninety degrees of facing forward, which is what makes that well posed —
    # taking the nose tip as the one landmark guaranteed to be on the front of it.
    g = np.linalg.svd(P - P.mean(0), full_matrices=False)[2][2]
    g *= np.sign(float((P[1] - P.mean(0)) @ g)) or 1.0
    s = np.sign(N @ g)
    N *= np.where(s == 0, 1.0, s)[:, None]
    return N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)


def fuse_shape(views, anchor=None):
    """One 468x3 landmark cloud from several photos of the same head.

    Each view is rigidly aligned to the anchor (the most frontal photo) and then averaged with a
    per-landmark weight = how squarely THAT view faced THAT landmark. A landmark on the left cheek
    is trusted from the left-turned photos and nearly ignored in the right-turned ones, which is
    exactly the information a single headshot cannot supply.

    On top of that, a turned view is discounted for being turned AT ALL, because mediapipe's 3-D
    output degrades with yaw in a way its 2-D output does not — see YAW_TRUST_POW.
    """
    if not views:
        raise ValueError("no usable reference photos")
    if anchor is None:
        anchor = int(np.argmin([abs(v["yaw"]) for v in views]))
    ref = views[anchor]["lm3"][:N_SKIN].astype(np.float64)
    ref = (ref - ref.mean(0)) / np.linalg.norm(np.ptp(ref, axis=0))
    # One normal field, from the anchor, reused for every view. The views are all aligned onto the
    # anchor anyway, so their surfaces agree to within the thing being measured; recomputing per view
    # would only let each view's own reconstruction error rate its own reliability.
    nrm = cloud_normals(ref)

    acc = np.zeros((N_SKIN, 3))
    wacc = np.zeros((N_SKIN, 1))
    anchor_A = None
    for v in views:
        P = v["lm3"][:N_SKIN].astype(np.float64)
        A = _apply(P, _similarity(P, ref))
        if v is views[anchor]:
            anchor_A = A
        # ⭐ The direction FROM the face TOWARD this photograph's camera, to dot against an outward
        # surface normal. The sign here was inverted, and had been since this was written: the face's
        # outward direction in mediapipe's frame is -Z, not +Z — measured on the anchor cloud, the
        # nose tip sits 30 units toward -Z of the centroid while both tragus landmarks sit +55 — so
        # dotting an outward normal against +(sin, 0, cos) gave the FRONT of the face the 0.08 floor
        # and handed the high weights to whichever view was looking at the back of the head. That is
        # a large part of why turned views were overruling the portrait on the front of the face.
        # Both axes of the corrected vector were then checked against something no convention can
        # argue with — which tragus is stretched away from the nose in the 2-D image, i.e. which side
        # the camera can actually see — and it agrees on 12 turned views out of 12 across both sets.
        a = np.radians(v["yaw"])
        vdir = -np.array([np.sin(a), 0.0, np.cos(a)])
        w = np.clip((nrm * vdir).sum(1), 0.0, 1.0)[:, None] ** 1.5 + 0.08
        w *= min(v["face_px"] / 200.0, 1.5)              # a bigger face is a better measurement
        # …and the yaw discount, applied to the whole view. It is safe to apply globally because the
        # squareness term above already separates the two cases: on the far cheek the anchor sits at
        # its 0.08 floor while a 47-degree view is near 0.85, so even after the discount the turned
        # view still carries several times the anchor's say and remains the only real evidence there;
        # across the front of the face both views are near 1.0 and the discount is the whole story.
        w = w * max(np.cos(a), 1e-3) ** YAW_TRUST_POW
        acc += A * w
        wacc += w
    fused = acc / np.maximum(wacc, 1e-9)

    # …and the anchor's authority over the two axes it actually measures. See FRONT_AUTHORITY.
    if FRONT_AUTHORITY > 0 and anchor_A is not None and len(views) > 1:
        av = np.radians(views[anchor]["yaw"])
        fwd = -np.array([np.sin(av), 0.0, np.cos(av)])
        right = np.array([np.cos(av), 0.0, -np.sin(av)])
        up = np.array([0.0, 1.0, 0.0])
        # visible, not square-on: 1 wherever the surface turns toward this camera at all, falling to
        # 0 only round the far side where the anchor genuinely cannot see the landmark.
        vis = np.clip(((nrm * fwd).sum(1) + 0.3) / 0.6, 0.0, 1.0)
        auth = (float(np.clip(FRONT_AUTHORITY, 0.0, 1.0)) * vis)[:, None]
        d = anchor_A - fused
        fused = fused + auth * (np.outer(d @ right, right) + np.outer(d @ up, up))
    return fused


# ───────────────────────────── mesh side ─────────────────────────────
def face_part(M):
    """The face island — the one part whose UVs are the per-player layout (mat 0)."""
    return next(p for p in M["parts"] if p["mat"] == 0)


def mesh_landmarks(M, uv_lm):
    """3D points on the game mesh at the 468 landmark UVs, by barycentric lookup on the face island.

    uv_lm is in [0,1] UV, i.e. face_builder.landmarks() pixels divided by the map size.
    """
    p = face_part(M)
    lo, hi = p["first_vtx"], p["first_vtx"] + p["n_vtx"]
    UV, POS = M["uv"][lo:hi].astype(np.float64), M["pos"][lo:hi].astype(np.float64)
    T = p["tris_idx"].reshape(-1, 3).astype(np.int64) - lo
    A, B, Cc = UV[T[:, 0]], UV[T[:, 1]], UV[T[:, 2]]
    v0, v1 = B - A, Cc - A
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    den = np.where(np.abs(den) < 1e-12, 1e-12, den)

    out = np.zeros((len(uv_lm), 3))
    for i, q in enumerate(np.asarray(uv_lm, np.float64)):
        v2 = q - A
        b1 = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / den
        b2 = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / den
        b0 = 1.0 - b1 - b2
        ok = (b0 >= -1e-6) & (b1 >= -1e-6) & (b2 >= -1e-6)
        if ok.any():
            k = np.nonzero(ok)[0][0]
            out[i] = b0[k] * POS[T[k, 0]] + b1[k] * POS[T[k, 1]] + b2[k] * POS[T[k, 2]]
        else:                                            # UV gutter — fall back on the nearest vertex
            out[i] = POS[np.argmin(((UV - q) ** 2).sum(1))]
    return out


def _sample_uv(field, uv):
    """Bilinear sample a UV-space field (H, W, C) at uv in 0..1 -> (n, C)."""
    F = np.asarray(field, np.float64)
    H, W = F.shape[:2]
    x = np.clip(np.asarray(uv, np.float64)[:, 0], 0, 1) * (W - 1)
    y = np.clip(np.asarray(uv, np.float64)[:, 1], 0, 1) * (H - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, W - 1), np.minimum(y0 + 1, H - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    return ((F[y0, x0] * (1 - fx) + F[y0, x1] * fx) * (1 - fy) +
            (F[y1, x0] * (1 - fx) + F[y1, x1] * fx) * fy)


def _basis_path(size, n):
    import os
    return (Path(os.path.expandvars("%APPDATA%")) / "NHL2K10 Mod Launcher" /
            f"shape_basis_{size}_{n}.npz")


def shape_basis(game_dir=None, size=96, n_comp=80, log=None, force=False):
    """Principal directions of head shape, learned from every shipped head. -> (V, sd, core)

    V is (n_comp, size, size, 3): one UV-space displacement field per component, zero outside the
    support every head shares. sd is each component's standard deviation across the library, in cm,
    which is what lets the ridge term speak in "library sigmas" rather than arbitrary numbers.

    WHY UV SPACE, and not the vertices. The obvious version — stack the vertex clouds and take their
    PCA — is wrong here, and measurably so. 288 of the 447 heads share a vertex count of 3,496, which
    looks like shared topology, but their UVs disagree by a median of 0.21 in a 0..1 space and their
    triangle lists differ outright: the count is a shared budget, not a shared mesh. Vertex 812 is a
    different point on every head, so that PCA describes indexing noise, and the numbers say so —
    deviation from the mean 4.70 cm, first component 9% of variance, 40 components only 62%.

    What the heads DO share is the UNWRAP, the same fact the haircut library runs on. Rasterise each
    head's positions into a fixed UV grid and texel (u,v) is the same anatomical point on every head
    by construction — that is what a shared unwrap means. The same measurement then reads: deviation
    from the mean 0.67 cm, first component 31%, ten components 76%, forty 93%. A face-shape spectrum
    instead of noise. It also keeps all 447 heads instead of 288, because a head no longer has to
    match a vertex count to take part.

    The basis is centred on the LIBRARY mean but always applied as a displacement from the base head,
    so c = 0 means "exactly the artist's head" and a fit that finds nothing degrades to the identity
    rather than to some average face.
    """
    path = _basis_path(size, n_comp)
    if not force and path.exists():
        try:
            z = np.load(path)
            if int(z["nid"]) == len(list(C.head_ids(game_dir))):
                return z["V"], z["sd"], z["core"]
        except Exception:
            pass
    log = log or (lambda *_a, **_k: None)
    ids = sorted(C.head_ids(game_dir))
    pos, msk = [], []
    for hid in ids:
        try:
            p, _n, m, _M = FB.uv_geometry(hid, game_dir, size=size)
        except Exception:
            continue
        pos.append(np.asarray(p, np.float32))
        msk.append(np.asarray(m, np.float32))
    P = np.stack(pos)
    core = (np.stack(msk) > 0.5).all(0)
    X = P[:, core, :].astype(np.float64)
    F = (X - X.mean(0)).reshape(len(X), -1)
    _U, s, Vt = np.linalg.svd(F, full_matrices=False)
    k = int(min(n_comp, len(s)))
    sd = (s[:k] / np.sqrt(max(len(F) - 1, 1))).astype(np.float32)
    V = np.zeros((k, size, size, 3), np.float32)
    V[:, core, :] = Vt[:k].reshape(k, -1, 3)
    log(f"  shape basis: {len(P)} heads, {k} components, "
        f"{(s[:k] ** 2).sum() / (s ** 2).sum() * 100:.0f}% of library variance")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, V=V, sd=sd, core=core, nid=len(ids))
    except Exception:
        pass
    return V, sd, core


def fit_basis(Glm, sd, resid, ridge=1.0):
    """Least-squares shape coefficients: move along plausible directions to hit the marks.

    Solves  min_c || sum_i c_i Glm[i] - resid ||^2 + ridge * sum (c_i / sd_i)^2  in one step. One
    step rather than an optimisation loop because the landmark map is linear in the positions: a
    landmark is a fixed bilinear tap on the UV field, so a field linear in c moves landmarks linearly
    in c too.

    The ridge is scaled per component by how much heads actually vary along it, so `ridge` is in
    library sigmas: ridge 1 means a direction has to earn a one-sigma move by explaining a matching
    amount of landmark error. That is what keeps the answer on the manifold of heads a human
    sculpted, which is the entire reason to fit a basis instead of a free displacement field.
    """
    G = np.asarray(Glm, np.float64).reshape(len(Glm), -1)
    lhs = G @ G.T + np.diag(float(ridge) / np.maximum(np.asarray(sd, np.float64), 1e-9) ** 2)
    return np.linalg.solve(lhs, G @ np.asarray(resid, np.float64).reshape(-1))


# The nose, in mediapipe's numbering: the midline from the bridge down over the tip, plus both alae
# and the creases that wrap them. Used to protect the artist's nose from the fit — see nose_weight().
NOSE_LM = [168, 6, 197, 195, 5, 4, 1, 19, 94, 2,
           98, 97, 326, 327, 64, 48, 278, 294, 115, 344, 220, 440, 45, 275]

# The mouth, outer vermilion ring then inner. Used to keep the nose's protection OFF it — see
# nose_weight(). Whatever else a photograph is bad at, it measures a mouth.
MOUTH_LM = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40,
            185, 78, 95, 88, 178, 87, 14, 317, 402, 318, 324, 308, 415, 310, 311, 312, 13, 82, 81,
            80, 191]


def mirror_map(src, axis=0, plane=0.0, tol=0.8):
    """Pair every landmark with its own mirror twin. -> (twin, ok)

    Derived from the BASE HEAD rather than hard-coded from a mediapipe table, because the pairing
    that matters is the one on this mesh: two landmarks are twins here if the head's own geometry
    puts them at mirrored places. Only mutual nearest neighbours inside `tol` are accepted, so a
    landmark whose partner is ambiguous is simply left alone instead of being paired with a stranger.

    A midline landmark comes back as its own twin, which is exactly right: mirroring its displacement
    and averaging cancels the sideways component and pins it to the plane, with no special case.
    """
    S = np.asarray(src, np.float64)
    Q = S.copy()
    Q[:, axis] = 2.0 * float(plane) - Q[:, axis]
    d2 = ((Q[:, None, :] - S[None, :, :]) ** 2).sum(-1)
    twin = d2.argmin(1)
    d = np.sqrt(d2[np.arange(len(twin)), twin])
    return twin, (d < float(tol)) & (twin[twin] == np.arange(len(twin)))


def nose_weight(src, falloff=1.2, mouth_clear=1.0):
    """1 on the nose, easing to 0 `falloff` cm off it — and never over the mouth. -> (n,)

    ⭐ MEASURED FAULT, fixed here. This used to be a SPHERE: distance from the nose landmarks'
    centroid, divided by the distance to the furthest of them, times 1.6. NOSE_LM runs from the
    nasion down to the alar creases, so on head 138 that radius came out 4.19 cm and the sphere
    6.70 cm — most of a face. The weight was 0.68 on the upper lip's vermilion, 0.43 on the inner
    lip, 0.21 at the mouth corners and 0.18 at the inner eye corner. Everything downstream that
    asks "how much of this is nose?" was therefore being told that a mouth mostly is: temper_nose
    threw away 65% of the MEASURED detail on the upper lip, and the symmetry step erased the
    player's real left/right difference there as a photo artefact. Measured on the finished heads,
    the fit's own TARGET was already 16% short on Boeser's philtrum and 13% long on Makar's, and
    15%/26% out on their lips, before any interpolation ran.

    Distance to the NEAREST nose landmark, in cm, which follows the nose's actual shape instead of
    bounding it in a ball; damped again by a matching falloff around the mouth. Both are needed —
    the nostril rim is barely a centimetre from the lip, so the nose's own falloff alone still
    reaches it.

    Smooth on purpose. The nose has to be treated differently from the cheek beside it, but the RBF
    that carries the landmarks is smooth, so a hard set boundary would put a crease down the face
    where the treatment changed. A radial falloff changes the treatment gradually instead.
    """
    S = np.asarray(src, np.float64)

    def near(ids):
        q = [i for i in ids if i < len(S)]
        return np.sqrt(_cdist2(S, S[q]).min(1)) if q else np.full(len(S), np.inf)

    def bump(d, r):
        return np.clip(1.0 - (d / max(float(r), 1e-6)) ** 2, 0.0, 1.0) ** 2

    w = bump(near(NOSE_LM), falloff)
    if mouth_clear > 0:
        w = w * (1.0 - bump(near(MOUTH_LM), mouth_clear))
    return w


def temper_nose(D, src, w, keep=0.35):
    """Keep the nose's overall move, damp its per-landmark DETAIL back toward the artist's sculpt.

    The nose is the highest-relief and most foreshortened thing on a face, so it is where mediapipe's
    already-weak depth is weakest — and measurably where the fit does most damage: 0.97 cm at the tip,
    the largest move of any region, landing on the alar creases and the tip definition that the 2009
    artist built by hand and that the landmarks cannot resolve at all.

    So split the motion. The nose's OVERALL move is a real, well-conditioned measurement — a longer
    or narrower nose is visible in any photograph and every landmark agrees on it — and is kept in
    full. The residual about it is the detail, it is the part that is noise, and it is damped to
    `keep`. The artist's nose structure rides along on top of the size change instead of being
    flattened by it.

    ⭐ "Overall" has to mean an AFFINE, not a mean. The mean is a translation, so under it a nose
    that is simply LONGER counted entirely as detail and was damped to `keep` along with the alar
    creases — the exact opposite of the paragraph above. It hid while nose_weight was a 6.7 cm
    sphere, because the mean was then taken over half a face and absorbed most of the size change;
    tightening the weight onto the actual nose exposed it, and Makar's nose came out at 84% of the
    photograph's length instead of 90%. Twelve degrees of freedom over ~24 weighted landmarks is
    still far stiffer than free per-landmark motion: translation, rotation, and stretch along each
    axis are kept, and only what none of those can explain is damped.
    """
    S = np.asarray(src, np.float64)
    W = np.asarray(w, np.float64)[:, None]
    if float(W.sum()) <= 1e-9:
        return D
    X = np.column_stack([S, np.ones(len(S))])
    A = np.linalg.solve((X * W).T @ X + 1e-6 * float(W.sum()) * np.eye(4), (X * W).T @ (S + D))
    gross = X @ A - S                        # what a single affine of the nose region explains
    return D + ((gross + (D - gross) * float(keep)) - D) * W


def ear_weights(P, feather=1.6):
    """Per-vertex 0..1 weight that PINS the ears — 0 on the ear, ramping to 1 `feather` cm out.

    Why this exists: the fit was sliding the ears up to 2.4 cm, and the ear is the one part of the
    head whose texture is a hard-edged drawing of itself. Move the geometry and the painted ear no
    longer covers the modelled one, so the game renders the modelled ear's shading in one place and
    the painted ear beside it — the "second ear behind the ear" that survived three rounds of
    texture touch-ups, because it was never a texture fault. mediapipe has no ear landmarks at all
    (see the LIMITS note at the top), so every centimetre the ears moved was extrapolation bleed
    from the jaw — pure loss, nothing gained by allowing it.

    The ears are found geometrically, so this works on any of the 447 heads (they share one topology
    and one UV layout): take the widest vertex on each side WITHIN the ear's height band, and grow a
    ball. Bands and radii are fractions of the asset's own height, not centimetres, because the head
    asset runs from the crown down to the collar and the widest point overall is the collar flare,
    not the ear.
    """
    P = np.asarray(P, np.float64)
    y0, y1 = P[:, 1].min(), P[:, 1].max()
    h = y1 - y0
    band = (P[:, 1] > y0 + 0.41 * h) & (P[:, 1] < y0 + 0.81 * h)
    r = 0.079 * h
    w = np.ones(len(P))
    if not band.any():
        return w
    cand = np.nonzero(band)[0]
    for pick in (np.argmax, np.argmin):
        seed = P[cand[pick(P[cand, 0])]]
        d = np.linalg.norm(P - seed, axis=1)
        t = np.clip((d - r) / max(feather, 1e-6), 0.0, 1.0)
        w = np.minimum(w, t * t * (3 - 2 * t))                      # smoothstep, 0 on the ear
    return w


def _thin(P, min_d):
    """Farthest-point-ish thinning: indices of points no closer than min_d to one already kept.

    mediapipe packs hundreds of landmarks into the eyes and lips and puts a bare handful along the
    jaw. Any interpolator fed all 468 is dominated by the crowd — and its matrix is singular, since
    near-coincident points are near-identical equations. Thinning gives every REGION one vote.
    """
    keep = []
    for i in range(len(P)):
        if not keep or ((P[keep] - P[i]) ** 2).sum(1).min() >= min_d ** 2:
            keep.append(i)
    return np.array(keep)


def _wendland(r, R):
    """C2 Wendland kernel: smooth, positive-definite, and exactly ZERO past R."""
    q = np.clip(r / R, 0.0, 1.0)
    return (1.0 - q) ** 4 * (4.0 * q + 1.0)


def _cdist2(A, B):
    return ((np.asarray(A)[:, None, :] - np.asarray(B)[None, :, :]) ** 2).sum(2)


def displace(P, src_lm, dst_lm, sigma=2.2, reach=6.0, node_d=1.0, ridge=1e-3, protect=None,
             levels=3, tol=0.03):
    """Smoothly carry a per-landmark displacement to every vertex — landing ON the measurement.

    This used to average the landmarks with normalised Gaussian (Shepard) weights. Shepard is a
    BLUR, not an interpolator, so the surface never arrived where the fit aimed it: the chin sits at
    the edge of the cloud with the whole lower face for neighbours, and a 2.2 cm kernel averaged its
    shortening away. Measured on the Makar head, the fit asked for 11% off the lower face and the
    mesh delivered 2% — which is exactly why a fitted head still read as the base model with a new
    face painted on. Deconvolving Shepard does not rescue it either; the operator's condition number
    is 5e15, and the node values explode long before the residual comes down.

    So: a real RBF interpolant, on a THINNED node set, with a compactly-supported kernel.
      · thinning (`node_d` cm) makes the system well-conditioned AND stops the eyes and lips, where
        mediapipe crowds hundreds of points, from outvoting the jaw, where it has six;
      · Wendland C2 is exactly zero past its support, so unlike a thin-plate spline it cannot fling
        the back of the skull — the property the old Shepard field was chosen for is kept;
      · `ridge` makes it approximating rather than exact, which absorbs landmark noise.
    `sigma` now names the kernel's support radius in cm (still smooth, no longer lossy), and `reach`
    still fades the field out so the neck, chest and rear skull stay where the artist put them.

    ⭐ MULTISCALE, and it is not an optimisation — one scale cannot express a face. `node_d` was
    doing two incompatible jobs: conditioning the system and choosing what the field is allowed to
    resolve. At 1.0 cm the second job was being done badly, because the features that read as a
    LIKENESS are smaller than that. An eye aperture is 0.7-1.0 cm from lid margin to lid margin, so
    thinning kept ONE of the two margins and the other's motion was never asked for; the philtrum is
    0.5 cm; a lip is 0.3 cm thick. Measured on the finished heads against the photographs, with one
    scale: eye opening landed at 135% and 142% of the photograph on Boeser and 106%/122% on Makar
    while the fit's own target was 101% — the aperture the fit asked to close simply did not close.
    The lips came out 15% and 28% too thick and the philtrum 16% too short, all the same fault.
    Loosening `node_d` is not the answer: nodes 3 mm apart under a 4.4 cm kernel are near-identical
    equations, which is the singularity the thinning exists to avoid.

    So solve the residual again at a third of the spacing under a third of the support, three times
    over. R/spacing is constant across levels, so every level is conditioned exactly as well as the
    original one was, and each is MORE local than the last — the "cannot fling the back of the
    skull" property is strengthened, not weakened. A level only takes the landmarks the coarser
    levels missed by more than `tol` cm, so a face that the 1 cm field already explains costs one
    extra solve over a handful of nodes and lands in the same place as before.
    """
    D = dst_lm - src_lm
    R0 = max(sigma * 2.0, node_d * 3.0)
    stages, resid = [], D.copy()
    for k in range(max(1, int(levels))):
        if k == 0:
            idx = _thin(src_lm, node_d)
        else:
            need = np.flatnonzero(np.linalg.norm(resid, axis=1) > float(tol))
            if not len(need):
                break
            idx = need[_thin(src_lm[need], node_d / 3.0 ** k)]
            if len(idx) > 1500:            # a dense source (the basis carry) is not a landmark set
                break                      # — solving it at 3 mm would be a 1500^3 solve for nothing
        N, R = src_lm[idx], R0 / 3.0 ** k
        K = _wendland(np.sqrt(_cdist2(N, N)), R)
        coef = np.linalg.solve(K + ridge * np.eye(len(K)), resid[idx])
        stages.append((N, coef, R))
        resid = resid - _wendland(np.sqrt(_cdist2(src_lm, N)), R) @ coef

    out = np.zeros_like(P)
    step = 4096
    for i in range(0, len(P), step):
        Q = P[i:i + step]
        near = np.sqrt(_cdist2(Q, src_lm).min(1))[:, None]
        fade = np.clip(1.0 - (near - reach * 0.45) / (reach * 0.55), 0.0, 1.0)
        fade = fade * fade * (3 - 2 * fade)                                  # smoothstep
        if protect is not None:
            fade = fade * np.asarray(protect, np.float64)[i:i + step, None]
        acc = np.zeros_like(Q)
        for N, coef, R in stages:
            acc += _wendland(np.sqrt(_cdist2(Q, N)), R) @ coef
        out[i:i + step] = acc * fade
    return out


def slim_face(P, taper=1.0, hollow=1.0):
    """Take the FLESH off a fitted head. -> new positions.

    The fit lands on an average head, and average is the wrong answer for a lean-faced player. It is
    deliberately not a global X squash, because frontal width is not what is wrong: measured
    photo-against-photo, Pettersson's frontal bands sit within 2.3% of Boeser's and Makar's at every
    height, and our build sits within 4% of the shipped-head median. Two things ARE wrong:

      taper   the chin-ward differential — he runs +2.3% at the cheekbone and -2.4% at the chin
              against those two, so roughly 5% of width wants rotating out of the lower face while
              the cheekbone stays put.
      hollow  cheek and jaw VOLUME in depth. No frontal measurement can see this and the landmark
              depth channel cannot either (MediaPipe's z is a canonical template — measured, two
              different players' signatures came back near-identical), so it is a knob and not an
              inference. It pulls the cheek panel toward the skull axis, which takes mass out
              WITHOUT moving the silhouette edge, and that is what a lean face actually looks like.

    Both default to 1.0 = the calibrated move. 0 disables. Applied after the basis and RBF fit
    because it is a statement about this player's build, not about where his landmarks are.
    """
    P = np.asarray(P, np.float64)
    Q = P.copy()
    x, y, z = Q[:, 0], Q[:, 1], Q[:, 2]
    ylo, yhi = np.percentile(y, 2), np.percentile(y, 98)
    h = max(yhi - ylo, 1e-6)
    cx, zc = float(np.median(x)), float(np.median(z))
    t = np.clip((yhi - y) / h, 0.0, 1.0)                    # 0 at the crown, 1 at the chin
    if taper:
        s = 1.0 - 0.05 * taper * np.clip((t - 0.45) / 0.55, 0, 1) ** 1.3
        Q[:, 0] = cx + (x - cx) * s
    if hollow:
        # the cheek panel: the band between cheekbone and jaw, off the midline, on the face side.
        # The face looks down -Z in this layout — see the fuse_shape sign note.
        band = np.clip((t - 0.40) / 0.18, 0, 1) * np.clip((0.88 - t) / 0.18, 0, 1)
        off = np.clip(np.abs(x - cx) / max(np.percentile(np.abs(x - cx), 95), 1e-6), 0, 1)
        front = np.clip((zc - z) / max(zc - np.percentile(z, 2), 1e-6), 0, 1)
        k = band * np.clip(off * 1.4, 0, 1) * front * 0.045 * hollow
        Q[:, 0] = cx + (Q[:, 0] - cx) * (1.0 - k)
        Q[:, 2] = z + (zc - z) * k * 0.7
    return Q


def fit(head_id, ref_paths, game_dir=None, strength=1.0, sigma=2.2, reach=6.0,
        keep_size=True, basis=1.0, n_comp=40, ridge_basis=1.0, pin_ears_basis=True,
        symmetry=0.6, nose_detail=0.35, taper=0.0, hollow=0.0, log=print):
    """Reshape a head from reference photos. Returns (blob, model, new_positions, info).

    strength   0..1, how far to go from the base head's geometry toward the fitted one
    keep_size  align the fitted cloud to the base head's OVERALL scale rather than adopting the
               photos' — heads in this game share a helmet and a skeleton, so a head that fits its
               own hat is worth more than one that is 4% taller
    basis      0..1, how much of the SHAPE BASIS solution to take (0 = the old RBF-only fit)
    n_comp     principal directions to solve over; more = more freedom, less regularisation
    ridge_basis  stiffness in library sigmas — higher keeps the head closer to what ships
    pin_ears_basis  hold the ears still through the basis too. ON by default. The argument for
               leaving it off was that, unlike RBF bleed, a basis ear-move is INFERRED from real
               sculpted heads rather than extrapolated — true, and beside the point. No photograph
               in a reference set measures an ear: MediaPipe has no ear landmark, the tragus is the
               nearest thing to one, and a frontal portrait barely shows the shell at all. So the
               basis is not inferring this player's ear from evidence, it is inferring it from
               whatever the rest of his face correlates with across the library. Meanwhile the
               PAINTED ear has to keep covering the modelled one, and the ear is the one part of the
               head where a millimetre of drift shows as a doubled helix. Held still, it costs
               nothing measurable and removes a whole class of failure.
    symmetry   0..1, how much of the measured left/right difference to treat as a photo artefact
               rather than as the player. Always applied in FULL to the nose regardless of this
               value; see the block below for why the nose is not a matter of taste.
    nose_detail  0..1, how much of the nose's per-landmark detail to take from the photos. 1.0 is
               the old behaviour; below that the nose's overall move is still taken in full and only
               the unmeasurable fine structure is left to the artist's sculpt.

    The base geometry is read PRISTINE, never from the modded copy, so fitting the same head twice
    lands in the same place instead of compounding — the second fit would otherwise start from the
    first one's output and push the jaw out again. (`base_maps` below already reads clean, so the
    colour side has always behaved this way; this is the geometry catching up.) The blob returned
    for `write_shape` is that pristine one, which is also what makes the write safe: it re-encodes
    the artist's stream rather than a stream that has already been snapped once.
    """
    asset = C.HEAD_FMT.format(int(head_id))
    b = bytearray(C.blob(False, game_dir, asset))
    m = C.scan_models(bytes(b), asset)[0]
    M = C.read_model(bytes(b), m)

    views = read_refs(ref_paths)
    if not views:
        raise ValueError("no reference photo produced a face detection")
    log(f"  {len(views)} reference views: " +
        ", ".join(f"{v['path'].name} (yaw {v['yaw']:+.0f})" for v in views))
    cloud = fuse_shape(views)

    base_map = FB.base_maps(head_id, game_dir)["color"]
    t_lm = FB.landmarks(base_map)[:N_SKIN]
    W, H = base_map.size
    uv = np.column_stack([t_lm[:, 0] / (W - 1), t_lm[:, 1] / (H - 1)])
    src = mesh_landmarks(M, uv)

    srt = _similarity(cloud, src)
    if keep_size:
        srt = (srt[0], srt[1], srt[2])
    dst = _apply(cloud, srt)
    dst = src + (dst - src) * float(strength)

    # ── clean the landmark displacement before anything is built on it ────────────────────────────
    # Two measured faults, both in the same place. Base head 138's nose is EXACTLY symmetric — mirror
    # error 0.000 cm across the alar pairs — and every fit introduced about 0.2 cm of left/right
    # mismatch into it. That is not Boeser, it is the reference set: yaw -56 to +25, left-biased, no
    # true right-side view, so the left ala is measured and the right is half guessed. And the tip
    # moved 0.97 cm, more than any other region, flattening the alar creases into two dark notches.
    D = dst - src
    nw = nose_weight(src)
    if symmetry > 0:
        twin, ok = mirror_map(src)
        mirrored = D[twin].copy()
        mirrored[:, 0] *= -1.0                      # the twin's motion, seen from this side
        sym = np.where(ok[:, None], 0.5 * (D + mirrored), D)
        # Face-wide the amount is deliberately short of 1: real faces ARE asymmetric and that
        # asymmetry is part of a likeness, so erasing it everywhere would cost the thing we are
        # trying to capture. The nose is the exception and gets the full treatment, because there we
        # measured the artist at perfect symmetry and the references unable to see one side of it.
        amt = float(symmetry) + (1.0 - float(symmetry)) * nw
        D = D + (sym - D) * amt[:, None]
    if nose_detail < 1.0:
        D = temper_nose(D, src, nw, keep=float(nose_detail))
    dst = src + D
    # What the nose actually ended up with, so a regression shows up in the log rather than in a
    # render three steps later. `nose_asym` is the residual left/right mismatch across the alar
    # pairs; the artist's own is 0.000, so anything much above zero is the photo set talking.
    _tw, _ok = mirror_map(src)
    _ni = [i for i in NOSE_LM if i < len(src) and _ok[i] and _tw[i] != i]
    nose_asym = float(np.abs(np.linalg.norm(D[_ni], axis=1) -
                             np.linalg.norm(D[_tw[_ni]], axis=1)).mean()) if _ni else 0.0
    nose_moved = float((np.linalg.norm(D, axis=1) * nw).sum() / max(nw.sum(), 1e-9))

    P = M["pos"].astype(np.float64)
    # Pin the ears: mediapipe has no ear landmarks, so any ear motion is extrapolation bleed, and
    # it slides the modelled ear out from under its painted one — the "second ear" artefact.
    ears = ear_weights(P)

    # ── shape basis, then RBF on whatever it could not reach ──────────────────────────────────
    # The RBF alone can only move what mediapipe can SEE, and mediapipe has no ears and stops at the
    # hairline — so cranium depth, jaw hinge and ear shape were inherited from the base head no
    # matter how good the photographs were. That is why a fitted head reads well from the front and
    # least like the player in profile and from behind. The basis is the way in: the 447 shipped
    # heads were sculpted by hand with those regions correlated to the face, so solving for a
    # combination of them INFERS the unseen parts from the seen ones. A free displacement field
    # cannot do that; it has no idea that a wide jaw comes with a wide skull.
    #
    # The RBF still runs afterwards, on the residual. The basis supplies plausibility and reach; the
    # residual supplies the last millimetres of this particular face, which no combination of other
    # people's heads will ever land exactly.
    basis_D = np.zeros_like(P)
    n_used, coef = 0, None
    if basis > 0:
        V, sd, _core = shape_basis(game_dir, n_comp=n_comp, log=log)
        # A landmark is a fixed bilinear tap on the UV grid, so the basis's effect at the marks is
        # just the basis sampled there — no operator matrix needed once the basis lives in UV space.
        Glm = np.stack([_sample_uv(V[i], uv) for i in range(len(V))])
        coef = fit_basis(Glm, sd, dst - src, ridge=ridge_basis)
        field = np.tensordot(coef, V, axes=(0, 0)) * float(basis)      # (size, size, 3) in UV
        # The basis is defined on the FACE ISLAND — uv_geometry rasterises mat 0 and nothing else, so
        # its UVs are the only ones this field is meaningful in. Sample it onto the island's own
        # vertices, then let the same RBF that carries the landmark fit carry it to everything else:
        # eyeballs, lashes, the mouth bag and the hair shells have to ride the skull they sit on or
        # they punch through it, and they have no UVs in this layout to be sampled with.
        fp = face_part(M)
        flo, fhi = fp["first_vtx"], fp["first_vtx"] + fp["n_vtx"]
        fdelta = _sample_uv(field, M["uv"][flo:fhi].astype(np.float64))
        basis_D = displace(P, P[flo:fhi], P[flo:fhi] + fdelta,
                           sigma=sigma, reach=reach, levels=1,
                           protect=ears if pin_ears_basis else None)
        # levels=1: this carry's "landmarks" are thousands of face-island VERTICES sampling a field
        # that is already smooth by construction, so there is no sub-millimetre structure to chase —
        # only a dense solve to pay for. The multiscale levels are for the 468 landmarks below.
        n_used = int((np.abs(coef / np.maximum(sd, 1e-9)) > 0.05).sum())
        src = src + _sample_uv(field, uv)             # what the basis actually achieved at the marks
        log(f"  basis: {len(V)} components, {n_used} active, "
            f"|c|/sigma max {float(np.abs(coef / np.maximum(sd, 1e-9)).max()):.2f}, "
            f"moves vertices up to {np.linalg.norm(basis_D, axis=1).max():.2f} cm")

    D = displace(P + basis_D, src, dst, sigma=sigma, reach=reach, protect=ears)
    newP = P + basis_D + D
    if taper or hollow:
        _pre = newP
        newP = slim_face(newP, taper=taper, hollow=hollow)
        log(f"  slim: taper {taper:.2f}, hollow {hollow:.2f}, "
            f"takes off up to {np.linalg.norm(newP - _pre, axis=1).max():.2f} cm")
    D = newP - P                                       # report TOTAL motion, basis included
    info = dict(views=len(views), moved=float(np.abs(D).max()), basis_comp=n_used,
                basis_moved=float(np.linalg.norm(basis_D, axis=1).max()),
                ear_moved=float(np.linalg.norm(D[ears < 0.5], axis=1).max()) if (ears < 0.5).any() else 0.0,
                mean=float(np.linalg.norm(D, axis=1).mean()),
                nose_asym=nose_asym, nose_moved=nose_moved,
                rms_landmark=float(np.linalg.norm(dst - src, axis=1).mean()))
    log(f"  fit: {info['views']} views, landmarks move {info['rms_landmark']:.2f} cm on average, "
        f"vertices up to {info['moved']:.2f} cm")
    log(f"  nose: moves {nose_moved:.2f} cm, residual left/right mismatch {nose_asym:.3f} cm "
        f"(artist's own is 0.000)")
    return b, m, M, newP, info


def write_shape(b, m, M, newP, head_id, game_dir=None, log=print, parts_override=None):
    """Write the moved vertices back into the head asset — in place, topology and rig untouched.

    The blob has to fit the slot it came from, and moved vertices compress worse than the artist's:
    the packed snorm16 stream loses the repeats the encoder was living on. So the positions are
    SNAPPED to a coarser sub-lattice of the same packing grid until it fits — a shared low bit
    pattern the encoder can match again. Each step costs 0.02 mm of precision on a 70 cm packing
    range, which is nothing next to the millimetre the artist's own quantisation already spends.

    `parts_override` is {submesh record: mesh} for parts that are NOT just the fitted head — a
    removed or transplanted hair shell (see facial_hair.plan). It has to be applied here rather
    than in a second pass because this function rebuilds the WHOLE mesh from the pristine blob
    every time; anything written separately would be clobbered on the next fit.
    """
    asset = C.HEAD_FMT.format(int(head_id))
    (ox, oy, oz), sc = C.pos_xform(bytes(b), m["pos_off"])
    orig = np.asarray(newP, np.float64)
    q = np.round((orig - np.array([ox, oy, oz])) / sc * 32767.0)

    err = None
    for snap in (1, 2, 4, 8, 16, 32, 64):
        P = (np.round(q / snap) * snap) / 32767.0 * sc + np.array([ox, oy, oz])
        buf = bytearray(b)
        for p in M["parts"]:
            lo, hi = p["first_vtx"], p["first_vtx"] + p["n_vtx"]
            T = p["tris_idx"].reshape(-1, 3).astype(np.int64) - lo
            if not len(T):
                continue
            over = (parts_override or {}).get(p["rec"])
            if over is not None:
                # Snap the override to the same sub-lattice, for the same compression reason.
                Q = np.asarray(over["pos"], np.float64)
                Q = (np.round(np.round((Q - np.array([ox, oy, oz])) / sc * 32767.0) / snap)
                     * snap) / 32767.0 * sc + np.array([ox, oy, oz])
                mesh = dict(over, pos=Q)
            else:
                mesh = dict(pos=P[lo:hi], uv=M["uv"][lo:hi], nrm=None, tris=T, ordered=True)
            C.replace_part(buf, m, p, mesh, log=lambda *_a, **_k: None)
        try:
            msg = C.write(bytes(buf), game_dir, log=lambda *_a, **_k: None, asset=asset)
        except ValueError as e:
            if "re-compresses" not in str(e):
                raise
            err = e
            continue
        d = np.linalg.norm(P - orig, axis=1).max() * 10.0
        log(f"  {asset}: geometry written ({len(newP)} vertices, snap {snap}, "
            f"{d:.2f} mm quantisation)")
        log(f"  {msg}")
        return msg
    raise ValueError(f"{asset}: shape does not fit its slot even at snap 64 — {err}")


def restore_shape(head_id, game_dir=None, log=print):
    return C.restore(game_dir, log=log,
                     asset=C.HEAD_FMT.format(int(head_id)))
