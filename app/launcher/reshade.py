"""reshade.py — install / remove the ReShade post-process layer for Xenia.

WHAT THIS IS FOR
    NHL 2K10's own lighting is a 2009 renderer: highlights clip flat, there is no
    tonemap worth the name, and nothing blooms. ReShade grades the finished frame
    on the host, which fixes the *look* without touching a single game file.

    That last part is also its limit. This is Xenia-only — it does NOT travel to a
    console, because the work happens on the PC after the frame is drawn. The
    game-side lighting levers (the c26 ArenaLightingTintAndIntensity constant and
    friends in default.xex) are a separate mechanism and DO travel; see the XEX
    lighting notes. The two are complementary, not alternatives.

WHY d3d12 AND NOT vulkan
    Xenia defaults to the Vulkan backend. ReShade *can* hook Vulkan, but only by
    registering a machine-wide Vulkan layer that then loads into every Vulkan app
    on the system — hard to scope, hard to remove cleanly. On D3D12 it is a plain
    d3d12.dll sitting next to xenia_canary.exe: local, and uninstalled by deleting
    a file. So enabling ReShade requires flipping Xenia to d3d12.

    ⚠ Xenia REWRITES its whole config file on exit, discarding manual edits. So the
    backend must be (re)asserted immediately before launch, not once at install
    time — see ensure_backend(). This is why the launcher checks on every launch
    rather than trusting the install.

WHAT GETS WRITTEN into the Xenia folder
    ReShade64.dll -> d3d12.dll   the injector, named for the API it proxies
    ReShade.ini                  our config: effect search paths, preset, depth
    NHL2K10.ini                  the preset (which effects, and their values)
    reshade-shaders/             125 effects + the small textures they need

    Nothing else in the Xenia folder is touched, and remove() deletes exactly
    this set. The user's own presets/screenshots are left alone.

ENABLED vs INSTALLED — two different things
    disable() does NOT delete anything. It renames d3d12.dll to d3d12.dll.disabled:
    Xenia finds no proxy, loads the real system d3d12.dll, and runs exactly as if
    ReShade were never there — zero overhead, and re-enabling is one rename rather
    than re-copying 7.9 MB. The preset, the shaders and the compiled effect cache
    all survive, so toggling costs nothing and loses no tuning.

    Deleting the files is remove(), a separate and deliberate action.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import resources

# Files we own in the Xenia directory. install() writes these, remove() deletes
# exactly these — keep the two in step or remove() starts leaving litter.
_DLL_NAME = "d3d12.dll"
# The parked name for a disabled-but-installed injector. Any name Windows won't
# load as a DLL proxy works; this one is self-describing in an Explorer window.
_DLL_OFF = "d3d12.dll.disabled"
_INI_NAME = "ReShade.ini"
_PRESET_NAME = "NHL2K10.ini"
_SHADER_DIR = "reshade-shaders"

_CONFIG_NAME = "xenia-canary.config.toml"
_BACKUP_SUFFIX = ".prereshade"

# ReShade reads this at startup. PresetPath is relative to the DLL, so the whole
# install stays relocatable.
_RESHADE_INI = """\
[GENERAL]
EffectSearchPaths=.\\reshade-shaders\\Shaders\\**
TextureSearchPaths=.\\reshade-shaders\\Textures\\**
PresetPath=.\\{preset}
PerformanceMode=1
PreprocessorDefinitions=RESHADE_DEPTH_LINEARIZATION_FAR_PLANE=1000.0,RESHADE_DEPTH_INPUT_IS_UPSIDE_DOWN=0,RESHADE_DEPTH_INPUT_IS_REVERSED=1,RESHADE_DEPTH_INPUT_IS_LOGARITHMIC=0

[INPUT]
KeyOverlay=36,0,0,0
KeyEffects=45,0,0,0
GamepadNavigation=1

[GENERIC_DEPTH]
DepthCopyBeforeClears=1
DepthCopyAtClearIndex=0
UseAspectRatioHeuristics=1

[SCREENSHOT]
SavePath=.\\screenshots
FileFormat=1
"""


# ── locating things ────────────────────────────────────────────────────────────
def payload_dir() -> Path:
    """The bundled ReShade payload (launcher/data/reshade/)."""
    return resources.data_path("reshade")


def payload_ok() -> bool:
    p = payload_dir()
    return (p / "ReShade64.dll").is_file() and (p / _PRESET_NAME).is_file()


def xenia_dir(xenia_path: str | Path | None) -> Path | None:
    """Settings stores a path to xenia_canary.exe, but tolerate a folder too."""
    if not xenia_path:
        return None
    p = Path(xenia_path)
    if p.is_dir():
        return p
    if p.parent.is_dir():
        return p.parent
    return None


def _config_path(d: Path) -> Path:
    return d / _CONFIG_NAME


# ── backend ────────────────────────────────────────────────────────────────────
def current_backend(d: Path) -> str | None:
    """The `gpu = "..."` value from Xenia's config, or None if unreadable."""
    cfg = _config_path(d)
    if not cfg.is_file():
        return None
    try:
        text = cfg.read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'^\s*gpu\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return m.group(1) if m else None


