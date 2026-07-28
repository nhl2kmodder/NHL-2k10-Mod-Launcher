"""scorebug_anim_descs.py — give the 3 SOG scorebug elements opacity/transform anim
descriptors so the engine draws them (overlay_static.iff blob0).

Background (RE 2026-07-28): the scorebug scene has an ANIM-DESCRIPTOR table
@0x2096A0, stride 0x1C, count @0x209618 (54), self-rel table ptr @0x20961C.
Descriptor: [k1][k2][flags][-1.0f][p10][p14][p18]; p10/p14/p18 are self-rel
(target = field_addr + value - 1) channel-data pointers.  flags: bits 20+ =
channel mask, bits 8+ = per-channel CONSTANT flag (else 16-byte spline
descriptor [u16 count][u16 cached][u32 0][selfrel->times][selfrel->coeff
rows(0x10)] evaluated by AnimEval 0x8418DE48), low byte = property class.

Per TEXT element there are two descriptors:
  [crc32-UPPER(bindname)][0x0CED9417]  flags 0x00100012  -> ONE SPLINE = OPACITY
  [0xE9015CE9('scorebug_text')][crc32-raw(name)] flags 0x03F03F21 -> 6 const
                                                    floats (transform/scale)
All 10 stock opacity splines are byte-identical (the intro fade-in, settling at
alpha 1.0).  KEY FINDING: the opacity desc is keyed by the element's BIND hash
(record +0xD8) — gameclock4's stock bind 365A707C is the ONLY record bind with
no opacity desc, which is why it never draws (alpha defaults 0).  Positional
desc pairing is disproven: DIGIT4's spline sits right before gameclock4's raw
desc yet gc4 stays invisible, while gameclock3 (which BINDS DIGIT4) draws.

This tool (append-only, applied on top of scorebug_add_shots + scorebug_xex_rows):
  1. relocates the 54-desc table to the end of blob0 (grown), appending 5 descs:
     opacity descs for SHOTS_AWAY / SHOTS_HOME / SHOTS_LABEL (spline data cloned
     per-desc from team2's) + raw transform descs for shots_away / shots_home
     (6 const floats cloned from gameclock4's).
  2. rebases the moved descs' self-rel channel ptrs (old channel data stays put;
     backward self-rel across the blob is proven in-game by the record-table
     relocation).
  3. count 54 -> 59 @0x209618, table self-rel @0x2096A0 -> new base, and the
     duplicate count 54 @0x20A0C8 (scene node[1] '68BF19CC' +0x48) -> 59.

STATUS: theory grounded in static RE; NOT yet verified in-game.
"""
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import overlay_editor as oe

IFF = "overlay_static.iff"

DESC_BASE   = 0x2096A0
DESC_STRIDE = 0x1C
DESC_COUNT  = 54
COUNT_OFF   = 0x209618          # u32 desc count
TABLEPTR_OFF = 0x20961C         # self-rel -> DESC_BASE
COUNT2_OFF  = 0x20A0C8          # duplicate count (scene node[1] +0x48)
SELFREL_FIELDS = (0x10, 0x14, 0x18)

KEY_SCOREBUG_TEXT = 0xE9015CE9  # crc32('scorebug_text')
K2_OPACITY  = 0x0CED9417        # shared k2 of every opacity (bindhash) desc
FLAGS_OPACITY   = 0x00100012    # 1 spline channel
FLAGS_TRANSFORM = 0x03F03F21    # 6 constant channels

TEAM2_OPACITY_DESC = 1          # desc index whose spline we clone (TEAM2)
GC4_TRANSFORM_DESC = 20         # desc index whose 6 consts we clone (gameclock4)

NEW_OPACITY_BINDS = ["SHOTS_AWAY", "SHOTS_HOME", "SHOTS_LABEL"]
NEW_TRANSFORM_NAMES = ["shots_away", "shots_home"]


def crc(s: str) -> int:
    return zlib.crc32(s.encode("ascii")) & 0xFFFFFFFF


def _rd_selfrel(d, off):
    v = struct.unpack_from(">I", d, off)[0]
    return (off + v - 1) & 0xFFFFFFFF if v else None


def _wr_selfrel(d, off, target):
    struct.pack_into(">I", d, off, (target - off + 1) & 0xFFFFFFFF)


