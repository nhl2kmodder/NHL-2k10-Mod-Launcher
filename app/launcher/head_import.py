"""Custom head model import — upload your own .obj/.glb/.fbx as a player head.

The model tab's replace_part machinery is size-preserving: a replacement mesh must fit inside
the vertex/index ranges the shipped submesh records carve out. A head someone sculpted outside
the game usually doesn't. This module breaks that ceiling by RELOCATING the whole mesh span —
[submesh table .. end of vertex streams) — to the end of blob 0 with enlarged per-part ranges,
after which the ordinary replace_part path works unchanged.

Why relocation is safe (measured on heads 8528 / 0116 / 0146, cross-checked field by field):

  * The span is referenced from outside by EXACTLY five self-relative(+1) header fields, all
    in [0x200, 0x500): one to the table, one to the index block, three into the metadata
    blocks between the index block and the stream descriptors. Same set, order and targets
    in every head probed.
  * The span points outside itself through ONE field per submesh record: u32 [7] (+0x1C) is a
    self-relative(+1) back-pointer to the `8000000a` remap block just before the table, which
    does not move. Rebase it per record and nothing else refers out.
  * The ~1,400 apparent tail->span "pointers" are smooth ramps of packed 16-bit pairs — data,
    not references (their per-file values do not track the span layout).
  * Everything else inside the span is span-relative: descriptor rel_off/end are relative to
    the descriptor, the per-part range table after the descriptors stores byte ranges into
    stream 0, index values address the streams through the record's first_vtx, and the
    position transform key block moves along with the span (pos_xform rfinds it).

The old span is zeroed (so the table signature can't be found twice) and the new one appended
at a 16-byte boundary. Growing blob 0 means rebuilding the container: re-encode blob 0, carry
blob 1 verbatim, and patch the section table — sect@0x20 +0x0C (decoded size) and +0x18
(packed size), sect@0x40 +0x14 (blob 1 moved), and the file total at +8. The third section's
+0x1C points 0x2a0 before the END of the shipped DRAM; it is an absolute offset to a footer
that does not move when we append past it, so it is deliberately left untouched (FOOTER note
below — if an in-game test ever shows the loader recomputing it from the decoded size instead
of reading it, the fallback is to splice the new span in FRONT of the footer and patch it).

Streaming: player heads don't fit streaming_pool's team-code families, so the same ballast
trick is applied head-shaped here — pad the biggest SHIPPED head so the engine's sizing pass
(which walks the roster) keeps a ceiling at least as big as the custom head, even if it turns
out not to enumerate roster-bound custom ids.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

from . import archive_textures as AT
from . import arena_model as AM
from . import char_model as C


def _u32(b, o) -> int:
    return struct.unpack_from(">I", b, o)[0]


def _p32(b, o, v):
    struct.pack_into(">I", b, o, v & 0xFFFFFFFF)


# where the five header reference fields live, in every head probed
_HDR_LO, _HDR_HI = 0x200, 0x500


# ───────────────────────────── span survey ─────────────────────────────
def span_info(b: bytes, m: dict) -> dict:
    """Everything a relocation must know about this head's mesh span — with every assumption
    the patch list rests on verified against THIS file, so an unusual head fails loudly here
    instead of shipping a corrupt blob."""
    t, recs, nidx = m["table"], m["recs"], m["nidx"]
    ib0 = m["ib0"]
    ib_end = ib0 + 2 * nidx
    meta_lo = (ib_end + 3) & ~3
    desc = m["desc"]
    if len(m["streams"]) != 2:
        raise ValueError(f"head span: expected 2 vertex streams, found {len(m['streams'])}")
    lastd = desc + 0x18 * 2
    s0 = m["streams"][0][0]
    s_end = max(off + st * n for off, st, n in m["streams"])
    if not (t < ib0 <= ib_end <= meta_lo < desc < lastd < s0 < s_end <= len(b)):
        raise ValueError("head span: region order broke — not a layout this importer knows")

    parts = C.submeshes(b, m)

    # per-part range table between the descriptors and stream 0: byte ranges into stream 0.
    # Shipped heads write record 0 without its leading flags word.
    nsub = (s0 - lastd) // 4
    if (s0 - lastd) % 4 == 0 and nsub == 4 + (recs - 1) * 5:
        style = "headless0"
    elif (s0 - lastd) % 4 == 0 and nsub == recs * 5:
        style = "full"
    else:
        raise ValueError(f"head span: per-part range table is {s0 - lastd} B for {recs} "
                         "records — unrecognised framing")
    W = [_u32(b, lastd + 4 * k) for k in range(nsub)]
    sub_flags = []
    for k, p in enumerate(parts):
        if style == "headless0":
            fl, ln, o1, o2, z = ((None, W[0], W[1], W[2], W[3]) if k == 0 else
                                 tuple(W[4 + (k - 1) * 5: 9 + (k - 1) * 5]))
        else:
            fl, ln, o1, o2, z = tuple(W[k * 5: k * 5 + 5])
        if ln != p["n_vtx"] * 8 or o1 != p["first_vtx"] * 8 or o2 != o1 or z != 0:
            raise ValueError(f"head span: range-table entry {k} does not match record {k} "
                             f"({ln:#x}/{o1:#x} vs {p['n_vtx'] * 8:#x}/{p['first_vtx'] * 8:#x})")
        sub_flags.append(fl)

    # the submesh records' one outward field: [7] -> the remap block before the table
    tgts = set()
    for k in range(recs):
        fo = t + k * 48 + 0x1C
        tgts.add((fo + _u32(b, fo) - 1) & 0xFFFFFFFF)
    if len(tgts) != 1:
        raise ValueError(f"head span: record field [7] targets diverge ({sorted(tgts)})")
    remap_tgt = tgts.pop()
    if not (0 < remap_tgt < t):
        raise ValueError(f"head span: record back-pointer 0x{remap_tgt:x} is not pre-table")

    # the five header fields that reference the span from outside
    refs = []
    for o in range(_HDR_LO, min(_HDR_HI, t), 4):
        tgt = (o + _u32(b, o) - 1) & 0xFFFFFFFF
        if not (t <= tgt < s_end):
            continue
        if tgt == t:
            refs.append((o, "table", 0))
        elif tgt == ib0:
            refs.append((o, "ib0", 0))
        elif meta_lo <= tgt < desc:
            refs.append((o, "meta", tgt - meta_lo))
        elif desc <= tgt < lastd:
            refs.append((o, "desc", tgt - desc))
        elif lastd <= tgt < s0:
            refs.append((o, "sub", tgt - lastd))
        else:
            raise ValueError(f"head span: header ref @0x{o:x} lands at 0x{tgt:x}, inside "
                             "index or stream data — no stable relocation target")
    kinds = sorted(k for _, k, _ in refs)
    if len(refs) != 5 or kinds.count("table") != 1 or kinds.count("ib0") != 1:
        raise ValueError(f"head span: expected the 5 known header references, found "
                         f"{[(hex(o), k) for o, k, _ in refs]}")

    return dict(t=t, recs=recs, nidx=nidx, ib0=ib0, ib_end=ib_end, meta_lo=meta_lo,
                desc=desc, lastd=lastd, s0=s0, s_end=s_end, parts=parts,
                sub_style=style, sub_flags=sub_flags, remap_tgt=remap_tgt, refs=refs)


# ───────────────────────────── the relocation ─────────────────────────────
def respan(b: bytes, m: dict, new_sizes: dict, log=print) -> bytes:
    """Rebuild the mesh span with resized per-part ranges and append it at the end of blob 0.

    `new_sizes` maps a record index to its new (n_vtx, n_idx); unnamed records keep their
    shipped sizes. Vertex/index content is carried over (index values shifted by each part's
    first_vtx delta, grown ranges padded with restarts / repeated rows) so the blob stays
    well-formed — the caller then writes real geometry through char_model.replace_part, whose
    budget checks read the enlarged records naturally.
    """
    info = span_info(b, m)
    recs, parts = info["recs"], info["parts"]
    old = [(p["first_idx"], p["n_idx"], p["first_vtx"], p["n_vtx"]) for p in parts]

    # shipped gap pattern between parts (scan_models allows a 1-index seam)
    gi, gv = [], []
    for k, (fi, ni, fv, nv) in enumerate(old):
        pfi, pni, pfv, pnv = old[k - 1] if k else (0, 0, 0, 0)
        gi.append(fi - (pfi + pni))
        gv.append(fv - (pfv + pnv))
    if any(g < 0 or g > 4 for g in gi) or any(g < 0 or g > 4 for g in gv):
        raise ValueError(f"head span: unexpected part seams (idx {gi}, vtx {gv})")

    new, ci, cv = [], 0, 0
    for k, p in enumerate(parts):
        nv2, ni2 = new_sizes.get(p["rec"], (old[k][3], old[k][1]))
        nv2, ni2 = max(int(nv2), 1), max(int(ni2), 3)
        ci += gi[k]
        cv += gv[k]
        new.append((ci, ni2, cv, nv2))
        ci += ni2
        cv += nv2
    new_nidx, new_nvtx = ci, cv
    if new_nvtx > 0xFFFE:
        raise ValueError(f"{new_nvtx:,} vertices total, but the 16-bit index stream tops out "
                         "at 65,534 — decimate the mesh")

    strides = [st for _, st, _ in m["streams"]]
    meta = bytes(b[info["meta_lo"]:info["desc"]])

    # new span layout (span-relative; the base below is 16-aligned and the table is a
    # multiple of 48 B, so span-relative alignment equals absolute alignment)
    r_ib0 = recs * 48
    r_meta = (r_ib0 + 2 * new_nidx + 3) & ~3
    r_desc = r_meta + len(meta)
    r_lastd = r_desc + 0x18 * 2
    r_subend = r_lastd + (info["s0"] - info["lastd"])
    r_s0 = (r_subend + 7) & ~7
    r_s1 = r_s0 + new_nvtx * strides[0]
    r_send = r_s1 + new_nvtx * strides[1]
    span = bytearray(r_send)

    # records — same rows, resized ranges ([7] is rebased later, once offsets are absolute)
    span[0:r_ib0] = b[info["t"]:info["t"] + recs * 48]
    for k in range(recs):
        fi, ni, fv, nv = new[k]
        o = k * 48
        _p32(span, o + 4, fi)
        _p32(span, o + 8, ni)
        _p32(span, o + 12, ni - 2)
        _p32(span, o + 20, fv)
        _p32(span, o + 24, nv)

    # index block — values are stream-global, so each part's copy shifts by its vertex delta
    old_ib = np.frombuffer(b[info["ib0"]:info["ib_end"]], ">u2").astype(np.int64)
    NI = np.full(new_nidx, 0xFFFF, np.int64)
    for k in range(recs):
        ofi, oni, ofv, onv = old[k]
        nfi, nni, nfv, nnv = new[k]
        d = nfv - ofv
        seg = old_ib[ofi:ofi + min(oni, nni)]
        NI[nfi:nfi + len(seg)] = np.where(seg == 0xFFFF, 0xFFFF, seg + d)
        if gi[k] and nfi:                      # carry the seam word (never drawn) shifted
            w = int(old_ib[ofi - 1]) if ofi else 0xFFFF
            NI[nfi - 1] = 0xFFFF if w == 0xFFFF else min(max(w + d, 0), 0xFFFE)
    span[r_ib0:r_ib0 + 2 * new_nidx] = NI.astype(">u2").tobytes()

    span[r_meta:r_desc] = meta

    # descriptors — copy, then refit sizes and the descriptor-relative data offsets
    span[r_desc:r_lastd] = b[info["desc"]:info["lastd"]]
    for k, r_data in enumerate((r_s0, r_s1)):
        o = r_desc + k * 0x18
        nbytes = new_nvtx * strides[k]
        rel = r_data - (o + 0x0B)
        _p32(span, o + 8, nbytes)
        _p32(span, o + 12, rel)
        _p32(span, o + 16, rel + nbytes - 4)

    # per-part range table — byte ranges into stream 0, refit to the new vertex ranges
    span[r_lastd:r_subend] = b[info["lastd"]:info["s0"]]
    for k in range(recs):
        fi, ni, fv, nv = new[k]
        if info["sub_style"] == "headless0":
            o = r_lastd + (0 if k == 0 else 16 + (k - 1) * 20 + 4)
        else:
            o = r_lastd + k * 5 * 4 + 4
        _p32(span, o, nv * 8)
        _p32(span, o + 4, fv * 8)
        _p32(span, o + 8, fv * 8)

    # vertex streams — per-part row copies; grown ranges repeat the part's last row so every
    # slot keeps finite, in-silhouette values until replace_part writes the real mesh
    for si, r_data in enumerate((r_s0, r_s1)):
        st = strides[si]
        soff = m["streams"][si][0]
        rows = np.frombuffer(b[soff:soff + m["nvtx"] * st], np.uint8).reshape(m["nvtx"], st)
        out = np.zeros((new_nvtx, st), np.uint8)
        for k in range(recs):
            ofi, oni, ofv, onv = old[k]
            nfi, nni, nfv, nnv = new[k]
            n = min(onv, nnv)
            out[nfv:nfv + n] = rows[ofv:ofv + n]
            if nnv > n and n:
                out[nfv + n:nfv + nnv] = rows[ofv + n - 1]
            if gv[k] and nfv:
                out[nfv - gv[k]:nfv] = rows[ofv - gv[k]:ofv]
        span[r_data:r_data + new_nvtx * st] = out.tobytes()

    # append, retarget, zero the old span
    base = (len(b) + 15) & ~15
    nb = bytearray(b) + b"\x00" * (base - len(b)) + span
    for k in range(recs):
        fo = base + k * 48 + 0x1C
        _p32(nb, fo, info["remap_tgt"] - fo + 1)
    r_of = dict(table=0, ib0=r_ib0, meta=r_meta, desc=r_desc, sub=r_lastd)
    for o, kind, delta in info["refs"]:
        _p32(nb, o, base + r_of[kind] + delta - o + 1)
    nb[info["t"]:info["s_end"]] = b"\x00" * (info["s_end"] - info["t"])

    # the scan the launcher (and every reader in it) uses must see exactly the new model
    models = C.scan_models(bytes(nb), "respan")
    if len(models) != 1 or models[0]["table"] != base:
        raise RuntimeError(f"respan self-check failed: scan found "
                           f"{[hex(mm['table']) for mm in models]}, expected [{hex(base)}]")
    mm = models[0]
    if (mm["nvtx"] != new_nvtx or mm["nidx"] != new_nidx or mm["ib0"] != base + r_ib0
            or mm["streams"][0][0] != base + r_s0 or mm["streams"][1][0] != base + r_s1
            or mm["xform"] != m["xform"]):
        raise RuntimeError("respan self-check failed: relocated layout does not scan back")
    log(f"  mesh span relocated to 0x{base:x} ({m['nvtx']:,} -> {new_nvtx:,} verts, "
        f"{m['nidx']:,} -> {new_nidx:,} indices; blob {len(b):,} -> {len(nb):,} B)")
    return bytes(nb)


# ───────────────────────────── container rebuild ─────────────────────────────
def build_head_container(res: bytes, new_dram: bytes, log=print) -> bytes:
    """A head .iff whose blob 0 decodes to `new_dram` — blob 1 (the texture VRAM) verbatim.

    FOOTER note: the third header section's +0x1C stays untouched. It is an absolute DRAM
    offset to a footer that sat 0x2a0 before the END of every shipped blob 0; appending the
    relocated span past it moves the end, not the footer, so the stored offset stays right.
    """
    from . import decode_e4837_fixed as DF
    from . import encode_e4837_lazy as EE
    hdr = _u32(res, 4)
    blobs = AT._walk_blobs(res, len(res))
    if len(blobs) != 2 or blobs[0]["off"] != hdr or blobs[0]["dec"] is None:
        raise ValueError("head container: expected DRAM + VRAM blobs right after the header")
    b0, b1 = blobs
    if _u32(res, 0x2C) != len(b0["dec"]) or _u32(res, 0x34) != hdr or _u32(res, 0x38) != b0["tot"]:
        raise ValueError("head container: section 0 does not describe blob 0 as expected")
    if _u32(res, 0x54) != b1["off"]:
        raise ValueError("head container: section 1 does not point at blob 1 as expected")

    enc = EE.encode_payload(new_dram, wparam=b0["wp"], codec=b0["codec"])
    back = DF.decompress_codec(enc[20:_u32(enc, 8)], len(new_dram),
                               (1 << b0["wp"]) - 1, b0["wp"])
    if bytes(back) != bytes(new_dram):
        raise RuntimeError("head container: re-encoded blob 0 failed its round trip")

    out = bytearray(res[:hdr] + enc + res[b1["off"]:])
    _p32(out, 8, len(out))                     # file total
    _p32(out, 0x2C, len(new_dram))             # sect 0: decoded size
    _p32(out, 0x38, len(enc))                  # sect 0: packed size
    _p32(out, 0x54, hdr + len(enc))            # sect 1: blob 1 moved
    log(f"  container rebuilt: dram {len(b0['dec']):,} -> {len(new_dram):,} B, "
        f"file {len(res):,} -> {len(out):,} B")
    return bytes(out)


# ───────────────────────────── mesh -> blob ─────────────────────────────
def apply_mesh_to_blob(b: bytes, mesh: dict, log=print, collapse_others=True):
    """The whole custom head, written into blob 0: the imported mesh takes the biggest part
    (the face skin — its material carries the colour/normal/occlusion maps), every other part
    is folded away, exactly like a removed facial-hair shell. -> (new blob, main record).

    The strip stream is computed HERE and sized exactly, so the respan budget and what
    replace_part writes can never disagree.
    """
    from . import facial_hair as FH
    m = C.scan_models(bytes(b), "custom-head")[0]
    parts = C.submeshes(b, m)
    main = max(parts, key=lambda p: p["n_vtx"])
    P = np.asarray(mesh["pos"], np.float64)
    T = np.asarray(mesh["tris"], np.int64).reshape(-1, 3)
    if not len(T) or T.min() < 0 or T.max() >= len(P):
        raise ValueError("imported mesh: triangle indices out of range")
    stream = AM._strip_stream(AM._stripify(T))
    nv, ni = len(P), max(len(stream), 3)

    if nv <= main["n_vtx"] and ni <= main["n_idx"]:
        log(f"  custom mesh fits part {main['rec']} in place "
            f"({nv:,}/{main['n_vtx']:,} verts, {ni:,}/{main['n_idx']:,} indices)")
        nb = bytearray(b)
    else:
        log(f"  part {main['rec']} needs {nv:,} verts / {ni:,} indices "
            f"(slot holds {main['n_vtx']:,} / {main['n_idx']:,})")
        nb = bytearray(respan(b, m, {main["rec"]: (nv, ni)}, log))
    m2 = C.scan_models(bytes(nb), "custom-head")[0]
    M2 = C.read_model(bytes(nb), m2)
    by_rec = {p["rec"]: p for p in M2["parts"]}

    mesh2 = dict(pos=P, uv=np.asarray(mesh["uv"], np.float64), tris=T, strip=stream)
    if mesh.get("nrm") is not None and len(mesh["nrm"]) == nv:
        mesh2["nrm"] = np.asarray(mesh["nrm"], np.float64)
    log("  " + C.replace_part(nb, m2, by_rec[main["rec"]], mesh2, log=log))

    if collapse_others:
        folded = 0
        for p in M2["parts"]:
            if p["rec"] == main["rec"] or not p["n_vtx"] or not len(p["tris_idx"]):
                continue
            C.replace_part(nb, m2, p, FH.collapse_mesh(M2, p), log=lambda *_: None)
            folded += 1
        log(f"  {folded} other parts folded away — the imported mesh IS the head")
    return bytes(nb), main["rec"]


# ───────────────────────────── fit + orient ─────────────────────────────
def orient(mesh: dict, flip180=False, zup=False) -> dict:
    """Axis fixes applied before the fit: `zup` turns a Z-up export into the game's Y-up,
    `flip180` spins the head about Y (glTF characters usually face +Z; the game faces -Z)."""
    P = np.asarray(mesh["pos"], np.float64).copy()
    N = None if mesh.get("nrm") is None else np.asarray(mesh["nrm"], np.float64).copy()
    for A in ([P] if N is None else [P, N]):
        if zup:
            A[:, 1], A[:, 2] = A[:, 2].copy(), -A[:, 1].copy()
        if flip180:
            A[:, 0], A[:, 2] = -A[:, 0], -A[:, 2]
    out = dict(mesh, pos=P)
    if N is not None:
        out["nrm"] = N
    return out


def fit_to_head(mesh: dict, b: bytes, m: dict, log=print) -> dict:
    """Uniform-scale + translate the import onto the base head so it lands inside the
    position packing range: head height (Y extent of the biggest part) sets the scale,
    bounding-box centres align. A mesh already inside 2% of the base box — a re-import of
    an export — is left untouched, so a round trip stays exact."""
    parts = C.submeshes(b, m)
    main = max(parts, key=lambda p: p["n_vtx"])
    lo, hi = main["first_vtx"], main["first_vtx"] + main["n_vtx"]
    base = C.read_model(bytes(b), m)["pos"][lo:hi].astype(np.float64)
    P = np.asarray(mesh["pos"], np.float64)
    b_lo, b_hi = base.min(0), base.max(0)
    p_lo, p_hi = P.min(0), P.max(0)
    b_ext, p_ext = b_hi - b_lo, p_hi - p_lo
    tol = 0.02 * max(float(b_ext.max()), 1e-6)
    if (np.abs(p_lo - b_lo) < tol).all() and (np.abs(p_hi - b_hi) < tol).all():
        log("  mesh already sits on the base head's box — no fit applied")
        return mesh
    if p_ext[1] < 1e-9:
        raise ValueError("imported mesh is flat — cannot fit it to the head")
    s = float(b_ext[1] / p_ext[1])
    P2 = (P - (p_lo + p_hi) / 2.0) * s + (b_lo + b_hi) / 2.0
    log(f"  fitted to the head: scale x{s:.4g}, centre moved to the base head's box")
    return dict(mesh, pos=P2)


# ───────────────────────────── model readers ─────────────────────────────
def read_mesh(path, log=print):
    """-> (mesh dict: pos/uv/tris(+nrm), embedded images {color/normal/occlusion: PIL} — GLB
    is the only format that can carry its textures along)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".obj":
        mesh, imgs = _merge_groups(C.read_obj_groups(p)), {}
    elif ext in (".glb", ".gltf"):
        mesh, imgs = _read_glb(p, log)
    elif ext == ".fbx":
        mesh, imgs = _read_fbx(p, log), {}
    else:
        raise ValueError(f"{p.name}: not a model format this importer reads "
                         "(.obj, .glb/.gltf, or binary .fbx)")
    n_bad = int((~np.isfinite(np.asarray(mesh["pos"], np.float64))).sum())
    if n_bad:
        raise ValueError(f"{p.name}: {n_bad} non-finite coordinates")
    log(f"  {p.name}: {len(mesh['pos']):,} vertices, {len(mesh['tris']):,} triangles"
        + (f", textures: {'/'.join(sorted(imgs))}" if imgs else ""))
    return mesh, imgs


