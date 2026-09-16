"""6-bit goalie-mask pattern lane — MORE PATTERNS ON STYLE 1, same mesh, same style.

The pattern index lives in roster +0xB8 bits 0-4 (5 bits, cap 32).  The mask
SHELL lives in +0xB4 conventional bits 23-26 (4 bits) but only ever holds 0-6,
so its top bit (conv bit 26) is ALWAYS ZERO in stock data.  We donate that bit
to the pattern:

    pattern6 = (B8 & 0x1F) | (((B4 >> 26) & 1) << 5)        # 0..63

Unlike the three dead lanes (g07 clamp, precache widening, g06 retarget), this
NEVER touches the style->mesh mapping: shell stays 0 (g01), mesh 0x27, gear
row 0.  Only the texture number changes -> helmet_g01_pattern_32..63.iff.
Live CE probing (2026-08-18) proved helmet textures load ON DEMAND BY NAME for
the goalies in the scene (only Luongo's worn pattern hash was in DRAM), so a
pattern number past 31 binds as long as the TOC entry exists.

Patch sites (all verified stock words first; every site independently
idempotent, whole module SHA-roundtrip revertible):
  1. Pattern getter @0x840A9010 (the ONLY roster pattern read in the XEX):
     first word `lwz` -> `b 0x84264880` stub that ORs in the donor bit.
  2. Stub @0x84264880 (24 B of zero padding in stock).
  3. Shell getters 4-bit -> 3-bit so no consumer ever sees the donor bit as
     "shell 8+": 0x840A888C (rlwinm r3), 0x840A88AC / 0x840A88D4 (rlwinm r9,
     the CZ next/prev-shell browse funcs).
  4. Shell setter @0x840A889C + browse write-backs 0x840A88C0/0x840A88E8:
     rlwimi 4-bit -> 3-bit so in-game shell writes can't clobber the donor.
  5. GetMaskPatternMax @0x840A9030: li r3,0x1F -> 0x3F.
  6. Per-shell max-pattern table DAT_8493CAA8[0]: 31 -> 63 (validators/
     sanitizer accept patterns 32-63 on shell 0).
  7. STREAMING-POOL SIZER GRID WIDTH 0x20 -> 0x40 at all ten
     `Tex_PrecacheGrid2D(s_goalie_mask_fmt, 0x20, 7)` call sites.
     `Tex_PrecacheGrid2D` is NOT a precache -- it is the helmet family's SIZE
     CALLBACK: it builds every `helmet_gNN_pattern_NN.iff` name in an
     (patterns x shells) grid, stats each through the TOC and keeps the
     MAXIMUM, and that maximum becomes the capacity of every helmet slot in
     the streaming pool.  At width 0x20 it only ever sees patterns 0-31, so an
     added slot 32-63 is INVISIBLE to the sizer and cannot raise the ceiling.
     `Asset_StreamLoadWorker` @0x83D32DF8 then does
     `if (capacity < file_size) { req+0x20 = 1; return; }` -- it marks the
     request finished but never signals completion, so entering gameplay with
     a goalie wearing an oversized added slot sticks on a blinking
     "Loading..." forever.  Widening the grid to 0x40 makes patterns 32-63
     participate in the max exactly like 0-31, so added slots auto-raise the
     ceiling the same way stock ones do.  Missing names are already the normal
     case here (the stock grid stats 224 names for 84 real files), so the
     extra 224 stats are harmless.  See [[project_streaming_pool_ceiling]].
Known caveat: the CZ appearance-editor keeps its OWN packed copy of the mask
fields (+0x0C accessor bank @0x83D481C0..) still capped at 31 — editing a
6-bit goalie's mask in Create Zone clamps it back under 32.  Assign via the
launcher instead.
"""
from __future__ import annotations

import struct

try:
    from . import xex_patch as XP
except ImportError:  # script use
    import xex_patch as XP

