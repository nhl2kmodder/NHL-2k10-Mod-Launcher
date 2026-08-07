"""Character models — the skater, the goalie and the other 48 meshes in `global.iff`.

Everything the arena tools do for `arena_*.iff` (whole-model export, per-part export,
size-preserving replacement) but for the character geometry, which is stored in a different
format and all in ONE asset.

How the file is laid out (verified against captures — docs 22 and 24):

  global.iff blob 0        22,519,256 B, loads VERBATIM at guest = file_offset + 0x5C59380,
                           so editing it edits exactly what the GPU fetches. No repack on load.
  submesh table            N x 48 B, big-endian: +0x00 prim(=6 strip), +0x04 first index,
                           +0x08 index count, +0x0C tris(=count-2), +0x14 first vertex,
                           +0x18 vertex count, +0x20 material, +0x24 LOD bitmask.
  index block              immediately after the table: BE u16 strips, 0xFFFF restart.
  stream descriptors       after the index block, 0x18 B each:
                           [1][stride u32][bytes u32][rel_off u32][end u32][flags]
                           and the data starts at descriptor + 0x0B + rel_off. That one rule
                           resolves ALL 50 models, including the skater's two streams (a
                           stride-8 position stream plus a stride-32 attribute stream) — the
                           earlier "vertex buffer is at header + 0x22" shortcut only ever
                           worked because most models have a single stream.
  vertex data              BE SNORM16 x4 groups (`f = max(s16/32767, -1)`):
                           position, then normal+U, tangent+V, UV2, 4x UNORM8 skin weights
                           (summing to 255) and 4 bone slots (each pre-multiplied by 3).
  ModelPosScaleAndOffset   `pos = snorm * scale + offset`. Per model, ON DISK: the parameter
                           block keyed 46E6CB71 801F78B9 nearest BEFORE the vertex data holds
                           offset at +0x10 and scale at +0x20. So every model exports in real
                           units (about 1 unit = 1 cm) and re-imports exactly, with no capture.

Writing back reuses arena_model.write_dram: re-encode blob 0 with its own codec/window and pad
the packed bytes back to the original slot length, so blob 1 does not move and the TOC needs no
edit. Nothing here can change a submesh's vertex or index budget.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

from . import archive_textures as A          # noqa: F401  (imported for symmetry / callers)
from . import arena_model as AM

ASSET = "global.iff"

# the parameter block that carries ModelPosScaleAndOffset; 897 copies (one per material), so
# the one that applies to a model is the last one before its vertex data
_XFORM_KEY = bytes.fromhex("46E6CB71801F78B9")

# models identified from captures. Everything else is real geometry too, it just has no name
# yet — identifying one means capturing a frame that draws it (see doc 22).
KNOWN = {
    0x1323AF4: "Skater (player)",
    0x6456F0: "Goalie",
}

# The props are ONE-record tables, so the table walk in scan_models never starts on them: it
# needs three tiling records to tell a real table from a coincidence. They are real geometry
# all the same — the sticks are NOT part of the skater or goalie mesh, they are separate meshes
# the engine attaches to the hand at runtime, which is why the two sticks can differ and why
# neither rig has a stick bone. Identified by rendering them (and the skater stick matches draw
# 1386 of jersey_model_capture.rdc exactly: 270 vertices, 460 triangles after strip expansion).
PROPS = {
    0x0097CF90: "Stick (skater)",
    0x0097FBA0: "Stick (goalie)",
    0x009822D0: "Stick (skater, low detail)",
    0x009839C0: "Stick (goalie, low detail)",
    0x0098AC10: "Puck",
    0x011BB240: "Crowd figure (low detail)",
}

UI_NAMES = {
    0x0022A030: 'Menu logo "2K" (extruded)',
    0x00230810: 'Menu logo "SPORTS" (extruded)',
}

# global.iff is where the players live, but it is not the only asset with geometry in it. Every
# TOC entry was scanned with this same scanner; these are the ones that came back with real 3D
# models, minus the arena_/rink_/led_ families (the Arena tab already owns those) and the flat
# overlay quads. Blob 0 of any of them can be pointed at, edited and written back exactly like
# global.iff, because they all use the same submesh-table + stream-descriptor layout.
EXTRA_SOURCES = [
    ("trophyroom.iff",              "Trophies — Stanley Cup and the awards"),
    ("awardroom.iff",               "Award room"),
    ("overlay_static.iff",          "Scorebug / overlay scene"),
    ("gamedata.iff",                "Game data scene"),
    ("overlay_wipes.iff",           "Overlay wipes / transitions"),
    ("frontend_sync.iff",           "Front-end sync scene"),
    ("online.iff",                  "Online menu scene"),
    ("loading.iff",                 "Loading screen scene"),
    ("overlay_static_skills.iff",   "Skills-competition overlay"),
    ("zamboni_ride_it.iff",         "Zamboni — the drivable one"),
    ("zamboni_game.iff",            "Zamboni minigame props"),
    ("zamboni_help_panel.iff",      "Zamboni minigame — help panel"),
    ("zamboni_times_up.iff",        "Zamboni minigame — time's up"),
    ("zamboni_winner.iff",          "Zamboni minigame — winner"),
    ("zamboni_winner_allstar.iff",  "Zamboni minigame — all-star winner"),
    ("player_stats_multi.iff",      "Player stats — multi"),
    ("player_stats.iff",            "Player stats"),
    ("player_stats_2_line.iff",     "Player stats — 2 line"),
    ("player_stats_3line.iff",      "Player stats — 3 line"),
    ("player_stats_complex.iff",    "Player stats — complex"),
    ("franchise.iff",               "Franchise menu scene"),
    ("titlepage.iff",               "Title page"),
    ("TEAM_VS.iff",                 "Team-vs matchup screen"),
    ("fight_meters.iff",            "Fight scene"),
    ("tutorial.iff",                "Tutorial scene"),
    ("champ_skate_around.iff",      "Championship skate-around scene"),
    ("goalie_control.iff",          "Goalie control tutorial scene"),
    ("skillcompetition.iff",        "Skills competition"),
    ("skills_welcome_accuracy.iff", "Skills — accuracy shooting"),
    ("skills_welcome_breakaway.iff", "Skills — breakaway"),
    ("skills_welcome_fastest.iff",  "Skills — fastest skater"),
    ("skills_welcome_goaltender.iff", "Skills — goaltender"),
    ("skills_welcome_hardest.iff",  "Skills — hardest shot"),
    ("HighlightCreate.iff",         "Highlight creator"),
    ("pressbook_control_panel.iff", "Pressbook control panel"),
    ("teamup_gamertag.iff",         "Team-up gamertag"),
    ("game_user_assign.iff",        "Controller assignment"),
    ("INJURY.iff",                  "Injury overlay"),
    ("English.iff",                 "Localised UI — English"),
    ("French.iff",                  "Localised UI — French"),
    ("German.iff",                  "Localised UI — German"),
    ("Finnish.iff",                 "Localised UI — Finnish"),
    ("Swedish.iff",                 "Localised UI — Swedish"),
]

# One asset per face. The id is the u16 at player record +0xB2 (ros_file frame) — measured
# against the roster: 91.4% of the values are ids that have an asset here, and no neighbouring
# offset scores above 1%. Ids of 9000+ have no asset (those players get a generated head).
HEAD_FMT = "player_head_id_{:04d}.iff"


def head_ids(game_dir=None) -> list[int]:
    """Every player_head_id_NNNN.iff actually present, by id. 447 of them ship."""
    toc, _ = A.load_toc(A._dir(game_dir))
    return [i for i in range(10000)
            if (zlib.crc32(HEAD_FMT.format(i).upper().encode()) & 0xFFFFFFFF) in toc]


def sources(game_dir=None) -> list[dict]:
    """Everything the tab can open: (asset, label). global.iff first, then the faces."""
    out = [dict(asset=ASSET, label="Players, goalies, sticks and props (global.iff)")]
    try:
        toc, _ = A.load_toc(A._dir(game_dir))
    except Exception:
        return out
    for asset, label in EXTRA_SOURCES:
        if (zlib.crc32(asset.upper().encode()) & 0xFFFFFFFF) in toc:
            out.append(dict(asset=asset, label=label))
    for i in head_ids(game_dir):
        out.append(dict(asset=HEAD_FMT.format(i), label=f"Face / head {i:04d}"))
    return out

# Which records a frame really draws. Measured, not guessed — from the goalie capture
# (`goali.rdc`, colour pass 1093; the per-record `drawn` flags live in
# UniformSubstance/mesh/goalie/submeshes.json). 16 of 49 records, 22,445 triangles.
DRAWN = {
    0x6456F0: (0, 1, 2, 8, 11, 12, 13, 14, 15, 16, 18, 19, 41, 42, 43, 44),
}


def blob(current: bool = True, game_dir=None, asset: str = ASSET) -> bytes:
    """Blob 0 of a model asset — global.iff unless another source is named.
    current=True is the (possibly modded) game copy."""
    b = AM.dram(asset, game_dir, current=current)
    if b is None and current:
        b = AM.dram(asset, game_dir)
    if b is None:
        raise ValueError(f"{asset}: no DRAM blob (is the game files folder set?)")
    return b


# ───────────────────────────── model scan ─────────────────────────────
def scan_models(b: bytes, asset: str = ASSET) -> list[dict]:
    """Every mesh in blob 0, with its index block and vertex streams resolved.

    A table is found by its FIRST record — prim 6, tris == indices - 2, first index and first
    vertex both 0 — and then grown for as long as the records keep tiling. Big tables leave a
    one-index gap between submeshes (a lone restart), which is why the walk accepts a gap of 1.
    """
    W = np.frombuffer(b[:len(b) // 4 * 4], dtype=">u4")
    n = len(W)
    if n < 16:
        return []
    cand = np.nonzero((W[:n - 11] == 6) & (W[3:n - 8] == W[2:n - 9] - 2)
                      & (W[1:n - 10] == 0) & (W[5:n - 6] == 0))[0]
    out = []
    for i in cand:
        o, recs, fi, fv = int(i) * 4, 0, 0, 0
        while True:
            r = W[i + recs * 12:i + recs * 12 + 12]
            if len(r) < 12 or r[0] != 6 or r[3] != r[2] - 2 or r[2] < 3:
                break
            if int(r[1]) != fi or int(r[5]) != fv:
                break
            fi, fv = int(r[1]) + int(r[2]), int(r[5]) + int(r[6])
            recs += 1
            nxt = W[i + recs * 12:i + recs * 12 + 12]
            if len(nxt) == 12 and nxt[0] == 6 and int(nxt[1]) == fi + 1:
                fi += 1                                   # the one-index gap
        if recs < 3 or fv < 3:
            continue
        m = dict(table=o, recs=recs, ib0=o + recs * 48, nidx=fi, nvtx=fv,
                 tris=int(W[i + 3::12][:recs].sum()) if recs else 0,
                 mats=sorted({int(x) for x in W[i + 8:i + recs * 12:12]}),
                 name=KNOWN.get(o))
        m["tris"] = int(sum(int(W[i + k * 12 + 3]) for k in range(recs)))
        if not _resolve_streams(b, m):
            continue
        if m.get("float_pos") and not _sane_float_pos(b, m):
            continue          # texture-only assets tile enough to fake a table; the positions
        out.append(m)         # are the giveaway -- they decode to 1e14 cm of nothing
    out += _scan_props(b, {m["table"] for m in out}, asset)
    out.sort(key=lambda m: m["table"])
    return out


def _scan_props(b: bytes, seen: set, asset: str = None) -> list[dict]:
    """The single-record meshes — the two sticks, the puck, the low-detail crowd figure.

    A lone record cannot be validated by tiling, so it is validated by its index block instead:
    a real submesh addresses exactly its own vertices, so the largest index must be nvtx-1.
    That plus a resolvable transform (a table with no ModelPosScaleAndOffset in front of it is
    a false positive, not a model) is enough to cut ~890 raw candidates down to the real props.
    """
    W = np.frombuffer(b[:len(b) // 4 * 4], dtype=">u4")
    n = len(W)
    if n < 16:
        return []
    cand = np.nonzero((W[:n - 11] == 6) & (W[1:n - 10] == 0) & (W[5:n - 6] == 0)
                      & (W[3:n - 8] == W[2:n - 9] - 2)
                      & (W[2:n - 9] >= 3) & (W[2:n - 9] < 1 << 18)
                      & (W[6:n - 5] >= 60) & (W[6:n - 5] < 1 << 17))[0]
    out = []
    for i in cand:
        o = int(i) * 4
        if o in seen:
            continue
        nidx, nvtx, ib0 = int(W[i + 2]), int(W[i + 6]), o + 48
        if ib0 + nidx * 2 > len(b):
            continue
        ib = np.frombuffer(b[ib0:ib0 + nidx * 2], dtype=">u2")
        real = ib[ib != 0xFFFF]
        if len(real) < 3 or int(real.max()) != nvtx - 1:
            continue
        m = dict(table=o, recs=1, ib0=ib0, nidx=nidx, nvtx=nvtx, tris=int(W[i + 3]),
                 mats=[int(W[i + 8])], name=PROPS.get(o))
        if not _resolve_streams(b, m):
            continue
        if m["xform"][1] == 1.0 and not _sane_float_pos(b, m):
            continue          # neither a packed model nor readable floats => not a model
        if m["name"] is None and m.get("float_pos") and asset == ASSET:
            # the float32 props in global.iff are all menu/HUD geometry — the 2K SPORTS logo,
            # menu panels, and a run of flat overlay quads. Real and editable, just not players.
            # Other assets keep their float32 props unnamed; there they are scene furniture.
            m["name"] = UI_NAMES.get(o, "Menu / HUD panel")
        out.append(m)
    return out


def _sane_float_pos(b: bytes, m: dict) -> bool:
    """A transformless candidate is real only if its positions read as finite float32 spanning a
    plausible object size. Junk that happens to look like a submesh record fails this."""
    if not m.get("float_pos"):
        return False
    n = min(m["nvtx"], 4096)
    P = np.frombuffer(b[m["pos_off"]:m["pos_off"] + n * m["pos_stride"]],
                      ">f4").reshape(n, m["pos_stride"] // 4)[:, 0:3]
    if not np.isfinite(P).all():
        return False
    e = float((P.max(0) - P.min(0)).max())
    return 0.01 < e < 1e6


def _resolve_streams(b: bytes, m: dict) -> bool:
    """Find the stream descriptors after the index block -> m['streams'] = [(off, stride, n)]."""
    ibend = m["ib0"] + m["nidx"] * 2
    nv = m["nvtx"]
    best = None
    for st in range(4, 80, 4):
        k = b.find(struct.pack(">III", 1, st, nv * st), ibend, ibend + 0x20000)
        if k >= 0 and (best is None or k < best):
            best = k
    if best is None:
        return False
    u32 = lambda a: int.from_bytes(b[a:a + 4], "big")     # noqa: E731
    streams, d = [], best
    while u32(d) == 1:
        st, sz = u32(d + 4), u32(d + 8)
        if st == 0 or sz != nv * st:
            break
        off = d + 0x0B + u32(d + 0x0C)                    # verified on all 50 models
        if off + sz > len(b):
            break
        streams.append((off, st, nv))
        d += 0x18
    if not streams:
        return False
    m["streams"] = streams
    m["desc"] = best
    m.update(_layout(b, m))
    return True


def _layout(b: bytes, m: dict) -> dict:
    """Where each field sits. Two streams => stream 0 is position and the attributes start at
    byte 0 of stream 1; one stream => position is at byte 0 and the attributes follow it."""
    S = m["streams"]
    pos_off, pos_st, _ = S[0]
    att_off, att_st, _ = S[-1]
    base = 0 if len(S) > 1 else 8
    nv = m["nvtx"]
    raw = np.frombuffer(b[att_off:att_off + nv * att_st], np.uint8).reshape(nv, att_st)

    def fits(k, w=4):
        return 0 <= k and k + w <= att_st

    wts = bones = None
    for k in range(att_st - 3):                           # 4 UNORM8 weights summing to 255
        if (raw[:, k:k + 4].astype(np.int32).sum(1) == 255).mean() > 0.98:
            wts = k
            break
    for k in range(att_st - 3):                           # bone slots, each pre-multiplied by 3
        if k != wts and (raw[:, k:k + 4] % 3 == 0).all():
            bones = k
    # A model with no ModelPosScaleAndOffset in front of it does not store packed positions at
    # all — it stores FLOAT32 xyz, unscaled and already in world units. That is how the menu and
    # ceremony assets (trophyroom, the zambonis, the skate-around) and the flat overlay quads in
    # global.iff are built. Decoding those as snorm16 gives the tell-tale 1 x 2 x 1 cm box.
    xf = pos_xform(b, pos_off)
    fpos = xf[1] == 1.0 and pos_st % 4 == 0 and pos_st >= 12
    if fpos:
        base = 0 if len(S) > 1 else 12                     # xyz floats come first, then the rest
    nrm = base if fits(base, 6) else None
    if nrm is not None:                                   # only claim it if it IS a unit vector
        v = np.maximum(raw[:, nrm:nrm + 6].copy().view(">i2").astype(np.float64) / 32767.0, -1.0)
        if abs(np.linalg.norm(np.pad(v, ((0, 0), (0, 1))), axis=1).mean() - 1) > 0.25:
            nrm = None
    return dict(pos_off=pos_off, pos_stride=pos_st, att_off=att_off, att_stride=att_st,
                f_nrm=nrm, f_u=base + 6 if fits(base + 6, 2) else None,
                f_tan=base + 8 if fits(base + 8, 6) else None,
                f_v=base + 14 if fits(base + 14, 2) else None,
                f_wts=wts, f_bones=bones, float_pos=fpos,
                skinned=wts is not None and bones is not None,
                xform=xf)


def pos_xform(b: bytes, before: int) -> tuple:
    """(offset xyz, scale) for `pos = snorm * scale + offset`, read off the disk."""
    k = b.rfind(_XFORM_KEY, 0, before)
    if k < 0:
        return ((0.0, 0.0, 0.0), 1.0)
    off = np.frombuffer(b[k + 0x10:k + 0x1C], ">f4").astype(np.float64)
    sc = float(np.frombuffer(b[k + 0x20:k + 0x24], ">f4")[0])
    if not np.isfinite(off).all() or not np.isfinite(sc) or sc == 0:
        return ((0.0, 0.0, 0.0), 1.0)
    return (tuple(off), sc)


def submeshes(b: bytes, m: dict) -> list[dict]:
    T = np.frombuffer(b[m["table"]:m["table"] + m["recs"] * 48], ">u4").reshape(m["recs"], 12)
    return [dict(rec=r, first_idx=int(T[r, 1]), n_idx=int(T[r, 2]), tris=int(T[r, 3]),
                 first_vtx=int(T[r, 5]), n_vtx=int(T[r, 6]), mat=int(T[r, 8]),
                 lod=int(T[r, 9])) for r in range(m["recs"])]


def describe(m: dict) -> str:
    s = m.get("name") or f"model @0x{m['table']:X}"
    return (f"{s} — {m['recs']} parts, {m['nvtx']:,} vertices, {m['tris']:,} triangles, "
            f"{'skinned' if m['skinned'] else 'rigid'}, "
            + " + ".join(f"stride {st}" for _, st, _ in m["streams"]))


# ───────────────────────────── read ─────────────────────────────
def _snorm(x):
    return np.maximum(np.asarray(x, np.float64) / 32767.0, -1.0)


def read_model(b: bytes, m: dict) -> dict:
    """pos (cm, Y up, +Z front), uv, normals and the triangles of every submesh."""
    nv = m["nvtx"]
    row = b[m["pos_off"]:m["pos_off"] + nv * m["pos_stride"]]
    if m.get("float_pos"):
        pos = np.frombuffer(row, ">f4").reshape(nv, m["pos_stride"] // 4)[:, 0:3].astype(np.float32)
    else:
        P = np.frombuffer(row, ">i2").reshape(nv, m["pos_stride"] // 2)[:, 0:3]
        (ox, oy, oz), sc = m["xform"]
        pos = (_snorm(P) * sc + np.array([ox, oy, oz])).astype(np.float32)

    att = np.frombuffer(b[m["att_off"]:m["att_off"] + nv * m["att_stride"]],
                        np.uint8).reshape(nv, m["att_stride"])
    g = lambda k, w: att[:, k:k + w].copy().view(">i2")   # noqa: E731
    uv = np.zeros((nv, 2), np.float32)
    if m["f_u"] is not None and m["f_v"] is not None:
        uv[:, 0] = _snorm(g(m["f_u"], 2)[:, 0]) * 2.0
        uv[:, 1] = _snorm(g(m["f_v"], 2)[:, 0]) * 2.0
    nrm = (_snorm(g(m["f_nrm"], 6)).astype(np.float32) if m["f_nrm"] is not None
           else np.zeros((nv, 3), np.float32))

    ib = np.frombuffer(b[m["ib0"]:m["ib0"] + m["nidx"] * 2], ">u2")
    parts = []
    for p in submeshes(b, m):
        run = ib[p["first_idx"]:p["first_idx"] + p["n_idx"]]
        parts.append(dict(p, tris_idx=AM._strip_to_tris(run)))
    return dict(pos=pos, uv=uv, nrm=nrm, parts=parts)


# ───────────────────────────── skinning ─────────────────────────────
# Which rig each mesh is bound to. Both sit immediately behind their skeleton in the blob
# (doc 30), which is also why the palette below can be found by walking back from the table.
RIG_TABLE = {"goalie": 0x6456F0, "skater": 0x1323AF4}


def rig_model(models: list[dict], rig: str) -> dict | None:
    """The mesh that the named rig deforms, or None if this blob does not hold it."""
    t = RIG_TABLE.get(rig)
    return next((m for m in models if m["table"] == t), None) if t else None


def bone_palette(b: bytes, m: dict):
    """slot -> rig bone index, or None.

    The vertex stream does NOT store bone indices. It stores a shader constant register: a bone
    matrix is 3 float4 rows, so the byte is `slot * 3` and the slot indexes a per-mesh palette
    that the draw call uploads into those registers. The palette is a u32 array ending exactly
    where the submesh table begins, introduced by a `0x8000<count>` word and one word of its own.
    Proof it is the right table: resolved through it, a vertex's dominant bone sits a median
    25 cm away (goalie) against 85 ± 13 cm for a shuffled palette and a 15 cm nearest-joint
    floor. Read raw, the byte is no better than random.
    """
    tbl = m["table"]
    for o in range(tbl - 8, max(tbl - 0x1000, 0), -4):
        v = int(np.frombuffer(b[o:o + 4], ">u4")[0])
        if v >> 16 != 0x8000:
            continue
        n = v & 0xFFFF
        for head in (o + 8, o + 4):                       # goalie/skater both carry the pad word
            if n and head + n * 4 == tbl:
                return np.frombuffer(b[head:tbl], ">u4").astype(np.int32)
        return None
    return None


def skin(b: bytes, m: dict) -> dict | None:
    """Per-vertex `idx` (n,4) RIG bone indices and `wts` (n,4) floats summing to 1, or None."""
    if not m.get("skinned"):
        return None
    P = bone_palette(b, m)
    if P is None:
        return None
    nv = m["nvtx"]
    att = np.frombuffer(b[m["att_off"]:m["att_off"] + nv * m["att_stride"]],
                        np.uint8).reshape(nv, m["att_stride"])
    slot = att[:, m["f_bones"]:m["f_bones"] + 4] // 3
    if int(slot.max()) >= len(P):
        return None
    return dict(idx=P[slot].astype(np.int32),
                wts=att[:, m["f_wts"]:m["f_wts"] + 4].astype(np.float32) / 255.0,
                palette=P)


# ───────────────────────────── props (stick, puck) ─────────────────────────────
# A prop is not weighted to the character rig. It carries its OWN little rig — 8 bones for a
# stick: shaft, shaft_01, shaft_02, blade_01..04 and a stick_end at the butt — written in the
# standard 48-byte bone format (doc 30) ending 0x10 before its submesh table. That is why
# `bone_palette` finds nothing for it: there is no `0x8000` palette word, the vertex slots index
# this local rig directly.
#
# What the asset does NOT carry is where the stick meets the hand. `stickBone` and
# `def_stickBone_pointConstraint1` exist in global.iff's name pool, but no rig in the file holds
# either, so the game applies the grip transform at runtime and it cannot be read off disk. It is
# not hiding in the mesh either: the fist is solid, with no bored-out socket to measure.
#
# So GRIP_FIT below is FITTED, and it is worth saying how, because three earlier attempts were
# wrong. Aiming the shaft at the ice in the BIND pose is meaningless — bind is a T-pose with the
# origin at the hips, and the required drop exceeds the shaft length. Least squares over "blade on
# the ice" across all 3,280 clips lands at 71 cm rms, because most clips are dives, fights and
# celebrations where the stick is nowhere near the ice. Assuming the skater's two hands ride one
# rigid shaft fails too — their separation swings 81 ± 26 cm and no tight mode exists.
#
# What works — for the goalie — is fitting only where the answer is known: `animations.KNOWN_TABLES`
# names the tables the game indexes for locomotion, and in a stride the stick IS on the ice. Fitting
# the hand-local rotation over those frames alone, scored by the median blade height with sinking
# weighted above floating, puts the goalie blade a median 5.8 cm over the ice across the shuffle and
# C-push, 68 cm in front, under the ice in 7% of frames. That is a usable socket.
#
# The SKATER does not settle, and the reason is worth recording: its top hand does not track the
# stick in the shipped clips at all. Threading a shaft through both hands — which is what the name
# `pointConstraint` suggests — needs their separation to hold steady, and over the same stride clips
# it swings 82 +/- 40 cm (p10 19, p90 125). No rigid two-handed grip survives that, so the game must
# run the stick through a procedural/IK pass at runtime that the clip data does not contain. The
# skater entry below is therefore the best constrained fit available (median 24 cm over the ice,
# 31% of frames under it) — the stick is in the hand and reads correctly, but it does not plant on
# the ice every frame the way the goalie's does. Fixing that properly needs a RenderDoc capture of
# the runtime bone, not more fitting.
STICK_MODEL = {"goalie": "Stick (goalie)", "skater": "Stick (skater)"}
GRIP_BONE = {"goalie": "def_R_Hand", "skater": "def_L_Hand"}   # blocker hand / top hand
GRIP_FIT = {   # hand-local stick orientation, fitted over the locomotion tables (see above)
    "goalie": ((0.72518, -0.20757, -0.65653),      # blade over ice: med +5.8, 7% below
               (-0.08519, 0.91911, -0.38468),
               (0.68327, 0.33489, 0.64883)),
    "skater": ((-0.75671, -0.30802, 0.57664),      # blade over ice: med +24.2, 31% below
               (-0.43689, -0.41791, -0.79654),
               (0.48634, -0.85468, 0.18167)),
}


def local_rig(b: bytes, m: dict, limit: int = 24) -> list[dict]:
    """The rig a prop carries in place of a bone palette, nearest-the-table first stripped off.

    Records are the usual 48 bytes (`+0x00` bind position, `+0x10` local offset, `+0x24`
    crc32(name), `+0x28` index/parent as two u16) and run backwards from 0x10 before the submesh
    table. Both float4s end in an exact 1.0, which is what tells a real record from the packed
    data that precedes the rig.
    """
    from . import skeleton as _SK
    names = _SK.bone_names(b)
    out = []
    for k in range(limit):
        o = m["table"] - 0x10 - (k + 1) * 48
        if o < 0:
            break
        f = np.frombuffer(b[o:o + 0x20], ">f4")
        u = np.frombuffer(b[o + 0x20:o + 0x30], ">u4")
        if float(f[3]) != 1.0 or float(f[7]) != 1.0 or int(u[1]) not in names:
            break
        p = int(u[2]) & 0xFFFF
        out.append(dict(index=int(u[2]) >> 16, parent=p - 0x10000 if p > 0x7FFF else p,
                        world=np.array(f[0:3], np.float32),
                        offset=np.array(f[4:7], np.float32),
                        hash=int(u[1]), name=names[int(u[1])]))
    return out[::-1]


def prop_model(models: list[dict], rig: str) -> dict | None:
    return next((m for m in models if m.get("name") == STICK_MODEL.get(rig)), None)


def stick_attach(sk: dict, rig: str) -> tuple | None:
    """(rotation 3x3, grip position, grip bone index) placing stick space into the bind pose.

    Bind is translation-only (doc 30), so the grip hand's bind frame IS world-axis-aligned and the
    fitted hand-local rotation can be used as-is. Everything after this is the ordinary skin: the
    stick rides the hand bone because it is weighted 1.0 to it.
    """
    R = GRIP_FIT.get(rig)
    i = next((x["index"] for x in sk["bones"] if x["name"] == GRIP_BONE.get(rig, "")), None)
    if R is None or i is None:
        return None
    return np.array(R, np.float32), np.array(sk["bones"][i]["world"], np.float32), i


def stick_scene(sk: dict, b: bytes, models: list[dict], rig: str,
                first_part_id: int = 800) -> dict | None:
    """The rig's stick, placed in its grip hand and bolted to that bone.

    -> dict(scene, skin) — `skin` weights every stick vertex 1.0 to the grip bone, so the same
    linear blend that moves the body carries the stick rigidly with the hand.
    """
    m = prop_model(models, rig)
    if m is None:
        return None
    at = stick_attach(sk, rig)
    sc = build_scene(b, m, first_part_id=first_part_id)
    if at is None or sc is None:
        return None
    R, g, gi = at
    sc["pos"] = (sc["pos"] @ R.T + g).astype(np.float32)
    sc["nrm"] = (sc["nrm"] @ R.T).astype(np.float32) if sc.get("nrm") is not None else None
    n = len(sc["pos"])
    idx = np.zeros((n, 4), np.int32)
    idx[:, 0] = gi
    wts = np.zeros((n, 4), np.float32)
    wts[:, 0] = 1.0
    return dict(scene=sc, skin=dict(idx=idx, wts=wts))


LIGHT = np.array([0.35, 0.55, 0.76], np.float32)          # the made-up preview key light
AMBIENT, KEY = 0.30, 0.70


def lambert(nrm, n: int):
    """The made-up head-on light characters are previewed with — see build_scene."""
    lam = (np.clip(nrm @ LIGHT, 0.0, 1.0) if nrm is not None and nrm.any()
           else np.full(n, 0.6, np.float32))
    return np.repeat((AMBIENT + KEY * lam)[:, None], 3, 1).astype(np.float32)


# ── the head editor's own light ───────────────────────────────────────────────
# LIGHT above points almost straight down the view axis, which is a fine way to SHOW a model and a
# useless way to JUDGE one. A head-on light lights the parts of a face that face you, which is all
# of it, so every plane change — the mandible, the cheekbone, the brow, the bridge — comes out as a
# gentle gradient and the head reads as a smooth mass. It flatters an inaccurate head and it hides
# an accurate one, and it cost real time here: a Boeser build was judged "too round in the jaw"
# under it, and measuring the fitted mesh against the profile reference afterwards put its jaw edge
# 0.5 mm from the photograph's, with the same corner sharpness. The shape was right; the light was
# wrong.
#
# So rake it. In CAMERA space (right, up, towards the viewer) — see arena_preview.light_dir — well
# off to one side and above, so the terminator crosses the ridges instead of running along them,
# and the ambient dropped so the shadow side is actually a shadow. This changes nothing that ships:
# it is a preview light, evaluated per pixel in shade(), and no map is baked through it.
HEAD_LIGHT = (0.78, 0.46, 0.43)
HEAD_FILL = (-0.74, 0.10, 0.66)          # dim, from the other side, so the shadow half is readable
HEAD_AMBIENT, HEAD_KEY, HEAD_FILLK = 0.14, 0.88, 0.24


def head_light(scene, light=HEAD_LIGHT, ambient=HEAD_AMBIENT, key=HEAD_KEY,
               fill_dir=HEAD_FILL, fill=HEAD_FILLK):
    """Point a head scene's key light across the face instead of down the lens. -> the scene."""
    if scene is not None:
        scene.update(light=np.asarray(light, np.float32), ambient=float(ambient),
                     key=float(key), fill_dir=np.asarray(fill_dir, np.float32),
                     fill=float(fill), light_rel=True)
    return scene