def _merge_groups(groups: dict) -> dict:
    """One mesh out of an OBJ's groups (read_obj_groups already flipped V)."""
    Ps, UVs, Ns, Ts, base = [], [], [], [], 0
    have_n = all(g.get("nrm") is not None for g in groups.values())
    for g in groups.values():
        Ps.append(np.asarray(g["pos"], np.float64))
        UVs.append(np.asarray(g["uv"], np.float64))
        if have_n:
            Ns.append(np.asarray(g["nrm"], np.float64))
        Ts.append(np.asarray(g["tris"], np.int64).reshape(-1, 3) + base)
        base += len(g["pos"])
    out = dict(pos=np.vstack(Ps), uv=np.vstack(UVs), tris=np.vstack(Ts))
    if have_n:
        out["nrm"] = np.vstack(Ns)
    return out


def _read_glb(path, log=print):
    """Self-contained glTF reader: triangle primitives, world transforms applied, the largest
    primitive's material images pulled out. glTF UVs are already top-left like the decoded
    game maps, so V is NOT flipped here."""
    import base64
    import io
    import json
    raw = Path(path).read_bytes()
    bin_ = None
    if raw[:4] == b"glTF":
        o, gltf = 12, None
        while o + 8 <= len(raw):
            ln, ty = struct.unpack_from("<II", raw, o)
            o += 8
            data = raw[o:o + ln]
            o += ln
            if ty == 0x4E4F534A:
                gltf = json.loads(data.decode("utf-8"))
            elif ty == 0x004E4942:
                bin_ = data
        if gltf is None:
            raise ValueError("GLB: no JSON chunk")
    else:
        gltf = json.loads(raw.decode("utf-8"))

    bufs = []
    for bu in gltf.get("buffers", []):
        uri = bu.get("uri")
        if uri is None:
            bufs.append(bin_ or b"")
        elif uri.startswith("data:"):
            bufs.append(base64.b64decode(uri.split(",", 1)[1]))
        else:
            side = Path(path).parent / uri
            if not side.exists():
                raise ValueError(f"glTF: external buffer {uri} not found next to the file")
            bufs.append(side.read_bytes())

    def acc(i):
        a = gltf["accessors"][i]
        bv = gltf["bufferViews"][a["bufferView"]]
        comp = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2",
                5125: "u4", 5126: "f4"}[a["componentType"]]
        dim = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[a["type"]]
        item = int(comp[1]) * dim
        off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        stride = bv.get("byteStride") or item
        flat = np.frombuffer(bufs[bv.get("buffer", 0)], np.uint8)
        idx = (off + stride * np.arange(a["count"]))[:, None] + np.arange(item)[None, :]
        return np.frombuffer(flat[idx.ravel()].tobytes(), "<" + comp).reshape(a["count"], dim)

    def nmat(nd):
        if "matrix" in nd:
            return np.array(nd["matrix"], np.float64).reshape(4, 4).T   # column-major
        x, y, z, w = nd.get("rotation", [0.0, 0.0, 0.0, 1.0])
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        M = np.eye(4)
        M[:3, :3] = R @ np.diag(nd.get("scale", [1.0, 1.0, 1.0]))
        M[:3, 3] = nd.get("translation", [0.0, 0.0, 0.0])
        return M

    prims = []
    def walk(i, W):
        nd = gltf["nodes"][i]
        W2 = W @ nmat(nd)
        if "mesh" in nd:
            for pr in gltf["meshes"][nd["mesh"]]["primitives"]:
                prims.append((W2, pr))
        for c in nd.get("children", []):
            walk(c, W2)
    if gltf.get("scenes"):
        for i in gltf["scenes"][gltf.get("scene", 0)].get("nodes", []):
            walk(i, np.eye(4))
    if not prims:
        for me in gltf.get("meshes", []):
            for pr in me["primitives"]:
                prims.append((np.eye(4), pr))
    if not prims:
        raise ValueError("glTF: no mesh primitives")

    Ps, UVs, Ns, Ts, base = [], [], [], [], 0
    best_n, best_mat = -1, None
    all_n = True
    for W, pr in prims:
        if pr.get("mode", 4) != 4:
            log("  glTF: skipped a non-triangle primitive")
            continue
        at = pr["attributes"]
        P = acc(at["POSITION"]).astype(np.float64) @ W[:3, :3].T + W[:3, 3]
        uv = (acc(at["TEXCOORD_0"]).astype(np.float64)[:, :2] if "TEXCOORD_0" in at
              else np.zeros((len(P), 2)))
        if "NORMAL" in at:
            N = acc(at["NORMAL"]).astype(np.float64) @ np.linalg.inv(W[:3, :3])
            L = np.linalg.norm(N, axis=1, keepdims=True)
            Ns.append(np.where(L > 1e-9, N / np.maximum(L, 1e-9), 0.0))
        else:
            all_n = False
        T = (acc(pr["indices"]).astype(np.int64).reshape(-1, 3) if "indices" in pr
             else np.arange(len(P), dtype=np.int64).reshape(-1, 3))
        if len(T) > best_n:
            best_n, best_mat = len(T), pr.get("material")
        Ps.append(P)
        UVs.append(uv)
        Ts.append(T + base)
        base += len(P)
    if not Ps:
        raise ValueError("glTF: no triangle primitives")
    mesh = dict(pos=np.vstack(Ps), uv=np.vstack(UVs), tris=np.vstack(Ts))
    if all_n and Ns:
        mesh["nrm"] = np.vstack(Ns)

    imgs = {}
    if best_mat is not None and "materials" in gltf:
        def img_of(ti):
            if not ti:
                return None
            src = gltf["textures"][ti["index"]].get("source")
            if src is None:
                return None
            im = gltf["images"][src]
            if "bufferView" in im:
                bv = gltf["bufferViews"][im["bufferView"]]
                o = bv.get("byteOffset", 0)
                data = bufs[bv.get("buffer", 0)][o:o + bv["byteLength"]]
            elif im.get("uri", "").startswith("data:"):
                data = base64.b64decode(im["uri"].split(",", 1)[1])
            elif "uri" in im and (Path(path).parent / im["uri"]).exists():
                data = (Path(path).parent / im["uri"]).read_bytes()
            else:
                return None
            from PIL import Image
            try:
                return Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:
                return None
        mat = gltf["materials"][best_mat]
        for key, ti in (("color", mat.get("pbrMetallicRoughness", {}).get("baseColorTexture")),
                        ("normal", mat.get("normalTexture")),
                        ("occlusion", mat.get("occlusionTexture"))):
            im = img_of(ti)
            if im is not None:
                imgs[key] = im
    return mesh, imgs


