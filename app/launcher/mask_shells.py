"""mask_shells.py — unlock goalie-mask styles g07-g16 as extra "standard shape" pattern slots.

## Why shells 6-15 don't render (and what this patches)

A goalie's mask is 4-bit shell + 5-bit pattern in the roster, so the game can request 16 styles
(g01-g16) x 32 patterns. Only six styles shipped art; the mesh picker (Goalie_GetMaskMeshId
@0x84090770) already maps every shell >= 6 to mesh 0x27 — the STANDARD g01/g04 shape — so a
g07+ mask would draw as a g01 with its own texture... except it never draws at all: the gear
material/visibility pass (Function_84095150) looks the shell up in a per-shell part-id table at
0x8208CDD0 that has EXACTLY 6 rows. Shell >= 6 reads garbage past the table, no mesh part ever
matches the row's part id, and the mask is silently invisible. That 6-row table is consumed by
this one function only, and the shell value feeding every one of its reads comes from a single
call: `bl Goalie_GetMaskModelIndex` at 0x84095558 (result parked in r14 by the next instruction).

## The patch

Redirect that one `bl` to a 6-instruction stub in .text tail padding. The stub inlines the getter
(same two instructions, byte-for-byte) and clamps: shells 0-5 return unchanged, shells 6-15
return 0 — so g07+ masks use g01's table row (part ids, cage/accessory config) and render exactly
like a g01. The mask TEXTURE name is built later from a separate, fresh getter call (0x84095D80)
that stays unpatched, so helmet_g07..g16_pattern_XX.iff keep their own art. Net effect: +320
addressable "standard shape" pattern slots. Zero behavior change for shells 0-5 — stock rosters
never store a shell above 5, so a stock install is bit-for-bit unaffected in play.

The stub lives at 0x84264850: the .text section's code ends at 0x8426484B and the section is
padded with zeros to the next 64K boundary (0x84270000, where .data begins) — 47 KB of zero,
executable, present in the flat XEX, with no references into it (checked via Ghidra xrefs).

## Revert

`revert()` restores the original `bl` word and re-zeroes the stub — byte-for-byte stock. The
launcher's baseline sync applies/reverts this from cfg["extra_mask_slots"] (default ON, set it
false to heal back to stock on the next launch). NOTE: reverting makes any goalie assigned a
g07+ mask invisible again — reassign those goalies to g01-g06 patterns first.
"""
from __future__ import annotations
import struct
from pathlib import Path

try:
    from . import xex_patch as XP
except ImportError:
    import xex_patch as XP

CALL_VA = 0x84095558              # the one goalie-branch `bl Goalie_GetMaskModelIndex`
GETTER_VA = 0x840A8888            # Goalie_GetMaskModelIndex
STUB_VA = 0x84264850              # zero .text tail padding (code ends 0x8426484B, data at 0x84270000)

MAX_REAL_SHELL = 6                # shells 0-5 have their own table rows; >= 6 clamps to 0


def _bl(frm: int, to: int) -> int:
    d = (to - frm) & 0x03FFFFFC
    return 0x48000000 | d | 1


ORIG_WORD = _bl(CALL_VA, GETTER_VA)        # 0x48013331 — verified against the shipped XEX
PATCH_WORD = _bl(CALL_VA, STUB_VA)         # bl stub

# lwz r11,0xB4(r3) / rlwinm r3,r11,9,28,31  == the getter's own two instructions, cloned
# verbatim, then: cmplwi r3,6 / bltlr / li r3,0 / blr
STUB_WORDS = (0x816300B4, 0x55634F3E,
              0x28000000 | (3 << 16) | MAX_REAL_SHELL,     # cmplwi r3,6
              0x4D800020,                                  # bltlr
              0x38600000,                                  # li r3,0
              0x4E800020)                                  # blr
STUB_BYTES = b"".join(struct.pack(">I", w) for w in STUB_WORDS)

# ---------------------------------------------------------------------------
# Precache grid widening — THE piece the first ship was missing.
#
# Textures only bind if the asset is already in DRAM, and helmet IFFs are bulk-
# loaded by Tex_PrecacheGrid2D(fmt="helmet_g{0:D2}_pattern_{1:D2}.iff", 0x20, 7)
# @0x83FDC8D0: an outer loop over file-shell 0..6 (g00 misses harmlessly — the
# loader skips absent files) x inner 0..31 patterns. g07+ sit OUTSIDE that grid,
# so their art never loads and the (correctly clamped) mask binds nothing =
# invisible. The bound is a `li r5,0x7` immediate cloned at 10 call sites; we
# raise it to 0x11 (17) so file shells g07..g16 precache too. Only files that
# actually exist get loaded — a stock install precaches exactly what it did
# before (g00 and g07..g16 all miss), so this is behavior-neutral until slots
# are added.
# ---------------------------------------------------------------------------
PRECACHE_SITES = (0x83FDDAD8, 0x83FDDBE8, 0x83FDE540, 0x83FDE650, 0x83FDEFA8,
                  0x83FDF0B8, 0x83FDFA10, 0x83FDFB20, 0x83FDCB94, 0x83FDCBB4)
