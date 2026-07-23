#!/usr/bin/env python3
"""Runtime vertex-buffer capture for NHL 2k10 under Xenia.

The game builds Xenos vertex/index buffers in GUEST RAM (physical window) before the GPU fetches
them. This scans a 512MB guest-RAM dump for coherent geometry (float32 + packed-short position
clouds), detects the stride/format, pairs likely index buffers, and exports OBJ + raw .bin.

Workflow:
  1. Get the game to a screen with a clear 3D MODEL (gameplay players, goalie-mask preview,
     team-select jersey render).
  2. Dump guest RAM to 4 slabs (the launcher / CE MCP does this):
       0x1C0000000, 0x1C8000000, 0x1D0000000, 0x1D8000000  (128MB each) -> ram_slab0..3.bin
  3. python vertex_capture.py <slab_dir> <out_dir>

Coord model: guest phys P -> host 0x1A0000000+P; the 4 slabs above cover phys 0x00000000..
0x20000000 (the 512MB window), so a hit at slab byte-offset O is guest phys O.
"""
import sys, struct
from pathlib import Path
import numpy as np


def _load_ram(slab_dir):
    slabs = []
    for i in range(4):
        p = Path(slab_dir) / f"ram_slab{i}.bin"
        slabs.append(np.fromfile(p, dtype=np.uint8) if p.exists() else np.zeros(1 << 27, np.uint8))
    return np.concatenate(slabs)


def _coherent_runs(vals, lo=-200.0, hi=200.0, min_run=256):
    """Byte offsets where float32 values stay in [lo,hi] for >= min_run consecutive floats
    (candidate vertex/attribute regions). vals = float32 array."""
    ok = np.isfinite(vals) & (vals >= lo) & (vals <= hi)
    # run-length over ok
    d = np.diff(np.concatenate([[0], ok.view(np.int8), [0]]))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0]
    return [(s, e) for s, e in zip(starts, ends) if e - s >= min_run]


def _score_vertexbuffer(pts):
    """0..1: do these (N,3) points look like a REAL connected mesh? Rejects the common false
    positives: scalar ramps / colour data where x==y==z, and perfectly-correlated lines."""
    if len(pts) < 64:
        return 0.0
    f = pts[np.isfinite(pts).all(1)]
    if len(f) < 64:
        return 0.0
    span = np.percentile(f, 98, 0) - np.percentile(f, 2, 0)
    if np.any(span < 1e-2) or np.any(span > 2000) or np.any(np.abs(f) > 5000):
        return 0.0
    # reject degenerate axes: real positions have x,y,z DIFFERENT per vertex (not x==y==z ramps)
    if (np.mean(np.abs(f[:, 0] - f[:, 1]) < 1e-4) > 0.4 or
            np.mean(np.abs(f[:, 1] - f[:, 2]) < 1e-4) > 0.4 or
            np.mean(np.abs(f[:, 0] - f[:, 2]) < 1e-4) > 0.4):
        return 0.0
    # axes must not be perfectly correlated (a straight line / gradient, not a surface)
    try:
        c = np.corrcoef(f.T)
        if np.max(np.abs(c[np.triu_indices(3, 1)])) > 0.97:
            return 0.0
    except Exception:
        return 0.0
    # reject outlier-driven noise: the interquartile core must span a real fraction of the bbox
    # (a scattered cross/star has a huge p2..p98 span but a tiny p25..p75 core)
    core = np.percentile(f, 75, 0) - np.percentile(f, 25, 0)
    if np.any(core < span * 0.08):
        return 0.0
    # reject origin/point clustering: not too many verts piled at the centroid
    med = np.median(f, 0)
    diag = float(np.linalg.norm(span))
    if diag < 1e-2:
        return 0.0
    clustered = np.mean(np.linalg.norm(f - med, axis=1) < diag * 0.02)
    if clustered > 0.35:
        return 0.0
    # local smoothness: consecutive verts spatially close relative to model size (connected mesh)
    step = np.linalg.norm(np.diff(f, axis=0), axis=1)
    smooth = float(np.median(step) < diag * 0.1)
    return smooth * min(1.0, len(f) / 128.0)


def scan(ram, out_dir, log=print):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    n = len(ram)
    log(f"scanning {n >> 20} MB guest RAM for vertex buffers…")
    hits = []
    for endian, dt in (("BE", ">f4"), ("LE", "<f4")):
        fl = np.frombuffer(ram[: (n // 4) * 4].tobytes() if False else ram[: (n // 4) * 4], dtype=dt)
        runs = _coherent_runs(fl, min_run=384)              # >=128 xyz verts
        log(f"  {endian}: {len(runs)} coherent float regions")
        for s, e in runs:
            byte0 = s * 4
            for stride in (3, 4, 6, 8, 12, 16):             # floats per vertex
                nfl = e - s
                nv = nfl // stride
                if nv < 64:
                    continue
                pts = fl[s:s + nv * stride].reshape(nv, stride)[:, :3]
                sc = _score_vertexbuffer(pts)
                if sc > 0.85:
                    v = pts[np.isfinite(pts).all(1)]
                    hits.append({"guest": int(byte0), "endian": endian, "stride_f": int(stride),
                                 "verts": int(len(v)), "score": round(float(sc), 3),
                                 "bbox_min": [round(float(x), 3) for x in v.min(0)],
                                 "bbox_max": [round(float(x), 3) for x in v.max(0)],
                                 "_pts": v})
                    break
    # dedup overlapping hits (keep highest score)
    hits.sort(key=lambda h: (-h["score"], -h["verts"]))
    kept = []
    used = []
    for h in hits:
        if any(abs(h["guest"] - g) < 0x1000 for g in used):
            continue
        used.append(h["guest"]); kept.append(h)
    log(f"  {len(kept)} distinct vertex-buffer candidates")
    manifest = []
    for i, h in enumerate(kept[:64]):
        pts = h.pop("_pts")
        stem = f"vbuf_{i:02d}_{h['guest']:08x}_{h['endian']}"
        # OBJ point cloud
        with open(out / (stem + ".obj"), "w") as f:
            for p in pts:
                f.write(f"v {p[0]:.5f} {p[1]:.5f} {p[2]:.5f}\n")
        # raw region bytes (verts * stride * 4, a bit of padding)
        blen = h["verts"] * h["stride_f"] * 4
        (out / (stem + ".bin")).write_bytes(ram[h["guest"]: h["guest"] + blen].tobytes())
        manifest.append({**h, "obj": stem + ".obj", "bin": stem + ".bin"})
        log(f"    #{i:02d} guest={h['guest']:#010x} {h['endian']} stride={h['stride_f']}f "
            f"verts={h['verts']} score={h['score']} bbox={h['bbox_min']}..{h['bbox_max']}")
    import json
    json.dump(manifest, open(out / "vbuf_manifest.json", "w"), indent=1)
    log(f"exported {len(manifest)} OBJ point clouds + raw bins -> {out}")
    return manifest


if __name__ == "__main__":
    slab_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "vbuf_out"
    scan(_load_ram(slab_dir), out_dir)
