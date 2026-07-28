"""gameplay_tuning.py — read/patch gameplay tuning constants ("tuners") in the flat default.xex.

NHL 2k10 has no named cvar system: gameplay tuning values are bare float globals in .data,
read directly by the gameplay engine (0x83DC0000-0x83E9xxxx). This registry holds the tuners
that Title Update #1 (v1.1, Nov 2009) retuned — recovered by diffing the v1.0 and patched v1.1
basefiles (docs 16+17, `NHL 2k10 Title Update x360/extracted/`). Because the TU is a full
relink we can't run the v1.1 exe with our mods, but we CAN write 2K's v1.1 values into the
v1.0 xex — same variables, same reading code — which is what the "2K Official Patch" preset does.

Verified reader functions (Ghidra, renamed 2026-07-27):
  Gameplay_PinCheck_InitiationGate  @0x83DE4830 — pin/check gate thresholds (849F15BC-15E4)
  Gameplay_ShotPowerObject_Create   @0x83E767D8 — rating->power curve (849F8CB0/B4/B8)
  Gameplay_StickCheck_PokeSweep_v10only @0x83DF4590 — poke/sweep (rewritten in v1.1; its
      restructured tables 849F23xx/38F4 are NOT in this registry — structure changed, values
      don't map 1:1)

Values are patched at their guest VA via xex_patch.va_to_offset (same mechanism as the
Scoreclock anchor table — which lives at 0x8499EF10 in this same .data neighborhood).
Floats are stored as exact BE bit patterns so stock/v1.1 detection is bit-accurate.
Takes effect on next game launch. Also live-pokeable via Cheat Engine while Xenia runs
(same VAs in guest memory) for instant experimentation.
"""
import struct
from pathlib import Path

try:
    from . import xex_patch
except ImportError:                       # run as a loose script from launcher/
    import xex_patch


def _f(bits):
    return struct.unpack(">f", struct.pack(">I", bits))[0]