STUB_VA = 0x84264880   # own zone — 0x84264850 belongs to the retired mask_shells clamp stub
STUB_WORDS = (
    0x816300B8,  # lwz    r11,0xB8(r3)
    0x556A06FE,  # rlwinm r10,r11,0,27,31    r10 = pattern low 5
    0x816300B4,  # lwz    r11,0xB4(r3)
    0x556B5EB4,  # rlwinm r11,r11,11,26,26   r11 = donor bit -> 0x20
    0x7D435B78,  # or     r3,r10,r11
    0x4E800020,  # blr
)
STUB_BYTES = b"".join(struct.pack(">I", w) for w in STUB_WORDS)

# Setter stub: the stock setter @0x840A9020 wrote ONLY the low 5 bits, so the
# in-game player editor could never move a goalie across the 32-pattern
# boundary (browse wrapped inside 1-32 or inside 33-64 depending on the stuck
# donor bit).  This writes all 6: low 5 into +0xB8, bit 5 into the +0xB4 donor.
STUB2_VA = 0x842648A0
STUB2_WORDS = (
    0x816300B8,  # lwz    r11,0xB8(r3)
    0x508B06FE,  # rlwimi r11,r4,0,27,31     B8 low5 := r4 low5
    0x916300B8,  # stw    r11,0xB8(r3)
    0x816300B4,  # lwz    r11,0xB4(r3)
    0x508BA94A,  # rlwimi r11,r4,21,5,5      B4 conv bit 26 := r4 bit 5
    0x916300B4,  # stw    r11,0xB4(r3)
    0x4E800020,  # blr
)
STUB2_BYTES = b"".join(struct.pack(">I", w) for w in STUB2_WORDS)

# (va, stock word, patched word)
WORD_SITES = (
    (0x840A9010, 0x816300B8, 0x481BB870),  # pattern getter -> b stub
    (0x840A9020, 0x816300B8, 0x481BB880),  # pattern setter -> b stub2 (6-bit write)
    (0x840A888C, 0x55634F3E, 0x55634F7E),  # shell getter r3: 4b -> 3b
    (0x840A88AC, 0x55694F3E, 0x55694F7E),  # shell getter r9 (next-shell)
    (0x840A88D4, 0x55694F3E, 0x55694F7E),  # shell getter r9 (prev-shell)
    (0x840A889C, 0x508BB950, 0x508BB990),  # shell setter rlwimi: 4b -> 3b
    (0x840A88C0, 0x514BB950, 0x514BB990),  # browse write-back (next)
    (0x840A88E8, 0x514BB950, 0x514BB990),  # browse write-back (prev)
    (0x840A9030, 0x3860001F, 0x3860003F),  # GetMaskPatternMax 31 -> 63
    (0x8493CAA8, 0x0000001F, 0x0000003F),  # max-pattern table, shell 0
) + tuple(
    # helmet streaming-pool sizer grid width: li r4,0x20 -> li r4,0x40, so the
    # size pass stats patterns 0-63 and added slots can raise the pool ceiling.
    # All ten sites load the same s_goalie_mask_fmt table base (0x84A25DC0,
    # +0x20 / +0x24), i.e. every one of them is the goalie-helmet family.
    (va, 0x38800020, 0x38800040) for va in (
        0x83FDCB9C, 0x83FDCBBC,  # Tex_PrecacheGoalieHelmets_0/_0b (shared prologue)
        0x83FDDAE0, 0x83FDDBF0,  # Tex_PrecacheGoalieHelmets   / _2
        0x83FDE548, 0x83FDE658,  # Tex_PrecacheGoalieHelmets_3 / _4
        0x83FDEFB0, 0x83FDF0C0,  # Tex_PrecacheGoalieHelmets_5 / _6
        0x83FDFA18, 0x83FDFB28,  # Tex_PrecacheGoalieHelmets_7 / _8
    )
)


