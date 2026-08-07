"""animpose.py — decode an animation clip's packed keyframe stream and pose a skeleton with it.

This is the piece doc 29 listed as missing ("the packed keyframe streams are NOT decoded, so there
is no posing, no skeletal preview"). Doc 30 §"+0x38 stream" now has the codec; this module is that
finding turned into something the Animations tab can draw.

    clip descriptor (doc 29 §2)              rig (skeleton.py, doc 30)
    +0x08  u64 rotation channel mask         bone array, 48 B records, translation-only bind pose
    +0x10  u64 translation channel mask
    +0x18  packed counts (n_rot/n_trans/…)
    +0x1C  sample count | bit30 = table column
    +0x38  -> packed sample stream

THE CODEC (`Anim_DecompressQuatBlock` @0x83B46810)
  Channels are decoded four at a time from a **24-byte planar group**: four X halfwords at +0x00,
  four Y at +0x08, four Z at +0x10. Each halfword is `[bit15 = selector bit][bits14..0 = signed
  magnitude]`, valued `sext15(hw) / 16384 * 23/32`. The fourth component is *reconstructed* as
  `W = sqrt(1 - X² - Y² - Z²)`. Classic smallest-three compression.
  Validated over the whole library: 1,897,029 slots, zero negative radicands, and the reconstructed
  component is the largest one on 100.000% of them.

  SELECTOR. `sel = (X.bit15 << 1) | Y.bit15` says which slot was dropped, and the quaternion is
  simply `rotate_right((X, Y, Z, W), sel)` — the three stored components keep their cyclic order:

      sel 0 -> (X, Y, Z, W)      sel 2 -> (Z, W, X, Y)
      sel 1 -> (W, X, Y, Z)      sel 3 -> (Y, Z, W, X)

  This is the `lvsl`/`vperm128` pair at 0x83b469a8/0x83b46a5c, whose byte amount is built as
  `12 - (X.bit15*8 + Y.bit15*4)` = 12/8/4/0 = a 3/2/1/0-lane rotate. Confirmed against the live
  capture: on samples whose decoded quaternion exactly matched a live bone's local rotation, the
  ordering came out right on 101/101 sel-0 samples and 3/3 sel-1 samples, and the two cases imply
  the same pre-permute vector independently.

  Translations ride in the leftover lanes of the last rotation group at `sext16(hw) / 128`.

CHANNEL -> BONE
  `Anim_BuildChannelSlotMap` @0x83B46738 expands a per-rig 3-byte-per-entry table into a byte array
  with `out[k] = table[3*i + 1 + col]` for the k-th set mask bit i, so a clip's mask bit is a
  *channel*, not a bone index. The stock fallback table @0x82000980 is the identity, and no clip in
  the library sets the column bit, so the clip-side channel order is fixed — and it is CHANNELS
  below. See that table's docstring for how each entry was established.
"""
from __future__ import annotations

import math
import struct
from pathlib import Path

try:
    from . import animations, skeleton
except ImportError:                       # run as a loose script from launcher/
    import animations
    import skeleton

# ── the codec's tuning constants (doc 30) ───────────────────────────────────────────────────
QUAT_SCALE = (23.0 / 32.0) / 16384.0     # 0.71875 / 16384; 0.71875 just clears 1/sqrt(2)
TRANS_SCALE = 1.0 / 128.0
UNUSED_SLOT = 0x3F                       # fill byte in the channel->slot array
DEFAULT_TABLE_VA = 0x82000980            # identity fallback table

GROUP_BYTES = 24                         # one 4-channel planar group
N_CHANNELS = 28                          # = (clip+0x18 >> 18) & 0x3F on every clip in the library

