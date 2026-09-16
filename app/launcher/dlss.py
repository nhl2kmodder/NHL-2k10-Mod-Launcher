"""dlss.py — install / remove the RenoDX DLSS-NR ("DLSS 5") add-on for Xenia.

WHAT THIS IS FOR
    NHL 2K10 has no DLSS and never will: it is a 2009 Xbox 360 title running under
    emulation, so there is no engine to feed NVIDIA jitter, motion vectors or depth.
    RenoDX's DLSS add-on has a path for exactly that case — "Direct Neural
    Rendering" hooked at Present. It takes the finished swapchain image, hands it to
    NVIDIA's neural-rendering model (nvngx_dlssnr.dll) with dummy temporal inputs,
    and puts the result back. No game cooperation required.

    That is why the two settings below are not defaults: Hook Method must be
    Present (not Auto, which looks for a real DLSS pass to attach to) and Require
    DLSS must be Off (On refuses to run without matching SR/AA/RR temporal inputs,
    which this game will never produce).

    It is a ReShade ADD-ON, not a standalone injector — ReShade 6.8 (Add-on build)
    is what loads it. So this module is a passenger on reshade.py: the add-on file
    goes in the same folder as d3d12.dll, its settings live in the same ReShade.ini,
    and with ReShade off nothing loads it at all.

WHAT WE CAN AND CANNOT SHIP
    renodx-dlss.addon64  — ships with the launcher (launcher/data/dlss/).
    nvngx_dlssnr.dll     — does NOT and cannot. It is NVIDIA's neural-rendering
                           runtime, redistributed under their terms, and it is not
                           ours to bundle. The user drops it next to xenia_canary.exe
                           themselves; status() reports when it is missing, and the
                           add-on itself says "Unavailable (nvngx_dlssnr.dll missing)".

HARDWARE
    DLSS needs NVIDIA RTX (Turing or newer). Neural rendering is newer still:
    RTX 50 runs the stock runtime, and RTX 20/30/40 need the patched build of
    nvngx_dlssnr.dll. A GTX card has no tensor cores and cannot run any of it —
    the toggle will install cleanly and then do nothing.

ENABLED vs INSTALLED
    Same mechanism as reshade.py, for the same reason: disable() renames
    renodx-dlss.addon64 to .addon64.disabled so ReShade no longer sees an add-on
    to load, and deletes nothing. Toggling costs one rename and loses no settings.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import reshade, resources

_ADDON_NAME = "renodx-dlss.addon64"
# Parked name for a disabled-but-installed add-on. ReShade globs for *.addon64, so
# any suffix past the extension takes it out of the search.
_ADDON_OFF = "renodx-dlss.addon64.disabled"

# The DLSS 5 add-on and the older DLSS-5-specific one cannot coexist — two neural
# consumers fighting over the same runtime. If the user has dropped one in by hand,
# park it rather than delete it.
_RIVAL_NAME = "renodx-dlss5.addon64"
_RIVAL_OFF = "renodx-dlss5.addon64x"

# NVIDIA's neural-rendering runtime. The user supplies this; we only look for it.
_RUNTIME_NAME = "nvngx_dlssnr.dll"

_INI_NAME = "ReShade.ini"

# ReShade.ini sections we write into. AddonPath is relative to the ReShade DLL, the
# same convention EffectSearchPaths already uses in reshade.py's template.
_ADDON_SECTION = "ADDON"
_RENODX_SECTION = "RENODX-DLSS"

# The two settings that make this work on a game with no DLSS of its own.
#
# ⚠ These are the add-on's own ReShade.ini keys, and the values are the combo
# INDEXES. Hook Method's list is (Auto, Upscaled, FrameGen, Present) — read out of
# the add-on binary — so Present is 3. Require DLSS is a two-state Off/On, so Off
# is 0. If a future build reorders them these numbers go stale silently, which is
# why the UI tells the user to confirm both in the ReShade overlay: the add-on
# writes the corrected value straight back here and it sticks.
_RENODX_SETTINGS = {
    "DirectNeuralRenderingHookPoint": "3",   # Present
    "DirectNeuralRenderingRequireDlss": "0",  # Off
}


# ── locating things ────────────────────────────────────────────────────────────
def payload_dir() -> Path:
    """The bundled add-on payload (launcher/data/dlss/)."""
    return resources.data_path("dlss")


def payload_ok() -> bool:
    return (payload_dir() / _ADDON_NAME).is_file()


def xenia_dir(xenia_path: str | Path | None) -> Path | None:
    """Same folder ReShade installs into — the add-on must sit beside d3d12.dll."""
    return reshade.xenia_dir(xenia_path)


# ── ReShade.ini editing ────────────────────────────────────────────────────────
def _set_key(text: str, section: str, key: str, value: str) -> str:
    """Set section/key in an INI, creating either if absent, leaving the rest alone.

    Hand-rolled rather than configparser because ReShade.ini is not ours: it holds
    the user's overlay tuning and ReShade's own bookkeeping, and configparser would
    rewrite comments, key case and value quoting on every save.
    """
    sec_re = re.compile(r"^\[" + re.escape(section) + r"\]\s*$", re.MULTILINE)
    m = sec_re.search(text)
    if not m:
        sep = "" if text.endswith("\n") or not text else "\n"
        return f"{text}{sep}\n[{section}]\n{key}={value}\n"

    start = m.end()
    nxt = re.compile(r"^\[", re.MULTILINE).search(text, start)
    end = nxt.start() if nxt else len(text)
    body = text[start:end]

    key_re = re.compile(r"^(" + re.escape(key) + r"\s*=).*$", re.MULTILINE)
    if key_re.search(body):
        body = key_re.sub(lambda mm: mm.group(1) + value, body, count=1)
    else:
        # `start` sits immediately after the "]" of the section header, so the new
        # key must bring its own leading newline. Dropping it glues the key onto the
        # header ("[RENODX-DLSS]Key=1"), which then stops matching sec_re — and every
        # later call appends a duplicate section instead of editing this one.
        body = f"\n{key}={value}\n" + body.lstrip("\n")
    return text[:start] + body + text[end:]


def apply_ini(d: Path) -> bool:
    """Point ReShade at the add-on folder and pre-set the two non-default settings.

    Called on install AND from reshade.install(), because reshade.install() rewrites
    ReShade.ini wholesale from its template — without this, refreshing ReShade would
    silently drop the add-on's configuration.

    Returns False if there is no ReShade.ini yet (ReShade not installed).
    """
    ini = d / _INI_NAME
    if not ini.is_file():
        return False
    try:
        text = ini.read_text(encoding="utf-8")
        text = _set_key(text, _ADDON_SECTION, "AddonPath", ".")
        for k, v in _RENODX_SETTINGS.items():
            text = _set_key(text, _RENODX_SECTION, k, v)
        ini.write_text(text, encoding="utf-8", newline="")
    except OSError:
        return False
    return True


# ── status ─────────────────────────────────────────────────────────────────────
def is_installed(d: Path | None) -> bool:
    """The add-on file is present — armed or parked."""
    if not d:
        return False
    return (d / _ADDON_NAME).is_file() or (d / _ADDON_OFF).is_file()


def is_enabled(d: Path | None) -> bool:
    """ReShade will load the add-on on next launch."""
    return bool(d) and (d / _ADDON_NAME).is_file()


def has_runtime(d: Path | None) -> bool:
    """The user has supplied NVIDIA's neural-rendering DLL."""
    return bool(d) and (d / _RUNTIME_NAME).is_file()