def tangents(pos, uv, tri, nrm=None):
    """Per-vertex tangent along +U, from the UV gradient. -> (n,3) float32.

    The vertex stream does carry a tangent (`f_tan`), but nothing says which handedness the
    bitangent it implies has, and a normal map read through the wrong handedness lights its
    creases backwards. Deriving the frame from the mesh's own UVs instead makes it consistent
    BY CONSTRUCTION with the image axes the map was authored against, which is the only thing
    that has to hold — see face_builder.detail_normal for the sign measurement.
    """
    T = np.zeros_like(pos)
    p0, p1, p2 = pos[tri[:, 0]], pos[tri[:, 1]], pos[tri[:, 2]]
    q0, q1, q2 = uv[tri[:, 0]], uv[tri[:, 1]], uv[tri[:, 2]]
    e1, e2 = p1 - p0, p2 - p0
    d1, d2 = q1 - q0, q2 - q0
    det = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    r = np.where(np.abs(det) > 1e-12, 1.0 / np.where(det == 0, 1.0, det), 0.0).astype(np.float32)
    t = (e1 * d2[:, 1, None] - e2 * d1[:, 1, None]) * r[:, None]
    for k in range(3):                                    # accumulate onto the shared vertices
        np.add.at(T, tri[:, k], t)
    if nrm is not None and nrm.any():                     # Gram-Schmidt, so T lies in the surface
        T -= nrm * (T * nrm).sum(1)[:, None]
    n = np.linalg.norm(T, axis=1)
    bad = n < 1e-8
    if bad.any():                                         # any perpendicular will do for a
        T[bad] = np.array([1.0, 0.0, 0.0], np.float32)    # vertex no textured triangle touches
        n[bad] = 1.0
    return (T / n[:, None]).astype(np.float32)


