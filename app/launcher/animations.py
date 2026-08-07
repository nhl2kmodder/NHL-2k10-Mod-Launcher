"""animations.py — read/edit NHL 2k10's animation library, which lives INSIDE default.xex.

Doc 29 (`29_animation_library.md`). The game has no animation asset: all **3,280 clips**
(~75 minutes) are compiled into the XEX's `.rdata`, spanning VA 0x82092278-0x83A749C8. Clips are
reached only by direct pointer from tables in `.rdata`/`.data` — there is no id, no name table and
no hash lookup, which is why no `.iff`/`names.txt` entry ever matched one.

Everything here works on the **flat** default.xex alone; the game does not need to be running.

WHAT THIS MODULE CAN DO
  * enumerate every clip + its metadata and event tracks          (scan_clips / read_events)
  * retime a clip                                                 (set_duration)
  * retime an animation event inside a clip                       (set_event_time)
  * repoint a table slot at a different clip = "play THAT instead" (set_table_slot)
  * byte-exact backup/restore of a clip's raw region              (export_region / restore_region)

DECODING THE KEYFRAMES lives next door in `animpose.py`, which cracks the packed rotation stream
  at `+0x38` (smallest-three quaternions, planar 4-channel groups) and binds its 28 channels to
  named bones on the `skeleton.py` rigs. That gives posing, skeletal preview and OBJ/JSON export;
  the whole library validates clean (1,971,146 quaternions, all unit to 1.1e-16, zero bone-length
  drift). Still out of reach: writing a stream back, i.e. importing authored motion. Retime +
  repoint remain the levers for changing behaviour — doc 28 proved goalie save latency is
  animation-driven rather than timer-driven, so retiming g_AnimClip_GoalieDive changes reaction
  speed directly.

NAMES ARE GONE. `+0x00` is a build-time hash with no runtime consumer; crc32 of all 597,510 strings
in the image matched nothing (1 hit, garbage — chance level). So this module keeps a **user-side
name database** in %APPDATA% instead: label clips yourself, share the JSON. `ref_label()` seeds
that with meaning derived from whichever table points at a clip.
"""
from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path

try:
    from . import xex_patch
except ImportError:                       # run as a loose script from launcher/
    import xex_patch

IMAGE_BASE = 0x82000000
RDATA_LO, RDATA_HI = 0x82000400, 0x83AE95DF      # .rdata segment (holds every clip)

# ── clip descriptor layout (doc 29 §2) ──────────────────────────────────────────────────────
OFF_HASH, OFF_BONES = 0x00, 0x0C
OFF_F18, OFF_F1C, OFF_F20, OFF_F24 = 0x18, 0x1C, 0x20, 0x24
OFF_DURATION = 0x28                      # float seconds — the field Function_83E3B568 reads
OFF_TICKS = 0x2C
OFF_TRACKS = (0x38, 0x3C, 0x40, 0x44)    # 0x40 is the keyed event list
OFF_EVENTS = 0x40

EVENT_STRIDE = 0x0C                      # {u32 key, float time, void* payload}
EVENT_TERMINATOR = 0x80000000

MAX_DURATION = 120.0                     # sanity clamp for writes


# ── the pointer tables that choose which clip plays ─────────────────────────────────────────
# `label(i)` turns a slot index into something a human can act on. Only tables verified present
# in the FILE are listed — g_Anim_ClipSetTable @0x84FD3890 is deliberately absent because it is
# BSS (built at runtime), so it cannot be edited statically.
def _skate_label(i):
    return f"{'fast' if i // 64 else 'normal'} · dir {(i // 4) % 16:2d} · phase {i % 4}"


_POSTURE = "0 · 1 · 2 · 3 · 4 · 5 · 6 · 7 · 8 · 9".split(" · ")


def _posture_label(i):
    return f"posture category {i}"