def ensure_backend(d: Path) -> tuple[bool, str]:
    """Force gpu = "d3d12". Returns (changed, message).

    Call this right before launching. Xenia regenerates the config on exit, so a
    value written at install time will not survive; this is the only reliable
    moment to assert it.
    """
    cfg = _config_path(d)
    if not cfg.is_file():
        return False, f"{_CONFIG_NAME} not found — launch Xenia once, then retry."
    cur = current_backend(d)
    if cur == "d3d12":
        return False, "Xenia is already on the d3d12 backend."
    try:
        text = cfg.read_text(encoding="utf-8")
        if re.search(r'^\s*gpu\s*=\s*"', text, re.MULTILINE):
            # Keep the trailing comment column aligned; Xenia's own writer does.
            new = re.sub(r'^(\s*gpu\s*=\s*)"[^"]*"', r'\g<1>"d3d12"', text,
                         count=1, flags=re.MULTILINE)
        else:
            new = text + '\ngpu = "d3d12"\n'
        cfg.write_text(new, encoding="utf-8", newline="")
    except OSError as e:
        return False, f"Could not update {_CONFIG_NAME}: {e}"
    return True, f"Switched Xenia from {cur or 'default'} to d3d12 (required by ReShade)."


# ── status ─────────────────────────────────────────────────────────────────────
def is_installed(d: Path | None) -> bool:
    """Files are present — whether or not the injector is currently active."""
    if not d:
        return False
    has_dll = (d / _DLL_NAME).is_file() or (d / _DLL_OFF).is_file()
    return has_dll and (d / _SHADER_DIR).is_dir()


def is_enabled(d: Path | None) -> bool:
    """The injector is armed: Xenia will load it on next launch."""
    return bool(d) and (d / _DLL_NAME).is_file()


def status(xenia_path: str | Path | None) -> dict:
    """Everything the Settings tab needs to describe the current state."""
    d = xenia_dir(xenia_path)
    if d is None:
        return {"ok": False, "installed": False, "enabled": False, "backend": None,
                "detail": "Set the Xenia executable path in Settings first."}
    installed, enabled = is_installed(d), is_enabled(d)
    backend = current_backend(d)
    if not installed:
        detail = "Not installed."
    elif not enabled:
        detail = "Installed but off. Your preset and shaders are kept — turn it back on any time."
    elif backend == "d3d12":
        detail = "On. Press HOME in-game to open the overlay and tune it live."
    else:
        detail = (f"On, but Xenia is set to the '{backend}' backend, which ReShade cannot "
                  "hook. The launcher will switch it to d3d12 before the next launch.")
    return {"ok": True, "installed": installed, "enabled": enabled, "backend": backend,
            "detail": detail, "dir": str(d)}


# ── install / enable / disable / remove ────────────────────────────────────────────────────────
def install(xenia_path: str | Path | None) -> tuple[bool, str]:
    """Copy the payload in and switch the backend. Idempotent — re-running
    refreshes the DLL and shaders but does NOT clobber a preset the user has
    since tuned in the overlay (ReShade saves edits straight back to it)."""
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."
    if not payload_ok():
        return False, ("The bundled ReShade files are missing from this install "
                       f"({payload_dir()}). Reinstall the launcher.")
    src = payload_dir()
    try:
        # Back up Xenia's config once, before we ever touch the backend, so there
        # is always a pre-ReShade state to return to.
        cfg = _config_path(d)
        bak = cfg.with_suffix(cfg.suffix + _BACKUP_SUFFIX)
        if cfg.is_file() and not bak.exists():
            shutil.copy2(cfg, bak)

        # A parked injector from a previous disable() would otherwise be left
        # behind next to the fresh one.
        (d / _DLL_OFF).unlink(missing_ok=True)
        shutil.copy2(src / "ReShade64.dll", d / _DLL_NAME)
        (d / _INI_NAME).write_text(
            _RESHADE_INI.format(preset=_PRESET_NAME), encoding="utf-8", newline="")

        # Preserve an existing preset — it holds the user's own tuning.
        if not (d / _PRESET_NAME).is_file():
            shutil.copy2(src / _PRESET_NAME, d / _PRESET_NAME)

        shutil.copytree(src / _SHADER_DIR, d / _SHADER_DIR, dirs_exist_ok=True)
    except OSError as e:
        return False, f"Install failed: {e}"

    # ReShade.ini was just rewritten from the template above, which knows nothing
    # about add-ons. Anything installed into it has to be put back or it is silently
    # lost on every refresh — currently that is the DLSS add-on's two settings.
    try:
        from . import dlss
        if dlss.is_installed(d):
            dlss.apply_ini(d)
    except Exception:
        pass

    _, msg = ensure_backend(d)
    return True, ("ReShade installed. " + msg +
                  "\n\nPress HOME in-game to open the overlay. The first launch "
                  "compiles the effects, so expect a pause.")