def _asset_maps(asset: str, game_dir=None) -> tuple:
    """(colour x occlusion, normal) for an asset that carries the head texture triple, else None.

    Head assets are the one character asset with its own maps and a UV layout that matches the
    mesh, so this is what makes the preview show the face the Face Builder just wrote instead of
    a grey lambert bust. Occlusion is folded into the albedo here rather than carried as a third
    channel — it is a fixed multiplier over the same UVs, so there is nothing to separate.
    """
    try:
        from . import archive_textures as A
    except ImportError:
        import archive_textures as A
    try:
        recs = {r["label"]: r for r in A.list_textures(asset, game_dir)}
        if "color" not in recs:
            return None
        col = np.asarray(A.decode_record(asset, recs["color"], game_dir, live=True
                                         ).convert("RGB"), np.float32)
        if "occlusion" in recs:
            ao = A.decode_record(asset, recs["occlusion"], game_dir, live=True).convert("L")
            col = col * (np.asarray(ao.resize(
                (col.shape[1], col.shape[0])), np.float32)[..., None] / 255.0)
        nrm = None
        if "normal" in recs:
            nrm = np.asarray(A.decode_record(asset, recs["normal"], game_dir, live=True
                                             ).convert("RGB"), np.uint8)
        return np.clip(col, 0, 255).astype(np.uint8), nrm
    except Exception:
        return None