def build(dram: bytearray):
    d = dram
    u32 = lambda o: struct.unpack_from(">I", d, o)[0]

    # ---- sanity: unmodified desc table ----
    if u32(COUNT_OFF) != DESC_COUNT:
        raise RuntimeError(f"desc count @{COUNT_OFF:#x} = {u32(COUNT_OFF)}, expected "
                           f"{DESC_COUNT} (already patched? wrong blob?)")
    if _rd_selfrel(d, TABLEPTR_OFF) != DESC_BASE:
        raise RuntimeError("desc table self-rel does not point at 0x2096A0")
    if u32(COUNT2_OFF) != DESC_COUNT:
        raise RuntimeError(f"duplicate count @{COUNT2_OFF:#x} = {u32(COUNT2_OFF)}, "
                           f"expected {DESC_COUNT}")
    t2 = DESC_BASE + TEAM2_OPACITY_DESC * DESC_STRIDE
    if u32(t2) != crc("TEAM2") or u32(t2 + 8) != FLAGS_OPACITY:
        raise RuntimeError("desc[1] is not the TEAM2 opacity desc")
    gc4 = DESC_BASE + GC4_TRANSFORM_DESC * DESC_STRIDE
    if u32(gc4) != KEY_SCOREBUG_TEXT or u32(gc4 + 4) != crc("gameclock4"):
        raise RuntimeError("desc[20] is not the gameclock4 transform desc")

    # ---- source channel data ----
    t2_hdr = _rd_selfrel(d, t2 + 0x18)                 # 16B spline header
    t2_cnt = struct.unpack_from(">H", d, t2_hdr)[0]    # 7 keys
    t2_times = _rd_selfrel(d, t2_hdr + 8)
    t2_coefs = _rd_selfrel(d, t2_hdr + 0xC)
    times_len = t2_coefs - t2_times                    # (=32B: 8 knot times)
    coefs_len = t2_cnt * 0x10
    if not (0 < times_len <= 0x100 and coefs_len == 0x70):
        raise RuntimeError(f"unexpected TEAM2 spline layout (times {times_len}B, "
                           f"{t2_cnt} keys)")
    times_blob = bytes(d[t2_times:t2_times + times_len])
    coefs_blob = bytes(d[t2_coefs:t2_coefs + coefs_len])
    gc4_ch = _rd_selfrel(d, gc4 + 0x18)                # 6 const floats
    consts_blob = bytes(d[gc4_ch:gc4_ch + 6 * 4])

    # ---- copy + rebase the 54 descs to the (aligned) end of the blob ----
    new_base = (len(d) + 0xF) & ~0xF
    d.extend(b"\x00" * (new_base - len(d)))
    new_count = DESC_COUNT + len(NEW_OPACITY_BINDS) + len(NEW_TRANSFORM_NAMES)
    descs = [bytearray(d[DESC_BASE + i * DESC_STRIDE:
                         DESC_BASE + (i + 1) * DESC_STRIDE])
             for i in range(DESC_COUNT)]
    for i, rec in enumerate(descs):
        for fo in SELFREL_FIELDS:
            v = struct.unpack_from(">I", rec, fo)[0]
            if v:
                target = DESC_BASE + i * DESC_STRIDE + fo + v - 1
                new_field = new_base + i * DESC_STRIDE + fo
                struct.pack_into(">I", rec, fo, (target - new_field + 1) & 0xFFFFFFFF)

    # ---- new descriptors (appended at table end so stock indices survive) ----
    chan_area = new_base + new_count * DESC_STRIDE     # channel data after table

    def desc_bytes(k1, k2, flags, p18_target, field_base):
        rec = bytearray(DESC_STRIDE)
        struct.pack_into(">IIII", rec, 0, k1, k2, flags, 0xBF800000)
        struct.pack_into(">I", rec, 0x18, (p18_target - (field_base + 0x18) + 1)
                         & 0xFFFFFFFF)
        return rec

    new_descs, chan_blob = [], bytearray()
    def alloc(chunk):
        off = chan_area + len(chan_blob)
        chan_blob.extend(chunk)
        return off

    for bind in NEW_OPACITY_BINDS:
        i = DESC_COUNT + len(new_descs)
        fb = new_base + i * DESC_STRIDE
        hdr_off = chan_area + len(chan_blob)
        hdr = bytearray(16)
        struct.pack_into(">HH", hdr, 0, t2_cnt, 0)
        # times follow the header, coeffs follow the times (self-rel from header)
        _wr = lambda o, t: struct.pack_into(">I", hdr, o, (t - (hdr_off + o) + 1)
                                            & 0xFFFFFFFF)
        _wr(8, hdr_off + 16)
        _wr(0xC, hdr_off + 16 + times_len)
        alloc(bytes(hdr)); alloc(times_blob); alloc(coefs_blob)
        new_descs.append(desc_bytes(crc(bind), K2_OPACITY, FLAGS_OPACITY, hdr_off, fb))

    for name in NEW_TRANSFORM_NAMES:
        i = DESC_COUNT + len(new_descs)
        fb = new_base + i * DESC_STRIDE
        ch_off = alloc(consts_blob)
        new_descs.append(desc_bytes(KEY_SCOREBUG_TEXT, crc(name), FLAGS_TRANSFORM,
                                    ch_off, fb))

    # ---- write out ----
    all_descs = descs + new_descs
    assert len(all_descs) == new_count
    d.extend(b"\x00" * (chan_area + len(chan_blob) - len(d)))
    for i, rec in enumerate(all_descs):
        d[new_base + i * DESC_STRIDE:new_base + (i + 1) * DESC_STRIDE] = rec
    d[chan_area:chan_area + len(chan_blob)] = chan_blob

    struct.pack_into(">I", d, COUNT_OFF, new_count)
    struct.pack_into(">I", d, COUNT2_OFF, new_count)
    _wr_selfrel(d, TABLEPTR_OFF, new_base)
    # neutralize the OLD table's keys so hash scans don't double-match
    for i in range(DESC_COUNT):
        struct.pack_into(">I", d, DESC_BASE + i * DESC_STRIDE, 0)

    return {"new_base": new_base, "count": new_count,
            "opacity_binds": {b: crc(b) for b in NEW_OPACITY_BINDS}}