# ── clip channel order ──────────────────────────────────────────────────────────────────────
# Established three independent ways, all agreeing:
#
#  1. EXACT MATCH against a live 900-frame capture of a goalie in Xenia (pose3.bin): decoded clip
#     quaternions and live local rotations were quantised to 1/3000 and intersected. Background is
#     0-2 hits; real hits stand far clear — channel 4 = def_Neck (22), 9 = def_L_Hand (23),
#     15 = def_R_Hand (15), 18 = def_L_Thigh (14), 20 = def_L_Foot (7), 23 = def_R_Thigh (17).
#  2. MIRROR PAIRING within the clip data alone: L/R counterparts must be mirror images, so their
#     mirror-invariant fingerprints overlap. Mutual-best pairs came out at exactly the offsets this
#     table predicts — +6 across the arms (6<->12, 7<->13, 8<->14, 11<->17) and +5 across the legs
#     (19<->24, 21<->26, 22<->27).
#  3. ANATOMY from the per-channel angle statistics: spine chain 9-12 deg median; upper arms and
#     thighs 44-58; elbows (8/14) and knees (19/24) hold only ~6-10k distinct values across the
#     whole library because they are 1-DOF hinges; fingers ~2k; toes (21/26) sit at 2 deg median.
#
# Channels 22/27 are the one soft spot: a near-static L/R pair (4 deg median) that is NOT a toe tip
# — no rig has def_*_Toe01. def_L/R_inner_thigh is the only unassigned pair present in both rigs and
# parented to the pelvis, and its range fits, so it is used here but flagged inferred.
# written out positionally so the index really is the mask bit
CHANNELS = [
    "def_Pelvis",        # 0
    "def_Spine",         # 1
    "def_Spine1",        # 2
    "def_Spine2",        # 3
    "def_Neck",          # 4   <- live exact match, 22 hits
    "def_Head",          # 5
    "def_L_Clavicle",    # 6
    "def_L_UpperArm",    # 7
    "def_L_Forearm",     # 8   <- hinge signature
    "def_L_Hand",        # 9   <- live exact match, 23 hits
    "def_L_Finger0",     # 10
    "def_L_Finger01",    # 11
    "def_R_Clavicle",    # 12
    "def_R_UpperArm",    # 13
    "def_R_Forearm",     # 14  <- hinge signature
    "def_R_Hand",        # 15  <- live exact match, 15 hits
    "def_R_Finger0",     # 16
    "def_R_Finger01",    # 17
    "def_L_Thigh",       # 18  <- live exact match, 14 hits
    "def_L_Calf",        # 19  <- hinge signature
    "def_L_Foot",        # 20  <- live exact match, 7 hits
    "def_L_Toe0",        # 21
    "def_L_inner_thigh", # 22  <- INFERRED
    "def_R_Thigh",       # 23  <- live exact match, 17 hits
    "def_R_Calf",        # 24  <- hinge signature
    "def_R_Foot",        # 25
    "def_R_Toe0",        # 26
    "def_R_inner_thigh", # 27  <- INFERRED
]
assert len(CHANNELS) == N_CHANNELS
INFERRED_CHANNELS = {22, 27}


def channel_map(bones) -> list[int]:
    """CHANNELS -> bone index for one rig, -1 where the rig lacks that bone.

    Both stock rigs resolve all 28; the lookup is by name so a modified rig degrades gracefully
    instead of posing the wrong joint."""
    idx = {b["name"]: b["index"] for b in bones}
    return [idx.get(n, -1) for n in CHANNELS]


# ── stream decode ───────────────────────────────────────────────────────────────────────────
def _sext15(u: int) -> int:
    return ((u & 0x7FFF) ^ 0x4000) - 0x4000


def _sext16(u: int) -> int:
    return u - 0x10000 if u & 0x8000 else u


def clip_layout(clip: dict, data: bytes, segs) -> dict:
    """Header fields that describe the packed stream. Pure arithmetic on the descriptor."""
    f18, f1c = clip["f18"], clip["f1c"]
    n_rot = (f18 >> 12) & 0x3F
    n_trans = (f18 >> 6) & 0x3F
    groups = (n_rot + n_trans + 3) >> 2
    rot_mask = struct.unpack_from(">Q", data, clip["off"] + 0x08)[0]
    trans_mask = struct.unpack_from(">Q", data, clip["off"] + 0x10)[0]
    stream_va = struct.unpack_from(">I", data, clip["off"] + 0x38)[0]
    return {
        # Sample count. The sampler scales normalised time by N-1, which made this field look like
        # it held N-1; it does not. The decoded stream settles it: `1 - X^2 - Y^2 - Z^2` is never
        # negative in real sample data, and reading one extra sample drives it negative in 1,356 of
        # 3,280 clips, while N = field is clean on all 3,280 (74,117 last-sample slots).
        "samples": f1c & 0x3FFF,
        "n_rot": n_rot,
        "n_trans": n_trans,
        "n_channels": (f18 >> 18) & 0x3F,
        "column": (f1c >> 30) & 1,
        "groups": groups,
        "stride": groups * GROUP_BYTES,
        "rot_mask": rot_mask,
        "trans_mask": trans_mask,
        "rot_channels": [i for i in range(64) if rot_mask & (1 << i)],
        "trans_channels": [i for i in range(64) if trans_mask & (1 << i)],
        "stream_va": stream_va,
        "stream_off": animations._va_to_off(segs, stream_va),
    }