def head_scene(sk: dict, head_asset: str, first_part_id: int = 1) -> dict | None:
    """A face asset re-bound onto a body rig, ready to draw beside the body it belongs to.

    Bodies ship headless — the head is its own asset, chosen per player by roster `+0xB2` — and
    it carries its OWN copy of the rig. The two are joined by `crc32(bone name)` at bone `+0x24`,
    which doc 30 established is stable across models; index would not survive, since the head rig
    orders its eye and eyelid bones in among the body's. Bones the body rig has no counterpart
    for (eyelids, `helmet_joint`) fall back to their nearest mapped ancestor, which is exactly
    what a clip does with them anyway — nothing keyframes an eyelid.

    -> dict(scene, skin, bind) where `bind` is the head rig's bind pose expressed in BODY bone
    order. Skinning the head against that and posing it with the body rig snaps it onto the
    body's neck even when the two rigs disagree (the goalie's differs by up to 5 cm).
    """
    hb = blob(asset=head_asset)
    models = scan_models(hb, head_asset)
    if not models:
        return None
    hm = models[0]
    from . import skeleton as _SK
    rigs = _SK.scan(hb)
    if not rigs:
        return None
    hbones = rigs[0]["bones"]
    body = {x["hash"]: x["index"] for x in sk["bones"]}

    def to_body(k: int) -> int | None:
        while 0 <= k < len(hbones):                       # walk up until the body rig knows it
            if hbones[k]["hash"] in body:
                return body[hbones[k]["hash"]]
            k = hbones[k]["parent"]
        return None

    sn = skin(hb, hm)
    sc = build_scene(hb, hm, first_part_id=first_part_id)
    if sn is None or sc is None:
        return None
    # `bind` is indexed by BODY bone, so it has to be a body-length array
    bind = np.array([x["world"] for x in sk["bones"]], np.float32)
    for h in hbones:
        if h["hash"] in body:
            bind[body[h["hash"]]] = h["world"]
    idx = np.zeros_like(sn["idx"])
    for v in np.unique(sn["palette"]):                     # skin() already resolved slot -> head
        t = to_body(int(v))
        if t is not None:
            idx[sn["idx"] == v] = t
    return dict(scene=sc, skin=dict(idx=idx, wts=sn["wts"]), bind=bind)