KNOWN_TABLES = [
    dict(key="skate", va=0x82048370, count=128, label=_skate_label,
         name="Skating — directional matrix",
         note="Entity_SelectDirectionalSkateAnim @0x83E00598 indexes this as "
              "[normal|fast][16 compass dirs][4 cycle phases]. Every stock entry is a 0.53 s "
              "stride loop. Dir bins 7=8 and 9=10=11=12 share clips (rear arc)."),
    dict(key="loco_fwd", va=0x84A07D4C, count=10, label=_posture_label,
         name="Locomotion — forward",
         note="Entity_UpdateLocomotionAndSelectAnim @0x83ECF578, indexed by posture category. "
              "Only categories 3,4,5,7 are populated. Shared by skaters AND goalies."),
    dict(key="loco_back", va=0x84A07D74, count=10, label=_posture_label,
         name="Locomotion — backward", note="As above; facing is offset by 0xC000."),
    dict(key="loco_lat_a", va=0x84A07D9C, count=10, label=_posture_label,
         name="Locomotion — lateral A", note="Left/right picked by entity+0xEC bit 0x80000000."),
    dict(key="loco_lat_b", va=0x84A07DC4, count=10, label=_posture_label,
         name="Locomotion — lateral B", note="The mirrored half of the lateral pair."),
    dict(key="loco_fastlat_a", va=0x84A07DEC, count=10, label=_posture_label,
         name="Locomotion — fast lateral A",
         note="Used above threshold DAT_84A07E8C (5.0) — the goalie shuffle→C-push. "
              "Only categories 2, 8, 9 are populated."),
    dict(key="loco_fastlat_b", va=0x84A07E14, count=10, label=_posture_label,
         name="Locomotion — fast lateral B", note="Mirrored half of the fast-lateral pair."),
    dict(key="loco_turn_a", va=0x84A07E3C, count=10, label=_posture_label,
         name="Locomotion — turn A", note="Chosen when |delta angle| >= DAT_84A07E94 (5.0)."),
    dict(key="loco_turn_b", va=0x84A07E64, count=10, label=_posture_label,
         name="Locomotion — turn B", note="Mirrored half of the turn pair."),
]
TABLES_BY_KEY = {t["key"]: t for t in KNOWN_TABLES}

# Clips reached by a code-embedded lis/addi rather than a table. They cannot be REPOINTED without
# patching code, but they can still be retimed — which for the dive is the interesting knob.
NAMED_CLIPS = {
    0x82BC2728: ("g_AnimClip_GoalieDive",
                 "The goalie lunge/dive. GoalieBehavior_Lunge_Update @0x83EFCA80 polls the anim "
                 "controller until this clip is reached, so its duration IS the save reaction "
                 "latency (doc 28 §4). Stock 2.7667 s."),
}

# Words that must match before any write — catches a wrong/incompatible/relinked XEX. v1.1
# (Title Update #1) is a full relink, so every VA here is meaningless there and these will fail,
# which is exactly the intent.
SENTINELS = [
    (0x82048370, 0x8263C7C8),   # skate matrix slot 0
    (0x82BC2728, 0x9AD693E2),   # goalie dive clip name hash
    (0x84A07D58, 0x837DC700),   # locomotion fwd, posture category 3
    (0x84A07E64 + 0x1C, 0x837CD370),   # turn B, category 7
]


# ── VA <-> file offset ──────────────────────────────────────────────────────────────────────
def _segments(xex_path):
    """[(file_start, file_end, va_start)] for every data-bearing basic block. The flat XEX is a
    list of (data_size, zero_size) blocks, so VA->offset is not linear and the inverse needs the
    same walk."""
    data_start, image_base, blocks = xex_patch.parse_basic_blocks(xex_path)
    segs, mem, file = [], 0, data_start
    for data_size, zero_size in blocks:
        if data_size:
            segs.append((file, file + data_size, image_base + mem))
        mem += data_size + zero_size
        file += data_size
    return segs


def _off_to_va(segs, off):
    for fs, fe, va in segs:
        if fs <= off < fe:
            return va + (off - fs)
    return None


def _va_to_off(segs, va):
    for fs, fe, vs in segs:
        if vs <= va < vs + (fe - fs):
            return fs + (va - vs)
    return None


