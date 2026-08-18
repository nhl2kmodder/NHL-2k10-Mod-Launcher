"""shader_tuning.py — retune the eye pixel shader's corneal specular by patching default.xex.

## The shader

The eyes have their own pixel shader — the ONLY shader in the XEX that declares
CorneaNormalMap/IrisNormalMap (full version @VA 0x84A546FC) plus one simplified LOD variant
without the normal maps (@0x84A54FC4). Both were disassembled from the embedded Xenos microcode
(2026-08-17); the lighting model is:

    key      = ArenaLightingTint.rgb * 3.8 * intensity          (c245.y / c247.x)
    ambient  = __USER6.rgb * 0.95 * intensity                   (c246.y / c245.y)
    spec     = key * pow(envCube, EXPONENT)                     <- the knob this module owns
    oC0.rgb  = saturate((ambient*gradA + key*gradB) * BaseMap.rgb + spec)

`envCube` is the `eye_reflection` cubemap in global.iff — a 64x64x6 R5G6B5 cube of the arena
ceiling lights: small bright lamp blobs on near-black (measured: mean luminance 0.031,
p95 0.20, max 0.92).

## Why the stock value looks dead

The shipped EXPONENT is 1.0848 — essentially linear. With the 3.8 gain, every direction where
the cube reads over ~0.02 produces visible specular, which is 15-20% of the sphere: the whole
cornea wears a flat gray sheen instead of a catchlight, which is exactly the "2009 lifeless
eyes" look. Raising the exponent concentrates the reflection into the lamp cores:

    exponent   sphere fraction with visible spec   lamp-core catchlight
    1.0848     ~15-20%  (gray veil)                saturates to white
    3.0        ~4%      (tight highlights)         still saturates (3.8 * 0.78 = 3.0 >> 1)

So at 3.0 the catchlight keeps its full brightness — pow() only kills the veil around it. That
is how a real tear film behaves: a smooth low-roughness surface with a tight specular lobe.
Diffuse lighting, the lid-shadow gradients and the ambient term are untouched.

## The patch sites

The exponent is a literal float in each shader blob's default-constant block (uploaded to the
GPU constant file when the shader binds — Xenia's shader cache only caches translated microcode,
so patching the float is safe). `3F8ADABA` (1.0848) appears at exactly TWO offsets in the whole
executable — these two shaders — so the change cannot leak into any other material:

    VA          default.xex off   register   shader
    0x84A54924  0x023D2924        c247.z     full eye shader (cornea+iris normal-mapped)
    0x84A55194  0x023D3194        c246.z     simplified/LOD eye shader

⚠ v1.0 addresses. Title Update #1 is a full relink — these VAs do not hold there.
"""
from __future__ import annotations
import struct
from pathlib import Path

try:
    from . import xex_patch as XP
except ImportError:
    import xex_patch as XP

STOCK_EXPONENT = 1.0848             # ships as 3F8ADABA in both blobs
DEFAULT_EXPONENT = 3.0              # tight catchlight, veil ~5x smaller, core still saturates
EXP_MIN, EXP_MAX = 0.5, 16.0        # sanity range for a cfg override / site validation

# (VA of the float, which literal register it lands in, shader) — see module docstring.
SITES = [
    (0x84A54924, "c247.z", "full eye shader (cornea+iris normals) @0x84A546FC"),
    (0x84A55194, "c246.z", "simplified/LOD eye shader @0x84A54FC4"),
]

_STOCK_BYTES = struct.pack(">f", STOCK_EXPONENT)


def _plausible(f: float) -> bool:
    return EXP_MIN <= f <= EXP_MAX


def read_exponent(xex_path) -> float | None:
    """The exponent the executable currently uses, or None if the sites don't decode
    (unexpected build) or the two shaders disagree (half-applied edit)."""
    vals = set()
    for va, _reg, _what in SITES:
        b = XP.read_va(xex_path, va, 4)
        if b is None or len(b) != 4:
            return None
        f = struct.unpack(">f", b)[0]
        if not _plausible(f):
            return None                      # not this build (TU1 relinks everything)
        vals.add(b)
    if len(vals) != 1:
        return None
    return struct.unpack(">f", vals.pop())[0]


def status(xex_path) -> str:
    exp = read_exponent(xex_path)
    if exp is None:
        return "eye specular: UNKNOWN (patch sites did not decode — unexpected executable)"
    if struct.pack(">f", exp) == _STOCK_BYTES:
        return f"eye specular exponent: {exp:g} — STOCK (flat corneal sheen)"
    return f"eye specular exponent: {exp:g} — PATCHED (stock is {STOCK_EXPONENT:g})"


def apply(xex_path, exponent: float = DEFAULT_EXPONENT, log=print) -> dict:
    """Set (or with exponent=STOCK_EXPONENT, restore) the corneal specular exponent on both
    eye shaders. Idempotent; verifies before writing."""
    if not _plausible(exponent):
        raise ValueError(f"eye specular exponent {exponent!r} outside sane range "
                         f"[{EXP_MIN}, {EXP_MAX}]")
    xex_path = Path(xex_path)
    XP.ensure_flat(xex_path, game_dir=xex_path.parent, log=log)

    want = struct.pack(">f", exponent)
    cur = read_exponent(xex_path)
    if cur is None:
        raise ValueError("cannot read the current eye exponent — refusing to patch blind")
    if struct.pack(">f", cur) == want:
        log(f"  eye specular exponent already {exponent:g} — nothing to do")
        return {"changed": False, "exponent": exponent, "sites": []}

    expect = struct.pack(">f", cur)
    done = []
    for va, reg, what in SITES:
        off = XP.patch_va(xex_path, va, want, expect=expect, log=log)
        done.append({"va": va, "off": off, "reg": reg, "what": what})
        log(f"    {reg}: {what}")

    got = read_exponent(xex_path)
    if got is None or struct.pack(">f", got) != want:
        raise RuntimeError(f"post-patch verify failed: exponent reads {got!r}, wanted {exponent:g}")
    log(f"  eye specular exponent {cur:g} -> {got:g}")
    return {"changed": True, "exponent": got, "sites": done}


def revert(xex_path, log=print) -> dict:
    """Put the stock flat-sheen 1.0848 back on both shaders."""
    return apply(xex_path, exponent=STOCK_EXPONENT, log=log)


if __name__ == "__main__":
    import sys
    xex = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex")
    print(status(xex))