def merge_scenes(a: dict, b: dict) -> dict:
    """Two draw lists into one — vertex indices and part ids rebased onto `a`."""
    n, p = len(a["pos"]), (max(a["pbox"]) if a["pbox"] else 0)
    out = dict(a)
    out["pos"] = np.concatenate([a["pos"], b["pos"]])
    out["uv"] = np.concatenate([a["uv"], b["uv"]])
    out["vcol"] = np.concatenate([a["vcol"], b["vcol"]])
    out["tri"] = np.concatenate([a["tri"], b["tri"] + n])
    out["mat"] = np.concatenate([a["mat"], b["mat"]])
    out["part"] = np.concatenate([a["part"], b["part"] + p])
    out["pbox"] = {**a["pbox"], **{k + p: v for k, v in b["pbox"].items()}}
    for k in ("nrm", "tan"):
        av, bv = a.get(k), b.get(k)
        out[k] = (np.concatenate([av, bv]) if av is not None and bv is not None else None)
    # Materials are per-MODEL ids: the head's mat 0 and the body's mat 0 are different materials
    # that the merge cannot tell apart, so keeping either side's maps would paint a face map onto
    # a torso. Textures do not survive a merge.
    out["tex"], out["ntex"] = {}, {}
    return out


def build_scene(b: bytes, m: dict, first_part_id: int = 1, only=None,
                asset: str | None = None, game_dir=None, also=None) -> dict | None:
    """The same draw-list dict arena_preview.raster/shade take, for ONE character model.

    Characters carry no baked lighting — the arena's vcol field is a bake, here it has to be
    made up — so light them with a simple head-on lambert off the vertex normals.

    `asset` opts the model into its own textures. Only head assets carry any (a colour, a normal
    and an occlusion map). With a normal map attached, shade() drops the vertex lambert for those
    materials and lights them per pixel instead, which is the only way the relief the Face Builder
    writes is visible at all.

    ⛔ Only the SKIN material reads those maps. A head's other submeshes — the mouth bag, the
    eyeballs, the brow, lash and hair CARDS — each unwrap over their own full 0..1 sheet, and
    those sheets are not in the head .iff. Handing them the face map prints a whole second face
    across the cheeks and jaw, which is what the hair shells (three stacked variants spanning the
    entire UV square) did. They are also alpha-cut in the game and this rasterizer has no alpha,
    so drawn opaque they are solid plates over the face either way: when the maps go on, the draw
    list is cut to the skin material. Pass `only` explicitly to override that.

    `only` = the submesh records to draw. The file holds every equipment alternative side by
    side (drawn_set), so leaving this open stacks six pad variants in the same space.

    `also` = records to keep DESPITE that cut, drawn untextured. That is how a facial-hair shell
    is previewed: no map is bound to its material, so it falls back to the vertex lambert and
    reads as the silhouette it is, instead of printing a second face across the jaw.
    """
    d = read_model(b, m)
    pos, nrm = d["pos"], d["nrm"]
    vcol = lambert(nrm, len(pos))
    got = _asset_maps(asset, game_dir) if asset else None
    # the skin is the big one — every card and shell beside it is a few hundred triangles
    skin_mat = (max(d["parts"], key=lambda p: len(p["tris_idx"]))["mat"]
                if got is not None else None)
    cut = skin_mat if (skin_mat is not None and only is None) else None
    TRI, MAT, PART, pbox = [], [], [], {}
    pid = first_part_id - 1
    for p in d["parts"]:
        pid += 1
        t = p["tris_idx"]
        if not len(t) or (only is not None and p["rec"] not in only):
            continue
        if cut is not None and p["mat"] != cut and p["rec"] not in (also or ()):
            continue
        v = np.unique(t)
        pbox[pid] = (pos[v].min(0), pos[v].max(0))
        if cut is not None and p["mat"] != cut:
            # An `also` part: no map is bound to its material, so all it has is the vertex
            # lambert, which prints a pale grey plate. Darken it to hair so the silhouette
            # reads as the beard it is rather than as a mask.
            vcol[v] = vcol[v] * np.array([0.20, 0.17, 0.15], np.float32)
        TRI.append(t)
        MAT.append(np.full(len(t), p["mat"], np.int32))
        PART.append(np.full(len(t), pid, np.int32))
    if not TRI:
        return None
    tri, mat = np.concatenate(TRI), np.concatenate(MAT)
    uv = d["uv"].astype(np.float32)
    sc = dict(pos=pos, uv=uv, vcol=vcol, nrm=nrm, tri=tri, mat=mat,
              part=np.concatenate(PART), tex={}, ntex={}, tan=None,
              light=LIGHT, ambient=AMBIENT, key=KEY, pbox=pbox, ref=[1.0])
    if got is not None:
        col, nmap = got
        # never beyond the skin, even when `only` kept the cards: their sheets are not in here
        for mid in ([skin_mat] if skin_mat in mat else []):
            sc["tex"][int(mid)] = col
            if nmap is not None:
                sc["ntex"][int(mid)] = nmap
        if nmap is not None:
            sc["tan"] = tangents(pos, uv, tri, nrm)
    return sc


