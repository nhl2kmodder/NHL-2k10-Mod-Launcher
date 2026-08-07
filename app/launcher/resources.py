"""resources.py — the single resolver for every data file the launcher reads at runtime.

All bundled data lives in **launcher/data/** and is collected into ``<app>/_internal/data/``
by the .spec. Nothing is read from the project root any more.

Why this module exists — a shipped copy of the app has no project root. Reading data from
``_HERE.parents[1]`` only ever worked on the dev machine, and it failed silently rather than
loudly: when discovered_assets.csv was moved out of the project root, resolve() quietly fell
back to a plain crc32 of the synthetic ``disc_<crc>`` name, missed the TOC, and every portrait
decoded to None ("Couldn't decode that portrait"). It stayed hidden for days because the
installed exe still carried a stale copy baked in by an older build, so only a rebuild — which
re-collects datas and drops anything missing — exposed it. The .spec compounded it by bundling
each file behind ``if path.exists()``, so a missing file silently produced a broken app instead
of a failed build.

So: one directory, one resolver, and REQUIRED is verified at build time (see the .spec) and at
startup (see missing()), loudly, instead of surfacing as a mystery failure three tabs deep.

User-writable state does NOT belong here — it goes to %APPDATA%\\NHL2K10 Mod Launcher\\
(see team_fields.defs_path and the launcher's _resolve_config_path), because PyInstaller wipes
the app folder on every --noconfirm build.
"""
from __future__ import annotations
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Files the app cannot function without. Checked at build time by the .spec and at startup.
REQUIRED = (
    "team_iff_catalog.csv",          # IFF tab: the team->asset catalog
    "discovered_assets.csv",         # IFF tab: the 1147 disc_<crc> assets + resolve() crc aliases
    "portrait_key_map.json",         # Portraits tab: player key -> blob (else a ~12s rebuild)
    "global_iff_runtime_map.csv",    # global.iff record -> file offset (captured live)
    "fe_components.json",            # Front-end tab: component map
    "fe_uniform_map.json",           # Front-end tab: team-select jersey decal sheets
    # SUPERSEDED — describes STOCK tile indices only, and adding a team shifts every index past the
    # insertion point. Tile names are now derived live from the logos_*.iff index
    # (logos_atlas.tile_labels); do not read this file. Kept only so old builds still validate.
    "frontend_logo_tile_map.json",   # Front-end tab: logo atlas tiles (stock-only, unread)
    "team_fields.json",              # Teams tab: team-record field defs (seed for %APPDATA% copy)
    "jersey_map.json",               # IFF tab: jersey -> its front-end twin (one edit, both copies)
    "live_offsets.json",             # IFF tab: content-matched tail-texture offsets (rink/arena/led/…)
    "audio_authored_names.json",     # Audio tab: the gameplay-SFX inventory (authored_sfx.py)
    "authored_sfx_labels.json",      # Audio tab: transcribed names for the PA banks (see above)
    "jersey_convert_profile.json",   # Jersey Conversion tab: UV calibration + slot layout
    "uniform_uv_regions.png",        # Jersey Conversion tab: the base's garment-island masks
    # Stamp placement. Without these the preview still renders, but EVERY stamp silently vanishes
    # -- no crest, no numbers -- because the shader path has no decal quads to evaluate against.
    "stamp_shader.json",             # Jersey Editor: per-site shader constants and slot roles
    "decal_atlas.npz",               # Jersey Editor: the mesh's baked decal channel (interp 3)
    # The two vision models the head builder runs on the reference photographs. Between them they
    # are 20 MB, which is most of the install -- but without the landmarker there is no fit at all,
    # and without the segmenter every head gets the base head's hair recoloured (the projector has
    # no other way to tell hair from the arena behind it). See face_builder._detector/_segmenter.
    "face_landmarker.task",          # Head editor: 478-point face fit + head pose
    "selfie_multiclass.tflite",      # Head editor: hair / skin / clothing segmentation
)


def data_dir() -> Path:
    """launcher/data/ from source, <app>/_internal/data/ when frozen."""
    mei = getattr(sys, "_MEIPASS", None)
    return (Path(mei) / "data") if mei else (_HERE / "data")


def data_path(name: str) -> Path:
    return data_dir() / name


def missing() -> list[str]:
    """REQUIRED files that aren't present — empty when the install is intact."""
    return [n for n in REQUIRED if not data_path(n).exists()]