def read_state(xex_path) -> str:
    """"stock" | "patched" | "partial" | "unknown"."""
    ws = [XP.read_u32(xex_path, va) for va, _o, _p in WORD_SITES]
    if any(x not in (o, p) for x, (_va, o, p) in zip(ws, WORD_SITES)):
        return "unknown"
    stub = XP.read_va(xex_path, STUB_VA, len(STUB_BYTES))
    stub2 = XP.read_va(xex_path, STUB2_VA, len(STUB2_BYTES))
    if stub not in (STUB_BYTES, b"\x00" * len(STUB_BYTES)):
        return "unknown"
    if stub2 not in (STUB2_BYTES, b"\x00" * len(STUB2_BYTES)):
        return "unknown"
    stubs_in = stub == STUB_BYTES and stub2 == STUB2_BYTES
    stubs_out = stub != STUB_BYTES and stub2 != STUB2_BYTES
    if all(x == o for x, (_va, o, _p) in zip(ws, WORD_SITES)) and stubs_out:
        return "stock"
    if all(x == p for x, (_va, _o, p) in zip(ws, WORD_SITES)) and stubs_in:
        return "patched"
    return "partial"


def status(xex_path) -> str:
    st = read_state(xex_path)
    if st == "patched":
        return "6-bit mask patterns: ENABLED (style-1 patterns 0-63)"
    if st == "stock":
        return "6-bit mask patterns: stock (style-1 patterns capped at 32)"
    if st == "partial":
        return "6-bit mask patterns: PARTIAL — run apply or revert"
    return "6-bit mask patterns: UNKNOWN state (unexpected words — not touching)"


def apply(xex_path, log=print) -> dict:
    st = read_state(xex_path)
    if st == "patched":
        return {"changed": False}
    if st == "unknown":
        raise ValueError("mask-pattern6 sites in an unexpected state — refusing to patch blind")
    # stubs first: dead code until the getter/setter branch to them
    if XP.read_va(xex_path, STUB_VA, len(STUB_BYTES)) != STUB_BYTES:
        XP.patch_va(xex_path, STUB_VA, STUB_BYTES,
                    expect=b"\x00" * len(STUB_BYTES), log=log)
    if XP.read_va(xex_path, STUB2_VA, len(STUB2_BYTES)) != STUB2_BYTES:
        XP.patch_va(xex_path, STUB2_VA, STUB2_BYTES,
                    expect=b"\x00" * len(STUB2_BYTES), log=log)
    for va, o, p in WORD_SITES:
        if XP.read_u32(xex_path, va) == o:
            XP.patch_va(xex_path, va, struct.pack(">I", p),
                        expect=struct.pack(">I", o), log=log)
    if read_state(xex_path) != "patched":
        raise RuntimeError("mask-pattern6 apply verification failed")
    log("  6-bit mask patterns enabled (style-1 patterns 0-63; donor = shell bit 26)")
    return {"changed": True}


def revert(xex_path, log=print) -> dict:
    st = read_state(xex_path)
    if st == "stock":
        return {"changed": False}
    if st == "unknown":
        raise ValueError("mask-pattern6 sites in an unexpected state — refusing to revert blind")
    for va, o, p in WORD_SITES:
        if XP.read_u32(xex_path, va) == p:
            XP.patch_va(xex_path, va, struct.pack(">I", o),
                        expect=struct.pack(">I", p), log=log)
    if XP.read_va(xex_path, STUB_VA, len(STUB_BYTES)) == STUB_BYTES:
        XP.patch_va(xex_path, STUB_VA, b"\x00" * len(STUB_BYTES),
                    expect=STUB_BYTES, log=log)
    if XP.read_va(xex_path, STUB2_VA, len(STUB2_BYTES)) == STUB2_BYTES:
        XP.patch_va(xex_path, STUB2_VA, b"\x00" * len(STUB2_BYTES),
                    expect=STUB2_BYTES, log=log)
    if read_state(xex_path) != "stock":
        raise RuntimeError("mask-pattern6 revert verification failed")
    log("  6-bit mask patterns reverted to stock")
    return {"changed": True}
