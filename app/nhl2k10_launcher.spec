# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

HERE = Path(SPECPATH)

# ── Collect data files ────────────────────────────────────────────────────────
datas = []

# Launcher sub-package (xenia_mem.py, xenos_tiling.py, __init__.py)
launcher_pkg = HERE / 'launcher'
for f in launcher_pkg.glob('*.py'):
    datas.append((str(f), 'launcher'))

# Window/taskbar icon + the game-XEX icon written by ensure_game_icon() (bundled to sys._MEIPASS).
# ('NHL 2K10 Logo.png' used to be listed here: it lives in the project root, not HERE, so it was
# never actually collected — and nothing reads it. Dropped rather than left as a phantom.)
for _res in ('NHL_2k_Launcher_Icon.png', 'app_icon.ico', 'NHL 2k27 Game Icon.png'):
    _p = HERE / _res
    if _p.exists():
        datas.append((str(_p), '.'))

# ── Runtime data: launcher/data/ -> <app>/_internal/data/ ─────────────────────
# ONE directory, resolved at runtime by launcher/resources.py. These used to be read from the
# PROJECT ROOT and bundled one-by-one behind `if path.exists()` — which silently produced a
# BROKEN app whenever a file wasn't where the spec looked. That is exactly how the Portraits tab
# broke: discovered_assets.csv was moved out of the project root, the build quietly dropped it,
# resolve() could no longer map disc_<crc> names, and every portrait decoded to None. Missing
# REQUIRED data is now a hard build failure.
data_src = HERE / 'launcher' / 'data'
_required = [
    'team_iff_catalog.csv', 'discovered_assets.csv', 'portrait_key_map.json',
    'global_iff_runtime_map.csv', 'fe_components.json', 'fe_uniform_map.json',
    'frontend_logo_tile_map.json', 'team_fields.json', 'jersey_map.json',
    'live_offsets.json',
]
_have = {p.name for p in data_src.glob('*') if p.is_file()} if data_src.is_dir() else set()
_gone = [f for f in _required if f not in _have]
if _gone:
    raise SystemExit(
        "BUILD ABORTED — required data file(s) missing from launcher/data/:\n"
        + "".join(f"  - {f}\n" for f in _gone)
        + "\nThese ship inside the app; without them the built exe is broken at runtime "
          "(e.g. no discovered_assets.csv => every portrait fails to decode).\n"
          "Restore them to launcher/data/ and rebuild. Keep this list in sync with "
          "launcher/resources.py REQUIRED."
    )
for _p in sorted(data_src.glob('*')):
    if _p.is_file():
        datas.append((str(_p), 'data'))

# onnxruntime ships native DLLs (capi / providers) that PyInstaller's static analysis misses — collect
# them explicitly so the bundled exe can run the face-parsing model (jersey compositing). Optional: if
# onnxruntime isn't installed, the app still runs and just falls back to the heuristic head/neck cutout.
try:
    from PyInstaller.utils.hooks import collect_dynamic_libs
    _ort_binaries = collect_dynamic_libs('onnxruntime')
except Exception:
    _ort_binaries = []

# scipy >= 1.18 vendors array_api_compat under scipy._external and imports its submodules
# dynamically (scipy._lib._array_api -> ...array_api_compat.numpy.fft), which static analysis and
# the hooks-contrib scipy hook (as of 2026.5) miss — the built exe then dies at startup with
# ModuleNotFoundError. Collect the whole vendored tree explicitly.
try:
    from PyInstaller.utils.hooks import collect_submodules
    _scipy_hidden = collect_submodules('scipy._external')
except Exception:
    _scipy_hidden = []

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    [str(HERE / 'nhl2k10_launcher.py')],
    pathex=[str(HERE), str(HERE / 'launcher')],
    binaries=_ort_binaries,
    datas=datas,
    hiddenimports=['numpy', 'PIL', 'PIL.Image', 'PIL.ImageTk',
                   'nhl2k10_trace_dump', 'decode_e4837_fixed',
                   'encode_e4837_lazy', 'encode_dxt5', 'archive_textures',
                   'bank_parser', 'audio_names', 'modpack', 'xenia_mem', 'live_capture', 'f0985030',
                   'goalie_equipment', 'customasset', 'roster_editor', 'ros_file', 'ros_editor_gui',
                   'ros_live_editor', 'team_colors', 'team_fields', 'team_fields_gui', 'colorpick', 'jerseys',
                   'portrait_assign', 'portrait_download', 'requests', 'urllib3', 'onnxruntime']
                  + _scipy_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,              # ONEFILE: binaries + datas are packed INSIDE the exe
    a.datas,
    [],
    exclude_binaries=False,
    name='NHL2K10 Mod Launcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # no black console window
    uac_admin=True,          # NEEDS admin: the goalie-mask / portrait LIVE-assign features write into
                             # Xenia's process memory (set_mask etc.), which requires elevation when
                             # Xenia runs at a higher/equal integrity. (The earlier uac_admin=False was
                             # a wrong fix for slow boot — the real cause was a list_textures loop, now
                             # fixed — and it broke the live memory writes.)
    icon=str(HERE / 'app_icon.ico') if (HERE / 'app_icon.ico').exists() else None,
)

# ── ONEFILE (single self-contained .exe) ─────────────────────────────────────
# Deliberate choice: ship ONE file the user double-clicks, with nothing beside it to explain.
# Accepted trade-offs (this was previously onedir for exactly these reasons — don't "fix" it back
# without asking): every launch unpacks ~30MB to %TEMP%\_MEI<pid>\ before the window appears, so
# start is slower; Defender re-scans the payload each run; and a hard kill leaks a stale _MEI
# folder. Everything the app reads (launcher/data/*) is unpacked to sys._MEIPASS, which is why
# resources.py resolves data through sys._MEIPASS and why user state must live in %APPDATA%
# instead — _MEIPASS is a temp dir that is deleted on exit.
# There is NO COLLECT step in onefile: the EXE above is the whole deliverable.