def status(xenia_path: str | Path | None) -> dict:
    """Everything the Settings tab needs to describe the current state."""
    d = xenia_dir(xenia_path)
    if d is None:
        return {"ok": False, "installed": False, "enabled": False, "runtime": False,
                "detail": "Set the Xenia executable path in Settings first."}
    installed, enabled = is_installed(d), is_enabled(d)
    runtime, rs_on = has_runtime(d), reshade.is_enabled(d)
    if not installed:
        detail = "Not installed."
    elif not enabled:
        detail = "Installed but off. Turning it back on is instant."
    elif not rs_on:
        detail = ("On, but ReShade is off — it is a ReShade add-on, so nothing loads it. "
                  "Tick \"Use ReShade\" above.")
    elif not runtime:
        detail = (f"On, but {_RUNTIME_NAME} is missing from the Xenia folder. NVIDIA's "
                  "neural-rendering runtime is not ours to ship — copy it in yourself "
                  "(RTX 20/30/40 need the patched build). Until then the add-on loads "
                  "and reports itself unavailable.")
    else:
        detail = ("On. Press HOME in-game, open the Add-ons tab and confirm RenoDX DLSS "
                  "shows Hook Method = Present and Require DLSS = Off.")
    return {"ok": True, "installed": installed, "enabled": enabled, "runtime": runtime,
            "reshade": rs_on, "detail": detail, "dir": str(d)}