def apply(gdir: str):
    dram, meta = oe.load_dram(IFF, gdir)
    dram = bytearray(dram)
    info = build(dram)
    oe.apply_dram(bytes(dram), meta, IFF, gdir)
    return info


# ── per-element TEXT glyph scale via the transform desc's constant channels ──────────
# The [E9015CE9][crc-raw(name)] desc's 6 constant channels are [tx,ty,tz, sx,sy,sz];
# stock 1.1 (abbrev/score-bound elements) or 1.2 (clock/shots-bound). Consumed at scene
# instantiation (file edit + relaunch, like all scorebug layout data).
#
# ★ MAPPING CORRECTION (2026-07-28, from an in-game symptom): the transform desc that
# drives an element is NOT the one whose k2 matches the element's record name — the raw
# names are shifted authoring labels (scaling the 'gameclock4' desc resized CLOCK DIGIT 4).
# The desc that drives element X is the FLAGS_TRANSFORM desc immediately FOLLOWING the
# opacity desc whose k1 == X's BIND hash (record +0xD8). All lookups below are by bind.
# STOCK_TXSCALE is keyed by ELEMENT (record) name under that corrected mapping — e.g.
# team1_score binds QUARTER, whose paired transform ('quarter' raw) holds 1.2.
STOCK_TXSCALE = {
    "away_abbrev": 1.1, "team1": 1.1, "team2": 1.1, "team2_score": 1.1,
    "team1_score": 1.2, "quarter": 1.2, "gameclock1": 1.2, "gameclock2": 1.2,
    "gameclock3": 1.2, "gameclock4": 1.2, "gameclock_semi": 1.2,
    "shots_away": 1.2, "shots_home": 1.2,
}


def _desc_table(d):
    """(base, count) of the anim-desc table — follows the self-rel @0x20961C, so it works
    on the pristine (0x2096A0 x54) and every relocated/patched layout."""
    base = _rd_selfrel(d, TABLEPTR_OFF)
    count = struct.unpack_from(">I", d, COUNT_OFF)[0]
    if base is None or not (1 <= count <= 0x200):
        raise RuntimeError("anim-desc table pointer/count invalid")
    return base, count


def transform_consts_off_by_bind(d, bindhash):
    """Offset of the 6 constant floats of the transform desc driving the element BOUND as
    `bindhash` — i.e. the FLAGS_TRANSFORM desc right after the [bindhash][0CED9417] opacity
    desc. None if the element has no opacity desc or no paired transform."""
    base, count = _desc_table(d)
    for i in range(count):
        e = base + i * DESC_STRIDE
        if (struct.unpack_from(">I", d, e)[0] == bindhash
                and struct.unpack_from(">I", d, e + 8)[0] == FLAGS_OPACITY):
            nxt = e + DESC_STRIDE
            if (i + 1 < count
                    and struct.unpack_from(">I", d, nxt + 8)[0] == FLAGS_TRANSFORM):
                return _rd_selfrel(d, nxt + 0x18)
            return None
    return None


def get_text_scale_by_bind(d, bindhash):
    """(sx, sy) consts of the transform desc paired with `bindhash`, or None."""
    o = transform_consts_off_by_bind(d, bindhash)
    if o is None:
        return None
    return struct.unpack_from(">2f", d, o + 0xC)