def validate(xex_path):
    """Raise ValueError unless this is the flat v1.0 default.xex we know. Returns the segment map
    so callers can reuse it."""
    enc, comp = xex_patch.get_comp_type(xex_path)
    if comp != 1:
        raise ValueError(
            f"default.xex is compressed (comp_type={comp}). Flatten it first — the Gameplay tab's "
            "Apply does this automatically, or run XexTool -c u -e u.")
    segs = _segments(xex_path)
    data = Path(xex_path).read_bytes()
    for va, want in SENTINELS:
        off = _va_to_off(segs, va)
        if off is None:
            raise ValueError(f"animation sentinel VA 0x{va:X} is not mapped by this XEX")
        got = struct.unpack_from(">I", data, off)[0]
        if got != want:
            raise ValueError(
                f"animation sentinel mismatch @VA 0x{va:X}: file has 0x{got:08X}, expected "
                f"0x{want:08X}. This is not the v1.0 default.xex (Title Update #1 is a full "
                f"relink — none of these addresses exist there).")
    return segs


# ── clip enumeration ────────────────────────────────────────────────────────────────────────
# Matches all 3,280 clips with zero false positives (doc 29 §2): +0x04..0x0B and +0x10..0x16 are
# zero and bytes +0x18/+0x19 are 0F 71.
_ANCHOR = re.compile(rb"\x0f\x71")
_ZERO8 = b"\x00" * 8
_ZERO7 = b"\x00" * 7


def _count_events(segs, data, desc_off):
    """How many keyed events this clip carries — cheap enough to fold into the scan."""
    p = _va_to_off(segs, struct.unpack_from(">I", data, desc_off + OFF_EVENTS)[0])
    if p is None:
        return 0
    n = 0
    while n < 4000 and p + 4 <= len(data):
        if struct.unpack_from(">I", data, p)[0] == EVENT_TERMINATOR:
            break
        n += 1
        p += EVENT_STRIDE
    return n


def scan_clips(xex_path, progress=None):
    """Enumerate every animation clip in the XEX. Returns a list of dicts sorted by VA:
    {va, hash, bones, duration, ticks, f18, f1c, f20, f24, off}."""
    segs = validate(xex_path)
    data = Path(xex_path).read_bytes()
    out = []
    for m in _ANCHOR.finditer(data):
        off = m.start() - 0x18
        if off < 0 or off + 0x80 > len(data):
            continue
        if data[off + 4:off + 12] != _ZERO8 or data[off + 0x10:off + 0x17] != _ZERO7:
            continue
        va = _off_to_va(segs, off)
        if va is None or not (RDATA_LO <= va <= RDATA_HI):
            continue
        u = lambda o: struct.unpack_from(">I", data, off + o)[0]      # noqa: E731
        f = lambda o: struct.unpack_from(">f", data, off + o)[0]      # noqa: E731
        out.append(dict(va=va, off=off, hash=u(OFF_HASH), bones=u(OFF_BONES),
                        f18=u(OFF_F18), f1c=u(OFF_F1C), f20=u(OFF_F20),
                        f24=f(OFF_F24), duration=f(OFF_DURATION), ticks=f(OFF_TICKS),
                        n_events=_count_events(segs, data, off)))
        if progress and len(out) % 500 == 0:
            progress(len(out))
    out.sort(key=lambda c: c["va"])
    return out


def read_events(xex_path, clip_va, segs=None, data=None):
    """The clip's keyed event/marker track (Track_FindByKey @0x83B47DF0). Returns
    [{key, time, payload, off}] — stride-0x0C records, terminated by key 0x80000000."""
    segs = segs or _segments(xex_path)
    data = data if data is not None else Path(xex_path).read_bytes()
    off = _va_to_off(segs, clip_va)
    if off is None:
        raise ValueError(f"clip VA 0x{clip_va:X} is not in this XEX")
    list_va = struct.unpack_from(">I", data, off + OFF_EVENTS)[0]
    p = _va_to_off(segs, list_va)
    if p is None:
        return []
    out = []
    for _ in range(4000):                       # bounded: the fattest stock clip has ~30
        key = struct.unpack_from(">I", data, p)[0]
        if key == EVENT_TERMINATOR:
            break
        out.append(dict(key=key, time=struct.unpack_from(">f", data, p + 4)[0],
                        payload=struct.unpack_from(">I", data, p + 8)[0], off=p))
        p += EVENT_STRIDE
    return out