# One tuner = dict(key, group, label, va, stock_bits, v11_bits, note).
# stock/v11 given as u32 BE bit patterns (exact bytes from the two basefiles).
# v11_bits None = not changed by the TU (not currently used, reserved).
TUNERS = [
    # ── Pinning / board play ──────────────────────────────────────────────────
    dict(key="pin_approach_dot_min", group="Pinning", label="Pin gate: approach angle min",
         va=0x849F15BC, stock=0x3F441A97, v11=0x3ED86028,
         note="Facing/approach-angle gate to start a pin. Lower = pins start from wider angles."),
    dict(key="pin_target_dot_min", group="Pinning", label="Pin gate: target angle min",
         va=0x849F15C4, stock=0xBEFFFC5F, v11=0x3ED86028,
         note="Victim-relative angle gate. v1.1 raised it to the same 0.42 as the approach gate."),
    dict(key="pin_speed_gate_min", group="Pinning", label="Pin gate: speed window min",
         va=0x849F15DC, stock=0x3F000000, v11=0x40200000,
         note="Speed window for pin initiation, interpolated vs approach speed "
              "(Gameplay_PinCheck_InitiationGate). v1.1: 0.5 -> 2.5 = pins start at slow speeds."),
    dict(key="pin_speed_gate_max", group="Pinning", label="Pin gate: speed window max",
         va=0x849F15E0, stock=0x3FA66666, v11=0x40200000,
         note="Upper end of the same window. v1.1: 1.3 -> 2.5."),
    # ── Skating ───────────────────────────────────────────────────────────────
    dict(key="skate_launch_a", group="Skating", label="Launch/accel scale A",
         va=0x849F15FC, stock=0x3F8CCCCD, v11=0x3FC00000,
         note="Acceleration/launch retune (patch note: 'retuned speed for acceleration and "
              "launches'). v1.1: 1.1 -> 1.5."),
    dict(key="skate_launch_b", group="Skating", label="Launch/accel scale B",
         va=0x849F1624, stock=0x3FA66666, v11=0x3FD9999A,
         note="Companion launch scale. v1.1: 1.3 -> 1.7."),
    # ── Stick / loose pucks ───────────────────────────────────────────────────
    dict(key="sweep_pickup_delay", group="Stick & loose pucks", label="Post-sweep pickup shot delay",
         va=0x849F1B2C, stock=0x3F000000, v11=0x3F666666,
         note="Matches patch note 'briefly disable loose shots when sweeping your stick then "
              "gaining possession'. v1.1: 0.5 -> 0.9 (seconds, presumed)."),
    dict(key="misc_6to8", group="Stick & loose pucks", label="Retuned scalar @849F0888",
         va=0x849F0888, stock=0x40C00000, v11=0x41000000,
         note="Unattributed float the TU changed 6.0 -> 8.0 (near a 0.05 rate constant). "
              "Flip and observe to attribute."),
    # ── Shooting ──────────────────────────────────────────────────────────────
    dict(key="shot_curve_lo", group="Shooting", label="Rating→power curve: low",
         va=0x849F8CB0, stock=0x3FC00000, v11=0x3F400000,
         note="Piecewise curve mapping 0-255 player rating to shot power "
              "(Gameplay_ShotPowerObject_Create). v1.1 halved: 1.5 -> 0.75."),
    dict(key="shot_curve_mid", group="Shooting", label="Rating→power curve: mid",
         va=0x849F8CB4, stock=0x3F99999A, v11=0x3F000000,
         note="v1.1: 1.2 -> 0.5."),
    dict(key="shot_curve_hi", group="Shooting", label="Rating→power curve: high",
         va=0x849F8CB8, stock=0x3F000000, v11=0x3E800000,
         note="v1.1: 0.5 -> 0.25."),
    dict(key="shot_vel_a", group="Shooting", label="Shot velocity multiplier A",
         va=0x849F8D08, stock=0x3F8E353F, v11=0x3F933333,
         note="Velocity multiplier triple (rebounds/loose-puck shot retune). v1.1: 1.111 -> 1.15."),
    dict(key="shot_vel_b", group="Shooting", label="Shot velocity multiplier B",
         va=0x849F8D0C, stock=0x3F8353F8, v11=0x3F8CCCCD,
         note="v1.1: 1.026 -> 1.10."),
    dict(key="shot_vel_c", group="Shooting", label="Shot velocity multiplier C",
         va=0x849F8D10, stock=0x3F7A7EFA, v11=0x3F800000,
         note="v1.1: 0.9785 -> 1.0."),
    dict(key="shot_reb_a", group="Shooting", label="Rebound shot scale A",
         va=0x849F8D24, stock=0x3F99999A, v11=0x3F80A3D7,
         note="v1.1: 1.2 -> 1.005."),
    dict(key="shot_reb_b", group="Shooting", label="Rebound shot scale B",
         va=0x849F8D28, stock=0x3F99999A, v11=0x3F9851EE,
         note="v1.1: 1.2 -> 1.19."),
    dict(key="shot_reb_c", group="Shooting", label="Rebound shot scale C",
         va=0x849F8D30, stock=0x3F800000, v11=0x3FA66666,
         note="v1.1: 1.0 -> 1.3."),
    dict(key="shot_65to5", group="Shooting", label="Retuned scalar @849F8DC4",
         va=0x849F8DC4, stock=0x40D00000, v11=0x40A00000,
         note="Unattributed float in the shot cluster, v1.1: 6.5 -> 5.0."),
]

# Words adjacent to the tuners that v1.0 AND v1.1 agree on — must match before any write.
# Catches wrong-file / already-shifted-image cases the same way scorebug_anchors validates
# its table signature.
SENTINELS = [
    (0x849F15F8, 0x3F800000),   # 1.0 between the two launch scales
    (0x849F1600, 0x40000000),   # 2.0 after skate_launch_a
    (0x849F8CAC, 0x3F866666),   # 1.05 before the shot curve
    (0x849F8CC0, 0x00002000),   # int 8192 after the shot curve
    (0x849F8DC0, 0x00000400),   # int 1024 before shot_65to5
    (0x849F0884, 0x00000000),   # 0 before misc_6to8
    (0x849F8D04, 0x3F99999A),   # 1.2 before the velocity triple
    (0x849F1B28, 0x3C23D70A),   # 0.01 before sweep_pickup_delay
]

