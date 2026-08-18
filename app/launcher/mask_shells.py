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


def read_state(xex_path) -> str:
    """"stock" | "patched" | "unknown" (unexpected words — refuse to touch)."""
    w = XP.read_u32(xex_path, CALL_VA)
    stub = XP.read_va(xex_path, STUB_VA, len(STUB_BYTES))
    if w == ORIG_WORD and stub == b"\x00" * len(STUB_BYTES):
        return "stock"
    if w == PATCH_WORD and stub == STUB_BYTES:
        return "patched"
    # half-applied (e.g. a crash between the two writes) still reports unknown; apply()/revert()
    # handle the recoverable halves explicitly.
    return "unknown"


def status(xex_path) -> str:
    st = read_state(xex_path)
    if st == "patched":
        return "extra mask slots: PATCHED (shells g07-g16 render as the standard g01 shape)"
    if st == "stock":
        return "extra mask slots: stock (shells g07-g16 invisible; only g01-g06 render)"
    return "extra mask slots: UNKNOWN state (call site or stub bytes unexpected — not touching)"


def apply(xex_path, log=print) -> dict:
    """Install the clamp stub + redirect the call. Idempotent; verifies every byte it replaces."""
    xex_path = Path(xex_path)
    XP.ensure_flat(xex_path, game_dir=xex_path.parent, log=log)
    st = read_state(xex_path)
    if st == "patched":
        return {"changed": False}
    if st == "unknown":
        # allow the one recoverable half-state: stub already written, bl still original
        w = XP.read_u32(xex_path, CALL_VA)
        stub = XP.read_va(xex_path, STUB_VA, len(STUB_BYTES))
        if not (w == ORIG_WORD and stub == STUB_BYTES):
            raise ValueError("mask-shell patch site is in an unexpected state — refusing to patch blind "
                             f"(call word {w:08X}, stub {stub[:8].hex() if stub else '??'}…)")
    else:
        # stub FIRST, call redirect second — the stub is dead code until the bl points at it, so
        # a crash between the writes leaves the game fully functional (and apply() recovers).
        XP.patch_va(xex_path, STUB_VA, STUB_BYTES, expect=b"\x00" * len(STUB_BYTES), log=log)
    XP.patch_va(xex_path, CALL_VA, struct.pack(">I", PATCH_WORD),
                expect=struct.pack(">I", ORIG_WORD), log=log)
    if read_state(xex_path) != "patched":
        raise RuntimeError("mask-shell patch verify failed after writing")
    log("  extra goalie-mask slots enabled (g07-g16 -> standard shape)")
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
    if read_state(xex_path) != "stock":
        raise RuntimeError("mask-shell revert verify failed")
    log("  extra goalie-mask slots patch removed (stock)")
    return {"changed": True}


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    print(status(p))
