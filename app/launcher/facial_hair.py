"""facial_hair.py — the beard/moustache/scalp shells that sit on top of a head asset.

WHAT THEY ARE
    A head asset (player_head_id_NNNN.iff) is not one mesh. Its submesh table carries, besides
    the face island (material 0) and the eyes/brows/lashes/mouth, up to four extra shells —
    thin alpha-cut cards of hair geometry. They are selected by the submesh record's `+0x24`
    field, which is a selection BITMASK, not a LOD level: one bit per slot, and the members of
    a slot are alternatives the game picks between.

        bit    role            heads   materials   vertices
        2048   facial hair      440     5           335
        4096   facial hair      440     5 or 6      335   (mat 6 variants 282-412)
        8192   facial hair      440     5 or 7      528
       16384   SCALP hair       122     6 or 8      282-412

    Measured over all 447 head assets (6,359 parts). Four per-head patterns account for all
    440: 171x all-mat-5, 147x mat 5/6/7, 82x all-mat-5 plus a mat-6 scalp, 40x mat 5/6/7 plus a
    mat-8 scalp. Seven heads carry no shell at all. Rendering the three facial slots separately
    on head 3040 shows 2048 and 4096 are near-identical full beards and 8192 is a bushier one —
    three variants of the same beard, not moustache/goatee/cheeks.

WHY THIS MODULE EXISTS
    The face builder paints the mat-0 texture and reshapes the mesh, and ignored these shells
    entirely. So a clean-shaven reference photo projected onto a bearded base head still came
    out bearded: the beard is GEOMETRY, and no amount of texture work removes it. That is the
    gap this closes — the shell is now something the build decides, not something inherited.

TWO MEASURED FACTS DECIDE WHAT IS POSSIBLE
    1. All 1,320 facial shells unwrap over the SAME full 0..1 sheet — only two distinct rounded
       UV boxes across the lot, and the sheet lives outside the head .iff. So moving a shell
       between heads is a purely GEOMETRIC operation; the target head textures a foreign shell
       correctly with no texture work at all.
    2. There is ZERO headroom. Every slot is exactly full (335 vertices into a 335 slot, 528
       into 528) because char_model.replace_part is size-preserving by construction. So a
       transplant must be like-for-like slot (2048 -> 2048, 8192 -> 8192) — which works,
       because the slot sizes are consistent across heads.

    Geometry clustering found 910 distinct shapes among the 946 mat-5 parts: the shells are
    sculpted per head, not drawn from a small reusable library. So the picker is "any of ~440
    heads x 3 slots", not "pick one of N styles".

HOW A SHELL IS MOVED
    The shells are sculpted to their own head, so a raw copy clips through a differently
    shaped jaw. The warp uses the face island as its correspondence: every head stores the
    SAME UV unwrap (sorted-UV delta between any two heads is exactly 0.00000) but in a
    DIFFERENT vertex order, so matching on UV gives an exact 1,397-point correspondence
    between any two heads for free — no landmark detection, no texture, no mediapipe. Feed
    that pair into face_shape.displace and the shell lands in the target's proportions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from . import char_model as C

# Selection bits in the submesh record's +0x24 field. See the table above.
FACIAL_BITS = (2048, 4096, 8192)
SCALP_BIT = 16384
HAIR_BITS = FACIAL_BITS + (SCALP_BIT,)

SLOT_NAME = {2048: "beard A", 4096: "beard B", 8192: "beard C (full)", SCALP_BIT: "scalp hair"}

# The catalogue is a two-minute scan of 447 assets, so it is cached. It keys on the asset set,
# not on file contents: these are shipped assets and the launcher never rewrites the shells of a
# head it is not editing.
CACHE_VERSION = 3          # 3 added n_idx/slot_idx — compatible() cannot be answered without them


def _cache_path() -> Path:
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or ""
    base = Path(appdata) / "NHL2K10 Mod Launcher" if appdata else Path.home()
    base.mkdir(parents=True, exist_ok=True)
    return base / "facial_hair_catalogue.json"


# ───────────────────────────── reading a head ─────────────────────────────
_HEADS: dict = {}


def load_head(head_id: int, game_dir=None, current: bool = False) -> tuple:
    """(blob, model record, decoded mesh) for one head asset. Cached per (id, current)."""
    key = (int(head_id), bool(current), str(game_dir))
    hit = _HEADS.get(key)
    if hit is not None:
        return hit
    asset = C.HEAD_FMT.format(int(head_id))
    b = C.blob(current, game_dir, asset)
    m = C.scan_models(b, asset)[0]
    M = C.read_model(b, m)
    _HEADS[key] = (b, m, M)
    return b, m, M


def hair_slots(M: dict) -> dict:
    """{selection bit: submesh part} for every hair shell this head carries."""
    out = {}
    for p in M["parts"]:
        if p["lod"] in HAIR_BITS and len(p["tris_idx"]):
            out[int(p["lod"])] = p
    return out


def shell(head_id: int, bit: int, game_dir=None) -> dict | None:
    """One shell as a standalone mesh — positions in cm, UVs on the shared sheet, local tris."""
    b, m, M = load_head(head_id, game_dir)
    p = hair_slots(M).get(int(bit))
    if p is None:
        return None
    lo, hi = p["first_vtx"], p["first_vtx"] + p["n_vtx"]
    # Carry the artist's own strip stream, not just the triangles. A generic stripifier packs
    # these shells 10% looser than the artist did, and the destination slot is exactly the size
    # the artist's packing needed — so a re-stripped transplant does not fit, while the
    # original stream does. Part-local, trailing padding dropped, restarts kept.
    ib = np.frombuffer(bytes(b[m["ib0"]:m["ib0"] + m["nidx"] * 2]), ">u2")
    run = ib[p["first_idx"]:p["first_idx"] + p["n_idx"]].astype(np.int64)
    keep = np.nonzero(run != 0xFFFF)[0]
    run = run[:keep[-1] + 1] if len(keep) else run[:0]
    return dict(pos=M["pos"][lo:hi].astype(np.float64),
                uv=M["uv"][lo:hi].astype(np.float64),
                nrm=M["nrm"][lo:hi].astype(np.float64),
                tris=p["tris_idx"].reshape(-1, 3).astype(np.int64) - lo,
                strip=np.where(run == 0xFFFF, 0xFFFF, run - lo),
                head=int(head_id), bit=int(bit), mat=int(p["mat"]), rec=p["rec"])


# ───────────────────────────── the catalogue ─────────────────────────────
def catalogue(game_dir=None, refresh: bool = False, log=None) -> list[dict]:
    """Every hair shell in the game: [{head, bit, mat, n_vtx, n_tri, size, drop}].

    `size` is the shell's bounding box in cm and `drop` how far below the chin line it hangs —
    together they are enough to sort a picker by "how much beard" without loading 447 assets.
    """
    ids = C.head_ids(game_dir)
    cache = _cache_path()
    if not refresh and cache.exists():
        try:
            got = json.loads(cache.read_text())
            if got.get("version") == CACHE_VERSION and got.get("heads") == len(ids):
                return got["rows"]
        except Exception:
            pass

    rows = []
    for i, hid in enumerate(ids):
        try:
            _b, _m, M = load_head(hid, game_dir)
        except Exception:
            continue
        face = [p for p in M["parts"] if p["mat"] == 0]
        chin = float(M["pos"][face[0]["first_vtx"]:
                              face[0]["first_vtx"] + face[0]["n_vtx"], 1].min()) if face else 0.0
        for bit, p in sorted(hair_slots(M).items()):
            lo, hi = p["first_vtx"], p["first_vtx"] + p["n_vtx"]
            V = M["pos"][lo:hi]
            s = shell(hid, bit, game_dir)
            rows.append(dict(head=int(hid), bit=int(bit), mat=int(p["mat"]),
                             n_vtx=int(p["n_vtx"]),
                             # what this shell COSTS (its own packed stream) and what its slot
                             # HOLDS — fits() needs both, and neither can be re-derived later
                             # without loading the asset again.
                             n_idx=int(len(s["strip"])), slot_idx=int(p["n_idx"]),
                             size=[round(float(x), 2) for x in (V.max(0) - V.min(0))],
                             drop=round(float(chin - V[:, 1].min()), 2)))
        _HEADS.pop((int(hid), False, str(game_dir)), None)      # 447 heads will not fit in RAM
        if log and i % 50 == 0:
            log(f"  scanning hair shells… {i}/{len(ids)}")
    try:
        cache.write_text(json.dumps(dict(version=CACHE_VERSION, heads=len(ids), rows=rows)))
    except Exception:
        pass
    return rows


# ───────────────────────────── moving a shell ─────────────────────────────
def _uv_key(uv):
    """The stored snorm16 halves — an exact key, because that is literally what is in the file."""
    return np.round(np.asarray(uv, np.float64) / 2.0 * 32767.0).astype(np.int64)


def face_correspondence(srcM: dict, dstM: dict, dst_pos=None) -> tuple:
    """Matched (source, destination) face-island points, paired by UV.

    Every head stores the same 1,397-vertex unwrap in its own vertex order, so a UV match is an
    exact correspondence — no interpolation, no landmark detection. Duplicate UVs (seam splits)
    are averaged so the pairing stays one-to-one.
    """
    def island(M, pos=None):
        p = next(x for x in M["parts"] if x["mat"] == 0)
        lo, hi = p["first_vtx"], p["first_vtx"] + p["n_vtx"]
        P = (np.asarray(pos, np.float64) if pos is not None else M["pos"])[lo:hi]
        K = _uv_key(M["uv"][lo:hi])
        acc = {}
        for k, v in zip(map(tuple, K), np.asarray(P, np.float64)):
            a = acc.setdefault(k, [np.zeros(3), 0])
            a[0] += v
            a[1] += 1
        return {k: v[0] / v[1] for k, v in acc.items()}

    A, B = island(srcM), island(dstM, dst_pos)
    keys = [k for k in A if k in B]
    if len(keys) < 64:
        raise ValueError(f"only {len(keys)} face vertices matched between these two heads — "
                         "they do not share the standard unwrap, so a shell cannot be retargeted")
    return (np.array([A[k] for k in keys]), np.array([B[k] for k in keys]))


def retarget(pos, srcM: dict, dstM: dict, dst_pos=None, sigma: float = 2.6,
             reach: float = 9.0) -> np.ndarray:
    """Warp a shell sculpted for `srcM` into the proportions of `dstM`.

    A raw copy clips: 335 vertices sculpted to one jaw sit inside or outside another by up to a
    centimetre. The face islands give the displacement field, and face_shape.displace carries
    it out to the shell with a compactly-supported RBF, so nothing past the head is disturbed.
    """
    from . import face_shape as FS
    src_lm, dst_lm = face_correspondence(srcM, dstM, dst_pos)
    P = np.asarray(pos, np.float64)
    return P + FS.displace(P, src_lm, dst_lm, sigma=sigma, reach=reach, node_d=1.2)


def collapse_mesh(M: dict, part: dict) -> dict:
    """This shell, with every vertex folded onto one point — i.e. drawn away to nothing.

    Why collapse rather than clear the index stream: the submesh record is never rewritten (see
    char_model.replace_part), so the slot must still hold a well-formed mesh. Folding all the
    vertices together makes every triangle zero-area, which produces no fragments, and it keeps
    the original topology so the artist's index strips are left exactly as they were. The point
    chosen is the shell's own centroid, which is inside the head — so nothing is left poking out
    even if some other code path draws it regardless.
    """
    lo, hi = part["first_vtx"], part["first_vtx"] + part["n_vtx"]
    V = np.asarray(M["pos"][lo:hi], np.float64)
    c = V.mean(0)
    return dict(pos=np.repeat(c[None, :], len(V), 0), uv=M["uv"][lo:hi].astype(np.float64),
                nrm=M["nrm"][lo:hi].astype(np.float64),
                tris=part["tris_idx"].reshape(-1, 3).astype(np.int64) - lo, ordered=True)


# ───────────────────────────── the build plan ─────────────────────────────
def plan(head_id: int, wanted: dict, game_dir=None, dst_pos=None, log=print) -> dict:
    """Turn a per-slot wish into the {submesh record: mesh} override write_shape consumes.

    `wanted` maps a selection bit to one of:
        None / False        drop it — this player is clean-shaven
        True / "keep"       leave the base head's own shell alone
        (head_id, bit)      transplant that head's shell into this slot

    Slots the caller does not mention are left alone. A transplant is like-for-like by
    convention (the source bit should be the destination bit) because the slots are sized per
    role: 335 vertices for 2048/4096 and 528 for 8192, with no headroom anywhere.
    """
    _b, _m, M = load_head(head_id, game_dir)
    slots = hair_slots(M)
    out = {}
    for bit, want in (wanted or {}).items():
        bit = int(bit)
        p = slots.get(bit)
        if p is None:
            if want not in (None, False, "keep", True):
                log(f"  head {head_id} has no slot {bit} ({SLOT_NAME.get(bit, '?')}) — skipped")
            continue
        if want in (True, "keep"):
            continue
        if want in (None, False):
            out[p["rec"]] = collapse_mesh(M, p)
            log(f"  {SLOT_NAME.get(bit, bit)}: removed ({p['n_vtx']} vertices collapsed)")
            continue
        src_head, src_bit = (want if isinstance(want, (tuple, list)) else (want, bit))
        src = shell(int(src_head), int(src_bit), game_dir)
        if src is None:
            log(f"  head {src_head} has no slot {src_bit} — {SLOT_NAME.get(bit, bit)} left alone")
            continue
        bud = C.part_budget(p)
        if len(src["pos"]) > bud["max_verts"]:
            log(f"  {SLOT_NAME.get(bit, bit)}: head {src_head}'s shell is {len(src['pos'])} "
                f"vertices and this slot holds {bud['max_verts']} — left alone")
            continue
        _sb, _sm, sM = load_head(int(src_head), game_dir)
        P = retarget(src["pos"], sM, M, dst_pos)
        moved = float(np.linalg.norm(P - src["pos"], axis=1).mean())
        out[p["rec"]] = dict(pos=P, uv=src["uv"], nrm=None, tris=src["tris"],
                             strip=src["strip"])
        log(f"  {SLOT_NAME.get(bit, bit)}: head {src_head}'s shell fitted "
            f"({len(P)} vertices, moved {moved:.2f} cm on average)")
    return out


# ───────────────────────────── reading the references ─────────────────────────────
# mediapipe landmark rings. The beard region is the face oval below the nose base, with the lips
# cut out; the cheek reference sits above it, on the part of the cheek no beard reaches.
_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400,
         377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67,
         109]
_LIPS = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185]
_NOSE_BASE = [2, 94, 98, 327]
_CHEEK = [50, 205, 280, 425, 116, 345]


def measure(built: dict) -> dict:
    """How much facial hair do the reference photographs actually show?

    The build already de-lights the projection, so what is left over the jaw is pigment, not
    shadow — which is what makes a plain darkness comparison honest here. The beard region is
    compared against the player's OWN mid-cheek, so this is invariant to complexion: a dark-
    skinned clean-shaven player scores 0 the same as a pale one.

    Returns `resolved` (how much of the jaw the cameras actually saw — under about a third,
    nothing here should be believed), `darkness` (median Lab L below the cheek, in L units) and
    `coverage` (the fraction of the jaw that is clearly darker than the cheek). Coverage is the
    useful one: stubble is a large area a few units dark, a shadow is a small area a lot dark.
    """
    import cv2
    from . import face_builder as FB

    lm = np.asarray(built["landmarks"], np.float64)
    img = np.asarray(built["color"].convert("RGB") if hasattr(built["color"], "convert")
                     else built["color"], np.uint8)
    H, W = img.shape[:2]
    have = np.asarray(built.get("coverage", np.ones((H, W), np.float32)), np.float32)

    def poly(idx, scale=1.0):
        p = lm[idx, :2]
        c = p.mean(0)
        return np.round(c + (p - c) * scale).astype(np.int32)

    oval = np.zeros((H, W), np.uint8)
    cv2.fillPoly(oval, [poly(_OVAL)], 255)
    y_nose = float(lm[_NOSE_BASE, 1].mean())
    band = np.zeros((H, W), np.uint8)
    band[int(max(y_nose, 0)):, :] = 255
    lips = np.zeros((H, W), np.uint8)
    cv2.fillPoly(lips, [poly(_LIPS, 1.15)], 255)
    beard = (oval > 0) & (band > 0) & (lips == 0) & (have > 0.6)

    cheek = np.zeros((H, W), np.uint8)
    for i in _CHEEK:
        cv2.circle(cheek, tuple(np.round(lm[i, :2]).astype(int)), max(3, W // 60), 255, -1)
    cheek_m = (cheek > 0) & (have > 0.6)

    region = ((oval > 0) & (band > 0) & (lips == 0)).sum()
    resolved = float(beard.sum()) / max(int(region), 1)
    if beard.sum() < 300 or cheek_m.sum() < 100:
        return dict(resolved=resolved, darkness=0.0, coverage=0.0, verdict="unknown")

    L = FB._lab(img)[..., 0]
    ref = float(np.median(L[cheek_m]))
    d = ref - L[beard]
    darkness = float(np.median(d))
    coverage = float((d > 6.0).mean())

    # Thresholds measured on 149 of the shipped face textures — real faces, sorted by this
    # statistic and looked at. `darkness` separates them cleanly: 0-15 is clean-shaven, 16-19 is
    # a faint jaw shadow, 21-25 picks up light stubble and small goatees, 29-35 is unmistakable
    # stubble turning into beard, and 45-75 is a full beard on every one of them.
    #
    # `coverage` is NOT usable as the discriminator and is only reported: on a shipped map the
    # median clean-shaven player already scores 0.67, because the jaw is shaded and a 6 L test
    # cannot tell shading from pigment. Darkness survives that because a shadow is shallow.
    # Those numbers are off LIT maps, and this runs on the build's DE-LIT one, so they need
    # shifting down by the shading the de-lighting removes. Measured: the shipped maps' median
    # clean-shaven player scores 12, and the de-lit Makar build — clean-shaven with light
    # stubble — scores -3. So the scale sits about 15 L lower here, and 30-lit (stubble turning
    # into beard) lands near 15, 45-lit (unmistakable full beard) near 30.
    #
    # The clean/stubble line does not change what happens — both drop the shells, because none
    # of the three slots IS a stubble; stubble is a texture result and the projection already
    # paints it. Only the beard line has consequences, and it is set at the conservative end:
    # a missed beard leaves the base head's own shell alone, a false one shaves somebody.
    if resolved < 0.30:
        verdict = "unknown"
    elif darkness < 8.0:
        verdict = "clean"
    elif darkness < 30.0:
        verdict = "stubble"
    else:
        verdict = "beard"
    return dict(resolved=resolved, darkness=darkness, coverage=coverage, verdict=verdict)


def recommend(built: dict, head_id: int, game_dir=None, log=print) -> dict:
    """The `wanted` dict for plan(), decided from the photographs rather than inherited.

    The three facial slots are all full beards (measured: 2048 and 4096 are near-identical 335-
    vertex beards, 8192 a bushier 528-vertex one), so there is no shell that means "stubble".
    Stubble is therefore a TEXTURE result and the shells come off — which is right, because the
    projection paints the stubble it measured straight into the face map. Only a real beard gets
    geometry, and it keeps the base head's own shell, which is already sculpted to this head.
    """
    m = measure(built)
    slots = sorted(hair_slots(load_head(head_id, game_dir)[2]))
    facial = [b for b in slots if b in FACIAL_BITS]
    if m["verdict"] == "unknown":
        log(f"  facial hair: the cameras only reached {100 * m['resolved']:.0f}% of the jaw — "
            "leaving the base head's shells alone")
        return {}
    log(f"  facial hair: {m['verdict']} — the jaw reads {m['darkness']:.0f} L darker than the "
        f"cheek over {100 * m['coverage']:.0f}% of its area")
    if m["verdict"] in ("clean", "stubble"):
        return {b: None for b in facial}
    keep = 8192 if (8192 in facial and m["darkness"] > 40.0) else (facial[0] if facial else None)
    return {b: (True if b == keep else None) for b in facial}


def compatible(head_id: int, game_dir=None, rows=None, log=None) -> list[dict]:
    """The catalogue, cut down to the shells that will actually install on `head_id`.

    A transplant carries the source artist's own index stream (see `shell`), because a generic
    stripifier packs ~12.6% looser than the artist and the destination slot was sized to the
    artist's packing — re-stripping simply does not fit. That was measured; growing strips both
    ways and picking low-degree neighbours changed the packed length by exactly zero indices on
    72 shells, so this gap is not closable from our side.

    The consequence is that a shell only fits a destination whose slot is at least as roomy as
    the source's own packing, and across all head pairs that is a little under half of them.
    Rather than let the picker offer a beard that throws on install, drop the ones that cannot
    fit. Both budgets are arithmetic on catalogue fields (`n_vtx` ≤ the slot's vertex count,
    `n_idx` ≤ the slot's index count), so this costs no asset loads.
    """
    rows = catalogue(game_dir, log=log) if rows is None else rows
    dst = {r["bit"]: r for r in rows if r["head"] == int(head_id)}
    if not dst:                                   # head not in the catalogue — offer nothing
        return []
    out = [r for r in rows
           if r["bit"] in dst
           and r["n_vtx"] <= dst[r["bit"]]["n_vtx"]
           and r["n_idx"] <= dst[r["bit"]]["slot_idx"]]
    if log:
        pool = [r for r in rows if r["bit"] in dst]
        log(f"  {len(out)} of {len(pool)} shells fit head {head_id}'s slots "
            f"({len(pool) - len(out)} pack too loosely for it)")
    return out


def describe(head_id: int, game_dir=None) -> str:
    _b, _m, M = load_head(head_id, game_dir)
    s = hair_slots(M)
    if not s:
        return "no hair shells"
    return ", ".join(f"{SLOT_NAME.get(b, b)} (mat {p['mat']}, {p['n_vtx']} v)"
                     for b, p in sorted(s.items()))