# ── install / enable / disable / remove ────────────────────────────────────────
def install(xenia_path: str | Path | None) -> tuple[bool, str]:
    """Copy the add-on in, park any rival, and write its settings into ReShade.ini.

    Idempotent: re-running refreshes the add-on binary and re-asserts the settings.
    """
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."
    if not payload_ok():
        return False, ("The bundled DLSS add-on is missing from this install "
                       f"({payload_dir()}). Reinstall the launcher.")
    notes = []
    try:
        # A parked copy from a previous disable() would otherwise sit next to the
        # fresh one, and a rival consumer would fight it for the runtime.
        (d / _ADDON_OFF).unlink(missing_ok=True)
        if (d / _RIVAL_NAME).is_file():
            (d / _RIVAL_NAME).replace(d / _RIVAL_OFF)
            notes.append(f"Parked {_RIVAL_NAME} as {_RIVAL_OFF} — two neural add-ons "
                         "cannot run together.")
        shutil.copy2(payload_dir() / _ADDON_NAME, d / _ADDON_NAME)
    except OSError as e:
        return False, f"Install failed: {e}"

    if not apply_ini(d):
        notes.append("ReShade.ini was not found, so the add-on's settings were not "
                     "pre-written — turn ReShade on and they will be.")
    if not has_runtime(d):
        notes.append(f"{_RUNTIME_NAME} is not in the Xenia folder yet. Copy NVIDIA's "
                     "neural-rendering runtime in (RTX 20/30/40 need the patched "
                     "build); the add-on cannot do anything without it.")
    return True, ("DLSS neural rendering installed.\n\n" + "\n\n".join(notes) +
                  ("\n\n" if notes else "") +
                  "Press HOME in-game and check the Add-ons tab: RenoDX DLSS should "
                  "read Hook Method = Present, Require DLSS = Off.")


def enable(xenia_path: str | Path | None) -> tuple[bool, str]:
    """Turn the add-on on, installing it — and ReShade, which loads it — if needed."""
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."

    # It is a ReShade add-on: without ReShade armed there is nothing to load it, so
    # bring ReShade up first rather than installing something inert.
    rs_note = ""
    if not reshade.is_enabled(d):
        ok, msg = reshade.enable(xenia_path)
        if not ok:
            return False, "DLSS needs ReShade, and ReShade could not be turned on:\n\n" + msg
        rs_note = "ReShade was turned on too — it is what loads the add-on.\n\n"

    if not is_installed(d):
        ok, msg = install(xenia_path)
        return ok, (rs_note + msg) if ok else msg
    if not is_enabled(d):
        try:
            (d / _ADDON_OFF).replace(d / _ADDON_NAME)
        except OSError as e:
            return False, f"Could not re-arm the DLSS add-on: {e}"
    apply_ini(d)
    tail = ""
    if not has_runtime(d):
        tail = (f"\n\n{_RUNTIME_NAME} is still missing from the Xenia folder, so the "
                "add-on will report itself unavailable.")
    return True, rs_note + "DLSS neural rendering is on." + tail


def disable(xenia_path: str | Path | None) -> tuple[bool, str]:
    """Turn it off without deleting anything — ReShade stops seeing an add-on."""
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."
    if not is_installed(d):
        return True, "The DLSS add-on is not installed."
    if not is_enabled(d):
        return True, "DLSS neural rendering is already off."
    try:
        (d / _ADDON_NAME).replace(d / _ADDON_OFF)
    except OSError as e:
        return False, (f"Could not turn DLSS off: {e}\n\n"
                       "If Xenia is running, close it and try again.")
    return True, ("DLSS neural rendering is off. Nothing was deleted, so turning it "
                  "back on is instant.")


def remove(xenia_path: str | Path | None) -> tuple[bool, str]:
    """Delete exactly what install() wrote.

    nvngx_dlssnr.dll is NEVER touched: the user put it there, it may be shared with
    other tools, and re-obtaining it is not trivial. ReShade.ini is left alone too —
    it is ReShade's file, and stale [RENODX-DLSS] keys are inert.
    """
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."
    removed = []
    try:
        for name in (_ADDON_NAME, _ADDON_OFF):
            p = d / name
            if p.is_file():
                p.unlink(); removed.append(name)
    except OSError as e:
        return False, f"Removal failed after deleting {removed}: {e}"
    if not removed:
        return True, "The DLSS add-on was not installed."
    return True, ("Removed " + ", ".join(removed) + f". {_RUNTIME_NAME} was left in "
                  "place — it is yours, not ours.")