def set_text_scale_by_bind(dram, bindhash, sx, sy):
    """Write absolute sx/sy (consts[3]/[4]) into the bind-paired transform desc, in the
    given (mutable) DRAM buffer. Returns True if the desc exists."""
    o = transform_consts_off_by_bind(dram, bindhash)
    if o is None:
        return False
    struct.pack_into(">2f", dram, o + 0xC, float(sx), float(sy))
    return True


# ---- v2 pair fix: interleave the SOG descs + give the label its own transform ----------
V1_COUNT = 59            # layout produced by build(): [..54 stock..][SA(U)][SH(U)][SL(U)][sa][sh]
V2_COUNT = 60            # paired: [SA(U)][sa][SH(U)][sh][SL(U)][label-transform]


def fix_pairs(dram: bytearray):
    """Rewrite the v1 appended tail (3 opacity + 2 transform, not interleaved) into proper
    [opacity][transform] PAIRS and append a transform desc for the label (which had none —
    its scale rode on desc 'gameclock4' = clock digit 4, the reported bug). Relocates the
    whole desc table again (append-only; old table keys zeroed)."""
    d = dram
    u32 = lambda o: struct.unpack_from(">I", d, o)[0]
    base, count = _desc_table(d)
    if count == V2_COUNT:
        raise RuntimeError("desc table already at v2 (60 descs) — nothing to fix")
    if count != V1_COUNT:
        raise RuntimeError(f"desc count = {count}, expected v1 = {V1_COUNT}")
    tail_keys = [u32(base + i * DESC_STRIDE) for i in range(54, 59)]
    if tail_keys != [crc("SHOTS_AWAY"), crc("SHOTS_HOME"), crc("SHOTS_LABEL"),
                     KEY_SCOREBUG_TEXT, KEY_SCOREBUG_TEXT]:
        raise RuntimeError("v1 tail signature mismatch")

    # read all descs with selfrel fields resolved to ABSOLUTE targets
    descs = []
    for i in range(count):
        e = base + i * DESC_STRIDE
        rec = bytearray(d[e:e + DESC_STRIDE])
        abs_t = {}
        for fo in SELFREL_FIELDS:
            v = struct.unpack_from(">I", rec, fo)[0]
            abs_t[fo] = (e + fo + v - 1) & 0xFFFFFFFF if v else None
        descs.append((rec, abs_t))

    # new order: stock 0..53, then pairs SA/sa, SH/sh, SL/(new label transform)
    order = list(range(54)) + [54, 57, 55, 58, 56]
    new_base = (len(d) + 0xF) & ~0xF
    d.extend(b"\x00" * (new_base - len(d)))
    chan_area = new_base + V2_COUNT * DESC_STRIDE

    # label transform desc: clone shots_home's transform (rec 58) with its own consts + name
    src_rec, src_abs = descs[58]
    lbl_rec = bytearray(src_rec)
    struct.pack_into(">I", lbl_rec, 4, crc("shots_label"))
    consts = bytes(d[src_abs[0x18]:src_abs[0x18] + 24])
    lbl_abs = {0x10: None, 0x14: None, 0x18: chan_area}

    out = [descs[i] for i in order] + [(lbl_rec, lbl_abs)]
    d.extend(b"\x00" * (chan_area + len(consts) - len(d)))
    for i, (rec, abs_t) in enumerate(out):
        e = new_base + i * DESC_STRIDE
        for fo in SELFREL_FIELDS:
            t = abs_t[fo]
            struct.pack_into(">I", rec, fo, 0 if t is None
                             else (t - (e + fo) + 1) & 0xFFFFFFFF)
        d[e:e + DESC_STRIDE] = rec
    d[chan_area:chan_area + len(consts)] = consts

    struct.pack_into(">I", d, COUNT_OFF, V2_COUNT)
    struct.pack_into(">I", d, COUNT2_OFF, V2_COUNT)
    _wr_selfrel(d, TABLEPTR_OFF, new_base)
    for i in range(count):                       # neutralize the old table's keys
        struct.pack_into(">I", d, base + i * DESC_STRIDE, 0)
    return {"new_base": new_base, "count": V2_COUNT}


def apply_fix_pairs(gdir: str):
    dram, meta = oe.load_dram(IFF, gdir)
    dram = bytearray(dram)
    info = fix_pairs(dram)
    oe.apply_dram(bytes(dram), meta, IFF, gdir)
    return info


if __name__ == "__main__":
    gd = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\cloug\Documents\NHL 2k10 Extracted"
    print(apply(gd))