def read_table(xex_path, key, segs=None, data=None):
    """[(slot_index, label, clip_va)] for a KNOWN_TABLES entry. clip_va 0 = empty slot."""
    t = TABLES_BY_KEY[key]
    segs = segs or _segments(xex_path)
    data = data if data is not None else Path(xex_path).read_bytes()
    off = _va_to_off(segs, t["va"])
    if off is None:
        raise ValueError(f"table {key} @0x{t['va']:X} is not mapped by this XEX")
    return [(i, t["label"](i), struct.unpack_from(">I", data, off + i * 4)[0])
            for i in range(t["count"])]


def scan_references(xex_path, clips, segs=None, data=None):
    """Every pointer in the image that names a clip — the only route to identifying the ~2,900
    clips no *named* table reaches. Returns {clip_va: [pointer VA, ...]}.

    Doc 29 §4 found ~78 such tables (592 refs in .rdata, 4,397 in .data); only nine are named so
    far, so this fills the rest of the browser in with "referenced from 0x84FD52C0"-style hints.
    Pointers living inside a descriptor's own header are skipped — those are its track arrays
    pointing at its own data, not a selection table."""
    segs = segs or _segments(xex_path)
    data = data if data is not None else Path(xex_path).read_bytes()
    want = {c["va"] for c in clips}
    bodies = [(c["off"], c["off"] + 0x80) for c in clips]
    bodies.sort()
    import bisect
    starts = [b[0] for b in bodies]

    def in_header(off):
        i = bisect.bisect_right(starts, off) - 1
        return i >= 0 and off < bodies[i][1]

    refs = {}
    for fs, fe, _vs in segs:
        for off in range(fs, fe - 3, 4):
            v = struct.unpack_from(">I", data, off)[0]
            if v in want and not in_header(off):
                va = _off_to_va(segs, off)
                if va is not None:
                    refs.setdefault(v, []).append(va)
    return refs


def ref_label(clip_va, tables, refs=None):
    """A human-meaningful description of a clip derived from what points at it — the only route
    to meaning now that the shipped names are gone. `tables` = {key: [(i, label, va), ...]};
    `refs` = the scan_references() map, used to fall back to raw pointer sites for the clips no
    named table reaches."""
    hits = []
    if clip_va in NAMED_CLIPS:
        hits.append(NAMED_CLIPS[clip_va][0])
    for key, rows in tables.items():
        for i, label, va in rows:
            if va == clip_va:
                hits.append(f"{TABLES_BY_KEY[key]['name']} [{label}]")
    if not hits and refs:
        sites = refs.get(clip_va, ())
        if sites:
            hits.append("referenced from " + ", ".join(f"0x{v:08X}" for v in sites[:3])
                        + (f" (+{len(sites) - 3} more)" if len(sites) > 3 else ""))
    return hits


# ── writes ──────────────────────────────────────────────────────────────────────────────────
def _open_rw(xex_path):
    try:
        return open(xex_path, "r+b")
    except PermissionError:
        raise ValueError("default.xex is locked (game running?) — close NHL 2k10 and retry")


def set_duration(xex_path, clip_va, seconds, log=print):
    """Retime a clip. This is the lever on goalie reaction speed (doc 28 §4)."""
    seconds = float(seconds)
    if not (0.0 < seconds <= MAX_DURATION):
        raise ValueError(f"duration {seconds} out of range (0, {MAX_DURATION}]")
    segs = validate(xex_path)
    off = _va_to_off(segs, clip_va + OFF_DURATION)
    if off is None:
        raise ValueError(f"clip VA 0x{clip_va:X} is not in this XEX")
    with _open_rw(xex_path) as f:
        f.seek(off)
        old = struct.unpack(">f", f.read(4))[0]
        f.seek(off)
        f.write(struct.pack(">f", seconds))
    log(f"  clip 0x{clip_va:08X} duration {old:g} -> {seconds:g} s (file 0x{off:X})")
    return old


