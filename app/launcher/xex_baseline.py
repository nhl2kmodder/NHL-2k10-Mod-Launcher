"""xex_baseline.py — keep the game's default.xex at the launcher's baseline, checked on startup.

## What this owns, and what it deliberately does not

Five XEX patches are *launcher baseline*: they are what the launcher assumes the game is, they
have no reason to be per-mod-pack, and a user who restores a clean default.xex should get them
back without having to know they exist.

    roster cap       roster_cap.py       always on; nothing to configure
    game calendar    game_date.py        cfg["season_start_year"], default "auto" = track the clock
    default matchup  default_matchup.py  cfg["default_matchup"], ONLY if the user has set one
    title icon       archive_textures.ensure_game_icon()
    eye specular     shader_tuning.py    cfg["eye_specular_exponent"]: absent/"auto" = 3.0,
                                         a number pins it, "stock" restores the shipped 1.0848

The scoreclock patches (SOG bind rows + the mode-7 anchor) are NOT here on purpose. They belong
to the scoreclock mod pack — modpack.apply_scoreclock() applies them and the pack's revert path
takes them back out — so auto-applying them would fight the pack and strand a user who never
installed one. See project_scorebug_sog / project_modpack_scoreclock.

## Why the matchup is opt-in and the rest is not

The XEX *is* the store for the boot matchup — there was no config key, so "what the user wants"
and "what the file says" were the same thing and there was nothing to sync from. The launcher's
Settings tab now mirrors every write into cfg["default_matchup"], which makes the two cases
distinct:

    key absent   -> the user has never expressed a preference; leave the XEX alone entirely
    key present  -> re-apply it whenever the file disagrees

So a user editing the matchup in the launcher is never a "mismatch" (both are written together),
while a user who replaces default.xex gets their choice restored.

The calendar is the other way round: it applies out of the box because there IS a right answer,
and that answer moves. It defaults to "auto" — the real-world season, from the system clock — so
an install that sits unopened until 2027 rolls itself forward instead of aging every player wrong.
Setting the key to a year pins it. See current_season_start_year() for the July 1 rollover and the
floor that keeps a wrong system clock from dragging the calendar backwards.

## Cost

This runs on every launch, so it has to be free. It is: the whole verify is a handful of 4-byte
seeks (xex_patch caches the XEX2 header parse and reads by seeking rather than slurping the
39 MB image) — about 3 ms for the code/constant patches (the eye-shader check is two more
4-byte reads). The icon is the exception: proving it
matches means decoding a PNG out of the file, so it gets a (size, mtime, source-hash) stamp in
cfg and is skipped outright when nothing has moved.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
from pathlib import Path

try:
    from . import xex_patch as XP
    from . import roster_cap as RC
    from . import game_date as GD
    from . import default_matchup as DM
    from . import shader_tuning as ST
except ImportError:                       # loose-script use from launcher/
    import xex_patch as XP
    import roster_cap as RC
    import game_date as GD
    import default_matchup as DM
    import shader_tuning as ST

MIN_SEASON_START_YEAR = 2026      # the season this mod's roster ships; never roll below it
SEASON_ROLLOVER_MONTH = 7         # July 1 — the offseason, after the Cup and before camp

CFG_SEASON = "season_start_year"
CFG_MATCHUP = "default_matchup"
CFG_ICON_STAMP = "xex_icon_stamp"
CFG_EYE = "eye_specular_exponent"

AUTO = "auto"


def current_season_start_year(today=None) -> int:
    """The NHL season that is current in the real world, as its start year.

    A season spans two calendar years (Oct 2026 → Apr 2027) and is named for the first, so the
    calendar year alone is the wrong answer for half of it: in January 2027 the season in progress
    is still 2026-27. The rollover is July 1 — past the Cup, before camp — so the whole offseason
    already points at the season about to start, which is what a user reinstalling in August
    expects to see.

    Floored at MIN_SEASON_START_YEAR. A PC with a dead CMOS battery reports 2009-ish, and letting
    that drive the calendar would quietly make every player's age wrong — the exact bug the date
    patch exists to fix. Rolling forward is safe; rolling backward past the shipped roster is not.
    """
    d = today or _dt.date.today()
    year = d.year if d.month >= SEASON_ROLLOVER_MONTH else d.year - 1
    return max(year, MIN_SEASON_START_YEAR)


def wanted_season_year(cfg) -> int:
    """The season the XEX should encode: cfg's pin if there is one, else the real-world season.

    cfg[CFG_SEASON] is "auto" (or missing/empty) to track the clock, or a year to pin it there —
    someone replaying the 2026-27 season in 2030 wants the ages that shipped with the roster.
    """
    v = cfg.get(CFG_SEASON, AUTO)
    if isinstance(v, bool):                    # bools are ints; not a year
        v = None
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v)
    if isinstance(v, int) and 2000 <= v <= 2126:
        return v
    return current_season_start_year()


def _sha1_file(p) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp_for(xex: Path, icon_src) -> dict:
    st = os.stat(xex)
    return {"xex": str(xex), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "src_sha1": _sha1_file(icon_src)}


# ── the individual steps ────────────────────────────────────────────────────────
# Each returns a note string when it DID something, None when already correct, and raises on a
# real problem. sync() runs them independently so one bad step can't take the others down.

def _step_flat(xex: Path, log):
    st = XP.ensure_flat(xex, game_dir=xex.parent, log=log)
    return None if st == "already flat" else st


def _step_roster_cap(xex: Path, log):
    want = (RC.HI_NEW << 16) | RC.LO
    if RC.read_cap(xex) == want:
        return None
    r = RC.apply(xex, log=lambda *_: None)
    return f"roster cap raised to {r['cap']:,} (max Roster.ROS {r['file_max']:,} bytes)"


def _step_game_date(xex: Path, cfg, log):
    year = wanted_season_year(cfg)
    want = GD.season_dates(year)
    if GD.read(xex) == want:
        return None
    GD.write(xex, want, log=lambda *_: None)
    return f"game calendar set to the {year}-{(year + 1) % 100:02d} season"


def _step_matchup(xex: Path, cfg, log):
    pref = cfg.get(CFG_MATCHUP)
    if not pref:                          # never set in the launcher — the XEX keeps whatever it has
        return None
    try:
        home, away = int(pref[0]), int(pref[1])
    except (TypeError, ValueError, IndexError):
        raise ValueError(f"cfg[{CFG_MATCHUP!r}] is not a [home_id, away_id] pair: {pref!r}")
    if DM.read(xex) == (home, away):
        return None
    DM.write(xex, home, away, log=lambda *_: None)
    return f"default matchup restored to team {home} v team {away}"


def wanted_eye_exponent(cfg) -> float:
    """The corneal specular exponent the XEX should carry: 3.0 out of the box (there IS a right
    answer — the measured cubemap says so, see shader_tuning.py), a number in cfg pins a custom
    value, and "stock"/"off" pins the shipped 1.0848 (an explicit opt-out heals BACK to stock,
    the same contract as a pinned season year)."""
    v = cfg.get(CFG_EYE, AUTO)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("stock", "off", "false", "none"):
            return ST.STOCK_EXPONENT
        try:
            v = float(s)
        except ValueError:
            v = None
    if isinstance(v, bool):
        return ST.STOCK_EXPONENT if not v else ST.DEFAULT_EXPONENT
    if isinstance(v, (int, float)) and ST.EXP_MIN <= v <= ST.EXP_MAX:
        return float(v)
    return ST.DEFAULT_EXPONENT


def _step_eye_shader(xex: Path, cfg, log):
    import struct as _s
    want = wanted_eye_exponent(cfg)
    cur = ST.read_exponent(xex)
    if cur is not None and _s.pack(">f", cur) == _s.pack(">f", want):
        return None
    ST.apply(xex, exponent=want, log=lambda *_: None)
    if _s.pack(">f", want) == _s.pack(">f", ST.STOCK_EXPONENT):
        return "eye shader restored to the stock corneal specular"
    return f"eye shader corneal specular exponent set to {want:g} (tight catchlight)"


def _step_icon(xex: Path, cfg, icon_src, log):
    """Stamped: the only step whose verify is expensive (PNG decode out of a 39 MB file)."""
    try:
        from . import archive_textures as archtex
    except ImportError:
        import archive_textures as archtex
    want = _stamp_for(xex, icon_src)
    if cfg.get(CFG_ICON_STAMP) == want:
        return None
    st = archtex.ensure_game_icon(str(xex), str(icon_src), log=lambda *_: None)
    # ensure_game_icon returns None for "already correct" and for several give-up paths, and a
    # string for both success and failure. Only stamp when we know the file is in the wanted
    # state, or the next launch would skip a write that never happened.
    if st is None or st.startswith("game icon set"):
        cfg[CFG_ICON_STAMP] = _stamp_for(xex, icon_src)
        return st
    return st                             # locked / failed: no stamp, retry next launch


# ── the startup task ────────────────────────────────────────────────────────────

def sync(xex_path, cfg, icon_src=None, log=print) -> dict:
    """Bring `xex_path` up to the launcher baseline. Safe to call on every launch.

    `cfg` is the launcher config dict; this may add CFG_ICON_STAMP to it, so the caller should
    save_config() when the returned dict has cfg_dirty set. Returns
    {"ok", "changed", "notes", "errors", "cfg_dirty"} and never raises."""
    out = {"ok": False, "changed": False, "notes": [], "errors": [], "cfg_dirty": False}
    if not xex_path:
        return out
    xex = Path(xex_path)
    if not xex.is_file():
        return out

    before = cfg.get(CFG_ICON_STAMP)
    steps = [("flatten", lambda: _step_flat(xex, log)),
             ("roster cap", lambda: _step_roster_cap(xex, log)),
             ("game date", lambda: _step_game_date(xex, cfg, log)),
             ("default matchup", lambda: _step_matchup(xex, cfg, log)),
             ("eye shader", lambda: _step_eye_shader(xex, cfg, log))]
    if icon_src and Path(icon_src).is_file():
        steps.append(("title icon", lambda: _step_icon(xex, cfg, icon_src, log)))

    for name, fn in steps:
        try:
            note = fn()
        except PermissionError:
            out["errors"].append(f"{name}: default.xex is locked (game running?) — will retry next launch")
            break                         # everything after this would fail the same way
        except Exception as e:
            out["errors"].append(f"{name}: {e}")
            continue
        if note:
            out["changed"] = True
            out["notes"].append(note)

    out["ok"] = not out["errors"]
    out["cfg_dirty"] = cfg.get(CFG_ICON_STAMP) != before
    if out["notes"]:
        log("[xex] baseline applied to " + xex.name + ":")
        for n in out["notes"]:
            log("  " + n)
    for e in out["errors"]:
        log("[xex] " + e)
    return out


def status(xex_path, cfg=None) -> list[str]:
    """Human-readable current state of the four baseline patches — for a future XEX tab, and for
    `python -m launcher.xex_baseline <xex>`."""
    cfg = cfg or {}
    xex = Path(xex_path)
    lines = []
    try:
        enc, comp = XP.get_comp_type(xex)
        lines.append(f"format: comp_type={comp} enc={enc}" + ("  (flat, patchable)" if comp == 1 and enc == 0
                     else "  (COMPRESSED — needs flattening before any patch)"))
        if comp != 1 or enc != 0:
            return lines
    except Exception as e:
        return [f"format: unreadable ({e})"]
    for label, fn in (
            ("roster cap", lambda: RC.status(xex)),
            ("game date", lambda: "game date: " + ", ".join(
                f"{k} {v[0]}-{v[1]:02d}-{v[2]:02d}" for k, v in GD.read(xex).items())),
            ("matchup", lambda: "default matchup: home id {}, away id {}".format(*DM.read(xex))),
            ("eye shader", lambda: ST.status(xex))):
        try:
            lines.append(fn())
        except Exception as e:
            lines.append(f"{label}: {e}")
    want = wanted_season_year(cfg)
    pinned = want != current_season_start_year() or cfg.get(CFG_SEASON) not in (None, "", AUTO)
    lines.append(f"wanted season start year: {want}-{(want + 1) % 100:02d} "
                 f"({'pinned in config' if pinned else 'auto, from the system clock'})"
                 + (f"; wanted matchup {tuple(cfg[CFG_MATCHUP])}" if cfg.get(CFG_MATCHUP)
                    else "; matchup: no preference set (XEX left alone)"))
    return lines


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\cloug\Documents\NHL 2k10 Extracted\default.xex"
    for line in status(p):
        print("  " + line)