BY_KEY = {t["key"]: t for t in TUNERS}
GROUPS = []
for _t in TUNERS:
    if _t["group"] not in GROUPS:
        GROUPS.append(_t["group"])


def validate(xex_path):
    """Raise ValueError unless every sentinel word matches. Returns {va: file_offset}
    for all tuner VAs (resolved once, reused by read/write)."""
    offs = {}
    with open(xex_path, "rb") as f:
        for va, want in SENTINELS:
            off = xex_patch.va_to_offset(xex_path, va)
            if off is None:
                raise ValueError(f"sentinel VA 0x{va:X} not mapped (not the flat default.xex?)")
            f.seek(off)
            got = struct.unpack(">I", f.read(4))[0]
            if got != want:
                raise ValueError(f"tuner sentinel mismatch @VA 0x{va:X}: file has 0x{got:08X}, "
                                 f"expected 0x{want:08X} — wrong or incompatible XEX")
        for t in TUNERS:
            off = xex_patch.va_to_offset(xex_path, t["va"])
            if off is None:
                raise ValueError(f"tuner VA 0x{t['va']:X} not mapped by this XEX")
            offs[t["va"]] = off
    return offs


def read_all(xex_path):
    """{key: float_value} of every tuner's CURRENT value in the xex (validates first)."""
    offs = validate(xex_path)
    out = {}
    with open(xex_path, "rb") as f:
        for t in TUNERS:
            f.seek(offs[t["va"]])
            out[t["key"]] = _f(struct.unpack(">I", f.read(4))[0])
    return out


def classify(key, value):
    """'stock' / 'v1.1' / 'custom' for a current float value (bit-exact vs the registry)."""
    t = BY_KEY[key]
    bits = struct.unpack(">I", struct.pack(">f", value))[0]
    if bits == t["stock"]:
        return "stock"
    if t["v11"] is not None and bits == t["v11"]:
        return "v1.1"
    return "custom"


def write_values(xex_path, edits, log=print):
    """edits = {key: float_value}. Validates sentinels, then patches each tuner in place.
    Returns number of words actually changed (already-matching are skipped)."""
    bad = [k for k in edits if k not in BY_KEY]
    if bad:
        raise ValueError(f"unknown tuner key(s): {bad}")
    for k, v in edits.items():
        v = float(v)
        if not (abs(v) < 1e6):
            raise ValueError(f"{k}: {v} is out of sane range")
    offs = validate(xex_path)
    changed = 0
    try:
        with open(xex_path, "r+b") as f:
            for k, v in edits.items():
                t = BY_KEY[k]
                new = struct.pack(">f", float(v))
                f.seek(offs[t["va"]])
                cur = f.read(4)
                if cur == new:
                    continue
                f.seek(offs[t["va"]])
                f.write(new)
                changed += 1
                log(f"  tuner {k}: {_f(struct.unpack('>I', cur)[0]):g} -> {float(v):g} "
                    f"@VA 0x{t['va']:X} (file 0x{offs[t['va']]:X})")
    except PermissionError:
        raise ValueError("XEX is locked (game running?) — close NHL 2k10 and retry")
    return changed


def preset_stock():
    return {t["key"]: _f(t["stock"]) for t in TUNERS}


def preset_v11():
    return {t["key"]: _f(t["v11"]) for t in TUNERS if t["v11"] is not None}


if __name__ == "__main__":
    XEX = r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    cur = read_all(XEX)
    print(f"validated {len(SENTINELS)} sentinels; {len(TUNERS)} tuners in {XEX}")
    for t in TUNERS:
        v = cur[t["key"]]
        tag = classify(t["key"], v)
        v11 = f"{_f(t['v11']):g}" if t["v11"] is not None else "-"
        print(f"  [{tag:6}] {t['key']:22} VA 0x{t['va']:X}  cur={v:g}  "
              f"stock={_f(t['stock']):g}  v1.1={v11}")