def _read_fbx(path, log=print):
    """Binary FBX 7.x reader: control points + per-corner UV/normal layers, fan-triangulated
    and welded back to an indexed mesh. FBX V runs bottom-up like OBJ, so it is flipped.
    Object transforms are NOT applied (the fit step normalises placement anyway)."""
    raw = Path(path).read_bytes()
    if not raw.startswith(b"Kaydara FBX Binary"):
        raise ValueError(f"{Path(path).name}: only BINARY FBX is supported — re-export as "
                         "binary FBX, or use .glb / .obj")
    ver = struct.unpack_from("<I", raw, 23)[0]
    big = ver >= 7500
    SEN = 25 if big else 13

    def node(o):
        if big:
            end, np_, pl = struct.unpack_from("<QQQ", raw, o)
            nl, p = raw[o + 24], o + 25
        else:
            end, np_, pl = struct.unpack_from("<III", raw, o)
            nl, p = raw[o + 12], o + 13
        if end == 0:
            return None, o + SEN
        name = raw[p:p + nl].decode("latin1")
        p += nl
        props, po = [], p
        for _ in range(np_):
            t = chr(raw[po])
            po += 1
            if t in "YCIFDL":
                fmt, sz = {"Y": ("<h", 2), "C": ("<b", 1), "I": ("<i", 4),
                           "F": ("<f", 4), "D": ("<d", 8), "L": ("<q", 8)}[t]
                props.append(struct.unpack_from(fmt, raw, po)[0])
                po += sz
            elif t in "fdlib":
                n, enc, cl = struct.unpack_from("<III", raw, po)
                po += 12
                data = raw[po:po + cl]
                po += cl
                if enc:
                    data = zlib.decompress(data)
                props.append(np.frombuffer(
                    data, {"f": "<f4", "d": "<f8", "l": "<i8", "i": "<i4", "b": "u1"}[t]))
            elif t in "SR":
                n = struct.unpack_from("<I", raw, po)[0]
                po += 4
                s = raw[po:po + n]
                po += n
                props.append(s.decode("latin1", "replace") if t == "S" else s)
            else:
                raise ValueError(f"FBX: unknown property type {t!r}")
        kids, co = [], p + pl
        while co < end:
            if raw[co:co + SEN] == b"\x00" * SEN:
                co += SEN
                break
            ch, co = node(co)
            if ch is None:
                break
            kids.append(ch)
        return dict(name=name, props=props, kids=kids), end

    roots, o = [], 27
    while o + SEN <= len(raw):
        nd, o = node(o)
        if nd is None:
            break
        roots.append(nd)

    def find(kids, name):
        return [k for k in kids if k["name"] == name]

    objs = find(roots, "Objects")
    geoms = [g for o_ in objs for g in find(o_["kids"], "Geometry")
             if find(g["kids"], "Vertices")]
    if not geoms:
        raise ValueError("FBX: no mesh geometry found")
    if len(geoms) > 1:
        log(f"  FBX: {len(geoms)} meshes — merging them as placed (no transforms)")

    Ps, UVs, Ns, Ts, base = [], [], [], [], 0
    have_uv = have_n = True
    for g in geoms:
        V = np.asarray(find(g["kids"], "Vertices")[0]["props"][0],
                       np.float64).reshape(-1, 3)
        pvi = np.asarray(find(g["kids"], "PolygonVertexIndex")[0]["props"][0], np.int64)
        corners = np.where(pvi < 0, ~pvi, pvi)

        def layer(kind, arr_name, idx_name):
            Ls = find(g["kids"], kind)
            if not Ls:
                return None
            L = Ls[0]
            arr = np.asarray(find(L["kids"], arr_name)[0]["props"][0], np.float64)
            arr = arr.reshape(-1, 2 if arr_name == "UV" else 3)
            mp = (find(L["kids"], "MappingInformationType") or [dict(props=[""])])[0]["props"][0]
            rf = (find(L["kids"], "ReferenceInformationType") or [dict(props=[""])])[0]["props"][0]
            IX = find(L["kids"], idx_name)
            ix = np.asarray(IX[0]["props"][0], np.int64) if IX else None
            if mp == "ByPolygonVertex":
                per = arr[ix] if (rf == "IndexToDirect" and ix is not None) else arr
                if len(per) < len(corners):
                    return None
                return per[:len(corners)]
            if mp in ("ByControlPoint", "ByVertice", "ByVertex"):
                per = arr[ix] if (rf == "IndexToDirect" and ix is not None) else arr
                return per[corners]
            if mp == "AllSame":
                return np.repeat(arr[:1], len(corners), 0)
            return None
        uv_c = layer("LayerElementUV", "UV", "UVIndex")
        n_c = layer("LayerElementNormal", "Normals", "NormalsIndex")
        have_uv &= uv_c is not None
        have_n &= n_c is not None

        # weld corners that agree on vertex + uv + normal back into an indexed mesh
        key = corners.astype(np.float64)[:, None]
        if uv_c is not None:
            key = np.hstack([key, np.round(uv_c * 1e5)])
        if n_c is not None:
            key = np.hstack([key, np.round(n_c * 1e3)])
        _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
        P = V[corners[first]]
        uvg = uv_c[first] if uv_c is not None else np.zeros((len(P), 2))
        ng = n_c[first] if n_c is not None else None

        tris, s = [], 0
        for e in np.nonzero(pvi < 0)[0]:
            for k in range(s + 1, e):
                tris.append((inv[s], inv[k], inv[k + 1]))
            s = e + 1
        Ps.append(P)
        UVs.append(uvg)
        if ng is not None:
            Ns.append(ng)
        Ts.append(np.asarray(tris, np.int64).reshape(-1, 3) + base)
        base += len(P)

    uv = np.vstack(UVs)
    if have_uv:
        uv[:, 1] = 1.0 - uv[:, 1]
    mesh = dict(pos=np.vstack(Ps), uv=uv, tris=np.vstack(Ts))
    if have_n and Ns:
        mesh["nrm"] = np.vstack(Ns)
    return mesh