# ───────────────────────────── OBJ export ─────────────────────────────
def _obj_group(mi: int, p: dict) -> str:
    return f"m{mi:02d}_sub{p['rec']:03d}_mat{p['mat']}"


def variant_groups(parts) -> dict:
    """`+0x24` is a single selection BIT, not a LOD level: consecutive records share a bit, and
    each bit is one equipment/body slot whose members are alternatives. The engine turns on one
    bit per slot, so the drawn model is a loadout picked across groups, not a prefix of the
    table. -> {bit: [rec, ...]} in file order."""
    g = {}
    for p in parts:
        g.setdefault(p["lod"], []).append(p["rec"])
    return g


def drawn_set(m: dict) -> set | None:
    """The records a real frame actually draws, or None when nobody has captured this model.

    There is no way to work this out from the table alone. `+0x24` is a per-slot selection bit
    and the game picks ONE record per slot at runtime from the roster's equipment, so the file
    holds every alternative side by side — for the goalie that is 49 records of which a frame
    draws 16. Guessing (e.g. "the first group of each material set") gets the skater right by
    luck and the goalie wrong, so the drawn sets here are measured, not inferred.
    """
    d = DRAWN.get(m["table"])
    return set(d) if d else None


def export_model_obj(dest, mi: int, b: bytes = None, models: list[dict] = None,
                     drawn_only: bool = False, only=None, log=print,
                     asset: str = ASSET) -> Path:
    """One whole character model as a single OBJ — every submesh as its own group.

    Group names are `m<NN>_sub<RRR>_mat<M>`, the same convention the arena exporter uses and
    the key replace_model_obj matches on, so an edited file goes back part by part.

    By default every record is exported, alternatives included, which for the goalie means six
    pad/glove variants stacked in the same space. `drawn_only=True` keeps just the records a
    capture proved are on screen (see drawn_set); `only=[rec, ...]` picks them by hand. Either
    way the file still imports — records it leaves out are simply not touched.
    """
    b = b if b is not None else blob(asset=asset)
    models = models if models is not None else scan_models(b, asset)
    if not (0 <= mi < len(models)):
        raise ValueError(f"no model {mi} in {asset}")
    m = models[mi]
    d = read_model(b, m)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    name = (m.get("name") or f"model{mi:02d}").lower().replace(" ", "_").split("_(")[0]
    keep = {p["rec"] for p in d["parts"]}
    tag = ""
    if only is not None:
        keep, tag = set(int(r) for r in only), "_sel"
    elif drawn_only:
        ds = drawn_set(m)
        if ds is None:
            log(f"  ! no capture has said which parts of model {mi:02d} are drawn — "
                "exporting all of them")
        else:
            keep, tag = ds, "_drawn"
    obj = dest / f"{Path(asset).stem}_{mi:02d}_{name}{tag}.obj"

    nwritten = 0
    with open(obj, "w") as f:
        f.write(f"# NHL 2K10 {asset} {describe(m)}\n"
                f"# table 0x{m['table']:X} — model space, Y up, +Z front, 1 unit ~ 1 cm\n"
                f"# groups are m<model>_sub<record>_mat<material>; a replacement is capped at "
                f"each part's own vertex and index slot.\n")
        base = 1
        for p in d["parts"]:
            if not len(p["tris_idx"]) or p["rec"] not in keep:
                continue
            # the WHOLE vertex range of the part, not just the referenced vertices: the n-th
            # vertex of the group has to stay the n-th vertex of the slot, or an unreferenced
            # vertex anywhere would shift every later one and the re-import would land the
            # geometry (and the skinning it inherits) on the wrong rows.
            lo, hi = p["first_vtx"], p["first_vtx"] + p["n_vtx"]
            f.write(f"g {_obj_group(mi, p)}\n")
            for v in d["pos"][lo:hi]:
                f.write("v %.4f %.4f %.4f\n" % tuple(v))
            for u in d["uv"][lo:hi]:
                f.write("vt %.6f %.6f\n" % (u[0], 1.0 - u[1]))
            for v in d["nrm"][lo:hi]:
                f.write("vn %.6f %.6f %.6f\n" % tuple(v))   # fewer digits costs an LSB on re-import
            for tri in (p["tris_idx"] - lo + base):
                a, c, e = (int(x) for x in tri)
                f.write(f"f {a}/{a}/{a} {c}/{c}/{c} {e}/{e}/{e}\n")
            base += p["n_vtx"]
            nwritten += 1
    log(f"  {asset} model {mi:02d}: {nwritten} parts -> {obj}")
    return obj


def export_part_obj(b: bytes, m: dict, part: dict, dest, asset: str = ASSET) -> Path:
    """One submesh on its own — the file you edit and hand back to replace_part()."""
    d = read_model(b, m)
    p = next((x for x in d["parts"] if x["rec"] == part["rec"]), None)
    if p is None or not len(p["tris_idx"]):
        raise ValueError(f"part {part['rec']} has no triangles")
    lo, hi = part["first_vtx"], part["first_vtx"] + part["n_vtx"]
    dest = Path(dest)
    with open(dest, "w") as f:
        f.write(f"# NHL 2K10 {asset} table 0x{m['table']:X} part {part['rec']} "
                f"(material {part['mat']})\n"
                f"# {part['n_vtx']} vertices, {len(p['tris_idx'])} triangles — model space, "
                f"Y up, +Z front, 1 unit ~ 1 cm.\n"
                f"# Budget when you import it back: at most {part['n_vtx']} vertices and "
                f"about {part_budget(part)['max_tris']} triangles — the slot in the file is "
                f"fixed, and how many triangles really fit depends on how well they strip. "
                f"Leave the topology alone and the original strips are kept as they are.\n")
        for v in d["pos"][lo:hi]:
            f.write("v %.4f %.4f %.4f\n" % tuple(v))
        for u in d["uv"][lo:hi]:
            f.write("vt %.6f %.6f\n" % (u[0], 1.0 - u[1]))
        for v in d["nrm"][lo:hi]:
            f.write("vn %.6f %.6f %.6f\n" % tuple(v))
        f.write(f"g part{part['rec']}\n")
        for a, c, e in (p["tris_idx"] - lo + 1):
            f.write(f"f {a}/{a}/{a} {c}/{c}/{c} {e}/{e}/{e}\n")
    return dest