def enable(xenia_path: str | Path | None) -> tuple[bool, str]:
    """Turn ReShade on, installing the files first if they aren't there yet."""
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."
    if not is_installed(d):
        return install(xenia_path)
    if not is_enabled(d):
        try:
            (d / _DLL_OFF).replace(d / _DLL_NAME)
        except OSError as e:
            return False, f"Could not re-arm the ReShade injector: {e}"
    _, msg = ensure_backend(d)
    return True, "ReShade is on. " + msg + "\n\nPress HOME in-game for the overlay."


def disable(xenia_path: str | Path | None) -> tuple[bool, str]:
    """Turn ReShade off WITHOUT deleting anything.

    Parking the proxy DLL under a name Windows won't load is the whole mechanism:
    Xenia falls back to the system d3d12.dll and runs as though ReShade were never
    installed. The preset, shaders and compiled effect cache are untouched, so
    re-enabling is instant and no tuning is lost.

    The GPU backend is deliberately left on d3d12. Flipping it back would change
    how Xenia emulates — a bigger change than the user asked for, and it would
    have to be flipped again on re-enable. remove() is where the backend is
    restored, because that is the deliberate "undo all of this" action.
    """
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."
    if not is_installed(d):
        return True, "ReShade is not installed."
    if not is_enabled(d):
        return True, "ReShade is already off."
    try:
        (d / _DLL_NAME).replace(d / _DLL_OFF)
    except OSError as e:
        return False, (f"Could not turn ReShade off: {e}\n\n"
                       "If Xenia is running, close it and try again.")
    return True, ("ReShade is off. Nothing was deleted — your preset and shaders are "
                  "still there, so turning it back on is instant.")


def remove(xenia_path: str | Path | None, keep_preset: bool = True) -> tuple[bool, str]:
    """Delete exactly what install() wrote and restore the original GPU backend.

    The preset is kept by default: it is the user's tuning, it is tiny, and it is
    worthless to Xenia on its own, so deleting it only risks losing work.
    """
    d = xenia_dir(xenia_path)
    if d is None:
        return False, "Set the Xenia executable path in Settings first."
    removed = []
    try:
        for name in (_DLL_NAME, _DLL_OFF, _INI_NAME):
            p = d / name
            if p.is_file():
                p.unlink(); removed.append(name)
        sh = d / _SHADER_DIR
        if sh.is_dir():
            shutil.rmtree(sh); removed.append(_SHADER_DIR + "/")
        if not keep_preset and (d / _PRESET_NAME).is_file():
            (d / _PRESET_NAME).unlink(); removed.append(_PRESET_NAME)
    except OSError as e:
        return False, f"Removal failed after deleting {removed}: {e}"

    # Put the backend back. Prefer whatever the pre-ReShade backup recorded over
    # assuming "vulkan", in case this machine was set up differently.
    note = ""
    cfg = _config_path(d)
    bak = cfg.with_suffix(cfg.suffix + _BACKUP_SUFFIX)
    prev = None
    if bak.is_file():
        m = re.search(r'^\s*gpu\s*=\s*"([^"]*)"', bak.read_text(encoding="utf-8"),
                      re.MULTILINE)
        prev = m.group(1) if m else None
    prev = prev or "vulkan"
    if cfg.is_file() and current_backend(d) == "d3d12":
        try:
            text = cfg.read_text(encoding="utf-8")
            cfg.write_text(
                re.sub(r'^(\s*gpu\s*=\s*)"[^"]*"', rf'\g<1>"{prev}"', text,
                       count=1, flags=re.MULTILINE),
                encoding="utf-8", newline="")
            note = f" Xenia's GPU backend restored to {prev}."
        except OSError:
            note = f" Could not restore the GPU backend — set gpu = \"{prev}\" manually."
    if not removed:
        return True, "ReShade was not installed." + note
    return True, "Removed " + ", ".join(removed) + "." + note