# ───────────────────────────── install paths ─────────────────────────────
def install_custom_head(game_dir, head_id: int, mesh: dict, maps=None, log=print):
    """The imported mesh into head `head_id`'s game files — in place when it fits, grown and
    relocated when it doesn't. Always starts from the pristine asset so installs never stack."""
    from . import face_builder as FB
    game_dir = Path(game_dir)
    name = C.HEAD_FMT.format(head_id)
    AT.ensure_clean(name, game_dir, log=log)
    b = C.blob(True, game_dir, name)
    nb, _ = apply_mesh_to_blob(b, mesh, log=log)
    if len(nb) == len(b):
        C.write(nb, game_dir, log=log, asset=name)
    else:
        arc, off, size, idx, _f = AT.resolve(name, game_dir)
        res = open(game_dir / arc, "rb").read()[off:off + size]
        new_res = build_head_container(res, nb, log=log)
        AT._relocate(name, new_res, idx, game_dir, 0, 0, "DXT4_5", log)
        ensure_head_headroom(game_dir, head_id, log=log)
    if maps:
        FB.install(head_id, maps, game_dir, log=log)
    log(f"  head {head_id:04d}: custom model installed")


def add_head_slot(game_dir, new_id: int, src_id: int, mesh: dict, maps=None, log=print):
    """A brand-new head asset built from `src_id`'s CURRENT container with the imported mesh
    inside — so a bigger head never has to squeeze into an existing slot. The caller binds it
    to a player afterwards (roster record +0xB2, i.e. table.set_head)."""
    from . import customasset as CA
    from . import face_builder as FB
    game_dir = Path(game_dir)
    new_name = C.HEAD_FMT.format(new_id)
    if AT.resolve(new_name, game_dir) is not None:
        raise ValueError(f"head id {new_id} already exists")
    src_name = C.HEAD_FMT.format(src_id)
    arc, off, size, idx, _f = AT.resolve(src_name, game_dir)
    res = open(game_dir / arc, "rb").read()[off:off + size]
    b = C.blob(True, game_dir, src_name)
    nb, _ = apply_mesh_to_blob(b, mesh, log=log)
    new_res = build_head_container(res, nb, log=log) if len(nb) != len(b) else _swap_dram(res, nb)
    CA.add_custom_asset(game_dir, new_name, new_res, log=log, flags_from=src_name)
    ensure_head_headroom(game_dir, new_id, log=log)
    if maps:
        FB.install(new_id, maps, game_dir, log=log)
    log(f"  head {new_id:04d}: added as a new asset (bind it to a player and save the roster)")