def decode(xex_path, clip: dict, segs=None, data=None) -> dict:
    """Decode a clip's whole rotation (and translation) stream.

    Returns {samples, channels, rot, trans, layout} where `rot[frame][channel]` is an (x,y,z,w)
    quaternion for each channel the clip actually animates, keyed by mask bit. Channels the clip
    does not touch are simply absent — the caller leaves those bones at bind pose.
    """
    segs = segs if segs is not None else animations._segments(xex_path)
    data = data if data is not None else Path(xex_path).read_bytes()
    L = clip_layout(clip, data, segs)
    base, stride, n = L["stream_off"], L["stride"], L["samples"]
    chans = L["rot_channels"]
    if base is None or L["n_rot"] == 0 or len(chans) != L["n_rot"]:
        return {"samples": 0, "channels": [], "rot": [], "trans": [], "layout": L}
    if base + n * stride > len(data):                      # trust the data, not the header
        n = max(0, (len(data) - base) // stride)

    rot, trans = [], []
    tchans = L["trans_channels"]
    for s in range(n):
        off = base + s * stride
        frame, tframe = {}, {}
        for g in range(L["groups"]):
            hw = struct.unpack_from(">12H", data, off + g * GROUP_BYTES)
            for lane in range(4):
                ch = g * 4 + lane
                if ch < L["n_rot"]:
                    x = _sext15(hw[lane]) * QUAT_SCALE
                    y = _sext15(hw[4 + lane]) * QUAT_SCALE
                    z = _sext15(hw[8 + lane]) * QUAT_SCALE
                    w = math.sqrt(max(0.0, 1.0 - (x * x + y * y + z * z)))
                    # the dropped component is rotated back into its slot (see SELECTOR above)
                    sel = ((hw[lane] >> 15) << 1) | (hw[4 + lane] >> 15)
                    q = (x, y, z, w)
                    frame[chans[ch]] = q[-sel:] + q[:-sel] if sel else q
                else:                                       # leftover lanes carry translation
                    t = ch - L["n_rot"]
                    if t < L["n_trans"] and t < len(tchans):
                        tframe[tchans[t]] = (_sext16(hw[lane]) * TRANS_SCALE,
                                             _sext16(hw[4 + lane]) * TRANS_SCALE,
                                             _sext16(hw[8 + lane]) * TRANS_SCALE)
        rot.append(frame)
        trans.append(tframe)
    return {"samples": n, "channels": chans, "rot": rot, "trans": trans, "layout": L}


# ── quaternion / matrix helpers ─────────────────────────────────────────────────────────────
def _slerp(a, b, t):
    d = sum(p * q for p, q in zip(a, b))
    if d < 0.0:
        b, d = tuple(-v for v in b), -d
    if d > 0.9995:                                          # degenerate -> lerp + renormalise
        out = tuple(p + (q - p) * t for p, q in zip(a, b))
    else:
        th = math.acos(max(-1.0, min(1.0, d)))
        s = math.sin(th)
        wa, wb = math.sin((1 - t) * th) / s, math.sin(t * th) / s
        out = tuple(p * wa + q * wb for p, q in zip(a, b))
    n = math.sqrt(sum(v * v for v in out)) or 1.0
    return tuple(v / n for v in out)


def _qmat(q):
    x, y, z, w = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w),     2 * (x * z + y * w)),
            (2 * (x * y + z * w),     1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w),     2 * (y * z + x * w),     1 - 2 * (x * x + y * y)))


def _mmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                 for i in range(3))


def _mvec(m, v):
    return tuple(sum(m[i][k] * v[k] for k in range(3)) for i in range(3))


def sample(dec: dict, t: float) -> dict:
    """Channel -> quaternion at normalised time t in [0,1], slerped between the two bracketing
    samples exactly as `Anim_SampleClipAtTime` does."""
    n = dec["samples"]
    if n == 0:
        return {}
    if n == 1:
        return dict(dec["rot"][0])
    u = max(0.0, min(1.0, t)) * (n - 1)
    i = min(int(u), n - 2)
    f = u - i
    a, b = dec["rot"][i], dec["rot"][i + 1]
    return {c: _slerp(a[c], b[c], f) for c in a if c in b}


