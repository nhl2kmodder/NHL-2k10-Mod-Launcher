#!/usr/bin/env python3
"""DXT5 (DXT4_5) encoder — exact inverse of nhl2k10_trace_dump.decode.
Outputs game byte-order blocks (BE 565 color words), gto-tiled.

HQ path (default): per-block colour endpoints are fit with PCA (principal colour
axis) + iterative least-squares refinement against the *quantised* 4-colour
palette the GPU actually samples — far tighter edges/gradients than the old
per-channel bounding box. Alpha uses min/max + the 8-level interpolation table.
Cut-outs are alpha-aware (default): opaque colour is bled into see-through pixels
and the colour fit is alpha-weighted, so transparent RGB can't pull the endpoints
or leave an edge halo. Vectorised with numpy. `enc_block` is kept for reference."""
import sys
sys.path.insert(0, '.')
import numpy as np
import nhl2k10_trace_dump as T


def _565(w): return ((w >> 11 & 31) * 255 // 31, (w >> 5 & 63) * 255 // 63, (w & 31) * 255 // 31)
def to565(r, g, b): return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


# ── legacy bounding-box per-block encoder (reference / fallback) ───────────────
def enc_block(px):   # px: 16x (r,g,b,a); pixel i at (i%4,i//4)
    al = [p[3] for p in px]; amax = max(al); amin = min(al)
    if amax > amin:
        a0, a1 = amax, amin
        t = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
        aidx = [min(range(8), key=lambda k: abs(al[j] - t[k])) for j in range(16)]
    else:
        a0 = a1 = amax; aidx = [0] * 16
    ai = 0
    for j in range(16): ai |= (aidx[j] & 7) << (3 * j)
    rs = [p[0] for p in px]; gs = [p[1] for p in px]; bs = [p[2] for p in px]
    c0 = to565(max(rs), max(gs), max(bs)); c1 = to565(min(rs), min(gs), min(bs))
    a = _565(c0); b = _565(c1)
    cols = [a, b, tuple((2 * a[i] + b[i]) // 3 for i in range(3)), tuple((a[i] + 2 * b[i]) // 3 for i in range(3))]
    cidx = [min(range(4), key=lambda k: sum((px[j][c] - cols[k][c]) ** 2 for c in range(3))) for j in range(16)]
    bits = 0
    for j in range(16): bits |= (cidx[j] & 3) << (2 * j)
    return bytes([a0, a1]) + ai.to_bytes(6, 'little') + bytes([(c0 >> 8) & 0xFF, c0 & 0xFF, (c1 >> 8) & 0xFF, c1 & 0xFF]) + bits.to_bytes(4, 'little')


# ── HQ vectorised encoder ─────────────────────────────────────────────────────
def _pack565(rgb):                                  # (...,3) float -> packed 565 int
    r = np.clip(rgb[..., 0], 0, 255).astype(np.int32)
    g = np.clip(rgb[..., 1], 0, 255).astype(np.int32)
    b = np.clip(rgb[..., 2], 0, 255).astype(np.int32)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def _unpack565(p):                                  # packed 565 int -> (...,3) float (decoder-exact)
    p = p.astype(np.int32)
    r = ((p >> 11) & 31) * 255 // 31
    g = ((p >> 5) & 63) * 255 // 63
    b = (p & 31) * 255 // 31
    return np.stack([r, g, b], -1).astype(np.float32)


def _palette(p0, p1):                               # (NB,) packed -> (NB,4,3) decoder-exact palette
    a = _unpack565(p0); b = _unpack565(p1)
    return np.stack([a, b, (2 * a + b) // 3, (a + 2 * b) // 3], 1)


def _eval_endpoints(rgb, e0, e1, w=None):
    """Quantise endpoints, assign indices, return (p0, p1, cidx, per-block SSE).
    If w (NB,16) is given the SSE used to *rank* candidate endpoints is alpha-weighted
    (see-through pixels don't get to decide the colour); index assignment stays nearest."""
    p0 = _pack565(e0); p1 = _pack565(e1)
    dd = ((rgb[:, :, None, :] - _palette(p0, p1)[:, None, :, :]) ** 2).sum(-1)   # (NB,16,4)
    cidx = dd.argmin(-1)
    err = np.take_along_axis(dd, cidx[..., None], -1)[..., 0]                    # (NB,16)
    if w is not None: err = err * w
    sse = err.sum(1)                                                            # (NB,)
    return p0, p1, cidx, sse


def _fit_color(rgb, w=None):
    """rgb: (NB,16,3) float -> (p0 packed565, p1 packed565, cidx (NB,16)).
    PCA init + least-squares refinement, keeping the lowest-SSE endpoints per block
    so refinement can never regress a degenerate (near-solid) block.
    Optional w (NB,16) alpha-weights the mean/covariance/least-squares/ranking so
    see-through pixels barely pull the endpoints (kills edge halos on cut-outs)."""
    NB = rgb.shape[0]
    if w is not None:                               # alpha-weighted statistics
        wt = w[:, :, None]                          # (NB,16,1)
        wsafe = np.where(w.sum(1, keepdims=True) > 0, w.sum(1, keepdims=True), 1.0)[:, :, None]
        mean = (wt * rgb).sum(1, keepdims=True) / wsafe
        d = rgb - mean
        cov = np.einsum('bi,bij,bik->bjk', w, d, d)
    else:
        mean = rgb.mean(1, keepdims=True); d = rgb - mean
        cov = np.einsum('bij,bik->bjk', d, d)       # (NB,3,3)
    ax = np.tile(np.array([0.577, 0.577, 0.577], np.float32), (NB, 1))
    for _ in range(8):                              # power iteration -> principal colour axis
        #ax = np.einsum('bjk,bk->bj', cov, ax)
        #n = np.linalg.norm(ax, axis=1, keepdims=True); n[n == 0] = 1.0; ax = ax / n
        n = np.sqrt((ax ** 2).sum(axis=1, keepdims=True))
        n[n == 0] = 1.0
        ax = ax / n
    proj = np.einsum('bij,bj->bi', d, ax)
    pca0 = np.clip(mean[:, 0, :] + ax * proj.max(1)[:, None], 0, 255)
    pca1 = np.clip(mean[:, 0, :] + ax * proj.min(1)[:, None], 0, 255)
    bb0 = rgb.max(1); bb1 = rgb.min(1)              # per-channel bounding box (old method)

    # seed best with the better of {PCA, bounding-box} per block -> never worse than old
    p0a, p1a, idxa, ssea = _eval_endpoints(rgb, pca0, pca1, w)
    p0b, p1b, idxb, sseb = _eval_endpoints(rgb, bb0, bb1, w)
    useb = sseb < ssea
    best_p0 = np.where(useb, p0b, p0a); best_p1 = np.where(useb, p1b, p1a)
    best_idx = np.where(useb[:, None], idxb, idxa); best_sse = np.where(useb, sseb, ssea)
    cur0 = np.where(useb[:, None], bb0, pca0); cur1 = np.where(useb[:, None], bb1, pca1)
    W = np.array([0.0, 1.0, 1.0 / 3.0, 2.0 / 3.0], np.float32)   # weight toward c1 per index
    pw = w if w is not None else 1.0                # alpha pixel-weight (1.0 == unweighted)
    for _ in range(5):
        cidx = best_idx
        wi = W[cidx]; a = 1.0 - wi
        Saa = (pw * a * a).sum(1); Sab = (pw * a * wi).sum(1); Sbb = (pw * wi * wi).sum(1)
        det = Saa * Sbb - Sab * Sab
        ok = np.abs(det) > 1e-4                      # well-conditioned blocks only
        safe = np.where(ok, det, 1.0)
        ne0 = cur0.copy(); ne1 = cur1.copy()
        for ch in range(3):
            col = rgb[:, :, ch]; Sa = (pw * a * col).sum(1); Sb = (pw * wi * col).sum(1)
            ne0[:, ch] = np.where(ok, (Sbb * Sa - Sab * Sb) / safe, cur0[:, ch])
            ne1[:, ch] = np.where(ok, (-Sab * Sa + Saa * Sb) / safe, cur1[:, ch])
        cur0 = np.clip(ne0, 0, 255); cur1 = np.clip(ne1, 0, 255)
        p0, p1, idx, sse = _eval_endpoints(rgb, cur0, cur1, w)
        imp = sse < best_sse                         # keep only improvements
        best_p0 = np.where(imp, p0, best_p0); best_p1 = np.where(imp, p1, best_p1)
        best_idx = np.where(imp[:, None], idx, best_idx); best_sse = np.where(imp, sse, best_sse)
    return best_p0, best_p1, best_idx.astype(np.int64)


def _fit_alpha(al):
    """al: (NB,16) float -> (a0 uint8, a1 uint8, aidx (NB,16)). a0=max>=a1=min so the
    decoder uses the 8-level interpolation table (matches enc_block)."""
    a0 = al.max(1); a1 = al.min(1)
    cols = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
    t = np.stack(cols, 1)                           # (NB,8)
    aidx = np.abs(al[:, :, None] - t[:, None, :]).argmin(-1)   # (NB,16)
    return np.round(a0).astype(np.int64), np.round(a1).astype(np.int64), aidx.astype(np.int64)


# BC4 decode weights for each of the 8 index slots in the a0>=a1 mode (matches _fit_alpha's `cols`
# and the decoder's `_at`): slot0=a0, slot1=a1, slot2..7 = ((7-j)a0 + j·a1)/7 for j=1..6.
_BC4_WA0 = np.array([1., 0., 6/7, 5/7, 4/7, 3/7, 2/7, 1/7])
_BC4_WA1 = np.array([0., 1., 1/7, 2/7, 3/7, 4/7, 5/7, 6/7])


def _fit_bc4_refined(al, iters=3):
    """Higher-quality single-channel BC4 fit than raw min/max `_fit_alpha`. Raw min/max pins the
    two endpoints to a block's extreme pixels, so a near-flat block gets its 8-level ladder
    over-stretched to its tiny local range — which quantizes sub-noise into visible dither and
    makes neighbouring blocks disagree at their seams (the "noise inside the shape" on DXN normal
    maps). This starts from min/max, then least-squares-refines (a0,a1) against the current index
    assignment (same technique the DXT5 colour path uses) and re-indexes — endpoints settle to fit
    the *bulk* of the block, so flat interiors stay flat and only genuine detail spends the ladder.
    al: (NB,16) float -> (a0,a1 int64, aidx (NB,16)). Keeps a0>=a1 (8-level interpolation mode)."""
    def _sse(a0, a1):
        t = a0[:, None] * _BC4_WA0[None, :] + a1[:, None] * _BC4_WA1[None, :]   # (NB,8)
        d = np.abs(al[:, :, None] - t[:, None, :]); idx = d.argmin(-1)          # (NB,16)
        return (np.take_along_axis(d, idx[:, :, None], -1)[:, :, 0] ** 2).sum(1), idx

    b0e = np.round(al.max(1)).astype(np.float32)        # min/max baseline (== _fit_alpha)
    b1e = np.round(al.min(1)).astype(np.float32)
    best_sse, best_idx = _sse(b0e, b1e)
    best0, best1 = b0e.copy(), b1e.copy()
    a0, a1 = al.max(1).astype(np.float32), al.min(1).astype(np.float32)
    for _ in range(max(1, iters)):                      # LSQ-refine endpoints vs current indices
        _, idx = _sse(a0, a1)
        wa0 = _BC4_WA0[idx]; wa1 = _BC4_WA1[idx]
        S00 = (wa0 * wa0).sum(1); S01 = (wa0 * wa1).sum(1); S11 = (wa1 * wa1).sum(1)
        rb0 = (wa0 * al).sum(1);  rb1 = (wa1 * al).sum(1)
        det = S00 * S11 - S01 * S01; ok = np.abs(det) > 1e-9; safe = np.where(ok, det, 1.0)
        na0 = np.where(ok, (S11 * rb0 - S01 * rb1) / safe, a0)
        na1 = np.where(ok, (S00 * rb1 - S01 * rb0) / safe, a1)
        a0 = np.clip(na0, 0, 255); a1 = np.clip(na1, 0, 255)
        sw = a1 > a0; a0c = np.where(sw, a1, a0); a1 = np.where(sw, a0, a1); a0 = a0c
        r0, r1 = np.round(a0), np.round(a1)             # keep per-block ONLY where it beats baseline
        sse, idx2 = _sse(r0, r1); win = sse < best_sse
        best0 = np.where(win, r0, best0); best1 = np.where(win, r1, best1)
        best_idx = np.where(win[:, None], idx2, best_idx); best_sse = np.where(win, sse, best_sse)
    return best0.astype(np.int64), best1.astype(np.int64), best_idx.astype(np.int64)

from scipy.ndimage import distance_transform_edt

def _alpha_bleed(rgba, max_passes=64, thresh=0):
    """
    Flood the colour of the nearest opaque pixel into see-through pixels (alpha <= thresh)
    using Euclidean Distance Transform (EDT).
    Replaces slow iterative np.roll loops while preserving the original interface.
    """
    known = rgba[..., 3] > thresh
    if known.all() or not known.any():
        return rgba.copy()
    
    # Compute nearest opaque neighbor coordinates
    indices = distance_transform_edt(~known, return_distances=False, return_indices=True)
    
    out = rgba.copy()
    # Fill transparent RGB with color of nearest opaque pixel
    out[..., :3] = rgba[indices[0], indices[1], :3]
    return out

def _gto_block_vec(bw, bh, log2_bpu):
    """Vectorized block tile index mapping for DXT1/5, DXN, and DXT5A formats."""
    bx, by = np.meshgrid(np.arange(bw, dtype=np.int64), np.arange(bh, dtype=np.int64))
    bx_flat = bx.ravel()
    by_flat = by.ravel()
    
    # Calculate byte offsets using vectorized gto calculation
    offsets = _gto_vec(bx_flat, by_flat, bw, log2_bpu)
    bytes_per_block = 1 << log2_bpu
    return offsets // bytes_per_block

def encode_image(img, levels=0, alpha_aware=True, premultiply=False):
    """PIL RGBA WxH -> game gto-tiled DXT5 bytes ((W//4)*(H//4)*16).

    premultiply DEFAULTS TO FALSE — most NHL2k10 DXT4_5 textures (UI/HUD/overlay) are
    STRAIGHT alpha, and premultiplying those multiplies RGB by alpha and DARKENS every
    partial-alpha pixel (measured: a faithful straight round-trip is ~35-42 dB, vs ~6-24 dB
    with premultiply on). Only a few textures (some cut-out logos) are truly premultiplied;
    the caller (archive_textures._orig_is_premult) DETECTS that per-texture and passes
    premultiply=True just for those, where the GPU composites premultiplied
    (dst = src.rgb + dst*(1-a)) so stored RGB must be colour*alpha to avoid edge bloom.
    (Opaque images are unaffected: alpha=255 -> premultiply is a no-op.)"""
    W, H = img.size
    rgba = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(H, W, 4).astype(np.float32)
    if levels:                                      # optional posterize (LZ compressibility)
        step = 256 // levels
        rgba[..., :3] = np.minimum(255, (rgba[..., :3] // step) * step + step // 2)
    # Clean up sub-pixel alpha noise on smaller UI elements / mips to prevent DXT5 block quantization noise
    if W <= 256 or H <= 256:
        alpha_chan = rgba[..., 3]
        # Snap weak anti-aliasing pixels to clean boundaries
        alpha_chan = np.where(alpha_chan < 32, 0.0, alpha_chan)
        alpha_chan = np.where(alpha_chan > 224, 255.0, alpha_chan)
        rgba[..., 3] = alpha_chan
    if premultiply:                                 # engine convention: premultiplied alpha
        rgba[..., :3] *= rgba[..., 3:4] / 255.0     # transparent -> 0, partial -> colour*alpha
        weighted = False                            # premult 0s are REAL samples -> fit them all
    elif alpha_aware:                               # legacy straight-alpha cut-out path
        rgba = _alpha_bleed(rgba)                   # fill transparent RGB before colour fit
        weighted = True
    else:
        weighted = False
    bw, bh = W // 4, H // 4
    blocks = rgba.reshape(bh, 4, bw, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bw * bh, 16, 4)
    rgb = blocks[:, :, :3]; alpha = blocks[:, :, 3]

    w = (np.maximum(alpha, 1.0) / 255.0) if weighted else None   # opaque px decide colour
    p0, p1, cidx = _fit_color(rgb, w)
    a0, a1, aidx = _fit_bc4_refined(alpha)          # LSQ-refined (never worse than min/max):
                                                    # smoother alpha gradients, no block seams
                                                    # wobbling a feathered cut-out edge

    NB = bw * bh

    # Create explicit bit shift vectors using uint64
    alpha_shifts = (3 * np.arange(16, dtype=np.uint64))[None, :]
    color_shifts = (2 * np.arange(16, dtype=np.uint64))[None, :]

    # Perform bitwise shift first, then reduce via sum
    ai = np.bitwise_or.reduce(aidx.astype(np.uint64) << alpha_shifts, axis=1) # 48-bit alpha index bitmask
    ci = np.bitwise_or.reduce(cidx.astype(np.uint64) << color_shifts, axis=1) # 32-bit color index bitmask
    
    blk = np.empty((NB, 16), np.uint8)
    blk[:, 0] = a0.astype(np.uint8); blk[:, 1] = a1.astype(np.uint8)
    for k in range(6): blk[:, 2 + k] = (ai >> np.uint64(8 * k)) & np.uint64(0xFF)
    blk[:, 8] = (p0 >> 8) & 0xFF; blk[:, 9] = p0 & 0xFF
    blk[:, 10] = (p1 >> 8) & 0xFF; blk[:, 11] = p1 & 0xFF
    for k in range(4): blk[:, 12 + k] = (ci >> np.uint64(8 * k)) & np.uint64(0xFF)

    gmap = _gto_block_vec(bw, bh, 4)
    tiled = np.zeros((max(NB, int(gmap.max()) + 1), 16), np.uint8)   # pow2>=128 wide -> == NB
    tiled[gmap] = blk                               # linear (by*bw+bx) -> gto-tiled slot
    return tiled.reshape(-1).tobytes()


def encode_image_dxt1(img, alpha_aware=True):
    """PIL RGBA WxH -> game gto-tiled DXT1/BC1 bytes ((W//4)*(H//4)*8) — inverse of
    nhl2k10_trace_dump.decode(...,'DXT1',8,1,...). 8-byte blocks: BE-565 c0,c1 + 4 idx
    bytes. Opaque blocks use the 4-colour mode (c0>c1); any block with transparent pixels
    (alpha<128) uses 1-bit punchthrough (c0<=c1, index 3 = transparent).
    alpha_aware=True bleeds/weights so punch-through pixels don't pull the colour fit."""
    W, H = img.size
    rgba = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(H, W, 4).astype(np.float32)
    if alpha_aware:
        rgba = _alpha_bleed(rgba, thresh=127)       # fill punch-through RGB before colour fit
    bw, bh = W // 4, H // 4
    blocks = rgba.reshape(bh, 4, bw, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bw * bh, 16, 4)
    rgb = blocks[:, :, :3]; alpha = blocks[:, :, 3]
    NB = bw * bh
    w = np.where(alpha >= 128, 1.0, 1.0 / 255.0) if alpha_aware else None   # kept px decide colour
    p0, p1, cidx = _fit_color(rgb, w)               # PCA 4-colour fit (shared with DXT5)
    trans = alpha < 128                             # (NB,16) transparent mask
    pt = trans.any(1)                               # (NB,) blocks needing punchthrough

    # opaque blocks -> 4-colour mode needs c0 > c1 (swap endpoints + flip idx bit0)
    swap = (~pt) & (p0 < p1)
    q0 = np.where(swap, p1, p0); q1 = np.where(swap, p0, p1)
    idx = np.where(swap[:, None], cidx ^ 1, cidx)

    # punchthrough blocks -> 3-colour mode needs c0 <= c1, then re-assign indices
    if pt.any():
        hi = q0 > q1
        a = np.where(pt & hi, q1, q0); b = np.where(pt & hi, q0, q1)
        q0 = np.where(pt, a, q0); q1 = np.where(pt, b, q1)
        col0 = _unpack565(q0); col1 = _unpack565(q1)             # (NB,3)
        pal3 = np.stack([col0, col1, (col0 + col1) // 2], 1)     # (NB,3,3) decoder palette
        dd = ((rgb[:, :, None, :] - pal3[:, None, :, :]) ** 2).sum(-1)  # (NB,16,3)
        idx_pt = np.where(trans, 3, dd.argmin(-1))               # transparent -> index 3
        idx = np.where(pt[:, None], idx_pt, idx)

    ci = (idx.astype(np.uint64) << (2 * np.arange(16, dtype=np.uint64))[None, :]).sum(1)   # 32-bit
    blk = np.empty((NB, 8), np.uint8)
    blk[:, 0] = (q0 >> 8) & 0xFF; blk[:, 1] = q0 & 0xFF          # c0 big-endian
    blk[:, 2] = (q1 >> 8) & 0xFF; blk[:, 3] = q1 & 0xFF          # c1 big-endian
    for k in range(4): blk[:, 4 + k] = (ci >> np.uint64(8 * k)) & np.uint64(0xFF)

    gmap = _gto_block_vec(bw, bh, 3)
    tiled = np.zeros((max(NB, int(gmap.max()) + 1), 8), np.uint8)    # pow2>=128 wide -> == NB
    tiled[gmap] = blk
    return tiled.reshape(-1).tobytes()


def encode_image_dxt5a(img):
    """PIL RGBA WxH -> game gto-tiled DXT5A/BC4 bytes ((W//4)*(H//4)*8) — inverse of
    nhl2k10_trace_dump.decode(...,'DXT5A',8,1,...). Single-channel block (the DXT5 alpha
    block): [a0,a1,6 idx bytes]. The decoder shows it as greyscale (value -> R=G=B), so we
    store the R channel."""
    W, H = img.size
    rgba = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(H, W, 4).astype(np.float32)
    bw, bh = W // 4, H // 4
    blocks = rgba.reshape(bh, 4, bw, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bw * bh, 16, 4)
    val = blocks[:, :, 0]                           # R = the stored greyscale value
    NB = bw * bh
    a0, a1, aidx = _fit_alpha(val)                  # reuse the DXT5 alpha-block fitter
    ai = (aidx.astype(np.uint64) << (3 * np.arange(16, dtype=np.uint64))[None, :]).sum(1)
    blk = np.empty((NB, 8), np.uint8)
    blk[:, 0] = a0.astype(np.uint8); blk[:, 1] = a1.astype(np.uint8)
    for k in range(6): blk[:, 2 + k] = (ai >> np.uint64(8 * k)) & np.uint64(0xFF)
    gmap = _gto_block_vec(bw, bh, 3)
    tiled = np.zeros((max(NB, int(gmap.max()) + 1), 8), np.uint8)
    tiled[gmap] = blk
    return tiled.reshape(-1).tobytes()


def encode_image_dxn(img):
    """PIL RGBA WxH -> game gto-tiled DXN/BC5 bytes ((W//4)*(H//4)*16) — inverse of
    nhl2k10_trace_dump.decode(...,'DXN',16,1,...). A normal map: two BC4 channels, X=R in
    bytes[0:8], Y=G in bytes[8:16]. Z is reconstructed by the GPU, so only R,G are stored."""
    W, H = img.size
    rgba = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(H, W, 4).astype(np.float32)
    bw, bh = W // 4, H // 4
    blocks = rgba.reshape(bh, 4, bw, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bw * bh, 16, 4)
    X = blocks[:, :, 0]; Y = blocks[:, :, 1]        # R=X, G=Y
    NB = bw * bh
    rx0, rx1, rxi = _fit_bc4_refined(X)              # LSQ-refined endpoints -> less interior dither
    ry0, ry1, ryi = _fit_bc4_refined(Y)
    aix = (rxi.astype(np.uint64) << (3 * np.arange(16, dtype=np.uint64))[None, :]).sum(1)
    aiy = (ryi.astype(np.uint64) << (3 * np.arange(16, dtype=np.uint64))[None, :]).sum(1)
    blk = np.empty((NB, 16), np.uint8)
    blk[:, 0] = rx0.astype(np.uint8); blk[:, 1] = rx1.astype(np.uint8)
    for k in range(6): blk[:, 2 + k] = (aix >> np.uint64(8 * k)) & np.uint64(0xFF)
    blk[:, 8] = ry0.astype(np.uint8); blk[:, 9] = ry1.astype(np.uint8)
    for k in range(6): blk[:, 10 + k] = (aiy >> np.uint64(8 * k)) & np.uint64(0xFF)
    gmap = _gto_block_vec(bw, bh, 4)
    tiled = np.zeros((max(NB, int(gmap.max()) + 1), 16), np.uint8)
    tiled[gmap] = blk
    return tiled.reshape(-1).tobytes()


# ── uncompressed (non-block) formats: 8888 / 8_8 / 4444 / 565 / 1555 / 8 ──────
def _gto_vec(x, y, pitch, bl):
    """Vectorised nhl2k10_trace_dump.gto — x,y int64 arrays -> byte-offset array."""
    pitch = (pitch + 31) & ~31
    macro = ((x >> 5) + (y >> 5) * (pitch >> 5)) << (bl + 7)
    micro = ((x & 7) + ((y & 0xE) << 2)) << bl
    o = macro + ((micro & ~0xF) << 1) + (micro & 0xF) + ((y & 1) << 4)
    return (((o & ~0x1FF) << 3) + ((y & 16) << 7) + ((o & 0x1C0) << 2)
            + (((((y & 8) >> 2) + (x >> 3)) & 3) << 6) + (o & 0x3F))


_LINEAR_BPU = {"8888": 4, "8_8": 2, "4444": 2, "565": 2, "1555": 2, "8": 1}


def encode_image_linear(img, fmt, tiled=1):
    """Encode an uncompressed Xenos format, gto-tiled — exact inverse of
    nhl2k10_trace_dump.decode for fmt in 8888/8_8/4444/565/1555/8. Returns W*H*bpu bytes.
    16-bit formats are big-endian words; channels quantised to match the decoder."""
    W, H = img.size
    a = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(H * W, 4).astype(np.uint32)
    R, G, B, A = a[:, 0], a[:, 1], a[:, 2], a[:, 3]
    bpu = _LINEAR_BPU[fmt]

    if fmt == "8888":                                   # bytes [A,R,G,B]
        px = np.stack([A, R, G, B], 1).astype(np.uint8)
    elif fmt == "8":                                    # grayscale (R)
        px = R.astype(np.uint8).reshape(-1, 1)
    elif fmt == "8_8":                                  # [R,G]
        px = np.stack([R, G], 1).astype(np.uint8)
    else:                                               # 16-bit big-endian word
        if fmt == "4444":
            w = ((A >> 4) << 12) | ((R >> 4) << 8) | ((G >> 4) << 4) | (B >> 4)
        elif fmt == "565":
            w = ((R >> 3) << 11) | ((G >> 2) << 5) | (B >> 3)
        else:  # 1555
            w = ((A >= 128).astype(np.uint32) << 15) | ((R >> 3) << 10) | ((G >> 3) << 5) | (B >> 3)
        px = np.stack([(w >> 8) & 0xFF, w & 0xFF], 1).astype(np.uint8)

    if not tiled:
        return px.reshape(-1).tobytes()

    # Calculate bit shift from byte size safely
    bl = int(np.log2(bpu)) if bpu > 0 else 0
    ys, xs = np.divmod(np.arange(W * H, dtype=np.int64), W)
    offs = _gto_vec(xs, ys, W, bl)

    # Ensure buffer size accounts for pitch-padding and total mapped offset
    max_offset = int(offs.max()) + bpu
    expected_size = W * H * bpu
    out_size = max(expected_size, max_offset)

    out = np.zeros(out_size, np.uint8)
    for k in range(bpu):
        out[offs + k] = px[:, k]
        
    return out[:expected_size].tobytes() if out_size > expected_size else out.tobytes()