def _swap_dram(res: bytes, new_dram: bytes) -> bytes:
    """Same-size dram into a container copy (the add-slot path can't use char_model.write,
    which writes to the archive in place)."""
    return build_head_container(res, new_dram, log=lambda *_: None)


def next_free_head_id(game_dir) -> int:
    """First unused player_head id after the shipped range (head ids must stay under 9000 —
    the id is a u16 the roster stores at +0xB2, and the asset namespace above 9000 collides
    with other player_head-adjacent keys)."""
    used = set(C.head_ids(game_dir))
    hid = max(used) + 1 if used else 8000
    while hid in used:
        hid += 1
    if hid >= 9000:
        for hid in range(8999, 0, -1):
            if hid not in used:
                break
        else:
            raise ValueError("no free head id under 9000")
    return hid


def ensure_head_headroom(game_dir, custom_id: int, log=print):
    """streaming_pool's ballast trick, head-shaped: the engine sizes its streaming pool off
    the biggest asset its sizing pass stats. A roster-bound custom head is very likely walked
    (the head sizing pass follows the roster), but insurance is one padded file: keep the
    biggest SHIPPED head at least as big as the custom head, zero-padded past its declared
    content (the container's u32 total at +8 makes the pad self-describing)."""
    from . import streaming_pool as SP
    game_dir = Path(game_dir)
    loc = AT.resolve(C.HEAD_FMT.format(custom_id), game_dir)
    if loc is None:
        return
    need = loc[2]
    best_id, best_sz = None, -1
    for i in C.head_ids(game_dir):
        if i == custom_id:
            continue
        try:
            if AT.resolve(C.HEAD_FMT.format(i), game_dir, clean=True) is None:
                continue                                   # not a shipped head
        except Exception:
            pass                                           # no pristine TOC yet: accept any
        l2 = AT.resolve(C.HEAD_FMT.format(i), game_dir)
        if l2 and l2[2] > best_sz:
            best_id, best_sz = i, l2[2]
    if best_id is None or best_sz >= need:
        return
    tgt = SP._target_size(need)
    name = C.HEAD_FMT.format(best_id)
    arc, off, size, idx, _f = AT.resolve(name, game_dir)
    data = open(game_dir / arc, "rb").read()[off:off + size]
    log(f"  head streaming headroom: padding {name} {size:,} -> {tgt:,} B "
        f"(ceiling for the {need:,} B custom head)")
    AT._relocate(name, data + b"\x00" * (tgt - len(data)), idx, game_dir, 0, 0, "DXT4_5", log)