# ── helper bones ────────────────────────────────────────────────────────────────────────────
# Clips key only the ~26 core bones (doc 30). Everything else in a 73/78-bone rig is a helper
# the runtime drives, and several of those helpers are *twins*: a second chain hanging off the
# same parent as a keyed chain, there so equipment can be weighted separately from flesh. The
# goalie's leg pads are the clearest case --
#
#     Thigh -> Calf -> Foot              keyed in 205/205 clips sampled
#     Thigh -> Calf_upper -> Calf_lower  keyed in   0/205, and the pads hang off it
#
# so with the helpers left at bind the pad top follows the thigh while the shin half stays put,
# and a butterfly drives the pads through the ice. Copying the twin's LOCAL rotation reproduces
# the constraint: `Calf_upper` shares Calf's parent and sits 6 cm from it, `Calf_lower` stands in
# the same relation to `Calf_upper` as `Foot` does to `Calf`. The arm entries are the same shape
# (`humerus_aimed` shares Clavicle with `UpperArm`, then twistA/twistB mirror Forearm/Hand).
#
# Keyed by name suffix: a bone `<pre>_<key>` takes the rotation of `<pre>_<value>`.
HELPER_TWIN = {
    "Calf_upper": "Calf", "Calf_lower": "Foot",
    "humerus_aimed": "UpperArm", "twistA": "Forearm", "twistB": "Hand",
}


def helper_twins(sk: dict) -> dict:
    """bone index -> index of the keyed bone whose local rotation it should copy. Cached on `sk`."""
    hit = sk.get("_twins")
    if hit is None:
        by_name = {b["name"]: b["index"] for b in sk["bones"]}
        hit = {}
        for b in sk["bones"]:
            nm = b["name"]
            for k, v in HELPER_TWIN.items():
                if nm.endswith("_" + k) and (nm[:-len(k)] + v) in by_name:
                    hit[b["index"]] = by_name[nm[:-len(k)] + v]
                    break
        sk["_twins"] = hit
    return hit


# ── posing ──────────────────────────────────────────────────────────────────────────────────
def pose(sk: dict, dec: dict, t: float, root_motion: bool = True,
         helpers: bool = True) -> list[dict]:
    """Pose a rig at normalised time t.

    Returns one entry per bone: {index, name, pos, matrix} where `pos` is the world-space joint
    position in the same units and frame as the mesh (doc 22: ~1 unit = 1 cm, +Y up, +Z front), so
    posed joints drop straight onto decoded vertices with no extra transform.

    The bind pose is translation-only, so a bone's world transform is
    `parent_world . translate(local offset) . clip rotation`.

    `helpers` fills in the twin bones the clips never key (see HELPER_TWIN) — without it the
    goalie's pads stay straight while the leg folds. They stay `animated: False`, since that flag
    means "the clip keys this", and the overlay uses it to show what the data actually contains.
    """
    bones = sk["bones"]
    cmap = channel_map(bones)
    bone_q = {}
    for ch, q in sample(dec, t).items():
        if ch < len(cmap) and cmap[ch] >= 0:
            bone_q[cmap[ch]] = q
    keyed = set(bone_q)
    if helpers:
        for i, j in helper_twins(sk).items():
            if i not in bone_q and j in bone_q:
                bone_q[i] = bone_q[j]
    bone_t = {}
    if root_motion and dec["samples"]:
        n = dec["samples"]
        idx = min(int(max(0.0, min(1.0, t)) * (n - 1)), n - 1)
        for ch, v in dec["trans"][idx].items():
            if ch < len(cmap) and cmap[ch] >= 0:
                bone_t[cmap[ch]] = v

    out = []
    for b in bones:
        i, p = b["index"], b["parent"]
        off = bone_t.get(i, b["offset"])
        rot = _qmat(bone_q[i]) if i in bone_q else ((1, 0, 0), (0, 1, 0), (0, 0, 1))
        if p < 0:
            pos, mat = tuple(off), rot
        else:
            pm, pp = out[p]["matrix"], out[p]["pos"]
            pos = tuple(pp[k] + _mvec(pm, off)[k] for k in range(3))
            mat = _mmul(pm, rot)
        out.append({"index": i, "name": b["name"], "pos": pos, "matrix": mat,
                    "parent": p, "animated": i in keyed})
    return out