def replace_part_obj(b: bytearray, m: dict, part: dict, path, log=print) -> str:
    """Import a single-part OBJ (the file export_part_obj wrote) back into that part."""
    groups = read_obj_groups(path)
    if len(groups) > 1:
        raise ValueError(f"{Path(path).name}: {len(groups)} groups — use replace_model_obj")
    return replace_part(b, m, part, next(iter(groups.values())), log=log)


# ───────────────────────────── replace ─────────────────────────────
def part_budget(part: dict) -> dict:
    return AM.part_budget(part)


def _vertex_normals(P, T):
    """Area-weighted normals for the imported mesh — the file has no other way to get them."""
    N = np.zeros_like(P, np.float64)
    a, b_, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    fn = np.cross(b_ - a, c - a)
    for k in range(3):
        np.add.at(N, T[:, k], fn)
    L = np.linalg.norm(N, axis=1, keepdims=True)
    return np.where(L > 1e-12, N / np.maximum(L, 1e-12), 0.0)


def replace_part(b: bytearray, m: dict, part: dict, mesh: dict, log=print) -> str:
    """Overwrite ONE submesh's geometry in place, size-preserving.

    The submesh record is never rewritten: the new mesh is written INSIDE the existing vertex
    and index ranges and the leftover indices are filled with 0xFFFF restarts, which draw
    nothing. So a replacement can be smaller than the original but never bigger.

    Skin weights and bone indices are inherited from the NEAREST original vertex — a character
    that does not carry them stops animating, and there is nothing in an OBJ to rebuild them
    from. Positions and UVs come from the file you supply; normals are recomputed from it and
    the inherited tangent is re-orthogonalised against them.
    """
    P, UV, T = np.asarray(mesh["pos"], np.float64), np.asarray(mesh["uv"], np.float64), \
        np.asarray(mesh["tris"], np.int64)
    bud = part_budget(part)
    if len(P) > bud["max_verts"]:
        raise ValueError(f"{len(P)} vertices, but this part's slot holds {bud['max_verts']}. "
                         "Decimate the mesh (or replace a larger part). Nothing was written.")
    lo = part["first_vtx"]
    if lo + len(P) >= 0xFFFF:
        raise ValueError("this part sits past vertex 65534 — it cannot be indexed. "
                         "Nothing was written.")
    (ox, oy, oz), sc = m["xform"]
    fpos = bool(m.get("float_pos"))
    if not fpos:
        q = (P - np.array([ox, oy, oz])) / sc
        out = int((np.abs(q) > 1.0).sum())
        if out:
            log(f"  ! {out} coordinates fall outside the model's ±{sc:.1f} cm packing range and "
                "were clamped — move the mesh back inside the original silhouette")
        S16 = np.clip(np.round(q * 32767.0), -32767, 32767).astype(">i2")

    # position stream
    pst, poff = m["pos_stride"], m["pos_off"]
    prow = np.frombuffer(b[poff + lo * pst:poff + (lo + part["n_vtx"]) * pst],
                         np.uint8).reshape(part["n_vtx"], pst)
    if fpos:
        opos = prow[:, 0:12].copy().view(">f4").astype(np.float64)
    else:
        opos = _snorm(prow[:, 0:6].copy().view(">i2")) * sc + np.array([ox, oy, oz])
    if mesh.get("ordered") and len(P) == part["n_vtx"]:
        near = np.arange(len(P))          # row n of the file IS row n of the slot
    else:
        near = _nearest(P, opos)
    R = prow[near].copy()
    if fpos:
        R[:, 0:12] = P.astype(">f4").view(np.uint8).reshape(len(P), 12)
    else:
        R[:, 0:6] = S16.view(np.uint8).reshape(len(P), 6)
    b[poff + lo * pst:poff + (lo + len(P)) * pst] = R.tobytes()

    # attribute stream — inherit everything, then overwrite what the OBJ actually defines
    ast, aoff = m["att_stride"], m["att_off"]
    if aoff != poff or ast != pst:
        arow = np.frombuffer(b[aoff + lo * ast:aoff + (lo + part["n_vtx"]) * ast],
                             np.uint8).reshape(part["n_vtx"], ast)
        Ra = arow[near].copy()
    else:
        Ra = R                                            # single interleaved stream
    N = mesh.get("nrm")
    if N is not None and len(N) == len(P):
        # take the file's normals as authoritative — rescaling them to exactly unit length
        # would move every one of them by an LSB, and the shipped normals are not quite unit
        # to begin with. Only a degenerate (zero) normal is worth touching.
        N = np.asarray(N, np.float64)
        L = np.linalg.norm(N, axis=1, keepdims=True)
        N = np.where(L > 1e-9, N, 0.0)
    else:
        N = _vertex_normals(P, T - T.min() if T.min() > 0 else T) if len(T) else None
    if m["f_nrm"] is not None and N is not None:
        q = np.clip(np.round(N * 32767.0), -32767, 32767)
        moved = (Ra[:, m["f_nrm"]:m["f_nrm"] + 6].copy().view(">i2") != q).any(1)
        _put16(Ra, m["f_nrm"], q)
        if m["f_tan"] is not None and moved.any():
            # re-orthogonalise the inherited tangent, but ONLY where the normal actually
            # changed: renormalising an untouched vertex would still shift it by an LSB, and a
            # part that was exported and imported back unedited has to come out byte-identical.
            t = _snorm(Ra[:, m["f_tan"]:m["f_tan"] + 6].copy().view(">i2"))
            t = t - N * (t * N).sum(1, keepdims=True)
            L = np.linalg.norm(t, axis=1, keepdims=True)
            t = np.where(L > 1e-9, t / np.maximum(L, 1e-9), 0.0)
            qt = np.clip(np.round(t * 32767.0), -32767, 32767)
            old = Ra[:, m["f_tan"]:m["f_tan"] + 6].copy().view(">i2")
            _put16(Ra, m["f_tan"], np.where(moved[:, None], qt, old))
    if m["f_u"] is not None and m["f_v"] is not None and len(UV):
        half = np.clip(np.round(UV / 2.0 * 32767.0), -32767, 32767)
        _put16(Ra, m["f_u"], half[:, 0:1])
        _put16(Ra, m["f_v"], half[:, 1:2])
    if Ra is not R:
        b[aoff + lo * ast:aoff + (lo + len(P)) * ast] = Ra.tobytes()
    else:
        b[poff + lo * pst:poff + (lo + len(P)) * pst] = Ra.tobytes()

    if _same_topology(b, m, part, T + lo):
        # Same triangles, only the vertices moved. Keep the artist's own strips: they pack far
        # tighter than anything a generic stripifier produces (the goalie's torso is 1,604
        # triangles in 2,422 indices — restripped it wants 2,701 and would not fit its own
        # slot), so re-stripping an untouched topology can fail a plain round trip.
        return (f"part {part['rec']} replaced — {len(P)} vertices moved, topology and the "
                f"original {part['n_idx']} indices kept")
    raw = mesh.get("strip")
    if raw is not None:
        # The caller brought the ORIGINAL strip stream this mesh was shipped with (part-local,
        # 0xFFFF restarts kept). Use it verbatim. Re-stripifying a shell lifted off another
        # asset is what breaks a transplant that would otherwise fit: a 663-triangle beard the
        # artist packed into 1,096 indices comes back out of a generic stripifier at 1,205 and
        # no longer fits the slot it came from. The artist's packing is not reproducible, so it
        # has to be carried rather than recomputed.
        raw = np.asarray(raw, np.int64)
        stream = np.where(raw == 0xFFFF, 0xFFFF, raw + lo).tolist()
    else:
        strips = AM._stripify(T + lo)
        stream = AM._strip_stream(strips)
    if len(stream) > part["n_idx"]:
        raise ValueError(
            f"{len(T)} triangles pack into {len(stream)} indices but this part's slot holds "
            f"{part['n_idx']}. Nothing was written for this part — simplify it, or split the "
            "change across parts.")
    ibp = m["ib0"] + part["first_idx"] * 2
    pad = [0xFFFF] * (part["n_idx"] - len(stream))
    b[ibp:ibp + part["n_idx"] * 2] = np.array(stream + pad, ">u2").tobytes()
    return (f"part {part['rec']} replaced — {len(P)} vertices, {len(T)} triangles "
            f"({len(stream)}/{part['n_idx']} indices used)")