PRECACHE_ORIG = 0x38A00007        # li r5,7   (file shells g00..g06)
PRECACHE_NEW = 0x38A00011         # li r5,17  (file shells g00..g16)

# ---------------------------------------------------------------------------
# g06 -> STANDARD SHAPE — the lane that finally works end-to-end (2026-08-18).
#
# Shells >= 6 kept failing in-game even with clamp + widened precache, so the
# shipping mechanism for "more style-1 patterns" is now to repurpose style g06
# (internal shell 5): ZERO goalies wear it in the user's roster, only 4 of its
# 32 patterns shipped art, and — critically — shells 0..5 ride the completely
# stock pipeline (precache was ALWAYS 32 patterns wide for them, CZ can browse
# them, every validator accepts them). Five sites turn g06 into "the standard
# shape with 32 patterns":
#   1. Goalie_GetMaskMeshId @0x84090770: the shell-dispatch is mtctr+bdz chain;
#      shell 5's final `b -> li r3,0x29` (vintage mesh) retargets to the
#      `li r3,0x27` (standard mesh) exit. In-game model picker.
#   2. Function_84096F38 (FE/CZ preview builder) has the same chain inlined;
#      same one-branch retarget.
#   3. Part-id/material row 5 @0x8208CE48 (table 0x8208CDD0, 24B rows, consumed
#      only by gear-visibility Function_84095150): row 5 := row 0, so shell-5
#      goalies enable exactly g01's mesh part set.
#   4. Per-shell max-pattern DAT_8493CAA8[5] @0x8493CABC: 3 -> 31, so CZ +
#      validators accept patterns 04..31.
#   5. Per-shell flag DAT_8493CAC0[5] @0x8493CAD4: 0 -> 1 (parity with g01 —
#      stops Function_83C12588 forcing the +0xB4 bits-20..22 sub-attribute to 0).
# Net: 28 fresh slots (g06 patterns 04..31) that render as a g01 in game AND in
# the CZ preview, browsable in CZ under style 6. Stock g06 art (patterns 00-03,
# unworn) would render on the standard mesh if ever selected.
# ---------------------------------------------------------------------------
G06_WORD_SITES = (
    # (va, orig, patched)
    (0x840907C8, 0x48000018, 0x4800002C),   # in-game mesh picker: shell5 b ->0x29  =>  b ->0x27
    (0x840975D8, 0x4800000C, 0x48000014),   # FE preview builder:  shell5 b ->0x29  =>  b ->0x27
    (0x8493CABC, 0x00000003, 0x0000001F),   # max pattern for shell 5: 3 -> 31
    (0x8493CAD4, 0x00000000, 0x00000001),   # shell-5 flag -> 1 (same as g01)
)
G06_ROW5_VA = 0x8208CE48
G06_ROW5_ORIG = bytes.fromhex("000000570000000000000000000000000000002400000000")
G06_ROW5_NEW = bytes.fromhex("0000005200000058000000000000002B0000002100000045")   # = row 0


def _precache_words(xex_path):
    return [XP.read_u32(xex_path, va) for va in PRECACHE_SITES]


def read_state(xex_path) -> str:
    """"stock" | "patched" | "partial" | "unknown" (unexpected words — refuse to touch).

    "partial" = clamp stub installed but precache grid still 7-wide (the pre-2026-08-18
    ship) or any mix of stock/patched precache immediates; apply() upgrades it."""
    w = XP.read_u32(xex_path, CALL_VA)
    stub = XP.read_va(xex_path, STUB_VA, len(STUB_BYTES))
    pw = _precache_words(xex_path)
    if any(x not in (PRECACHE_ORIG, PRECACHE_NEW) for x in pw):
        return "unknown"
    gw = [XP.read_u32(xex_path, va) for va, _o, _p in G06_WORD_SITES]
    if any(x not in (o, n) for x, (_va, o, n) in zip(gw, G06_WORD_SITES)):
        return "unknown"
    row5 = XP.read_va(xex_path, G06_ROW5_VA, len(G06_ROW5_ORIG))
    if row5 not in (G06_ROW5_ORIG, G06_ROW5_NEW):
        return "unknown"
    all_stock = (all(x == PRECACHE_ORIG for x in pw)
                 and all(x == o for x, (_va, o, _n) in zip(gw, G06_WORD_SITES))
                 and row5 == G06_ROW5_ORIG)
    all_patched = (all(x == PRECACHE_NEW for x in pw)
                   and all(x == n for x, (_va, _o, n) in zip(gw, G06_WORD_SITES))
                   and row5 == G06_ROW5_NEW)
    if w == ORIG_WORD and stub == b"\x00" * len(STUB_BYTES):
        return "stock" if all_stock else "partial"
    if w == PATCH_WORD and stub == STUB_BYTES:
        return "patched" if all_patched else "partial"
    # half-applied (e.g. a crash between the two writes) still reports unknown; apply()/revert()
    # handle the recoverable halves explicitly.
    return "unknown"


