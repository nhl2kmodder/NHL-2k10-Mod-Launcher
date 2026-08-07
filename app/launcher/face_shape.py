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
    """
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
        out.append(dict(path=p, img=img, lm=px, lm3=lm3,
                        yaw=float(np.degrees(np.arctan2(-R[2, 0], np.hypot(R[2, 1], R[2, 2])))),
                        pitch=float(np.degrees(np.arctan2(R[2, 1], R[2, 2]))),
                        roll=float(np.degrees(np.arctan2(R[1, 0], R[0, 0]))),
                        face_px=float(np.ptp(px[:, 0]))))
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


def fuse_shape(views, anchor=None):
    """One 468x3 landmark cloud from several photos of the same head.

    Each view is rigidly aligned to the anchor (the most frontal photo) and then averaged with a
    per-landmark weight = how squarely THAT view faced THAT landmark. A landmark on the left cheek
    is trusted from the left-turned photos and nearly ignored in the right-turned ones, which is
    exactly the information a single headshot cannot supply.
    """
    if not views:
        raise ValueError("no usable reference photos")
    if anchor is None:
        anchor = int(np.argmin([abs(v["yaw"]) for v in views]))
    ref = views[anchor]["lm3"][:N_SKIN].astype(np.float64)
    ref = (ref - ref.mean(0)) / np.linalg.norm(np.ptp(ref, axis=0))

    acc = np.zeros((N_SKIN, 3))
    wacc = np.zeros((N_SKIN, 1))
    for v in views:
        P = v["lm3"][:N_SKIN].astype(np.float64)
        A = _apply(P, _similarity(P, ref))
        # radial direction of each landmark ~ its outward normal on a head; the camera looks down
        # -Z in mediapipe's frame, so rotate that by the view's yaw to get the view direction.
        rad = A - A.mean(0)
        rad /= np.maximum(np.linalg.norm(rad, axis=1, keepdims=True), 1e-9)
        a = np.radians(v["yaw"])
        vdir = np.array([np.sin(a), 0.0, np.cos(a)])
        w = np.clip((rad * vdir).sum(1), 0.0, 1.0)[:, None] ** 1.5 + 0.08
        w *= min(v["face_px"] / 200.0, 1.5)              # a bigger face is a better measurement
        acc += A * w
        wacc += w
    return acc / np.maximum(wacc, 1e-9)


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


def displace(P, src_lm, dst_lm, sigma=2.2, reach=6.0, node_d=1.0, ridge=1e-3, protect=None):
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
    """
    D = dst_lm - src_lm
    idx = _thin(src_lm, node_d)
    N, Dn = src_lm[idx], D[idx]
    R = max(sigma * 2.0, node_d * 3.0)
    K = _wendland(np.sqrt(((N[:, None, :] - N[None, :, :]) ** 2).sum(2)), R)
    coef = np.linalg.solve(K + ridge * np.eye(len(K)), Dn)

    out = np.zeros_like(P)
    step = 4096
    for i in range(0, len(P), step):
        Q = P[i:i + step]
        d2 = ((Q[:, None, :] - N[None, :, :]) ** 2).sum(2)
        near = np.sqrt(((Q[:, None, :] - src_lm[None, :, :]) ** 2).sum(2).min(1))[:, None]
        fade = np.clip(1.0 - (near - reach * 0.45) / (reach * 0.55), 0.0, 1.0)
        fade = fade * fade * (3 - 2 * fade)                                  # smoothstep
        if protect is not None:
            fade = fade * np.asarray(protect, np.float64)[i:i + step, None]
        out[i:i + step] = (_wendland(np.sqrt(d2), R) @ coef) * fade
    return out


def fit(head_id, ref_paths, game_dir=None, strength=1.0, sigma=2.2, reach=6.0,
        keep_size=True, log=print):
    """Reshape a head from reference photos. Returns (blob, model, new_positions, info).

    strength   0..1, how far to go from the base head's geometry toward the fitted one
    keep_size  align the fitted cloud to the base head's OVERALL scale rather than adopting the
               photos' — heads in this game share a helmet and a skeleton, so a head that fits its
               own hat is worth more than one that is 4% taller

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

    P = M["pos"].astype(np.float64)
    # Pin the ears: mediapipe has no ear landmarks, so any ear motion is extrapolation bleed, and
    # it slides the modelled ear out from under its painted one — the "second ear" artefact.
    ears = ear_weights(P)
    D = displace(P, src, dst, sigma=sigma, reach=reach, protect=ears)
    newP = P + D
    info = dict(views=len(views), moved=float(np.abs(D).max()),
                ear_moved=float(np.linalg.norm(D[ears < 0.5], axis=1).max()) if (ears < 0.5).any() else 0.0,
                mean=float(np.linalg.norm(D, axis=1).mean()),
                rms_landmark=float(np.linalg.norm(dst - src, axis=1).mean()))
    log(f"  fit: {info['views']} views, landmarks move {info['rms_landmark']:.2f} cm on average, "
        f"vertices up to {info['moved']:.2f} cm")
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
