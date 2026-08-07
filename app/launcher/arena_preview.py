"""NHL 2K10 — software preview renderer for the Arena tab.

The launcher is tkinter, so there is no GPU to draw with: this is a small numpy rasterizer.

It is split into two halves on purpose, because they change at very different rates:

  raster(scene, camera)  -> a G-buffer: which triangle owns each pixel, and its barycentrics.
  shade(scene, gbuf, …)  -> the actual image: smooth (Gouraud) baked lighting, per-pixel UVs,
                            bilinear texture sampling, exposure and tint.

Only the camera, the viewport size and the cutaway invalidate the G-buffer. Everything the
lighting sliders touch is in shade(), which is a handful of gathers over W*H pixels — tens of
milliseconds instead of a full re-rasterization. That is what makes the lighting sliders live.

Rasterization speed comes from bucketing: triangles are grouped by screen bounding-box size and
every triangle in a bucket is rasterized at once against one shared (w+1)x(h+1) offset grid, so
the work is a handful of big vectorized passes instead of 200,000 tiny ones. Depth is a real
z-buffer (minimum-scatter, then a second pass records the winner per pixel), so the image is
order-independent — no painter's-algorithm sorting artifacts.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

BG = (14, 15, 18)
NO_TEX = 0.72                 # grey for a material whose texture binding isn't known

# The game's own lens, not a guess. Solving `clip = M @ [pos,1]` by least squares from the 968
# UV-matched arm vertices of RenderDoc draw 1137 comes out with ZERO residual (max NDC error
# 0.00000) and lands on a pure projection matrix -- no rotation, no translation -- which is also
# the proof that `player_mesh.npz` stores the capture's CAMERA-space positions. Its focal terms
# are fx = fy = 2.33344, a 46.4 deg field of view. Because the mesh is already in camera space the
# eye distance does not have to be derived from that: it is just -ctr.z, about 313 units.
GAME_FOCAL = 2.33344


def build_scene(blob, models, tex_lookup=None, max_tris=None, ref=None):
    """Flatten models into one draw list, keeping everything shading needs PER VERTEX.

    tex_lookup(material_id) -> (h,w,3+) uint8 array or None.
    ref: per-model light normalisation scale from a PREVIOUS build (see below). Pass the value
    returned in scene["ref"] when re-loading the same asset after an edit — otherwise every
    model is re-normalised to its own 99th percentile and a global brightness change to the
    file renormalises straight back out of the preview, making Apply look like a no-op.

    -> dict(pos (n,3) world, uv (n,2), vcol (n,3) 0..1, tri (m,3), mat (m,), part (m,),
            tex {mat: array}, pbox {part: (lo,hi)}, ref [...]) or None.
    """
    from . import arena_model as AM

    P, UV, VC, TRI, MAT, PART = [], [], [], [], [], []
    tex, pbox, scales = {}, {}, []
    off = 0
    pid = 0
    for mi, m in enumerate(models):
        d = AM.read_model(blob, m)
        pos, uv, light = d["pos"], d["uv"], d["light"]
        if not d.get("lit", True):
            # alternate vertex declaration — there is no bake to show, so draw it flat rather
            # than reinterpreting the field and painting the roof fluorescent green
            scales.append(1.0)
            VC.append(np.full((len(pos), 3), 0.55, np.float32))
        else:
            # the bake is stored at a different scale per material, so it has to be normalised
            # to be viewable at all; reuse the caller's scale when there is one (see docstring)
            hi = (ref[mi] if ref is not None and mi < len(ref)
                  else max(float(np.percentile(light, 99)), 1e-9))
            scales.append(float(hi))
            VC.append(np.clip(light / hi, 0, 1) ** (1 / 2.2))
        for p in d["parts"]:
            t = p["tris_idx"]
            pid += 1
            if not len(t):
                continue
            if p["mat"] not in tex and tex_lookup is not None:
                a = tex_lookup(p["mat"])
                if a is not None and len(a):
                    tex[p["mat"]] = np.ascontiguousarray(a[:, :, :3], np.uint8)
            v = np.unique(t)
            pbox[pid] = (pos[v].min(0), pos[v].max(0))
            TRI.append(t + off)
            MAT.append(np.full(len(t), p["mat"], np.int32))
            PART.append(np.full(len(t), pid, np.int32))
        P.append(pos)
        UV.append(uv)
        off += len(pos)
    if not TRI:
        return None
    tri = np.concatenate(TRI)
    mat = np.concatenate(MAT)
    part = np.concatenate(PART)
    pos = np.concatenate(P)
    if max_tris and len(tri) > max_tris:                  # drop the smallest triangles first
        e1 = pos[tri[:, 1]] - pos[tri[:, 0]]
        e2 = pos[tri[:, 2]] - pos[tri[:, 0]]
        area = np.linalg.norm(np.cross(e1, e2), axis=1)
        keep = np.argsort(-area)[:max_tris]
        tri, mat, part = tri[keep], mat[keep], part[keep]
    return dict(pos=pos, uv=np.concatenate(UV).astype(np.float32),
                vcol=np.clip(np.concatenate(VC), 0, 1).astype(np.float32),
                tri=tri, mat=mat, part=part, tex=tex, pbox=pbox, ref=scales)


def project(g, pts):
    """World points -> output-image pixels, through the exact camera `g` was rasterized with.

    So an overlay (joints, labels, a measuring line) lands on the same pixels as the geometry
    it annotates, without the caller re-deriving the projection and drifting out of step.
    """
    P = np.asarray(pts, np.float32).reshape(-1, 3) - np.asarray(g["ctr"], np.float32)
    cy, sy = np.cos(g["yaw"]), np.sin(g["yaw"])
    cp, sp = np.cos(g["pitch"]), np.sin(g["pitch"])
    x = P[:, 0] * cy + P[:, 2] * sy
    zt = -P[:, 0] * sy + P[:, 2] * cy
    y = P[:, 1] * cp - zt * sp
    s = min(g["W"], g["H"]) / g["span"]
    return np.stack([x * s + g["W"] / 2 + g["pan"][0] * g["W"],
                     g["H"] / 2 - y * s + g["pan"][1] * g["H"]], 1) / g["ss"]


# ───────────────────────────────── geometry pass ─────────────────────────────────
def raster(scene, W=760, H=520, yaw=0.6, pitch=0.35, zoom=1.0, pan=(0.0, 0.0),
           cutaway=0.0, isolate=None, ss=1, ctr=None, span=None, pixel_depth=False,
           cam_dist=None):
    """Rasterize the camera into a G-buffer. Independent of all lighting/exposure settings.

    cutaway (0..1) drops the nearest slice of geometry — the projection is orthographic, so
    without it the roof and the near stands hide the bowl interior; ~0.35 puts you inside.
    isolate: draw only this part id — or this collection of them, which is how a whole model
        is isolated (nothing else can then occlude it).
    ss: supersampling factor; shade() box-filters it back down, which is the anti-aliasing.
    pixel_depth: interpolate depth per pixel instead of using each triangle's mean. The arena is
        mostly large, well-separated slabs and does not need it, but a character mesh folds back
        on itself constantly — a flat per-triangle depth there lets far-side triangles beat
        near-side ones and shreds the surface.
    ctr/span: pin the framing instead of re-fitting it to this frame's bounding box. An
        animated mesh changes shape every frame, and a camera that refits would make the figure
        swim; pass the bind pose's centre and extent once and it stays put.

    cam_dist: distance from the eye to `ctr`, which turns the projection perspective; None keeps
        the orthographic default. `span` still sets the framing, so zooming narrows the field of
        view rather than dollying the camera -- the near/far divergence stays fixed at whatever
        the real distance is, which is what makes the preview comparable to a game screenshot.
        The arena stays orthographic: `cutaway` slices by depth and wants parallel rays.

    -> dict(W,H,ss, tid (H*W int32, -1 = background), l1,l2 (H*W float32)) plus the ctr/span
       actually used, so a caller can project its own overlay through the same camera.
    """
    RW, RH = int(W) * ss, int(H) * ss
    g = dict(W=RW, H=RH, ss=ss, out=(int(W), int(H)),
             yaw=yaw, pitch=pitch, pan=pan, ctr=np.zeros(3, np.float32), span=1.0,
             tid=np.full(RH * RW, -1, np.int32),
             l1=np.zeros(RH * RW, np.float32), l2=np.zeros(RH * RW, np.float32))
    if not scene or not len(scene["tri"]):
        return g
    pos, tri = scene["pos"], scene["tri"]

    ctr = (pos.min(0) + pos.max(0)) / 2.0 if ctr is None else np.asarray(ctr, np.float32)
    Pw = pos - ctr
    cy, sy = np.cos(yaw), np.sin(yaw)
    cp, sp = np.cos(pitch), np.sin(pitch)
    x = Pw[:, 0] * cy + Pw[:, 2] * sy
    zt = -Pw[:, 0] * sy + Pw[:, 2] * cy
    y = Pw[:, 1] * cp - zt * sp
    dep_v = Pw[:, 1] * sp + zt * cp                       # view depth (smaller = nearer)
    span = (max(float(np.abs(np.stack([x, y])).max()), 1e-6) * 2.05 if span is None
            else float(span)) / max(zoom, 1e-3)
    sc = min(RW, RH) / span
    # Perspective divide, when asked for. dep_v is measured from the model centre and gets
    # smaller towards the viewer, so the camera sits at D in front of it and each vertex is
    # scaled by D/(D+dep_v). Clamped well short of zero so geometry level with or behind the
    # camera can't produce an infinite screen coordinate.
    if cam_dist:
        D = float(cam_dist)
        k = (D / np.maximum(D + dep_v, D * 1e-2)).astype(np.float32)
    else:
        k = np.float32(1.0)
    sx = (x * sc * k + RW / 2 + pan[0] * RW).astype(np.float32)
    sy2 = (RH / 2 - y * sc * k + pan[1] * RH).astype(np.float32)
    g["ctr"], g["span"] = ctr, span

    ax, ay = sx[tri], sy2[tri]
    dep3 = dep_v[tri].astype(np.float32)
    dep = dep3.mean(1)
    fx0 = np.floor(ax.min(1))
    fx1 = np.ceil(ax.max(1))
    fy0 = np.floor(ay.min(1))
    fy1 = np.ceil(ay.max(1))
    keep = (fx1 >= 0) & (fx0 < RW) & (fy1 >= 0) & (fy0 < RH) & np.isfinite(dep)
    if isolate is not None:
        # int = one submesh, collection = a whole model
        keep &= (scene["part"] == isolate if isinstance(isolate, (int, np.integer))
                 else np.isin(scene["part"], np.asarray(list(isolate), np.int64)))
    elif cutaway > 0 and keep.any():
        lo, hi = float(dep[keep].min()), float(dep[keep].max())
        keep &= dep >= lo + (hi - lo) * float(cutaway)
    vis = np.nonzero(keep)[0]
    if not len(vis):
        return g
    # CLAMP the bounding boxes to the screen before bucketing. Geometry behind/outside the
    # camera can have a box thousands of pixels wide; without this, one such triangle makes its
    # bucket allocate a grid of that size and the render takes seconds instead of milliseconds.
    x0 = np.clip(fx0, 0, RW - 1).astype(np.int32)
    x1 = np.clip(fx1, 0, RW - 1).astype(np.int32)
    y0 = np.clip(fy0, 0, RH - 1).astype(np.int32)
    y1 = np.clip(fy1, 0, RH - 1).astype(np.int32)

    zbuf = np.full(RH * RW, np.inf, np.float32)

    # Bucket on WIDTH and HEIGHT independently, each rounded up to a power of two.  A single
    # bucket key (max of the two) would put a 600x8 sliver in the 1024 bucket and rasterize a
    # million cells for it; splitting the axes caps the waste at 4x whatever the box really is.
    def _p2(v):
        return np.where(v < 1, 1, 1 << np.ceil(np.log2(np.maximum(v, 1))).astype(np.int32))

    bwv, bhv = _p2(x1 - x0)[vis], _p2(y1 - y0)[vis]
    buckets = []
    for key in np.unique(bwv.astype(np.int64) << 20 | bhv):
        sw, sh = int(key >> 20), int(key & 0xFFFFF)
        sel = vis[(bwv == sw) & (bhv == sh)]
        # chunk so one pass never allocates more than ~4M grid cells
        per = max(1, 4_000_000 // ((sw + 1) * (sh + 1)))
        for k in range(0, len(sel), per):
            buckets.append((sw, sh, sel[k:k + per]))

    passes = []
    for sw, sh, sel in buckets:
        gx, gy = np.meshgrid(np.arange(sw + 1), np.arange(sh + 1))
        px = (x0[sel][:, None] + gx.ravel()[None, :]).astype(np.float32)
        py = (y0[sel][:, None] + gy.ravel()[None, :]).astype(np.float32)
        AX, AY = ax[sel], ay[sel]
        det = ((AY[:, 1] - AY[:, 2]) * (AX[:, 0] - AX[:, 2])
               + (AX[:, 2] - AX[:, 1]) * (AY[:, 0] - AY[:, 2]))
        ok = np.abs(det) > 1e-9
        det = np.where(ok, det, 1.0).astype(np.float32)[:, None]
        l1 = ((AY[:, 1] - AY[:, 2])[:, None] * (px - AX[:, 2][:, None])
              + (AX[:, 2] - AX[:, 1])[:, None] * (py - AY[:, 2][:, None])) / det
        l2 = ((AY[:, 2] - AY[:, 0])[:, None] * (px - AX[:, 2][:, None])
              + (AX[:, 0] - AX[:, 2])[:, None] * (py - AY[:, 2][:, None])) / det
        inside = ((l1 >= -1e-4) & (l2 >= -1e-4) & (l1 + l2 <= 1 + 1e-4) & ok[:, None]
                  & (px >= 0) & (px < RW) & (py >= 0) & (py < RH))
        if not inside.any():
            continue
        ti = np.nonzero(inside)[0]
        flat = (py[inside] * RW + px[inside]).astype(np.int64)
        if pixel_depth:
            b1, b2 = l1[inside], l2[inside]
            d3 = dep3[sel][ti]
            dd = (d3[:, 0] * b1 + d3[:, 1] * b2
                  + d3[:, 2] * (1.0 - b1 - b2)).astype(np.float32)
        else:
            dd = dep[sel][ti]
        np.minimum.at(zbuf, flat, dd)
        passes.append((flat, dd, sel[ti], l1[inside], l2[inside]))

    for flat, dd, ti, b1, b2 in passes:
        win = zbuf[flat] >= dd - 1e-6
        f = flat[win]
        g["tid"][f] = ti[win]
        g["l1"][f] = b1[win]
        g["l2"][f] = b2[win]
    return g


# ───────────────────────────────── shading pass ─────────────────────────────────
def _bilinear(tex, uv):
    """Wrapped bilinear sample; uv is (k,2) with V already in texture orientation."""
    th, tw = tex.shape[:2]
    x = (uv[:, 0] % 1.0) * tw - 0.5
    y = (uv[:, 1] % 1.0) * th - 0.5
    x0f, y0f = np.floor(x), np.floor(y)
    fx = (x - x0f).astype(np.float32)[:, None]
    fy = (y - y0f).astype(np.float32)[:, None]
    x0 = x0f.astype(np.int32) % tw
    y0 = y0f.astype(np.int32) % th
    x1 = (x0 + 1) % tw
    y1 = (y0 + 1) % th
    # gather as uint8 and convert only the ~k*4 samples we actually took. Converting the whole
    # texture to float first costs more than the entire rest of the shading pass.
    def g(yy, xx):
        return tex[yy, xx].astype(np.float32)
    top = g(y0, x0) * (1 - fx) + g(y0, x1) * fx
    bot = g(y1, x0) * (1 - fx) + g(y1, x1) * fx
    return (top * (1 - fy) + bot * fy) * np.float32(1 / 255.0)


def light_dir(scene, g, which="light"):
    """The key light's direction in MESH space, for this frame's camera.

    Normally the light is authored in mesh space and rides along with the model, which is right
    for an arena — the sun does not orbit the building when you spin the view. It is wrong for a
    head. A light fixed to the mesh means every orbit changes what is lit, and the one direction
    it never gives you is the one that shows shape: a raking light across the surface, where the
    terminator runs along the ridge and a mandible or a brow reads as a hard edge rather than a
    gentle gradient. Set `scene["light_rel"]` and the vector is interpreted in CAMERA space
    instead — (right, up, towards the viewer) — so the rake holds however you turn the model.

    The basis is the one raster() projects with, read straight off its own arithmetic: screen x is
    P.(cy,0,sy), screen y is P.(sy*sp, cp, -cy*sp), and view depth grows AWAY from the eye, so the
    direction towards the viewer is its negation.
    """
    L = np.asarray(scene.get(which, (0.35, 0.55, 0.76)), np.float32)
    if not scene.get("light_rel"):
        return L
    yaw, pitch = float(g.get("yaw", 0.0)), float(g.get("pitch", 0.0))
    cy, sy, cp, sp = np.cos(yaw), np.sin(yaw), np.cos(pitch), np.sin(pitch)
    R = np.array([cy, 0.0, sy], np.float32)                 # screen right
    U = np.array([sy * sp, cp, -cy * sp], np.float32)       # screen up
    V = np.array([sy * cp, -sp, -cy * cp], np.float32)      # towards the viewer
    return (L[0] * R + L[1] * U + L[2] * V).astype(np.float32)


def _bump(scene, t, w0, w1, w2, nt, L=None, F=None):
    """Re-light one material's pixels off its normal map. -> (k,3) float32 replacing vcol.

    The vertex lambert those pixels came in with is a per-VERTEX quantity: on a head that is one
    value every few millimetres of skin, which no amount of texturing can add a pore to. Here the
    interpolated normal is perturbed by the map and the same key light is evaluated per pixel.

    The tangent frame is (T along +U, B along +V) with V running DOWN the image, matching the
    axes face_builder.detail_normal measured the shipped maps against. Those maps store
    `nx = -dh/dx, ny = +dh/dy`; a heightfield's normal in this frame is `(-dh/dx, -dh/dy, 1)`,
    so green is negated on the way in. Getting that sign wrong lights every crease as a ridge.
    """
    N, T = scene["nrm"], scene["tan"]
    n = N[t[:, 0]] * w0 + N[t[:, 1]] * w1 + N[t[:, 2]] * w2
    tg = T[t[:, 0]] * w0 + T[t[:, 1]] * w1 + T[t[:, 2]] * w2
    n /= np.maximum(np.linalg.norm(n, axis=1)[:, None], 1e-8)
    tg -= n * (tg * n).sum(1)[:, None]
    tg /= np.maximum(np.linalg.norm(tg, axis=1)[:, None], 1e-8)
    b = np.cross(n, tg)

    tx = nt[:, 0] * 2.0 - 1.0
    ty = -(nt[:, 1] * 2.0 - 1.0)
    tz = np.sqrt(np.maximum(1.0 - tx * tx - ty * ty, 0.0))
    nn = tg * tx[:, None] + b * ty[:, None] + n * tz[:, None]
    nn /= np.maximum(np.linalg.norm(nn, axis=1)[:, None], 1e-8)

    if L is None:
        L = np.asarray(scene.get("light", (0.35, 0.55, 0.76)), np.float32)
    amb, key = float(scene.get("ambient", 0.30)), float(scene.get("key", 0.70))
    lam = np.clip(nn @ (L / max(float(np.linalg.norm(L)), 1e-8)), 0.0, 1.0)
    out = amb + key * lam
    # An optional FILL from the other side. A single raking key shows shape beautifully and then
    # loses the whole shadow half of the face to a flat ambient, which is the opposite failure to
    # the one the rake fixes — you can read the near cheekbone and nothing at all about the far
    # one. A dim second lambert, no shadow term, costs one dot product and puts detail back over
    # there without softening the terminator that carries the shape.
    fk = float(scene.get("fill", 0.0))
    if fk > 0.0 and F is not None:
        out = out + fk * np.clip(nn @ (F / max(float(np.linalg.norm(F)), 1e-8)), 0.0, 1.0)
    return np.repeat(out[:, None], 3, 1).astype(np.float32)


def shade(scene, g, exposure=1.0, highlight_part=None, highlight_col=(1.0, 0.32, 0.26),
          textures=True, flat=False):
    """Turn a G-buffer into a PIL image. Cheap enough to re-run on every slider tick.

    exposure only brightens the PREVIEW; it never touches the baked light in the file (that is
    arena_model.relight).  The arena interior bake is genuinely dark, so a viewing gain of ~2-4
    is normally what you want just to see the geometry.  It may be a scalar or an RGB triple —
    a triple is how the tab previews a lighting tint, since relight() is multiplicative and so
    is this.
    """
    RW, RH, ss = g["W"], g["H"], g["ss"]
    OW, OH = g.get("out", (RW, RH))
    img = np.empty((RH * RW, 3), np.float32)
    img[:] = np.array(BG, np.float32) / 255.0
    tid = g["tid"]
    hit = np.nonzero(tid >= 0)[0]
    if not scene or not len(hit):
        return Image.fromarray((img.reshape(RH, RW, 3) * 255).astype(np.uint8)
                               ).resize((OW, OH), Image.BILINEAR)

    # Work in MATERIAL-SORTED order from here on. Texturing has to be done one material at a
    # time, and a boolean mask per material is a full pass over every shaded pixel — 88 of them
    # costs far more than one argsort plus contiguous slices.
    idx = tid[hit]
    tex_on = bool(textures and scene["tex"])
    if tex_on:
        order = np.argsort(scene["mat"][idx], kind="stable")
        hit, idx = hit[order], idx[order]
    t = scene["tri"][idx]
    if flat:                                              # per-triangle constant, no gradients
        w0 = w1 = w2 = np.full((len(idx), 1), 1 / 3, np.float32)
    else:
        # raster stores lambda for corners 0 and 1 (l1 is 1 at vertex 0, l2 is 1 at vertex 1) --
        # pairing them with corners 1 and 2 instead rotates every triangle's texture lookup,
        # which a smooth vertex-colour bake hides but a crest on a jersey does not.
        w0 = g["l1"][hit][:, None]
        w1 = g["l2"][hit][:, None]
        w2 = 1.0 - w0 - w1
    V = scene["vcol"]
    col = V[t[:, 0]] * w0 + V[t[:, 1]] * w1 + V[t[:, 2]] * w2

    if tex_on:
        U = scene["uv"]
        uvp = U[t[:, 0]] * w0 + U[t[:, 1]] * w1 + U[t[:, 2]] * w2
        ms = scene["mat"][idx]
        uniq = np.unique(ms)
        lo = np.searchsorted(ms, uniq, "left")
        hi_ = np.searchsorted(ms, uniq, "right")
        samp = np.empty((len(idx), 3), np.float32)
        ntex = scene.get("ntex") or {}
        Lm = light_dir(scene, g)
        Fm = light_dir(scene, g, "fill_dir") if scene.get("fill") else None
        for mid, a, b in zip(uniq, lo, hi_):
            tex = scene["tex"].get(int(mid))
            samp[a:b] = NO_TEX if tex is None else _bilinear(tex, uvp[a:b])
            nm = ntex.get(int(mid))
            if nm is not None and scene.get("tan") is not None:
                col[a:b] = _bump(scene, t[a:b], w0[a:b], w1[a:b], w2[a:b],
                                 _bilinear(nm, uvp[a:b]), Lm, Fm)
        # A scene may finish its own albedo at the FRAGMENT rather than in a texture. The jersey
        # does: its stamps are a shader the game evaluates per pixel, and baking them into a
        # texture caps each one at however much of the base map its quad happens to own.
        fx = scene.get("fragment_pass")
        if fx is not None:
            fx(samp, ms, uvp)
        col = col * samp
    else:
        col = col * NO_TEX

    ex = np.asarray(exposure, np.float32).ravel()
    if ex.size != 1 or ex[0] != 1.0:
        col = col * (ex[None, :] if ex.size == 3 else ex[0])
    if highlight_part is not None:
        # a collection highlights a whole model (all of its submeshes at once), an int one part
        if isinstance(highlight_part, (int, np.integer)):
            sel = scene["part"][idx] == highlight_part
        else:
            sel = np.isin(scene["part"][idx], np.asarray(list(highlight_part), np.int64))
        if sel.any():
            # blend rather than replace, so the part's own shape stays readable while it flashes
            col[sel] = col[sel] * 0.30 + np.array(highlight_col, np.float32) * 0.70
    img[hit] = np.clip(col, 0, 1)

    out = img.reshape(RH, RW, 3)
    if ss > 1:
        out = out.reshape(RH // ss, ss, RW // ss, ss, 3).mean((1, 3))
    im = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8))
    return im if im.size == (OW, OH) else im.resize((OW, OH), Image.BILINEAR)


def pick(g, x, y):
    """Triangle index under a viewport pixel, or None. Used for click-to-select in the tab."""
    if not g:
        return None
    RW, RH, ss = g["W"], g["H"], g["ss"]
    OW, OH = g.get("out", (RW, RH))
    px = int(x * RW / max(OW, 1))
    py = int(y * RH / max(OH, 1))
    if not (0 <= px < RW and 0 <= py < RH):
        return None
    # search a small neighbourhood so a click on a thin rail still lands on it
    for r in (0, 1, 2, 3, 5, 8):
        x0, x1 = max(px - r, 0), min(px + r + 1, RW)
        y0, y1 = max(py - r, 0), min(py + r + 1, RH)
        w = g["tid"].reshape(RH, RW)[y0:y1, x0:x1]
        h = w[w >= 0]
        if len(h):
            return int(np.bincount(h).argmax())
    return None


def render(scene, W=760, H=520, yaw=0.6, pitch=0.35, zoom=1.0, pan=(0.0, 0.0),
           highlight_part=None, highlight_col=(1.0, 0.30, 0.25), exposure=1.0, cutaway=0.0,
           ss=1, textures=True, isolate=None):
    """raster + shade in one call — convenience for scripts and one-off renders."""
    g = raster(scene, W, H, yaw, pitch, zoom, pan, cutaway=cutaway, isolate=isolate, ss=ss)
    return shade(scene, g, exposure=exposure, highlight_part=highlight_part,
                 highlight_col=highlight_col, textures=textures)