def set_event_time(xex_path, clip_va, event_index, seconds, log=print):
    """Retime one animation event (e.g. when the blade is considered to bite)."""
    seconds = float(seconds)
    segs = validate(xex_path)
    evs = read_events(xex_path, clip_va, segs)
    if not (0 <= event_index < len(evs)):
        raise ValueError(f"event index {event_index} out of range (clip has {len(evs)})")
    ev = evs[event_index]
    if not (0.0 <= seconds <= MAX_DURATION):
        raise ValueError(f"event time {seconds} out of range")
    with _open_rw(xex_path) as f:
        f.seek(ev["off"] + 4)
        f.write(struct.pack(">f", seconds))
    log(f"  clip 0x{clip_va:08X} event #{event_index} (key 0x{ev['key']:02X}) "
        f"{ev['time']:g} -> {seconds:g} s")
    return ev["time"]


def set_table_slot(xex_path, key, slot, clip_va, log=print):
    """Repoint one table slot at a different clip — "play THAT animation here instead". This is
    the closest thing to animation replacement that the current understanding supports, and it is
    exact: clips are reached only by pointer, so a 4-byte write fully reassigns one."""
    t = TABLES_BY_KEY[key]
    if not (0 <= slot < t["count"]):
        raise ValueError(f"slot {slot} out of range for {key} (0..{t['count'] - 1})")
    if clip_va != 0 and not (RDATA_LO <= clip_va <= RDATA_HI):
        raise ValueError(f"0x{clip_va:X} is not a clip address (expected 0x{RDATA_LO:X}"
                         f"-0x{RDATA_HI:X})")
    segs = validate(xex_path)
    data = Path(xex_path).read_bytes()
    if clip_va:                                  # refuse to point at something that isn't a clip
        o = _va_to_off(segs, clip_va)
        if o is None or data[o + 0x18:o + 0x1A] != b"\x0f\x71":
            raise ValueError(f"0x{clip_va:X} is not a valid clip descriptor")
    off = _va_to_off(segs, t["va"] + slot * 4)
    with _open_rw(xex_path) as f:
        f.seek(off)
        old = struct.unpack(">I", f.read(4))[0]
        f.seek(off)
        f.write(struct.pack(">I", clip_va))
    log(f"  {key}[{slot}] ({t['label'](slot)}): 0x{old:08X} -> 0x{clip_va:08X}")
    return old


# ── raw region backup / restore ─────────────────────────────────────────────────────────────
# A clip's data is stored BEFORE its descriptor: measured across the whole library, 1,996 of the
# 2,001 clips that carry events put the event list in [previous descriptor, own descriptor), and
# none reaches back past the previous one. So [prev_desc_va, own_desc_va + HEADER) covers a clip's
# header plus every byte this module can change. The 5 stragglers that point forward get widened
# to the next descriptor. It is NOT a portable animation format — the keyframe codec is undecoded
# — so a restore only ever writes back to the exact VA it came from.
MAGIC = b"N2KANIM1"
HEADER_SPAN = 0x80


def region_for(clips, clip_va, data, segs):
    """(start_va, end_va) of the block holding this clip's header and the data it points at."""
    vas = [c["va"] for c in clips]
    try:
        i = vas.index(clip_va)
    except ValueError:
        raise ValueError(f"0x{clip_va:X} is not a known clip")
    off = clips[i]["off"]
    ptrs = [struct.unpack_from(">I", data, off + o)[0] for o in OFF_TRACKS]
    ptrs = [p for p in ptrs if _va_to_off(segs, p) is not None]
    prev = vas[i - 1] if i > 0 else clip_va
    nxt = vas[i + 1] if i + 1 < len(vas) else clip_va + HEADER_SPAN
    start = min([prev] + [p for p in ptrs if p < clip_va])
    end = nxt if any(p >= clip_va for p in ptrs) else min(clip_va + HEADER_SPAN, nxt)
    return start, end


def export_region(xex_path, clips, clip_va, dest_path, name=""):
    """Write a byte-exact .n2kanim backup of the clip's block."""
    segs = validate(xex_path)
    data = Path(xex_path).read_bytes()
    start, end = region_for(clips, clip_va, data, segs)
    so, eo = _va_to_off(segs, start), _va_to_off(segs, end)
    if so is None or eo is None or eo <= so:
        raise ValueError("clip block is not contiguous in this XEX")
    blob = data[so:eo]
    header = json.dumps(dict(version=1, clip_va=clip_va, start_va=start, end_va=end,
                             size=len(blob), name=name)).encode()
    with open(dest_path, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">I", len(header)))
        f.write(header)
        f.write(blob)
    return len(blob)