def skin_pose(pos, nrm, sk: dict, skinning: dict, posed: list[dict], bind=None):
    """Deform a mesh by a posed rig — linear blend skinning. -> (positions, normals).

    The bind pose is translation-only (doc 30: bone `+0x00` is a position, there is no rotation
    field), so a bone's inverse bind transform is just "subtract its bind position". That makes
    the whole thing `v' = SUM w_k . (R_k . (v - bind_k) + t_k)` with no matrix inversion and no
    accumulated error. `pos` and the rig share a frame already (~1 unit = 1 cm), so nothing else
    has to line up.

    `bind` overrides the bind joints, in the same bone order. A head asset is modelled against
    its OWN copy of the rig; binding it to those joints while posing it with the body's is what
    seats it on the body's neck when the two disagree (char_model.head_scene).
    """
    import numpy as np
    R = np.array([p["matrix"] for p in posed], np.float32)          # world rotation per bone
    T = np.array([p["pos"] for p in posed], np.float32)             # world joint per bone
    B = (np.array([b["world"] for b in sk["bones"]], np.float32) if bind is None
         else np.asarray(bind, np.float32))                         # bind joint per bone
    idx, w = skinning["idx"], skinning["wts"]
    out = np.zeros_like(pos, np.float32)
    onr = np.zeros_like(nrm, np.float32) if nrm is not None else None
    for k in range(idx.shape[1]):
        i = idx[:, k]
        wk = w[:, k:k + 1]
        if not wk.any():
            continue
        out += wk * (np.einsum("vij,vj->vi", R[i], pos - B[i]) + T[i])
        if onr is not None:
            onr += wk * np.einsum("vij,vj->vi", R[i], nrm)
    if onr is not None:
        n = np.linalg.norm(onr, axis=1, keepdims=True)
        onr = onr / np.where(n < 1e-6, 1.0, n)
    return out, onr


def frame_times(dec: dict, clip: dict) -> list[float]:
    """Wall-clock seconds for each stored sample, from the clip's own duration field."""
    n = dec["samples"]
    if n <= 1:
        return [0.0] * n
    d = float(clip.get("duration") or 0.0)
    return [d * i / (n - 1) for i in range(n)]


# ── export ──────────────────────────────────────────────────────────────────────────────────
def export_obj(sk: dict, dec: dict, dest, t: float = 0.0) -> Path:
    """One posed frame as an OBJ skeleton (vertices = joints, lines = bones)."""
    dest = Path(dest)
    P = pose(sk, dec, t)
    with dest.open("w", encoding="utf-8") as fh:
        fh.write("# NHL 2k10 posed skeleton — %s, t=%.4f\n" % (sk["name"], t))
        for b in P:
            fh.write("v %.4f %.4f %.4f\n" % b["pos"])
        for b in P:
            if b["parent"] >= 0:
                fh.write("l %d %d\n" % (b["parent"] + 1, b["index"] + 1))
    return dest


def export_frames_obj(sk: dict, dec: dict, clip: dict, dest_dir, stride: int = 1) -> list[Path]:
    """Every sample as its own OBJ, ready to import as a sequence."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    n = dec["samples"]
    out = []
    for i in range(0, n, max(1, stride)):
        t = 0.0 if n <= 1 else i / (n - 1)
        out.append(export_obj(sk, dec, dest_dir / ("frame_%04d.obj" % i), t))
    return out


def export_json(sk: dict, dec: dict, clip: dict, dest) -> Path:
    """The whole decoded clip: channel names, per-sample quaternions, and the rig it binds to."""
    import json
    dest = Path(dest)
    cmap = channel_map(sk["bones"])
    names = {b["index"]: b["name"] for b in sk["bones"]}
    payload = {
        "clip_va": "0x%08X" % clip["va"],
        "clip_hash": "0x%08X" % clip["hash"],
        "duration": clip.get("duration"),
        "samples": dec["samples"],
        "rig": sk["name"],
        "channels": [
            {"bit": c, "channel": CHANNELS[c] if c < N_CHANNELS else "channel_%d" % c,
             "bone": names.get(cmap[c], None) if c < len(cmap) else None,
             "inferred": c in INFERRED_CHANNELS}
            for c in dec["channels"]
        ],
        "times": frame_times(dec, clip),
        "rotations": [[list(fr[c]) for c in dec["channels"]] for fr in dec["rot"]],
    }
    dest.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return dest


def summary(xex_path, clip: dict, segs=None, data=None) -> str:
    """One-screen human report — what this clip animates and how."""
    dec = decode(xex_path, clip, segs, data)
    L = dec["layout"]
    lines = [
        "clip 0x%08X  hash %08X  %.4f s" % (clip["va"], clip["hash"], clip.get("duration") or 0.0),
        "  %d samples x %d rotation + %d translation channels (%d B/sample)"
        % (dec["samples"], L["n_rot"], L["n_trans"], L["stride"]),
        "  stream 0x%08X   channel mask %016X" % (L["stream_va"], L["rot_mask"]),
        "  animates:",
    ]
    for c in dec["channels"]:
        nm = CHANNELS[c] if c < N_CHANNELS else "channel_%d" % c
        lines.append("    bit %2d  %s%s" % (c, nm, "   (inferred)" if c in INFERRED_CHANNELS else ""))
    if L["trans_channels"]:
        lines.append("  translates: " + ", ".join(
            CHANNELS[c] if c < N_CHANNELS else "channel_%d" % c for c in L["trans_channels"]))
    return "\n".join(lines)
