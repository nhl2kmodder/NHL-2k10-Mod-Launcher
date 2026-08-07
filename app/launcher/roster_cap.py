"""roster_cap.py — raise the game's hardcoded Roster.ROS size limit by patching default.xex.

## The limit

The roster is one flat image with a hardcoded size, `0x26A2A0` = 2,532,000 bytes. The shipped
Roster.ROS is exactly 2,532,004 bytes — that size plus the 4-byte length prefix — so the stock
save sits *exactly* on the ceiling with zero headroom. The loader is `Function_83D58420`:

    if ((img != NULL) && (n = *img, n <= DAT_84d8e9f8)) { ...copy n, install, fix up... }
    return;                              // else SILENTLY nothing

`*img` is the u32 at file offset 0x00 (filesize-4) and `DAT_84d8e9f8` is set once at init by
`Function_83D4E628` to the constant. Hand the game a larger roster and it fails the `<=`, returns
without a word, and keeps whatever roster it already had — which at boot is the built-in default
from `rostermem:droster.iff`. In game that looks like every mod being erased at once (Phoenix and
Atlanta return), not like a rejected file.

## The patch

The constant is never stored as data; it is always built as `lis rX,0x26` + `ori rY,rX,0xa2a0`,
and each site loads it into ONE register that all of that function's uses share. So raising it is
a single halfword per site: bump the `lis` immediate and the low half rides along. `HI_NEW = 0x28`
gives 0x28A2A0 = 2,663,584, i.e. **+131,072 bytes** of roster headroom — room for thousands of
extra records, at a cost of 128 KB of guest RAM.

Seven sites build the constant. Only two are patched, and the choice is deliberate:

  VA          function              role                                        patched
  0x83D4E690  Function_83D4E628     sets DAT_84d8e9f8 (THE limit) + allocates    YES
                                    the one roster buffer, from the same word
  0x83B835C0  Function_83B834C0     roster snapshot: allocates the backup and    YES
                                    is also the copy loop's byte length
  0x83B84838  Function_83B846F8     loads the built-in default roster asset      no
  0x83D53A7C  Function_83D53A38     copies a roster IFF section -> ea08 buffer   no
  0x83D57E28  Function_83D57DE0     allocates the ea08 buffer                    no
  0x83D57E44  Function_83D57DE0     stores its size to DAT_84d8ea04              no
  0x83D587C4  Function_83D58760     copies a roster IFF section -> main buffer   no

The five unpatched sites only ever *fill a prefix* of a buffer — they copy the stock 2,532,000-byte
packaged roster into a buffer that is now larger. A short fill is harmless; the roster's own chunk
directory bounds every read. The two patched sites are the only ones that would otherwise
**truncate user data**: the limit itself, and the snapshot that copies back *out* of the live
roster. Patching the default-roster loader too would risk its decompressor validating an output
size the packaged asset can't produce, for no benefit.

## Consequences to know about

* `FUN_83d4e718` reports the roster's save-section stride as `DAT_84d8e9f8 + 4`, so after the
  patch **the game will write larger Roster.ROS files itself**. Those saves will NOT load on an
  unpatched executable. Reverting the XEX means reverting to a pre-patch roster too.
* This does not make bigger rosters *correct* — it only makes them loadable. The file still has to
  be structurally valid (see ros_grow.py).

Why not make it dynamic (scale with team count)? There is nowhere to scale from. The size has to be
known before a single byte of the roster is parsed — it's the allocation size at startup, long
before any table exists to count. A fixed, generous ceiling is the whole of what's available.
"""
from __future__ import annotations
import struct
from pathlib import Path

try:
    from . import xex_patch as XP
except ImportError:
    import xex_patch as XP

HI_OLD = 0x26                       # lis immediate in the shipped executable
HI_NEW = 0x28                       # -> 0x28A2A0, +131,072 bytes
LO = 0xA2A0                         # the ori immediate, never changed

STOCK_IMAGE = 0x26A2A0              # 2,532,000 — stock cap; stock file is this + 4

# (VA of the `lis`, register, what it does) — see the module docstring for why only these two.
SITES = [
    (0x83D4E678, 9,  "roster size limit + main buffer (Function_83D4E628)"),
    (0x83B835B8, 11, "roster snapshot buffer + copy length (Function_83B834C0)"),
]


def _lis_word(rd: int, imm: int) -> int:
    """addis rD,0,imm — `lis rD,imm`. Primary opcode 15, rA = 0."""
    return (15 << 26) | (rd << 21) | (0 << 16) | (imm & 0xFFFF)


def read_cap(xex_path) -> int | None:
    """The image size the executable currently enforces, or None if the sites don't decode."""
    data = Path(xex_path).read_bytes()
    vals = set()
    for va, rd, _what in SITES:
        off = XP.va_to_offset(xex_path, va)
        if off is None:
            return None
        w = struct.unpack_from(">I", data, off)[0]
        if (w >> 26) != 15 or ((w >> 21) & 31) != rd or ((w >> 16) & 31) != 0:
            return None                      # not a `lis rD,imm` — unexpected build
        vals.add(((w & 0xFFFF) << 16) | LO)
    return vals.pop() if len(vals) == 1 else None


def max_roster_bytes(xex_path) -> int | None:
    """Largest Roster.ROS FILE size this executable will load (image + the 4-byte prefix)."""
    cap = read_cap(xex_path)
    return None if cap is None else cap + 4


def status(xex_path) -> str:
    cap = read_cap(xex_path)
    if cap is None:
        return "roster cap: UNKNOWN (patch sites did not decode — unexpected executable)"
    tag = "STOCK" if cap == STOCK_IMAGE else f"PATCHED (stock is 0x{STOCK_IMAGE:X})"
    return f"roster cap: 0x{cap:X} = {cap:,} bytes image / {cap + 4:,} byte file — {tag}"


def apply(xex_path, hi: int = HI_NEW, log=print) -> dict:
    """Raise (or with hi=HI_OLD, restore) the cap. Idempotent; verifies before writing."""
    xex_path = Path(xex_path)
    XP.ensure_flat(xex_path, game_dir=xex_path.parent, log=log)

    cur = read_cap(xex_path)
    want = (hi << 16) | LO
    if cur == want:
        log(f"  roster cap already 0x{want:X} — nothing to do")
        return {"changed": False, "cap": want, "file_max": want + 4, "sites": []}
    if cur is None:
        raise ValueError("cannot read the current roster cap — refusing to patch blind")

    hi_cur = cur >> 16
    done = []
    for va, rd, what in SITES:
        expect = struct.pack(">I", _lis_word(rd, hi_cur))
        new = struct.pack(">I", _lis_word(rd, hi))
        off = XP.patch_va(xex_path, va, new, expect=expect, log=log)
        done.append({"va": va, "off": off, "what": what})
        log(f"    {what}")

    got = read_cap(xex_path)
    if got != want:
        raise RuntimeError(f"post-patch verify failed: cap reads 0x{got:X}, wanted 0x{want:X}")
    log(f"  roster cap 0x{cur:X} -> 0x{got:X}  (max Roster.ROS {cur + 4:,} -> {got + 4:,} bytes)")
    return {"changed": True, "cap": got, "file_max": got + 4, "sites": done}


def revert(xex_path, log=print) -> dict:
    """Put the stock 0x26A2A0 limit back."""
    return apply(xex_path, hi=HI_OLD, log=log)


if __name__ == "__main__":
    import sys
    xex = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex")
    print(status(xex))