def restore_region(xex_path, src_path, log=print):
    """Write a .n2kanim back to the VA it was taken from. Size- and address-locked."""
    raw = Path(src_path).read_bytes()
    if raw[:8] != MAGIC:
        raise ValueError(f"{Path(src_path).name} is not a .n2kanim export")
    hlen = struct.unpack_from(">I", raw, 8)[0]
    meta = json.loads(raw[12:12 + hlen].decode())
    blob = raw[12 + hlen:]
    if len(blob) != meta["size"]:
        raise ValueError("export is truncated or corrupt")
    segs = validate(xex_path)
    so = _va_to_off(segs, meta["start_va"])
    eo = _va_to_off(segs, meta["end_va"])
    if so is None or eo is None or eo - so != len(blob):
        raise ValueError("this export does not fit this XEX (wrong version or edited image)")
    with _open_rw(xex_path) as f:
        f.seek(so)
        f.write(blob)
    log(f"  restored 0x{meta['start_va']:08X}-0x{meta['end_va']:08X} ({len(blob)} bytes) "
        f"from {Path(src_path).name}")
    return meta


# ── user-side name database ─────────────────────────────────────────────────────────────────
# The shipped names were stripped at build time and are not recoverable (doc 29 §2). So naming is
# a user artefact: kept in %APPDATA% (never in the app folder — PyInstaller wipes that on build),
# and shareable as plain JSON so naming work can be pooled.
def names_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    p = Path(base) / "NHL2K10 Mod Launcher"
    p.mkdir(parents=True, exist_ok=True)
    return p / "animation_names.json"


def load_names() -> dict:
    p = names_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {int(k, 16): v for k, v in raw.get("names", {}).items()}


def save_names(names: dict):
    p = names_path()
    p.write_text(json.dumps(
        {"version": 1, "names": {f"{va:08X}": n for va, n in sorted(names.items()) if n}},
        indent=1), encoding="utf-8")
    return p


def merge_names(path, names: dict):
    """Import a shared name file over the current one. Returns (added, updated)."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    incoming = {int(k, 16): v for k, v in raw.get("names", {}).items()}
    added = updated = 0
    for va, n in incoming.items():
        if va not in names:
            added += 1
        elif names[va] != n:
            updated += 1
        names[va] = n
    return added, updated


def export_csv(clips, names, tables, dest_path, refs=None):
    """The whole inventory as CSV — same columns as docs/29_animation_clips.csv plus your names."""
    import csv
    with open(dest_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["clip_va", "name", "derived_from", "name_hash", "bone_mask",
                    "duration_s", "ticks", "n_events", "flags_18", "flags_1c", "flags_20"])
        for c in clips:
            w.writerow([f"{c['va']:08X}", names.get(c["va"], ""),
                        " | ".join(ref_label(c["va"], tables, refs)),
                        f"{c['hash']:08X}", f"{c['bones']:08X}",
                        f"{c['duration']:.4f}", f"{c['ticks']:.3f}", c.get("n_events", 0),
                        f"{c['f18']:08X}", f"{c['f1c']:08X}", f"{c['f20']:08X}"])
    return len(clips)


if __name__ == "__main__":
    XEX = r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    cl = scan_clips(XEX)
    print(f"{len(cl)} clips, {sum(c['duration'] for c in cl) / 60:.1f} min total")
    tabs = {t["key"]: read_table(XEX, t["key"]) for t in KNOWN_TABLES}
    for t in KNOWN_TABLES:
        used = sum(1 for _, _, v in tabs[t["key"]] if v)
        print(f"  {t['key']:16} @0x{t['va']:08X}  {used}/{t['count']} slots used")
    dive = next(c for c in cl if c["va"] == 0x82BC2728)
    print(f"  g_AnimClip_GoalieDive: {dive['duration']:.4f} s, "
          f"{len(read_events(XEX, dive['va']))} events")
