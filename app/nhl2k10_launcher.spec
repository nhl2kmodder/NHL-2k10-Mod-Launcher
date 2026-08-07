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
    'live_offsets.json', 'audio_authored_names.json', 'authored_sfx_labels.json',
    'jersey_convert_profile.json', 'uniform_uv_regions.png',
    'stamp_shader.json', 'decal_atlas.npz',
    # Head editor. Without the landmarker there is no face fit at all; without the segmenter the
    # projector cannot tell hair from the arena behind it and every head ships with the base head's
    # hair recoloured. Both are ~20 MB and both are silently droppable by a glob, which is exactly
    # what this list is for.
    'face_landmarker.task', 'selfie_multiclass.tflite',
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
# Editing tools drop rescue copies next to the real file (speech_seed_names.json.prereferee.bak,
# named_assets.csv.bak, …). A bare glob swept them into the ONEFILE exe — ~40 MB of snapshots the
# app never opens, decompressed to _MEIPASS on every launch. Keep them on disk, out of the build.
for _p in sorted(data_src.glob('*')):
    if _p.is_file() and not _p.name.endswith('.bak'):
        datas.append((str(_p), 'data'))
# SUBDIRECTORIES too (data/stamp_art/ — the Jersey Conversion tab's patch art). The glob above
# is files-only, so a subfolder was silently dropped from the build and the shipped app fell
# back to the kit's own 2009 sponsor marks with no error anywhere.
for _d in sorted(p for p in data_src.glob('*') if p.is_dir()):
    for _p in sorted(_d.rglob('*')):
        if _p.is_file() and not _p.name.endswith('.bak'):
            datas.append((str(_p), str(Path('data') / _p.parent.relative_to(data_src))))

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

# mediapipe (head editor: the face landmarker and the hair/skin segmenter) is not a pure-Python
# package. Besides its native _framework_bindings pyd it reads DATA off disk at runtime — the
# .binarypb calculator graphs and the .tflite models its task APIs build themselves from — and
# those are opened by path relative to the package, so static analysis never sees them and the
# frozen app dies inside create_from_options with a missing-file error rather than an ImportError.
# The two models the builder passes explicitly (face_landmarker.task, selfie_multiclass.tflite)
# are OURS and travel in launcher/data; these are mediapipe's own.
try:
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules
    _mp_datas = collect_data_files('mediapipe', includes=['**/*.binarypb', '**/*.tflite',
                                                          '**/*.txt', '**/*.pbtxt'])
    _mp_binaries = collect_dynamic_libs('mediapipe')
    _mp_hidden = collect_submodules('mediapipe.tasks.python')
except Exception:
    _mp_datas, _mp_binaries, _mp_hidden = [], [], []

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    [str(HERE / 'nhl2k10_launcher.py')],
    pathex=[str(HERE), str(HERE / 'launcher')],
    binaries=_ort_binaries + _mp_binaries,
    datas=datas + _mp_datas,
    hiddenimports=['numpy', 'PIL', 'PIL.Image', 'PIL.ImageTk',
                   'nhl2k10_trace_dump', 'decode_e4837_fixed',
                   'encode_e4837_lazy', 'encode_dxt5', 'archive_textures',
                   'bank_parser', 'audio_names', 'authored_sfx', 'modpack', 'xenia_mem',
                   'live_capture', 'f0985030',
                   'goalie_equipment', 'customasset', 'roster_editor', 'ros_file', 'ros_editor_gui',
                   'ros_live_editor', 'team_colors', 'team_fields', 'team_fields_gui', 'colorpick', 'jerseys',
                   'portrait_assign', 'portrait_download', 'requests', 'urllib3', 'onnxruntime',
                   # head editor
                   'cv2', 'mediapipe', 'face_shape', 'face_builder', 'facial_hair',
                   'face_editor_gui']
                  + _scipy_hidden + _mp_hidden,
    hookspath=[],
    runtime_hooks=[],
    # scipy is here for exactly THREE functions -- distance_transform_edt (archive_textures,
    # encode_dxt5), gaussian_filter and sobel (normal_stitcher) -- all from scipy.ndimage. The
    # full package is ~114 MB on disk and PyInstaller was packing all of it, which is dead weight
    # in a ONEFILE build: every launch unpacks the whole payload to %TEMP% and Defender rescans it
    # after every rebuild. Dropping the unused subpackages removes ~92 MB of that.
    # ndimage genuinely needs scipy._lib, scipy.linalg and scipy.special -- verified by importing
    # the three functions with everything below blocked -- so those stay.
    excludes=['scipy.stats', 'scipy.optimize', 'scipy.sparse', 'scipy.signal',
              'scipy.interpolate', 'scipy.spatial', 'scipy.integrate', 'scipy.io',
              'scipy.fft', 'scipy.fftpack', 'scipy.cluster', 'scipy.odr',
              'scipy.constants', 'scipy.datasets', 'scipy.differentiate'],
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