def _canon(T) -> set:
    """Triangles as a set, each rotated to start at its lowest index so the same winding
    compares equal however the OBJ happened to write it out."""
    out = set()
    for a, c, e in np.asarray(T, np.int64):
        a, c, e = int(a), int(c), int(e)
        k = min(range(3), key=lambda i: (a, c, e)[i])
        out.add(((a, c, e)[k], (a, c, e)[(k + 1) % 3], (a, c, e)[(k + 2) % 3]))
    return out


def _same_topology(b, m: dict, part: dict, T) -> bool:
    """Does the incoming triangle list match what is already in this part's index slot?"""
    ib = np.frombuffer(bytes(b[m["ib0"] + part["first_idx"] * 2:
                              m["ib0"] + (part["first_idx"] + part["n_idx"]) * 2]), ">u2")
    old = AM._strip_to_tris(ib)
    return len(old) == len(T) and _canon(old) == _canon(T)


def _put16(R, k, vals):
    """Write an (n, w) signed-16 array into byte column k of a uint8 vertex block."""
    v = np.asarray(vals).astype(">i2").view(np.uint8).reshape(len(R), -1)
    R[:, k:k + v.shape[1]] = v


def _nearest(P, opos):
    """For each new vertex, the original vertex it should inherit skinning from."""
    if len(P) * len(opos) <= 4_000_000:
        return np.argmin(((P[:, None, :] - opos[None, :, :]) ** 2).sum(2), 1)
    near = np.empty(len(P), np.int64)
    for k in range(0, len(P), 512):
        blk = P[k:k + 512]
        near[k:k + 512] = np.argmin(((blk[:, None, :] - opos[None, :, :]) ** 2).sum(2), 1)
    return near


def _obj_raw(path):
    """(positions, uvs, normals, {group: [[(v, vt, vn), ...], ...]}).

    arena_model's parser throws `vn` away; a character mesh needs it. Authored normals carry
    the hard edges an area-weighted recompute would smooth over — and a plain export/import
    round trip has to give the vertex back exactly as it was.
    """
    P, UV, NR, G, cur = [], [], [], {}, ""
    for line in Path(path).read_text(errors="replace").splitlines():
        w = line.split()
        if not w:
            continue
        if w[0] == "v":
            P.append([float(x) for x in w[1:4]])
        elif w[0] == "vt":
            UV.append([float(w[1]), float(w[2]) if len(w) > 2 else 0.0])
        elif w[0] == "vn":
            NR.append([float(x) for x in w[1:4]])
        elif w[0] in ("g", "o"):
            cur = w[1] if len(w) > 1 else ""
        elif w[0] == "f":
            idx = []
            for tok in w[1:]:
                a = (tok.split("/") + ["", ""])[:3]
                vi = int(a[0])
                idx.append((vi, int(a[1]) if a[1] else vi, int(a[2]) if a[2] else vi))
            faces = G.setdefault(cur, [])
            for k in range(1, len(idx) - 1):              # fan-triangulate n-gons
                faces.append([idx[0], idx[k], idx[k + 1]])
    return P, UV, NR, G


def read_obj_groups(path) -> dict:
    """OBJ groups with the FILE's vertex order preserved.

    arena_model's reader renumbers vertices in face order and drops any that no face uses,
    which is right for a regenerated arena mesh but wrong here: a character slot's rows carry
    skin weights and bone indices, so row n of the group has to stay row n of the slot. When a
    group is not a plain `vt` in lockstep with `v` (an editor that has added UV seams, say),
    fall back to the generic reader and let replace_part inherit skinning by proximity.
    """
    P, UV, NR, G = _obj_raw(path)
    out = {}
    for name, F in G.items():
        if not F:
            continue
        pairs = [(vi, ti) for f in F for vi, ti, _ in f]
        lo, hi = min(v for v, _ in pairs), max(v for v, _ in pairs)
        if all(ti == vi for vi, ti in pairs) and lo >= 1 and hi <= len(P) and hi <= len(UV):
            uv = np.array(UV[lo - 1:hi], np.float32)
            uv[:, 1] = 1.0 - uv[:, 1]
            g = dict(pos=np.array(P[lo - 1:hi], np.float32), uv=uv,
                     tris=np.array([[vi - lo for vi, _, _ in f] for f in F], np.int32),
                     ordered=True)
            if len(NR) >= hi and all(ni == vi for f in F for vi, _, ni in f):
                g["nrm"] = np.array(NR[lo - 1:hi], np.float32)   # keep the authored normals
            out[name] = g
        else:
            out[name] = AM._resolve_faces(f"{Path(path).name} [{name or 'default'}]", P, UV,
                                          [[(vi, ti) for vi, ti, _ in f] for f in F])
    if not out:
        raise ValueError(f"{Path(path).name}: no faces found")
    return out


def replace_model_obj(b: bytearray, models: list[dict], path, mi: int = None,
                      log=print) -> list[str]:
    """Import an OBJ written by export_model_obj — each group back to its own submesh.

    Group names carry the routing, but a Blender round trip loses them: its OBJ importer merges
    the file into one object named after the FILE, so a re-export says `o global_102_stick`, not
    `g m102_sub000_mat0`. When the target model has a single submesh there is only one thing the
    single group can mean, so take it rather than making the user hand-edit the file.
    """
    groups = read_obj_groups(path)
    if mi is not None and len(groups) == 1 and 0 <= mi < len(models):
        only = next(iter(groups))
        if not AM.GROUP_RE.search(only or ""):
            parts = submeshes(b, models[mi])
            if len(parts) == 1:
                log(f"  group {only!r} is not a m##_sub###_mat# name, but this model has one "
                    f"submesh — using it")
                groups = {_obj_group(mi, parts[0]): groups[only]}
    msgs = []
    for name, mesh in groups.items():
        g = AM.GROUP_RE.search(name)
        if not g:
            log(f"  skipped group {name!r}: not a m##_sub###_mat# name")
            continue
        gmi, rec = int(g.group(1)) if mi is None else mi, int(g.group(2))
        if not (0 <= gmi < len(models)):
            log(f"  skipped group {name!r}: no model {gmi}")
            continue
        m = models[gmi]
        part = next((p for p in submeshes(b, m) if p["rec"] == rec), None)
        if part is None:
            log(f"  skipped group {name!r}: model {gmi} has no submesh {rec}")
            continue
        msgs.append(f"m{gmi:02d} sub{rec:03d}: " + replace_part(b, m, part, mesh, log=log))
    if not msgs:
        raise ValueError(
            f"{Path(path).name}: no group matched a submesh in this model. The OBJ needs its "
            f"`g m##_sub###_mat#` lines — in Blender, import with 'Split by Group' on and export "
            f"with 'Object Groups' (or 'Objects as OBJ Groups') on so they survive the round trip.")
    return msgs


# ───────────────────────────── write-back ─────────────────────────────
def write(new_blob: bytes, game_dir, log=print, asset: str = ASSET) -> str:
    """Put the edited blob 0 back into the archive (in place, no TOC edit)."""
    return AM.write_dram(asset, bytes(new_blob), game_dir, log=log)


def restore(game_dir, log=print, asset: str = ASSET) -> str:
    """Undo every geometry edit to this asset — the pristine blob 0 goes back."""
    return AM.restore(asset, game_dir, log=log)


def headroom(game_dir=None, asset: str = ASSET) -> dict:
    return AM.headroom(asset, game_dir)