def status(xex_path) -> str:
    st = read_state(xex_path)
    if st == "patched":
        return "extra mask slots: PATCHED (shells g07-g16 render as the standard g01 shape)"
    if st == "stock":
        return "extra mask slots: stock (shells g07-g16 invisible; only g01-g06 render)"
    if st == "partial":
        return "extra mask slots: PARTIAL (old clamp-only patch; precache grid not widened — run apply)"
    return "extra mask slots: UNKNOWN state (call site or stub bytes unexpected — not touching)"


def apply(xex_path, log=print) -> dict:
    """Install the clamp stub + redirect the call. Idempotent; verifies every byte it replaces."""
    xex_path = Path(xex_path)
    XP.ensure_flat(xex_path, game_dir=xex_path.parent, log=log)
    st = read_state(xex_path)
    if st == "patched":
        return {"changed": False}
    w = XP.read_u32(xex_path, CALL_VA)
    stub = XP.read_va(xex_path, STUB_VA, len(STUB_BYTES))
    if st == "unknown":
        # allow the one recoverable half-state: stub already written, bl still original
        if not (w == ORIG_WORD and stub == STUB_BYTES):
            raise ValueError("mask-shell patch site is in an unexpected state — refusing to patch blind "
                             f"(call word {w:08X}, stub {stub[:8].hex() if stub else '??'}…)")
    if stub != STUB_BYTES:
        # stub FIRST, call redirect second — the stub is dead code until the bl points at it, so
        # a crash between the writes leaves the game fully functional (and apply() recovers).
        XP.patch_va(xex_path, STUB_VA, STUB_BYTES, expect=b"\x00" * len(STUB_BYTES), log=log)
    if w != PATCH_WORD:
        XP.patch_va(xex_path, CALL_VA, struct.pack(">I", PATCH_WORD),
                    expect=struct.pack(">I", ORIG_WORD), log=log)
    # widen the helmet precache grid 7 -> 17 file shells (each site independently idempotent)
    for va in PRECACHE_SITES:
        if XP.read_u32(xex_path, va) == PRECACHE_ORIG:
            XP.patch_va(xex_path, va, struct.pack(">I", PRECACHE_NEW),
                        expect=struct.pack(">I", PRECACHE_ORIG), log=log)
    # g06 -> standard shape (each site independently idempotent)
    for va, o, n in G06_WORD_SITES:
        if XP.read_u32(xex_path, va) == o:
            XP.patch_va(xex_path, va, struct.pack(">I", n),
                        expect=struct.pack(">I", o), log=log)
    if XP.read_va(xex_path, G06_ROW5_VA, len(G06_ROW5_ORIG)) == G06_ROW5_ORIG:
        XP.patch_va(xex_path, G06_ROW5_VA, G06_ROW5_NEW, expect=G06_ROW5_ORIG, log=log)
    if read_state(xex_path) != "patched":
        raise RuntimeError("mask-shell patch verify failed after writing")
    log("  extra goalie-mask slots enabled (g06 patterns 04-31 = standard shape; g07-g16 clamp + precache kept)")
    return {"changed": True}


def revert(xex_path, log=print) -> dict:
    """Byte-for-byte back to stock: restore the bl, zero the stub."""
    xex_path = Path(xex_path)
    st = read_state(xex_path)
    if st == "stock":
        return {"changed": False}
    w = XP.read_u32(xex_path, CALL_VA)
    if w == PATCH_WORD:
        XP.patch_va(xex_path, CALL_VA, struct.pack(">I", ORIG_WORD),
                    expect=struct.pack(">I", PATCH_WORD), log=log)
    elif w != ORIG_WORD:
        raise ValueError(f"call site holds unexpected word {w:08X} — refusing to revert blind")
    stub = XP.read_va(xex_path, STUB_VA, len(STUB_BYTES))
    if stub == STUB_BYTES:
        XP.patch_va(xex_path, STUB_VA, b"\x00" * len(STUB_BYTES), expect=STUB_BYTES, log=log)
    for va in PRECACHE_SITES:
        if XP.read_u32(xex_path, va) == PRECACHE_NEW:
            XP.patch_va(xex_path, va, struct.pack(">I", PRECACHE_ORIG),
                        expect=struct.pack(">I", PRECACHE_NEW), log=log)
    for va, o, n in G06_WORD_SITES:
        if XP.read_u32(xex_path, va) == n:
            XP.patch_va(xex_path, va, struct.pack(">I", o),
                        expect=struct.pack(">I", n), log=log)
    if XP.read_va(xex_path, G06_ROW5_VA, len(G06_ROW5_ORIG)) == G06_ROW5_NEW:
        XP.patch_va(xex_path, G06_ROW5_VA, G06_ROW5_ORIG, expect=G06_ROW5_NEW, log=log)
    if read_state(xex_path) != "stock":
        raise RuntimeError("mask-shell revert verify failed")
    log("  extra goalie-mask slots patch removed (stock)")
    return {"changed": True}


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    print(status(p))
