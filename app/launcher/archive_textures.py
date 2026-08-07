"""archive_textures.py — PERMANENT NHL2k10 texture extract/replace (static, file-based).

This is the proven Buffalo-logo pipeline (see HOW_TEXTURE_MODDING_WORKS.txt):
  name -> uppercase-CRC32 -> 0A TOC -> archive+offset
  resource = run of 0x0E4837 LZ blobs (descriptor pairs + VRAM); VRAM = GPU-tiled DXT.

EXTRACT  : read from CLEAN (pristine), decompress, find the primary fetch constant,
           un-tile+DXT-decode -> uncompressed A8R8G8B8 DDS (keeps alpha).
REPLACE  : edited image -> game-order tiled DXT5 (encode_dxt5) -> re-pack the VRAM
           blob's mip0 (mips kept) -> custom-LZ compress (encode_e4837_lazy) -> splice
           into the GAME archives. IN-PLACE if it fits the original blob; else RELOCATE
           the whole resource to the end of 1B and repoint its TOC entry. CLEAN is never
           written; only the game-dir archives are modified, after a one-time .orig backup.
"""
from __future__ import annotations
import struct, zlib, re, sys, shutil, csv, json
import hashlib
import os
from collections import OrderedDict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PROJ = _HERE.parents[1]                              # .../NHL 2k10 Extracted
for p in (str(_HERE), str(_PROJ)):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from . import nhl2k10_trace_dump as T
except ImportError:
    import nhl2k10_trace_dump as T
import decode_e4837_fixed as DF          # project root (proven tools)
import encode_e4837_lazy as EE
import encode_dxt5 as ED
import resources as R
import numpy as np
from PIL import Image

# GAME_DIR — the ONE game-files folder (config "root_path"); set by the launcher at startup.
# There is no separate "clean files" copy any more: the launcher already writes a one-time
# <arc>.orig backup before it first modifies an archive, so the pristine bytes are always
# <game>/X.orig if it exists, else <game>/X (never modified => already clean). Verified: the old
# CLEAN_DIR duplicate was byte-identical (md5) to 0A.orig / 0B.orig / 1B.orig, and to 1A which has
# no .orig. Keeping a 5GB copy + a path hardcoded into one machine's Documents bought nothing and
# was a shipping blocker. Pristine reads go through clean=True (see _arc_file/load_toc/resolve).
GAME_DIR = None


def set_game_dir(d):
    """Point the reader at the game-files folder. Call before any read."""
    global GAME_DIR, _PORTRAIT_PACK, _PORTRAIT_OFFS, _PORTRAIT_KEY_MAP, _PORTRAIT_CUR
    GAME_DIR = Path(d) if d else None
    _TREE_CACHE.clear()                 # every cache below is keyed to the old folder
    _PORTRAIT_PACK = _PORTRAIT_OFFS = _PORTRAIT_KEY_MAP = None
    _PORTRAIT_CUR = None


def _dir(d=None):
    d = d or GAME_DIR
    if not d:
        raise RuntimeError("game files folder not set - set it in the Settings tab")
    return Path(d)


def _arc_file(arc_dir, arc, clean=False):
    """Path to an archive file; clean=True prefers the pristine <arc>.orig backup when present."""
    base = _dir(arc_dir)
    if clean:
        o = base / (arc + ".orig")
        if o.exists():
            return o
    return base / arc
# Every bundled data file resolves through resources.data_path() -> launcher/data/ (source) or
# <app>/_internal/data/ (frozen). NEVER read data from the project root: a shipped app has none,
# and doing so failed silently (see resources.py).
CATALOG_CSV = R.data_path("team_iff_catalog.csv")
# Discovered-asset catalog: the 1147 texture assets surfaced by the full-TOC sweep that the
# original team_iff_catalog didn't list (uniforms/components, portraits, flags, atlases, …).
# Rows with a synthetic `disc_<crc>` iff carry the real TOC crc so resolve() can find them.
DISCOVERED_CSV = R.data_path("discovered_assets.csv")
# Live-capture catalog (live_capture.py output): real file offsets for "loader-repacked"
# packs (global.iff, …) whose sub-texture offsets aren't stored in the file. Optional.
LIVE_CATALOG = _PROJ / "live_capture" / "live_offsets.json"
# Shipped-with-the-launcher copy (arena_trace.py captures we distribute). Merged UNDER the user's
# local catalog by _live_catalog(); offsets are install-independent so this "just works" for anyone.
LIVE_CATALOG_BUNDLED = R.data_path("live_offsets.json")
MAGIC = bytes([0x0e, 0x48, 0x37, 0xc3])
ARCS = ("0A", "0B", "1A", "1B")            # the shipped four; the header is authoritative
_BE = lambda b, o: struct.unpack_from(">I", b, o)[0]


def arc_table(d0a):
    """The 0A header's archive table -> [(name, size_bytes), ...] in concat order.

    Count is at 0x08 and each 16-byte row is [sectors u32][0][name UTF-16BE, NUL-terminated, 8 B],
    so the file names are DATA, not a hardcoded list — which is what makes a 5th archive possible.
    The entry table starts right after this table, at `entry_base()`."""
    n = _BE(d0a, 0x08)
    align = _BE(d0a, 0x04)
    out = []
    for i in range(n):
        o = 0x18 + i * 16
        nm = d0a[o + 8:o + 16].decode("utf-16-be").split("\0")[0]
        out.append((nm, _BE(d0a, o) * align))
    return out


def entry_base(d0a):
    return 0x18 + _BE(d0a, 0x08) * 16

# Scene/front-end assets whose textures live at a FIXED offset inside a multi-texture
# VRAM blob (no standard fetch constant — located via a title-screen GPU trace + byte
# match). pseudo-name -> (real_iff, vram_offset, w, h, fmt). Verified full-match.
SCENE_ASSETS = {
    "titlepage_cover.iff":        ("titlepage.iff", 0x125000, 1024, 1024, "DXT4_5"),  # Ovechkin cover
    "titlepage_nhl2k10_logo.iff": ("titlepage.iff", 0x029000, 256,  128,  "DXT4_5"),  # NHL 2K10 wordmark
    "titlepage_eula_text.iff":    ("titlepage.iff", 0x03D000, 1024, 512,  "DXT4_5"),  # ESRB/legal text
    "titlepage_2k_logo.iff":      ("titlepage.iff", 0x0F1000, 256,  256,  "DXT4_5"),  # 2K Sports red
    "titlepage_stats.iff":        ("titlepage.iff", 0x111000, 256,  128,  "DXT4_5"),  # "STATS" text
}
_DXT_BPU = {"DXT4_5": 16, "DXT5": 16, "DXT1": 8}

# The engine stores these smooth-alpha formats PREMULTIPLIED (DXT4 semantics): stored RGB =
# colour*alpha, transparent texels = 0 (verified against native logos; the GPU composites
# src.rgb + dst*(1-a)). The encoder (encode_dxt5.encode_image) re-premultiplies on REPLACE;
# on EXTRACT/PREVIEW we UN-premultiply so the user edits/sees true straight-alpha colour.
_PREMULT_FMTS = {"DXT2_3", "DXT4_5", "DXT5"}


def _rgba_is_premult(a):
    """a = float (H,W,4). True if the stored RGB looks premultiplied (RGB <= alpha for ~all
    pixels). Straight-alpha art violates this (a partial-alpha pixel can be bright); those must
    NOT be un-premultiplied/premultiplied or every partial-alpha pixel darkens. Opaque/empty
    -> False (premultiply is a no-op there anyway)."""
    al = a[..., 3]
    if al.max() == 0 or al.min() >= 255:
        return False
    return float(np.mean(a[..., :3].max(-1) > al + 8)) < 0.02   # straight art shows 7%+; premult ~0%


def _orig_is_premult(dec, vram_off, w, h, fmt, tiled):
    """Detect whether the ORIGINAL stored texture (in the CLEAN blob) is premultiplied-alpha,
    so we treat each texture correctly instead of assuming all DXT4_5 are premultiplied.
    MUST decode the RAW stored bytes (NOT _decode_at, which un-premultiplies — that would make
    this always read straight and return False, silently breaking premultiplied textures)."""
    if fmt not in _PREMULT_FMTS:
        return False
    try:
        bpu = _FMT_BPU[fmt]; mip0 = (w // 4) * (h // 4) * bpu
        img = T.decode(_dxt_endian(dec[vram_off:vram_off + mip0], fmt), w, h, fmt, bpu, True, tiled, 0).convert("RGBA")
        a = np.frombuffer(img.tobytes(), np.uint8).reshape(h, w, 4).astype(np.float32)
    except Exception:
        return False
    return _rgba_is_premult(a)


def _to_straight(img, fmt):
    """Premultiplied-alpha decode -> STRAIGHT-alpha PIL image for editing/preview (inverse of
    the encoder's premultiply: straight_rgb = premult_rgb*255/alpha, transparent -> 0).
    No-op for non-premultiplied / fully-opaque images AND for straight-alpha textures (detected
    by the premult invariant — most DXT4_5 UI/HUD art is straight, not premultiplied)."""
    if img is None or fmt not in _PREMULT_FMTS:
        return img
    a = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(img.height, img.width, 4).astype(np.float32)
    al = a[..., 3:4]
    if al.max() == 0 or al.min() >= 255 or not _rgba_is_premult(a):   # opaque or straight -> as-is
        return img
    a[..., :3] = np.where(al > 0, np.clip(a[..., :3] * 255.0 / np.maximum(al, 1.0), 0, 255), 0)
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def _premult_pil(img):
    """Straight-alpha PIL -> premultiplied-alpha PIL (RGB *= alpha/255). Used to downscale mip
    levels IN premultiplied space — the correct filter for the engine's premultiplied DXT4_5
    (transparent texels contribute 0, so edges stay clean and mips don't bloat / go blocky)."""
    a = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(img.height, img.width, 4).astype(np.float32)
    a[..., :3] *= a[..., 3:4] / 255.0
    return Image.fromarray(a.astype(np.uint8), "RGBA")


def _box_lvl(img, size):
    """BOX-downscale a mip level with RGB and alpha filtered INDEPENDENTLY.

    PIL's RGBA resize weights the colour channels by alpha (an implicit premultiply), which drags
    transparent texels' RGB toward black. Measured against the shipped 4444 uniform mips: resizing
    as RGBA scores MSE 13,635 vs the real stored level, resizing RGB and alpha separately scores
    43.7 (ANA/BOS rec0, ANA rec2 all agree). That alpha-weighting is also why regenerated mips came
    out far more zero-filled than the originals. Callers that genuinely want premultiplied mips
    premultiply the SOURCE first (_premult_pil) — going through PIL's RGBA path as well would
    premultiply twice."""
    r = img.convert("RGB").resize(size, Image.BOX)
    a = img.convert("RGBA").getchannel("A").resize(size, Image.BOX)
    out = r.convert("RGBA")
    out.putalpha(a)
    return out


def _posterize(img, levels):
    """Quantise RGB to `levels` steps/channel (alpha untouched) -> fewer distinct colours -> the
    BC3 endpoints repeat -> the 0x0E4837 blob compresses smaller. Used to AUTO-FIT a multi-texture
    in-place edit that's a hair too big (levels=64 is ~imperceptible; DXT 565 is already ~6-bit)."""
    if not levels:
        return img
    a = np.frombuffer(img.convert("RGBA").tobytes(), np.uint8).reshape(img.height, img.width, 4)
    step = 256 // levels
    out = a.copy()
    out[..., :3] = np.minimum(255, (a[..., :3] // step) * step + step // 2)
    return Image.fromarray(out, "RGBA")

# Scene textures with a MIP CHAIN in the same VRAM blob (contiguous, verified by
# alpha-silhouette match). Replace MUST rewrite every level from the edited image —
# otherwise the GPU's trilinear filtering blends the new mip0 with the ORIGINAL mips
# (= ghosting + softness). name -> [(vram_off, w, h), ...] (mip0 first).
MIP_CHAINS = {
    "titlepage_cover.iff":   [(0x125000, 1024, 1024), (0x225000, 512, 512),
                              (0x265000, 256, 256), (0x275000, 128, 128)],
    "titlepage_eula_text.iff": [(0x03D000, 1024, 512), (0x0BD000, 512, 256)],
    "titlepage_2k_logo.iff":   [(0x0F1000, 256, 256), (0x101000, 128, 128)],
}


# ── paths (root = launcher's …/NHL2k10_Extracted_Files) ─────────────────────
def extracted_root(root):
    """The single extract+edit folder (grouped: Logos/, Uniform/<TEAM>/<KIT>/, …). Replaces the
    old Original/ + Modified/ split — you extract here and edit in place; a hash manifest records
    the pristine bytes so Apply-All only re-encodes files you actually changed."""
    return Path(root) / "Textures" / "Extracted"


# ── extract-time hash manifest (so Apply-All skips unedited extractions) ─────
def _manifest_file(ex_root):
    return Path(ex_root) / ".extract_manifest.json"


def _load_manifest(ex_root):
    try:
        return json.loads(_manifest_file(ex_root).read_text())
    except Exception:
        return {}


def _save_manifest(ex_root, m):
    try:
        Path(ex_root).mkdir(parents=True, exist_ok=True)
        _manifest_file(ex_root).write_text(json.dumps(m))
    except Exception:
        pass


def _rel_key(ex_root, file_path):
    try:
        return Path(file_path).resolve().relative_to(Path(ex_root).resolve()).as_posix()
    except Exception:
        return Path(file_path).name


def _sha1(path):
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def mark_extracted(root, file_path):
    """Record the pristine hash of a just-extracted file (root = …/NHL2k10_Extracted_Files)."""
    if not file_path:
        return
    ex = extracted_root(root)
    h = _sha1(file_path)
    if h:
        m = _load_manifest(ex)
        m[_rel_key(ex, file_path)] = h
        _save_manifest(ex, m)


def is_edited(root, file_path):
    """True if `file_path` differs from its recorded extract-time hash (or has none -> treat as an
    edit, so legacy Modified/ files and hand-dropped files always apply)."""
    if not file_path:
        return False
    ex = extracted_root(root)
    rec = _load_manifest(ex).get(_rel_key(ex, file_path))
    if rec is None:
        return True
    return _sha1(file_path) != rec


def folders_with_edits(extracted_dir: Path) -> list[Path]:
    """
    Scans only the Extracted/ directory for valid edit targets.
    Avoids multi-directory comparison overhead and skips deep unused subtrees.
    """
    if not extracted_dir.exists():
        return []

    edited_folders = []
    
    # Efficient os.scandir walk through Extracted/
    for root, dirs, files in os.walk(extracted_dir):
        # Filter for relevant folders containing image/manifest edits
        if any(f.endswith((".png", ".dds", ".json")) for f in files):
            edited_folders.append(Path(root))
            
    return edited_folders


def revert_extract(root, name, rec_list=None, clean_dir=None, log=print, remove_png=False):
    """Re-extract `name` from the CLEAN game files back into Extracted/ (undo edits, refresh the
    hash so it counts as unedited again). rec_list = list_textures(name) or None for single/primary.
    remove_png=True also deletes any sibling .png with the same stem as each recovered .dds —
    PNGs outrank DDS everywhere edits are resolved, so a leftover PNG would keep masking the
    pristine file (used by mod-pack revert; the IFF tab's Extract Original stays non-destructive).
    Returns the number of files rewritten."""
    ex = extracted_root(root)
    folder = asset_iff(name)
    out_dir = ex / folder
    n = 0

    def _drop_png(dds_path):
        if not remove_png:
            return
        png = Path(dds_path).with_suffix(".png")
        if png.exists():
            try:
                png.unlink()
                log(f"  removed stale {png.name} (masked the recovered original .dds)")
            except Exception as pe:
                log(f"  WARNING: couldn't remove {png.name}: {pe}")

    if rec_list:
        for r, pth in extract_all_textures(name, out_dir, clean_dir):
            mark_extracted(root, pth); _drop_png(pth); n += 1
    else:
        out = out_dir / texture_filename(name, None)
        extract_dds(name, out, clean_dir)
        mark_extracted(root, out); _drop_png(out); n += 1
    log(f"  reverted {name} -> Extracted/{folder}/ ({n} file(s) from clean)")
    return n


# ── edit-file search across the new Extracted/ + legacy Modified//Original/ ──
def _find_edit_in_dir(folder_dir, name, rec=None):
    """The user's edit file for this texture inside ONE folder, or None (current + legacy names).
    PNG wins over DDS when both exist — the PNG is the file the user actually edits."""
    folder_dir = Path(folder_dir)
    base = folder_dir / texture_filename(name, rec)
    for cand in (base.with_suffix(".png"), base):
        if cand.exists():
            return cand
    if rec is not None and "index" in rec and not rec.get("label"):     # legacy t{idx}_{w}x{h}.*
        for ext in ("png", "dds"):
            g = sorted(folder_dir.glob(f"t{rec['index']:02d}_*.{ext}"))
            if g:
                return g[0]
    if rec is not None and rec.get("index") == 0 and rec.get("label"):  # decal sheet = old primary
        stem = name[:-4] if name.lower().endswith(".iff") else name
        hid = _player_head_id(name)                    # …and the flat Player_Heads/<id>.dds layout
        for nm2 in ((hid + ".png", hid + ".dds") if hid else ()):
            c = folder_dir / nm2
            if c.exists():
                return c
        for nm2 in (stem + ".png", stem + ".dds"):
            c = folder_dir / nm2
            if c.exists():
                return c
    if rec is None:                                    # legacy single-primary name (e.g. logo_bos.dds)
        stem = name[:-4] if name.lower().endswith(".iff") else name
        for nm2 in (stem + ".png", stem + ".dds"):
            c = folder_dir / nm2
            if c.exists():
                return c
    return None


def edit_dirs(root, name):
    """Candidate folders for an asset's edit file: the grouped Extracted/ layout first, then the
    legacy per-iff folders under Extracted/ // Modified/ // Original/ (so old edits keep applying)."""
    tex = Path(root) / "Textures"
    seen, out = set(), []
    # "Player_Heads" (flat, one file per head) was the layout before the 3-map split; keep it in the
    # search so a colour-only edit dropped there still applies.
    extra = ("Player_Heads",) if _player_head_id(name) else ()
    for b in ("Extracted", "Modified", "Original"):
        for fol in (asset_iff(name), _legacy_asset_iff(name), *extra):
            d = tex / b / fol
            if d not in seen:
                seen.add(d); out.append(d)
    return out


def find_any_edit(root, name, rec=None):
    """The user's edit file for this texture anywhere in the edit folders, or None. `root` =
    …/NHL2k10_Extracted_Files."""
    for d in edit_dirs(root, name):
        hit = _find_edit_in_dir(d, name, rec)
        if hit:
            return hit
    return None


# ── UI / logo customizer: friendly labels + curated categories ───────────────
# Maps an .iff -> (friendly label, grouped category) so the launcher's IFF Textures
# tab surfaces the brand / menu / logo art as a browsable "customizer" (Phase 1).
UI_LABELS = {
    "titlepage_nhl2k10_logo.iff": ("NHL 2K10 logo (title screen)",   "ui_logos"),
    "titlepage_2k_logo.iff":      ("2K Sports logo (title screen)",  "ui_logos"),
    "titlepage_cover.iff":        ("Cover / player art (title)",     "ui_logos"),
    "titlepage_eula_text.iff":    ("EULA / legal text (title)",      "ui_logos"),
    "titlepage_stats.iff":        ("STATS logo (title screen)",      "ui_logos"),
    "frontend.iff":               ("Menu UI sprites (front-end)",    "ui_menu"),
    "bootup.iff":                 ("Boot / splash screen",           "ui_logos"),
    "logos_large.iff":            ("Team logos - large set",         "team_logos"),
    "logos_medium.iff":           ("Team logos - medium set",        "team_logos"),
    "logos_small.iff":            ("Team logos - small set",         "team_logos"),
}


# ── catalog (browseable per-team asset index) ────────────────────────────────
def _goalie_label(iff: str) -> str:
    """Friendly label for a goalie mask/gear .iff (helmet_g01_pattern_01 -> 'Goalie mask g01 ·
    pattern 01'; blocker_g02_logos_l -> 'Goalie blocker g02 logos (L)')."""
    import re
    m = re.match(r"(helmet|pad|blocker|catcher)_g(\d+)(?:_pattern_(\d+))?(_logos(?:_[lr])?)?", iff)
    if not m:
        return iff
    part, shell, pat, logo = m.groups()
    nice = {"helmet": "mask", "pad": "pads", "blocker": "blocker", "catcher": "catcher"}
    lbl = f"Goalie {nice.get(part, part)} g{shell}"
    if pat:
        lbl += f" - pattern {pat}"
    if logo:
        side = " (L)" if logo.endswith("_l") else " (R)" if logo.endswith("_r") else ""
        lbl += f" logos{side}"
    return lbl


# Catalog rows for teams that exist in Roster.ROS but not in the shipped 30-team CSV (expansion
# teams — Seattle, Vegas). Derived from the roster at runtime by `extra_teams.catalog_rows` and
# installed here by the app, because the CSV is frozen shipping data and load_catalog has no idea
# where the user's roster lives. Empty on a stock install, so nothing changes for anyone else.
EXTRA_TEAM_ROWS: list = []


def set_extra_team_rows(rows):
    """Install (or clear, with []) the expansion-team catalog rows. Safe to call repeatedly — the
    list is replaced wholesale, so re-running discovery after a roster change can't duplicate rows."""
    global EXTRA_TEAM_ROWS
    EXTRA_TEAM_ROWS = list(rows or [])
    return len(EXTRA_TEAM_ROWS)


def load_catalog():
    """Return resolved catalog rows: [{team, category, iff, archive, offset, size, label}].
    A friendly `label` and curated `category` are added so UI/logo art (titlepage brand
    logos, front-end menu, per-team logos) is easy to find in the launcher's IFF tab."""
    rows = []
    if CATALOG_CSV.exists():
        for r in csv.DictReader(open(CATALOG_CSV, encoding="utf-8")):
            if r.get("resolved") == "1" and r.get("iff"):
                rows.append(r)
    if DISCOVERED_CSV.exists():                              # 1147 assets from the full-TOC sweep
        for r in csv.DictReader(open(DISCOVERED_CSV, encoding="utf-8")):
            if r.get("resolved") == "1" and r.get("iff"):
                rows.append(r)                              # keeps its own `label` (see below)
    rows.extend(EXTRA_TEAM_ROWS)                              # expansion teams (see set_extra_team_rows)
    for key, (iff, vo, w, h, fmt) in SCENE_ASSETS.items():    # front-end scene textures (separate rows)
        rows.append({"team": "frontend", "category": "titlepage", "iff": key,
                     "archive": "", "offset": "", "size": ""})
    for r in rows:                                            # friendly labels + UI/logo grouping
        iff = r.get("iff", "")
        lbl, cat = UI_LABELS.get(iff, (None, None))
        if lbl:
            r["label"] = lbl
            if cat:
                r["category"] = cat
        elif r.get("category") == "logo":                    # the 30 per-team logo_<code>.iff
            r["label"] = f"{(r.get('team') or '').upper()} team logo"
            r["category"] = "team_logos"
        elif r.get("category") == "ice":                     # ice_<code>_playoffs/finals.iff
            team = (r.get("team") or "").upper()
            # "playoffs" art is also the regular-season ice, so we surface it as "Regular".
            variant = "Finals" if "_finals" in iff else "Regular"
            r["label"] = f"{team} ice — {variant}"
        elif r.get("category") == "rink":                    # rink_<code>.iff
            r["label"] = f"{(r.get('team') or '').upper()} rink"
        elif r.get("category") == "zamboni_team":            # zamboni_team_<code>.iff (the textured one)
            r["label"] = f"{(r.get('team') or '').upper()} zamboni"
            r["category"] = "zamboni"                        # zamboni_<code> (untextured model) is hidden
        elif r.get("category") == "arena_presentation":      # arena_presentation_<code>.iff
            r["label"] = f"{(r.get('team') or '').upper()} arena presentation"
        elif r.get("category") == "arena":                   # arena_<code>.iff — the BOWL
            # Was catalogued as "arena_audio" and filtered out of the IFF tab entirely; it is
            # actually the arena bowl: 86 textures (seats, signage, banners, lights) in blob 1
            # plus the bowl geometry in blob 0. See doc 24 §6.
            r["label"] = f"{(r.get('team') or '').upper()} arena (bowl, seats, signage)"
        elif iff.startswith("uniform_frontend_"):             # the team-select jersey sheet
            slot = iff.rsplit("_", 1)[-1][:-4]                # …_home.iff -> home
            r["label"] = f"{(r.get('team') or '').upper()} front-end jersey — {slot.title()}"
        elif iff.startswith(("helmet_", "pad_", "blocker_", "catcher_")):
            r["label"] = _goalie_label(iff)                   # goalie masks + gear
        else:
            r["label"] = r.get("label") or iff                # keep a discovered-asset label if set
    return rows


def decode_preview(name: str, clean_dir: Path = None, clean: bool = True):
    """Decode the primary texture -> PIL RGBA for preview (no file written), or None.
    clean=True (default) reads the pristine .orig archives — the shipped design. clean=False reads
    the LIVE archives, so applied mods/repaints show (see decode_preview_current)."""
    if name in SCENE_ASSETS:
        return decode_preview_at(*SCENE_ASSETS[name][:5], clean_dir)
    # Pristine reads go through resolve_clean so an expansion team's assets — whose TOC entries
    # were spliced into the LIVE archives and so are absent from the `.orig` backups — preview at
    # all instead of silently returning None. `clean` is REASSIGNED from the fallback: the flag
    # decides which file is opened below, and reading a live offset out of a `.orig` yields
    # garbage rather than an error. clean=False callers (decode_preview_current) are untouched.
    if clean:
        loc, clean = resolve_clean(name, clean_dir)
    else:
        loc = resolve(name, clean_dir, clean=False)
    if not loc:
        return None
    arc, off, size, idx, f3 = loc
    with open(_arc_file(clean_dir, arc, clean=clean), "rb") as f:
        f.seek(off); data = f.read(size + 0x200000)
    blobs = _walk_blobs(data, size)
    vram, fetch = _find_primary(blobs)
    if not vram:
        return None
    fmt, bpu, block, w, h, tiled, mip0 = fetch
    vo = _primary_vram_off(blobs, len(vram["dec"]), mip0)
    try:
        return _to_straight(T.decode(_dxt_endian(vram["dec"][vo:vo + mip0], fmt), w, h, fmt, bpu, block, tiled, 0).convert("RGBA"), fmt)
    except Exception:
        return None


def _primary_vram_off(blobs, vlen, mip0):
    """Where the primary texture's mip0 starts inside the VRAM blob. Normally 0 (the record carries
    the +0x6c==1 placeholder and the loader places it), but a relocate-GROW replace APPENDS the new
    pixels and redirects the record's stored offset — so a repainted asset's texture is no longer at
    the head of the blob. Reading it as 0 shows a band of garbage over the old data."""
    dram = next((b["dec"] for b in blobs if b["dec"] and len(b["dec"]) == 0xE0), None)
    if dram is None:
        return 0
    v = _BE(dram, 0x6C)
    if v <= 1:                                        # placeholder -> loader-placed at the head
        return 0
    vo = v - 1
    return vo if 0 < vo and vo + mip0 <= vlen else 0


def decode_preview_current(name: str, arc_dir: Path = None):
    """Decode the primary texture from the CURRENT game files (mods/repaints applied) -> PIL RGBA,
    or None. Same idea as decode_portrait_current: no .orig preference, so what you see is what the
    game will load. Falls back to nothing — callers decide whether to show the clean design."""
    return decode_preview(name, arc_dir, clean=False)


def asset_stamp(name: str, arc_dir: Path = None):
    """Cheap cache key for an asset's CURRENT bytes: (archive path, mtime_ns, offset, size). Changes
    whenever a replace/repaint could have altered the asset. None if it doesn't resolve."""
    loc = resolve(name, arc_dir)
    if not loc:
        return None
    arc, off, size, _idx, _f3 = loc
    p = _arc_file(arc_dir, arc)
    try:
        return (str(p), p.stat().st_mtime_ns, off, size)
    except OSError:
        return None


def primary_fetch(name: str, clean_dir: Path = None):
    """(fmt, w, h) of the primary texture — for the sub-list format/size of a single
    asset. No full pixel decode (cheap). Returns (None, None, None) if not found."""
    if name in SCENE_ASSETS:
        iff, vo, w, h, fmt = SCENE_ASSETS[name]
        return fmt, w, h
    loc, _cl = resolve_clean(name, clean_dir)
    if not loc:
        return None, None, None
    arc, off, size, idx, f3 = loc
    with open(_arc_file(clean_dir, arc, clean=_cl), "rb") as f:
        f.seek(off); data = f.read(size + 0x200000)
    vram, fetch = _find_primary(_walk_blobs(data, size))
    if not fetch:
        return None, None, None
    fmt, bpu, block, w, h, tiled, mip0 = fetch
    return fmt, w, h


# ── TOC ──────────────────────────────────────────────────────────────────────
def load_toc(arc_dir: Path, clean=False):
    """clean=True reads the PRISTINE toc (0A.orig). Never pass clean=True on a write path: the
    writers need the CURRENT toc, whose entries move when data is relocated."""
    f0a = _arc_file(arc_dir, "0A", clean)
    # entry count is stored at 0x10 (NOT hardcoded 2407) so custom-added assets resolve
    d0a = open(f0a, "rb").read(0x9800)
    count = _BE(d0a, 0x10)
    ebase = entry_base(d0a)
    need = ebase + count * 16
    if need > len(d0a):
        d0a = open(f0a, "rb").read(need)
    bounds = []; cum = 0
    for arc, sz in arc_table(d0a):                 # names + sizes come from the header, not ARCS
        bounds.append((cum, cum + sz, arc)); cum += sz
    toc = {}
    for i in range(count):
        _fl, s, f2, f3 = struct.unpack_from(">4I", d0a, ebase + i * 16)
        toc[f2] = (i, s, f3)
    return toc, bounds


_CRC_ALIAS = None


def _crc_alias():
    """{SYNTHETIC_NAME.IFF -> real TOC crc32} for discovered assets whose real filename is unknown
    (listed under a `disc_<crc>` pseudo-name). Lets resolve() find them by their pseudo-name even
    though crc32(pseudo-name) != the real hash. Real-named discovered assets need no alias."""
    global _CRC_ALIAS
    if _CRC_ALIAS is None:
        _CRC_ALIAS = {}
        try:
            for r in csv.DictReader(open(DISCOVERED_CSV, encoding="utf-8")):
                crc = r.get("crc32", "").strip()
                if not crc:
                    continue
                c = int(crc, 16)
                if (zlib.crc32(r["iff"].upper().encode("ascii")) & 0xffffffff) != c:   # synthetic
                    _CRC_ALIAS[r["iff"].upper()] = c
        except FileNotFoundError:
            pass
    return _CRC_ALIAS


def resolve(name: str, arc_dir: Path, clean=False):
    """name -> (archive, local_off, size, toc_index, f3). clean=True resolves against the pristine
    0A.orig toc (original bytes / revert); the default resolves the live archives."""
    toc, bounds = load_toc(arc_dir, clean)
    h = _crc_alias().get(name.upper())
    if h is None:
        h = zlib.crc32(name.upper().encode("ascii")) & 0xffffffff
    if h not in toc:
        return None
    idx, sz, f3 = toc[h]; co = f3 * 0x800
    for lo, hi, arc in bounds:
        if lo <= co < hi:
            return (arc, co - lo, sz, idx, f3)
    return None


def resolve_clean(name: str, arc_dir: Path):
    """`(loc, clean)` for a PRISTINE read — resolve against `<arc>.orig`, falling back to the LIVE
    archive for assets the shipped game never had.

    An expansion team's assets (Seattle, Vegas — see `extra_teams`) exist only because their TOC
    entries were spliced into the live archives; the `.orig` backups predate them, so a plain
    `resolve(..., clean=True)` returns None and every pristine reader — extract, preview, the
    sub-texture list — fails with "not found in TOC". For an asset with no shipped version the
    live bytes ARE its pristine bytes, so falling back is not a compromise, it's the definition.

    The returned `clean` flag MUST be passed straight back to `_arc_file`: a location is only
    meaningful against the archive it was resolved in, and reading a live offset out of a `.orig`
    file would silently yield garbage rather than an error.
    """
    loc = resolve(name, arc_dir, clean=True)
    if loc:
        return loc, True
    return resolve(name, arc_dir, clean=False), False


# ── blob / fetch parsing ─────────────────────────────────────────────────────
def _walk_blobs(data: bytes, size: int):
    """Sequential 0x0E4837 blob walk (avoids false magic hits inside compressed data)."""
    blobs = []
    o = data.find(MAGIC)                              # skip any tiny resource header
    if o < 0:
        return blobs
    while o + 20 <= len(data) and o < size:
        if data[o:o + 4] != MAGIC:
            break
        dec_sz = _BE(data, o + 4); tot = _BE(data, o + 8)
        codec = _BE(data, o + 12); wp = _BE(data, o + 16)
        if tot < 20 or o + tot > len(data) + 4:
            break
        try:
            dec = DF.decompress_codec(data[o + 20:o + tot], dec_sz, (1 << wp) - 1, wp)
        except Exception:
            dec = None
        blobs.append({"off": o, "tot": tot, "dec": dec, "dec_sz": dec_sz,
                      "wp": wp, "codec": codec})
        o += tot
    return blobs


_FETCH_FMT = set(T.FMT.keys())


def _valid_fetch(dram, o, vlen):
    """Validate a Xenos texture fetch constant at offset o. -> 8-tuple or None."""
    if o + 12 > len(dram):
        return None
    d0, d1, d2 = struct.unpack_from(">III", dram, o); fmt = d1 & 0x3F
    if (d0 & 3) != 2 or fmt not in _FETCH_FMT:         # type bits 2 = texture
        return None
    w = (d2 & 0x1FFF) + 1; h = ((d2 >> 13) & 0x1FFF) + 1
    if not (4 <= w <= 4096 and 4 <= h <= 4096) or w % 4 or h % 4:
        return None
    name, bpu, block = T.FMT[fmt]
    mip0 = (w // 4) * (h // 4) * bpu if block else w * h * bpu
    if mip0 == 0 or mip0 > vlen:                       # mip0 must fit VRAM
        return None
    return (fmt, name, bpu, block, w, h, (d0 >> 31) & 1, mip0)


def _find_fetch(dram, vlen):
    if len(dram) <= 0x2000:                            # small per-texture descriptor
        return _valid_fetch(dram, 0x94, vlen)
    cands = [c for o in range(0, len(dram) - 12, 4) if (c := _valid_fetch(dram, o, vlen))]
    if not cands:
        return None
    p2 = lambda n: n > 0 and (n & (n - 1)) == 0
    cands.sort(key=lambda c: (p2(c[4]) and p2(c[5]), c[7]), reverse=True)  # pow2 dims, largest mip0
    return cands[0]


def _find_primary(blobs):
    """Return (vram_blob, fetch) for the primary texture, or (None, None).
    fetch = (fmt_name, bpu, block, w, h, tiled, mip0)."""
    pend = None
    for i, b in enumerate(blobs):
        dec = b["dec"]
        if dec is None:
            continue
        if len(dec) <= 0x2000:                         # descriptor
            nxt = next((bb["dec"] for bb in blobs[i + 1:] if bb["dec"] and len(bb["dec"]) > 0x2000), None)
            if nxt is not None:
                fc = _find_fetch(dec, len(nxt))
                if fc:
                    pend = fc
        elif pend:                                     # VRAM following a good descriptor
            fmt, name, bpu, block, w, h, tiled, mip0 = pend
            return b, (name, bpu, block, w, h, tiled, mip0)
    bigs = [b for b in blobs if b["dec"] and len(b["dec"]) > 0x2000]
    if len(bigs) >= 2:
        fc = _find_fetch(bigs[0]["dec"], len(bigs[1]["dec"]))
        if fc:
            fmt, name, bpu, block, w, h, tiled, mip0 = fc
            return bigs[1], (name, bpu, block, w, h, tiled, mip0)
    return None, None


# ── multi-texture resource tree (count@0x20, texArray@0x24, 0xE0 records) ─────
def _read_asset(name, clean_dir, live=False):
    """Raw asset bytes. Pristine (`.orig`) by default; `live=True` reads the CURRENT archives so an
    already-applied repaint reads back as what the game will load — the Jersey Editor needs that,
    because loading a kit is supposed to show the kit as it is now, not as it shipped."""
    if live:
        loc, _cl = resolve(name, clean_dir, clean=False), False
    else:
        loc, _cl = resolve_clean(name, clean_dir)
    if not loc:
        return None, None, None
    arc, off, size, idx, f3 = loc
    with open(_arc_file(clean_dir, arc, clean=_cl), "rb") as f:
        f.seek(off); data = f.read(size + 0x400000)
    return loc, data, size


_TREE_CACHE = OrderedDict()              # (name, clean_dir) -> (vram, recs); tiny LRU (blobs big)
_TREE_CACHE_MAX = 3


def _load_tree(name, clean_dir, live=False):
    """Cached _texture_tree(read_asset(name)) -> (vram, recs). Decompressing a big VRAM blob is
    slow (global.iff = 67 MB, ~16 s), so previewing/extracting an asset's many sub-textures must
    NOT re-read+re-decompress per texture. CLEAN is read-only, so the cache never goes stale — a
    LIVE read can, so its key carries asset_stamp (archive mtime + offset + size) and an Apply
    therefore misses the cache instead of serving pre-Apply pixels."""
    key = (name, str(_dir(clean_dir)), asset_stamp(name, clean_dir) if live else None)
    hit = _TREE_CACHE.get(key)
    if hit is not None:
        _TREE_CACHE.move_to_end(key)
        return hit
    loc, data, size = _read_asset(name, clean_dir, live)
    res = _texture_tree(data, size) if loc is not None else (None, [])
    if live and res[0] is not None and not res[1]:
        # An edited contiguous pack no longer fills its blob exactly (see _relink_contiguous_live).
        clean_recs = _load_tree(name, clean_dir)[1]
        if clean_recs and all("rec_off" in r for r in clean_recs):
            blobs = [b["dec"] for b in _walk_blobs(data, size) if b["dec"]]
            if blobs:
                res = (res[0], _relink_contiguous_live(clean_recs, blobs[0], res[0]))
    _TREE_CACHE[key] = res
    while len(_TREE_CACHE) > _TREE_CACHE_MAX:
        _TREE_CACHE.popitem(last=False)
    return res


# ── contiguous multi-texture layouts (offsets assigned at LOAD, not stored) ──
# Some multi-texture assets (uniforms, …) have count@0x20==0: the textures are packed
# contiguously in the VRAM blob and the loader assigns each offset from a running sum of
# per-record footprints (mip0@+0x70 + mip-tail@+0x74). The in-VRAM ORDER is a build-time
# permutation that is NOT derivable from the file, so it was captured from the running game
# via Cheat Engine (resolved texture fetch-constant base addresses). Keyed by the DRAM-order
# record signature (w,h,fmt,has_fetch); value = (vram_order = DRAM indices in VRAM order,
# labels). Validated only when the cumulative footprints fill the VRAM blob EXACTLY.
_MULTI_LAYOUTS = {
    # uniform_<team>_home/away.iff — 6 textures, byte-identical structure across all 30 teams.
    # slot order (live-confirmed): color, normal(embroidery), numbers, detail, detail2, letters.
    ((2048, 512, "DXN", True), (2048, 512, "4444", True), (1024, 256, "4444", True),
     (1024, 1024, "DXT4_5", True), (3968, 256, "DXT4_5", True), (3968, 256, "DXN", True)):
        ([1, 0, 2, 4, 5, 3], ["stamps", "normal", "helmet", "letters", "letters_normal", "crowd"]),
    # uniform_base_<team>_<kit>.iff — 3 contiguous textures (records at DRAM ~0x154E0, past the old
    # scan window). The three footprints are all different sizes, so the cumulative-fill order is
    # FULLY determined (no build-time permutation to guess): base fabric color, a detail layer, and
    # the fabric/stitching NORMAL — the ONLY DXN, so its label is unambiguous. This is the "no normal
    # in the iff, but there is in the game" stitching: it was here all along, just never enumerated
    # (the pack showed as primary-only = the base color). Replaced IN-PLACE only (see replace_many).
    ((1024, 1024, "565", True), (512, 512, "565", True), (1024, 1024, "DXN", True)):
        ([0, 1, 2], ["base", "detail", "base_normal"]),
    # Same pack with the base colour stored as DXT4_5 instead of 565. 565 is uncompressed 2 B/px,
    # so a DETAILED base layer (stock ones are near-flat — ANA ships 23 distinct 565 values in a
    # megabyte) blows the pack past the size every shipped uniform_base stays under and the game
    # hangs on load. DXT4_5 is 1 B/px and block-compressed: same 1024x1024 footprint as the DXN
    # record beside it (mip0 1,048,576 + tail 393,216), so the contiguous cumulative-fill order is
    # still fully determined. Order and labels are unchanged — only rec0's storage format differs.
    ((1024, 1024, "DXT4_5", True), (512, 512, "565", True), (1024, 1024, "DXN", True)):
        ([0, 1, 2], ["base", "detail", "base_normal"]),
}


def _infer_multi_fmt(m0, w, h):
    """Format of a contiguous record with no embedded fetch, inferred from mip0 size."""
    if m0 == (w // 4) * (h // 4) * 16: return "DXT4_5", 16, 1
    if m0 == (w // 4) * (h // 4) * 8:  return "DXT1", 8, 1
    if m0 == w * h * 4:                return "8888", 4, 0
    if m0 == w * h * 2:                return "4444", 2, 0
    if m0 == w * h:                    return "8", 1, 0
    return None


def _parse_multi_rec(dram, base):
    """One 0xE0 contiguous record -> dict (or None). fmt from embedded fetch when present,
    else inferred from mip0; footprint = mip0(+0x70) + mip-tail(+0x74)."""
    if base + 0xE0 > len(dram):
        return None
    w = struct.unpack_from(">H", dram, base + 0x60)[0]
    h = struct.unpack_from(">H", dram, base + 0x62)[0]
    if not (4 <= w <= 8192 and 4 <= h <= 8192):
        return None
    m0 = _BE(dram, base + 0x70); tl = _BE(dram, base + 0x74)
    if m0 <= 0 or tl < 0:
        return None
    fid = None; tiled = 1
    for o in range(base, base + 0xE0 - 12, 4):
        d0, d1, d2 = struct.unpack_from(">III", dram, o)
        if (d0 & 3) == 2 and (d2 & 0x1FFF) + 1 == w and ((d2 >> 13) & 0x1FFF) + 1 == h \
           and (d1 & 0x3F) in _FETCH_FMT:
            fid = d1 & 0x3F; tiled = (d0 >> 31) & 1; break
    if fid is not None:
        nm, bpu, blk = T.FMT[fid]
        if m0 != ((w // 4) * (h // 4) * bpu if blk else w * h * bpu):
            return None
        fetch = True
    else:
        inf = _infer_multi_fmt(m0, w, h)
        if inf is None:
            return None
        nm, bpu, blk = inf; fetch = False
    return {"w": w, "h": h, "fmt": nm, "bpu": bpu, "block": blk, "tiled": tiled,
            "mip0": m0, "tail": tl, "foot": m0 + tl, "fetch": fetch}


def _contiguous_records(dram, vram):
    """Enumerate a contiguously-packed multi-texture asset (see _MULTI_LAYOUTS).
    Returns records with cumulative vram_off in the live-confirmed order, or [] when the
    structure is unknown / doesn't fill the VRAM blob exactly (stays safe = primary-only)."""
    n = len(vram)
    # Scan window widened past 0x400: some contiguous packs put their record array deep in the DRAM
    # (uniform_base_*.iff at ~0x154E0). Still double-gated below — a run is only accepted when it fills
    # the VRAM blob EXACTLY *and* its signature is a registered _MULTI_LAYOUTS entry — so a wider scan
    # can't produce a false match. Capped for speed (base records are well under this).
    for start in range(0, min(len(dram), 0x18000), 4):
        recs = []; total = 0; b = start
        while True:
            r = _parse_multi_rec(dram, b)
            if r is None:
                break
            recs.append(r); total += r["foot"]; b += 0xE0
            if total > n:
                break
        if len(recs) < 2 or total != n:
            continue
        sig = tuple((r["w"], r["h"], r["fmt"], r["fetch"]) for r in recs)
        layout = _MULTI_LAYOUTS.get(sig)
        if layout is None:
            continue                    # exact fill but not a known layout — keep scanning, stay safe
        order, labels = layout
        out = []; off = 0
        for slot, ri in enumerate(order):
            r = dict(recs[ri], index=slot, vram_off=off, label=labels[slot],
                     rec_off=start + ri * 0xE0)
            off += r["foot"]; out.append(r)
        return out
    return []


def _relink_contiguous_live(clean_recs, dram, vram):
    """Re-point a contiguous pack's PRISTINE records at the LIVE blob.

    `_contiguous_records` only accepts a run whose footprints fill the VRAM blob exactly, and a
    grow-replace breaks that: it APPENDS the new pixels and redirects one record's stored offset
    (+0x6c), so the blob is longer than the sum of the records and enumeration comes back empty —
    which is why an edited kit read back as nothing at all. Since a grow only ever appends, the
    head of the blob still holds the original packing: keep the pristine offsets, and take the
    offset/size/format of any record the writer redirected from the LIVE record. Returns [] if the
    live records don't line up, so an unrecognised layout degrades to primary-only as before.
    """
    out = []
    for cr in clean_recs:
        b = cr.get("rec_off")
        if b is None:
            return []
        lr = _parse_multi_rec(dram, b)
        if lr is None or (lr["w"], lr["h"]) != (cr["w"], cr["h"]):
            return []
        v = _BE(dram, b + 0x6C)
        vo = (v - 1) if v > 1 else cr["vram_off"]
        if vo < 0 or vo + lr["mip0"] > len(vram):
            return []
        out.append(dict(cr, **lr, vram_off=vo))
    return out


def _extra_fetch_records(dram, vram, have):
    """Every texture whose 0xE0 fetch record sits OUTSIDE the formal resource-tree (@0x20/0x24)
    — e.g. overlay_static.iff is a master HUD file whose tree lists 18 but ~21 textures exist,
    the rest bound per-overlay. Scan all 0xE0 records, keep those at a vram_off not already in
    `have` (mutated). Marked packing="scatter" (decode/extract + in-place DXT only, never a free
    relocate) so a stored-offset texture can't be corrupted. Sorted by vram_off."""
    out = []; b = 0
    while b + 0xE0 <= len(dram):
        w = struct.unpack_from(">H", dram, b + 0x60)[0]
        h = struct.unpack_from(">H", dram, b + 0x62)[0]
        vv = _BE(dram, b + 0x6C); flags = _BE(dram, b + 0x5C)
        if 8 <= w <= 4096 and 8 <= h <= 4096 and vv > 1:
            fid = None; tiled = 0
            for o in range(b, b + 0xE0 - 12, 4):
                d0, d1, d2 = struct.unpack_from(">III", dram, o)
                if (d0 & 3) == 2 and (d2 & 0x1FFF) + 1 == w and \
                   ((d2 >> 13) & 0x1FFF) + 1 == h and (d1 & 0x3F) in _FETCH_FMT:
                    fid = d1 & 0x3F; tiled = (d0 >> 31) & 1; break
            if fid is not None:
                align = 0x10 if (flags & 0xC0000000) == 0xC0000000 else 0x1000
                voff = ((vv - 1) // align) * align
                nm, bpu, blk = T.FMT[fid]
                mip0 = (w // 4) * (h // 4) * bpu if blk else w * h * bpu
                if voff + mip0 <= len(vram) and voff not in have:
                    tail = _BE(dram, b + 0x74)
                    out.append({"w": w, "h": h, "fmt": nm, "bpu": bpu, "block": blk,
                                "tiled": tiled, "vram_off": voff, "mip0": mip0, "tail": tail,
                                "foot": mip0 + tail, "packing": "scatter", "rec_off": b})
                    have.add(voff)
                b += 0xE0; continue
        b += 4
    out.sort(key=lambda r: r["vram_off"])
    return out


def _scatter_records(dram, vram):
    """For multi-texture assets the count@0x20 resource-tree UNDER-counts (arena
    presentation): the texture records aren't all in that one array, but every texture has
    a 0xE0 record (with an embedded fetch) somewhere in the DRAM, and the textures are
    packed in DRAM-base order. Scan for all such records, order by base, assign cumulative
    offsets. Accept ONLY when footprints fill the VRAM blob EXACTLY and every record that
    carries a stored offset (@+0x6c) lands at its cumulative position — self-validation, so
    a wrong layout can never produce a (corrupting) replace target. Returns [] otherwise."""
    n = len(vram); found = {}; b = 0
    while b + 0xE0 <= len(dram):
        r = _parse_multi_rec(dram, b)
        if r is not None and r["fetch"]:
            found[b] = r; b += 0xE0
        else:
            b += 4
    if len(found) < 2:
        return []
    out = []; off = 0
    for base in sorted(found):
        out.append(dict(found[base], base=base, vram_off=off)); off += found[base]["foot"]
    if off != n:
        return []
    # Validate. These assets are several sub-packages concatenated in one VRAM blob; each
    # record's stored offset (@+0x6c) is RELATIVE to its sub-package base, and a sub-package
    # starts where that offset resets to 0. Require every record's (cumulative - group_base)
    # to equal its stored relative offset -> proves DRAM-base order == the true packing
    # (catches a wrong order, since the stored offsets would then disagree).
    group_base = 0; anchors = 0
    for r in out:
        vv = _BE(dram, r["base"] + 0x6C)
        flags = _BE(dram, r["base"] + 0x5C)
        rel = 0 if vv <= 1 else ((vv - 1) // (0x10 if (flags & 0xC0000000) == 0xC0000000 else 0x1000)) \
                                * (0x10 if (flags & 0xC0000000) == 0xC0000000 else 0x1000)
        if rel == 0:
            group_base = r["vram_off"]
        else:
            anchors += 1
        if r["vram_off"] - group_base != rel:
            return []
    if anchors == 0:        # no real stored offsets -> order is unverifiable (e.g. uniforms,
        return []           # whose VRAM order is a permutation) -> stay safe, don't guess
    for i, r in enumerate(out):
        # `packing="scatter"` = several sub-packages concatenated, each record's +0x6c is
        # GROUP-RELATIVE (not the cumulative blob offset). These decode fine, but the blob offset
        # can't be freely redirected, so they are NOT safe to grow / convert to 8888 -> in-place
        # DXT only (like the game shipped them). rec_off keeps the DRAM record position.
        r["index"] = i; r["packing"] = "scatter"; r["rec_off"] = r.pop("base", None)
    return out


def _stored_offset_records(blobs, dram):
    """Contiguous multi-texture packs whose sub-texture VRAM offsets ARE stored in the file
    (HUD / overlay / per-screen UI packages, e.g. overlay_static.iff, global.iff): count@0x20
    is 0 so the resource-tree parse comes up empty, but — unlike the uniforms (a build-time
    permutation) — every 0xE0 record here carries an embedded fetch constant AND its resolved
    VRAM offset @+0x6C. Enumerate those records, resolve each offset, and pick the texture blob
    = smallest decompressed blob (other than the records/dram blob) big enough to hold them
    (the records blob is usually the LARGER one, so max-by-len picks wrong — that was the bug).
    Self-validating: needs >=2 fetch records, all in-bounds, none overlapping. Read-only path
    (preview/extract); returns (vram_blob, [records]) or (None, [])."""
    recs = []; b = 0; n = len(dram)
    while b + 0xE0 <= n:
        r = _parse_multi_rec(dram, b)
        if r is not None and r["fetch"]:
            vv = _BE(dram, b + 0x6C); flags = _BE(dram, b + 0x5C)
            align = 0x10 if (flags & 0xC0000000) == 0xC0000000 else 0x1000
            r["vram_off"] = ((vv - 1) // align) * align if vv > 1 else 0
            recs.append(r); b += 0xE0
        else:
            b += 4
    if len(recs) < 2:
        return None, []
    extent = max(r["vram_off"] + r["mip0"] for r in recs)
    cand = [bl for bl in blobs if bl is not dram and len(bl) >= extent] \
        or [bl for bl in blobs if len(bl) >= extent]
    if not cand:
        return None, []
    vram = min(cand, key=len)
    spans = sorted((r["vram_off"], r["vram_off"] + r["mip0"]) for r in recs)
    for j in range(1, len(spans)):
        if spans[j][0] < spans[j - 1][1]:      # overlapping textures -> layout wrong -> reject
            return None, []
    for i, r in enumerate(recs):
        r["index"] = i
    return vram, recs


def _texture_tree(data: bytes, size: int):
    """Parse the serialized DRAM resource tree of a multi-texture asset.
    -> (vram_dec, [records]); record = {index,w,h,fmt,bpu,block,tiled,vram_off,mip0}.
    Empty list when there's no parseable tree (single/primary asset)."""
    blobs = [b["dec"] for b in _walk_blobs(data, size) if b["dec"]]
    if not blobs:
        return None, []
    dram = blobs[0]; vram = max(blobs, key=len); recs = []
    if len(dram) >= 0x28:
        count = _BE(dram, 0x20); V = _BE(dram, 0x24)
        if 0 < count <= 8192 and V != 0:
            arr = 0x24 + V - 1
            for i in range(count):
                base = arr + i * 0xE0
                if base + 0xE0 > len(dram):
                    break
                flags = _BE(dram, base + 0x5C)
                w = struct.unpack_from(">H", dram, base + 0x60)[0]
                h = struct.unpack_from(">H", dram, base + 0x62)[0]
                vv = _BE(dram, base + 0x6C)
                if not (8 <= w <= 4096 and 8 <= h <= 4096) or vv == 0:
                    continue
                fid = None; tiled = 0
                for o in range(base, base + 0xE0 - 12, 4):
                    d0, d1, d2 = struct.unpack_from(">III", dram, o)
                    if (d0 & 3) == 2 and (d2 & 0x1FFF) + 1 == w and \
                       ((d2 >> 13) & 0x1FFF) + 1 == h and (d1 & 0x3F) in _FETCH_FMT:
                        fid = d1 & 0x3F; tiled = (d0 >> 31) & 1; break
                if fid is None:
                    continue
                align = 0x10 if (flags & 0xC0000000) == 0xC0000000 else 0x1000
                voff = ((vv - 1) // align) * align
                nm, bpu, blk = T.FMT[fid]
                mip0 = (w // 4) * (h // 4) * bpu if blk else w * h * bpu
                if voff + mip0 > len(vram):
                    continue
                tail = _BE(dram, base + 0x74)
                recs.append({"index": i, "w": w, "h": h, "fmt": nm, "bpu": bpu,
                             "block": blk, "tiled": tiled, "vram_off": voff, "mip0": mip0,
                             "tail": tail, "foot": mip0 + tail})
    if not recs:                       # count@0x20==0 -> contiguously-packed layout (uniforms…)
        recs = _contiguous_records(dram, vram)
    if not recs:                       # stored-offset packs (HUD/overlay/per-screen UI)
        v2, srecs = _stored_offset_records(blobs, dram)
        if srecs:                      # append any fetch texture this stricter pass skipped
            have = {r["vram_off"] for r in srecs}
            for r in _extra_fetch_records(dram, v2, have):
                r["index"] = len(srecs); srecs.append(r)
            return v2, srecs
    # resource-tree under-counts (arena presentation): if the records don't cover the whole
    # VRAM blob, scan for the rest and take it only if it self-validates (exact fill).
    covered = sum(r.get("foot", r["mip0"]) for r in recs)
    if len(recs) < 8 and covered < len(vram):   # small tree, blob not covered -> under-counted
        scatter = _scatter_records(dram, vram)
        if len(scatter) > len(recs):
            recs = scatter
    # Formal tree can also UNDER-count a big master file (overlay_static: tree=18 but ~21 exist,
    # the rest bound per-overlay). Append any fetch-record texture the tree missed, keeping the
    # tree's indices stable so existing per-index edits never shift.
    if recs:
        have = {r["vram_off"] for r in recs}
        for r in _extra_fetch_records(dram, vram, have):
            r["index"] = len(recs); recs.append(r)
    return vram, recs


_LIVE_CACHE = None


def _live_catalog():
    """Lazy-load the live-capture catalog (list of {iff,file_offset,w,h,fmt,...}).

    Two sources, merged: the BUNDLED catalog shipped in launcher/data/live_offsets.json (the
    offsets we captured — install-independent because they're content-matched into the retail
    archives, which are byte-identical on every disc) PLUS the user's own LOCAL captures at
    <game>/live_capture/live_offsets.json. Local entries win on (iff,file_offset) collisions, so
    a user can extend coverage (new arenas/screens) without losing the shipped data."""
    global _LIVE_CACHE
    if _LIVE_CACHE is None:
        merged = {}
        for src in (LIVE_CATALOG_BUNDLED, LIVE_CATALOG):   # bundled first, local overrides/extends
            try:
                for e in json.loads(Path(src).read_text()):
                    merged[(e["iff"], e["file_offset"])] = e
            except Exception:
                pass
        _LIVE_CACHE = list(merged.values())
    return _LIVE_CACHE


def reload_live_catalog():
    """Drop the cache so a fresh live_capture run is picked up without restart."""
    global _LIVE_CACHE
    _LIVE_CACHE = None


# Runtime record→file-offset map for sequential loader-placed packs (global.iff). The file stores
# +0x6c = 1 placeholders, so a texture's TRUE file position + its DRAM RECORD can't be derived from
# the file — they were captured from the running game (each record's resolved +0x6c minus the VRAM
# base; see the Ghidra Tex_BindVramPointers trace). This map (base, file_off, dims, fmt) makes
# global.iff browse/extract/replace RELIABLE (the old live-capture catalog content-matched de-duped
# runtime textures back to the file, which gave degenerate offsets on blank/repeating textures).
GLOBAL_MAP_CSV = R.data_path("global_iff_runtime_map.csv")
_RUNTIME_MAPS = {"global.iff": GLOBAL_MAP_CSV}
_FMT_BPU_BLOCK = {"DXT1": (8, 1), "DXT4_5": (16, 1), "DXT5": (16, 1), "DXN": (16, 1), "DXT5A": (8, 1),
                  "8": (1, 0), "565": (2, 0), "8888": (4, 0), "8_8": (2, 0), "4444": (2, 0), "1555": (2, 0)}
_runtime_map_cache = {}


def _runtime_map(name: str):
    """[records] from the captured runtime map for `name` (sorted by true file_off = placement order),
    or None if there's no map. Each record carries `rec_base` (its DRAM record position, so the convert
    can rewrite the right record) and `vram_off` = the TRUE file offset (so decode/splice hit real data)."""
    if name not in _RUNTIME_MAPS:
        return None
    if name in _runtime_map_cache:
        return _runtime_map_cache[name]
    p = _RUNTIME_MAPS[name]; out = []
    if p.exists():
        for r in csv.DictReader(open(p, encoding="utf-8")):
            fmt = r["fmt"]; bpu, block = _FMT_BPU_BLOCK.get(fmt, (16, 1))
            m0 = int(r["mip0"]); ft = int(r["foot"])
            out.append({"rec_base": int(r["base"]), "vram_off": int(r["file_off"]), "w": int(r["w"]),
                        "h": int(r["h"]), "fmt": fmt, "bpu": bpu, "block": block, "tiled": 1,
                        "mip0": m0, "tail": ft - m0, "foot": ft})
        out.sort(key=lambda e: e["vram_off"])
    _runtime_map_cache[name] = out or None
    return _runtime_map_cache[name]


# ── PORTRAIT PACKS ───────────────────────────────────────────────────────────
# disc_b9610aac.iff is the player-portrait PIXEL store (indexed by portrait.iff's 1478 entries):
# a flat stream of 0x0E4837 blobs, one per player, each 0x20000 decompressed = a 256x256 DXT4_5
# portrait (mip0 @ blob offset 0) + its mip chain (@0x10000). Blobs are self-delimiting (+0x08 =
# total size), so a portrait is a discrete unit — replace one by re-encoding + recompressing its
# blob in place. (Every earlier search missed this pack for being over a 60MB size cap.) A friendly
# display name is mapped for the UI.
PORTRAIT_PACKS = {"disc_b9610aac.iff": "player_portraits"}
PORTRAIT_PACK_NAME = "disc_b9610aac.iff"
PORTRAIT_COUNT = 1478                                 # portraits in the pack (fixed by portrait.iff)
_PORTRAIT_MIP0 = 0x10000                              # 256x256 DXT4_5 mip0 bytes
# Portraits are engine-locked to 256x256 DXT4_5 (the loader hardcodes format/size — 512x512 overflows
# the fixed 0x20000 VRAM slot, and 4444/8888 mis-tile as a loader-placed pack; both verified in-game).
# Edge quality (diagnosed 2026-07-10 by SSE-matching an in-game capture against every mip level):
# the player-card screens render the MIP CHAIN (LOD ~1: mip1 128x128 + trilinear into mip2 64x64),
# NOT mip0 — so mip-level edge quality is what shows in-game. The chunky grey/black fringe came from
# resizing the cut-out STRAIGHT (per-channel Lanczos): the matte colour (cut-out PNGs are white in
# their transparent area) bleeds into every edge pixel's RGB, and Lanczos ringing on the near-binary
# alpha sprays partial-alpha speckle outside the silhouette; each mip re-mixed more of both. Native
# portraits keep the smooth studio backdrop under a ~1.4px feathered mask at EVERY level, so they
# never fringe. Fix: _unmatte + premultiplied-space resize (matte can't contaminate), non-ringing
# alpha filter, then a small feather to match the measured native edge band (~1.4 partial px per
# boundary px at each level).
PORTRAIT_FEATHER = 0.35                               # Gaussian sigma applied to alpha per level


_PORTRAIT_PACK = None                                    # cached (data, size) for disc_b9610aac.iff
_PORTRAIT_OFFS = None                                    # cached portrait blob metadata (no decode)


def _portrait_pack(clean_dir=None):
    global _PORTRAIT_PACK
    if _PORTRAIT_PACK is None:
        loc, data, size = _read_asset("disc_b9610aac.iff", clean_dir)
        _PORTRAIT_PACK = (data, size) if loc is not None else (b"", 0)
    return _PORTRAIT_PACK


def _portrait_offsets(clean_dir=None):
    """Per-portrait blob metadata WITHOUT decoding (walks headers only, following each blob's total
    size) — so a single portrait can be decoded on demand in ~10ms instead of re-decoding all 1478
    (~12s). Order matches _portrait_blobs (dec_sz >= mip0, in walk order). Cached."""
    global _PORTRAIT_OFFS
    if _PORTRAIT_OFFS is not None:
        return _PORTRAIT_OFFS
    data, size = _portrait_pack(clean_dir)
    _PORTRAIT_OFFS = _walk_portrait_offsets(data, size)
    return _PORTRAIT_OFFS


def _walk_portrait_offsets(data, size):
    """Header-only walk of a portrait pack -> per-portrait blob metadata (see _portrait_offsets)."""
    offs = []
    o = data.find(MAGIC)
    while 0 <= o and o + 20 <= len(data) and o < size:
        if data[o:o + 4] != MAGIC:
            break
        dec_sz = _BE(data, o + 4); tot = _BE(data, o + 8)
        wp = _BE(data, o + 16); codec = _BE(data, o + 12)
        if tot < 20 or o + tot > len(data) + 4:
            break
        if dec_sz >= _PORTRAIT_MIP0:
            offs.append({"off": o, "tot": tot, "dec_sz": dec_sz, "wp": wp, "codec": codec})
        o += tot
    return offs


def portrait_records(name, clean_dir=None):
    """Tree-style records for a portrait pack — one 256x256 DXT4_5 texture per blob (its mip0)."""
    return [{"index": i, "w": 256, "h": 256, "fmt": "DXT4_5", "bpu": 16, "block": 1, "tiled": 1,
             "mip0": _PORTRAIT_MIP0, "tail": 0, "foot": _PORTRAIT_MIP0, "blob_off": b["off"]}
            for i, b in enumerate(_portrait_offsets(clean_dir))]


def decode_portrait(name, index, clean_dir=None):
    """Decode portrait `index` (its mip0) -> PIL RGBA, or None. Fast: decodes only that one blob."""
    offs = _portrait_offsets(clean_dir)
    if not (0 <= index < len(offs)):
        return None
    data, _size = _portrait_pack(clean_dir)
    return _decode_portrait_blob(data, offs[index])


def _decode_portrait_blob(data, b):
    try:
        dec = DF.decompress_codec(data[b["off"] + 20:b["off"] + b["tot"]],
                                  b["dec_sz"], (1 << b["wp"]) - 1, b["wp"])
        # _dxt_endian: portraits are fetched with 8-in-16 endian swap — decode what the GPU sees
        return T.decode(_dxt_endian(dec[:_PORTRAIT_MIP0]), 256, 256, "DXT4_5", 16, 1, 1, 0).convert("RGBA")
    except Exception:
        return None


_PORTRAIT_CUR = None                     # (cache_key, data, offs) for the CURRENT (modded) pack


def _portrait_pack_current():
    """(data, offs) for disc_b9610aac.iff read from the CURRENT game files — no .orig preference,
    so applied portrait mods are visible. Replacements splice blobs in place, so blob ORDER (and
    thus portrait indices / the key->blob map) matches the clean pack even after edits. Cached;
    invalidated when the archive file or the asset's TOC location changes."""
    global _PORTRAIT_CUR
    loc = resolve("disc_b9610aac.iff", None)
    if not loc:
        return b"", []
    arc, off, size, _idx, _f3 = loc
    p = _arc_file(None, arc)
    try:
        key = (str(p), p.stat().st_mtime_ns, off, size)
    except OSError:
        return b"", []
    if _PORTRAIT_CUR and _PORTRAIT_CUR[0] == key:
        return _PORTRAIT_CUR[1], _PORTRAIT_CUR[2]
    with open(p, "rb") as f:
        f.seek(off)
        data = f.read(size)
    offs = _walk_portrait_offsets(data, size)
    _PORTRAIT_CUR = (key, data, offs)
    return data, offs


def decode_portrait_current(index):
    """Decode portrait `index` from the CURRENT game files (mods applied) -> PIL RGBA, or None.
    Same index space as decode_portrait; different pixels wherever a portrait was replaced."""
    data, offs = _portrait_pack_current()
    if not (0 <= index < len(offs)):
        return None
    return _decode_portrait_blob(data, offs[index])


_PORTRAIT_KEY_MAP = None
# Precomputed {key: blob} — bundled at exe root when frozen, else project root. Built once (slow, it
# decodes the whole 66MB portrait pack) then cached to disk so the launcher loads it instantly.
PORTRAIT_KEY_JSON = R.data_path("portrait_key_map.json")


def portrait_key_blob_map(clean_dir=None):
    """{portrait_key -> blob_index} for every portrait. The game resolves a player's portrait by the
    u16 at player_record+0x1C ('key'): it loads the asset crc32('%04d_image' % key) (Str_Hash=CRC32,
    format '{0:d4}_image'), and that crc equals the u32 at the start of each portrait blob's 224-byte
    header chunk (the small 0E4837 blob immediately preceding each portrait in disc_b9610aac.iff).
    Reverse-engineered from Function_83D32188 / FUN_840a69e0. Cached (memory + on-disk JSON)."""
    global _PORTRAIT_KEY_MAP
    if _PORTRAIT_KEY_MAP is not None:
        return _PORTRAIT_KEY_MAP
    import json
    try:                                                     # fast path: bundled/precomputed JSON
        if PORTRAIT_KEY_JSON.exists():
            _PORTRAIT_KEY_MAP = {int(k): v for k, v in json.loads(PORTRAIT_KEY_JSON.read_text()).items()}
            return _PORTRAIT_KEY_MAP
    except Exception:
        pass
    import zlib
    loc, data, size = _read_asset("disc_b9610aac.iff", clean_dir)
    if loc is None:
        _PORTRAIT_KEY_MAP = {}
        return _PORTRAIT_KEY_MAP
    crc_to_key = {zlib.crc32(("%04d_image" % n).encode()) & 0xFFFFFFFF: n for n in range(10000)}
    key_blob = {}
    blob_i = 0
    prev_hash = None
    for b in _walk_blobs(data, size):
        dec = b.get("dec")
        if dec is None:
            continue
        if len(dec) >= _PORTRAIT_MIP0:                       # a portrait
            if prev_hash is not None:
                k = crc_to_key.get(prev_hash)
                if k is not None:
                    key_blob[k] = blob_i
            blob_i += 1
            prev_hash = None
        elif len(dec) >= 4:                                  # its 224-byte header chunk
            prev_hash = struct.unpack_from(">I", dec, 0)[0]
    try:
        PORTRAIT_KEY_JSON.write_text(json.dumps(key_blob))
    except Exception:
        pass
    _PORTRAIT_KEY_MAP = key_blob
    return key_blob

import numpy as np
from scipy.ndimage import distance_transform_edt

def _alpha_bleed_fast(rgb: np.ndarray, alpha: np.ndarray, threshold: int = 10) -> np.ndarray:
    """
    Fills transparent/semi-transparent pixels with the color of the nearest fully opaque pixel.
    Replaces iterative roll loops with a fast Exact Euclidean Distance Transform.
    """
    mask = alpha >= threshold  # Opaque/valid pixels
    
    # If image is entirely transparent or entirely opaque, return original RGB
    if not np.any(mask) or np.all(mask):
        return rgb

    # Find coordinates of the nearest opaque pixel for every pixel in the image
    indices = distance_transform_edt(~mask, return_distances=False, return_indices=True)
    
    # Map the nearest opaque colors to the transparent regions
    bled_rgb = rgb[indices[0], indices[1]]
    
    return bled_rgb

def _alpha_bleed(img, iters=16):
    """Dilate opaque RGB outward into the fully-transparent region (edge-bleed / alpha-fill). The
    alpha-weighted DXT5 encoder leaves GARBAGE RGB in all-transparent 4x4 blocks (no opaque pixel to
    fit the colour endpoints); once the cut-out alpha edge is softened/anti-aliased, that garbage
    becomes partly visible and speckles/fringes the hair line. Native portraits avoid it because their
    transparent area holds the smooth studio backdrop. Filling transparent RGB with the nearest opaque
    colour keeps the edge clean. Cheap: a few neighbour-dilation passes on 256x256."""
    arr = np.array(img.convert("RGBA"))
    rgb = arr[:, :, :3].astype(np.float32); a = arr[:, :, 3]
    filled = a > 16
    if filled.all() or not filled.any():
        return img
    rgb[~filled] = 0
    for _ in range(iters):
        if filled.all():
            break
        acc = np.zeros_like(rgb); cnt = np.zeros(a.shape, np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            sr = np.roll(np.roll(rgb, dy, 0), dx, 1)
            sf = np.roll(np.roll(filled, dy, 0), dx, 1)
            if dy == 1: sf[0, :] = False                 # kill np.roll wrap-around — otherwise
            elif dy == -1: sf[-1, :] = False             # border colours (white jersey hem)
            if dx == 1: sf[:, 0] = False                 # flood in from the OPPOSITE side
            elif dx == -1: sf[:, -1] = False
            sf = sf.astype(np.float32)
            acc += sr * sf[..., None]; cnt += sf
        newly = (~filled) & (cnt > 0)
        rgb[newly] = acc[newly] / cnt[newly][..., None]
        filled = filled | newly
    out = arr.copy(); out[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _unmatte(img):
    """Undo a flat colour matte on a cut-out source: exporters that composite onto a solid
    background (white/black) leave every partial-alpha edge pixel as true*a + matte*(1-a) —
    resizing/encoding then drags that matte colour into the visible edge (the in-game grey
    fringe). Detected by the fully-transparent region being one flat colour; a native-extracted
    portrait keeps its (non-uniform) studio backdrop there and passes through untouched."""
    a = np.asarray(img.convert("RGBA")).astype(np.float32)
    al = a[..., 3]
    tr = al == 0
    if tr.mean() < 0.05:
        return img
    m = a[tr][:, :3]
    c = m.mean(0)
    if np.abs(m - c).max() > 3.0:                     # not a flat matte (e.g. photo backdrop)
        return img
    part = (al > 0) & (al < 255)
    if not part.any():
        return img
    w = al[part][:, None] / 255.0
    a[part, :3] = np.clip((a[part, :3] - c[None, :] * (1 - w)) / np.maximum(w, 1.0 / 255), 0, 255)
    return Image.fromarray(a.astype(np.uint8), "RGBA")

def _premult_resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Resizes an RGBA image using premultiplied alpha downscaling.
    Prevents black border bleeding and DXT5 quantization noise on small UI mips.
    """
    if img.size == size:
        return img.copy()

    # Convert PIL Image to float32 NumPy array
    arr = np.array(img.convert("RGBA"), dtype=np.float32)

    # 1. Premultiply RGB channels by Alpha
    alpha_norm = arr[..., 3:4] / 255.0
    arr[..., :3] *= alpha_norm

    # 2. Resample in premultiplied space
    pm_img = Image.fromarray(arr.astype(np.uint8), mode="RGBA")
    resized_pm = pm_img.resize(size, Image.Resampling.LANCZOS)

    # 3. Un-premultiply back to Straight RGBA
    res_arr = np.array(resized_pm, dtype=np.float32)
    a_res = res_arr[..., 3:4]
    mask = a_res[..., 0] > 0

    # Avoid division by zero on fully transparent pixels
    res_arr[..., :3][mask] /= (a_res / 255.0)[mask]

    return Image.fromarray(np.clip(res_arr, 0, 255).astype(np.uint8), mode="RGBA")


def _feather_alpha(img, sigma):
    """Soften the cut-out mask a hair (alpha only). Native portrait masks carry ~1.4 partial px
    per boundary px at every mip level; a hard imported mask reads as stair-steps in-game."""
    if not sigma:
        return img
    from PIL import ImageFilter
    r, g, b, a = img.split()
    return Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.GaussianBlur(sigma))))


def _straight_resize(img, size):
    """Per-channel straight RGBA resize (float, no alpha special-casing). Pillow's own RGBA
    resize premultiplies internally, which ZEROES the transparent region's RGB — portraits must
    keep their synthetic backdrop colour there (native photos keep the studio backdrop)."""
    a = np.asarray(img.convert("RGBA")).astype(np.float32)
    ch = [np.asarray(Image.fromarray(a[..., c], "F").resize(size, Image.BILINEAR), np.float32)
          for c in range(4)]
    return Image.fromarray(np.clip(np.stack(ch, -1), 0, 255).astype(np.uint8), "RGBA")


def _smooth_surround(img, sigma=4.0):
    """Turn the transparent region's RGB into a SMOOTH synthetic backdrop (native portraits keep
    the real studio backdrop there). The nearest-opaque bleed leaves directional streak wedges
    with hard colour seams; the engine's runtime portrait repack re-encodes blocks without
    alpha-weighting, so streaky low-alpha blocks come back as grey chunk noise around the head
    (the in-game fringe). Locally-flat colour out there survives any re-encode/blend. Fills
    pixels the bleed never reached (mean colour), then blends in a heavy blur below the mask."""
    from PIL import ImageFilter
    a = np.asarray(img.convert("RGBA")).astype(np.float32)
    al = a[..., 3]
    out = al < 128
    if not out.any() or out.all():
        return img
    rgb = a[..., :3]
    unreached = out & (rgb.max(-1) < 1)                  # bleed reach limit -> still black
    if unreached.any():
        rgb[unreached] = rgb[~out].mean(0)
    blurred = np.asarray(Image.fromarray(a.astype(np.uint8), "RGBA")
                         .filter(ImageFilter.GaussianBlur(sigma))).astype(np.float32)[..., :3]
    w = np.clip((96.0 - al) / 96.0, 0.0, 1.0)[..., None]  # full blur at a=0, none above a=96
    a[..., :3] = rgb * (1 - w) + blurred * w
    return Image.fromarray(a.astype(np.uint8), "RGBA")


# byte-pair swap patterns per format: which STORED byte pairs differ from OUR encoders'/decoder's
# historical byte order under the Xenos 8-in-16 fetch. Our tools already used big-endian for the
# 565 colour-endpoint words (so those pairs are correct as-is); every OTHER field was written/read
# in little-endian intent and must be pair-swapped to match what the GPU's fetch swap expects.
_DXT_ENDIAN_PAIRS = {
    "DXT1":   [4, 5, 6, 7],                                    # BC1: idx bytes (565 words already BE)
    "DXT4_5": [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15],        # BC3: alpha block + colour idx
    "DXT5":   [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15],
    "DXT5A":  [0, 1, 2, 3, 4, 5, 6, 7],                        # BC4: whole block
    "DXN":    list(range(16)),                                 # BC5: two BC4 halves
}


def _dxt_endian(enc: bytes, fmt: str = "DXT4_5") -> bytes:
    """Xenos 8-in-16 fetch-endian transform for block-compressed formats (involution: apply on
    ENCODE and DECODE alike). Fetch constants across the game carry endianness=1 (verified in
    LIVE fetch constants via CE for portraits, logo_bos, goalie masks): the GPU swaps every
    16-bit word's bytes on fetch. Relative to our tools' historical byte order that means the
    565 colour words were already correct (written/read big-endian) but alpha endpoints were
    swapped (FLIPS the BC3 alpha interpolation mode!), and alpha/colour index bytes were
    pair-swapped (pixel-row order 1,0,3,2). Solved empirically against native portrait blobs:
    with this transform the native-vs-reference error collapses to the quantisation floor
    (mip1 RGB 150->12, mip1 ALPHA 1409->0.9). Without it on ENCODE, the GPU pair-swaps every
    imported block's pixel rows (jitter) and mode-flips edge alpha (speckled fringe); without
    it on DECODE, extractions of native DXT art look zig-zaggy with noisy alpha. This was the
    silent root cause of 'DXT4_5 looks off' across the whole project — invisible whenever a
    texture was extracted and re-imported through the same (symmetric) tools."""
    pairs = _DXT_ENDIAN_PAIRS.get(fmt)
    if not pairs:
        return enc
    bpu = _FMT_BPU[fmt]
    a = np.frombuffer(enc[:len(enc) - len(enc) % bpu], np.uint8).reshape(-1, bpu).copy()
    src = [p ^ 1 for p in pairs]
    a[:, pairs] = a[:, src]
    tail = enc[len(enc) - len(enc) % bpu:]
    return a.tobytes() + tail


def _native_backdrop_plate(index, clean_dir=None):
    """Full-canvas STUDIO BACKDROP plate recovered from the CLEAN pack's original portrait at
    this blob index: the real backdrop photo (lighting, vignette, brightness) with the original
    player inpainted away (backdrop flooded inward, then smoothed over the filled area). The
    imported cut-out is composited over this plate, so every mip's edge pixels mix with the SAME
    bright studio backdrop native portraits mix with — a synthetic grey surround reads as a dirty
    grey ring on the card (the UI shows edge/aura detail), the real backdrop reads as rim light."""
    img = decode_portrait("disc_b9610aac.iff", index, clean_dir)
    if img is None:
        return None
    a = np.asarray(img.convert("RGBA")).astype(np.float32)
    subject = a[..., 3] > 0
    if not subject.any():
        return Image.fromarray(a[..., :3].astype(np.uint8), "RGB")
        
    inv = a.copy()
    inv[..., 3] = np.where(subject, 0.0, 255.0)          # "opaque" = the backdrop region
    
    # Extract RGB and Alpha channels as uint8 arrays for the fast alpha bleed
    inv_uint8 = inv.astype(np.uint8)
    rgb = inv_uint8[..., :3]
    alpha = inv_uint8[..., 3]
    
    # Fast Euclidean Distance Transform bleed (replaces slow iterative rolling)
    bled_rgb = _alpha_bleed_fast(rgb, alpha)
    
    pr = bled_rgb.astype(np.float32)
    from PIL import ImageFilter
    bl = np.asarray(Image.fromarray(pr.astype(np.uint8), "RGB")
                    .filter(ImageFilter.GaussianBlur(6)), np.float32)
    out = np.where(subject[..., None], bl, pr)           # inpainted area smooth, backdrop crisp
    return Image.fromarray(out.astype(np.uint8), "RGB")


def _portrait_level(src, size, plate=None):
    """The BASE level of a portrait: premult-resize from the (un-matted) source, fill the
    transparent region's RGB with the nearest opaque colour, then lay the subject over the
    original portrait's real studio backdrop (`plate`; synthetic smooth surround as fallback),
    feather the mask to native width. The result is structurally a native studio photo — valid
    colour on the whole canvas — so mip levels below it are made by plain straight halving."""
    im = _premult_resize(src, size)
    
    # Convert PIL Image to RGB and Alpha arrays
    arr = np.array(im.convert("RGBA"), dtype=np.uint8)
    rgb = arr[..., :3]
    alpha = arr[..., 3]
    
    # Bleed RGB into transparent regions
    arr[..., :3] = _alpha_bleed_fast(rgb, alpha)
    im = Image.fromarray(arr, mode="RGBA")
    
    if plate is not None:
        a = np.asarray(im.convert("RGBA")).astype(np.float32)
        p = np.asarray(plate.convert("RGB").resize(size, Image.BILINEAR), np.float32)
        w = a[..., 3:4] / 255.0
        a[..., :3] = a[..., :3] * w + p * (1 - w)        # subject over real backdrop
        im = Image.fromarray(a.astype(np.uint8), "RGBA")
    else:
        im = _smooth_surround(im)
    return _feather_alpha(im, PORTRAIT_FEATHER)


def replace_portraits(name, edits, game_dir, log=print) -> str:
    """Replace one or more portraits in a portrait pack. `edits` = [{index, path}]. Re-encodes each
    edited portrait's mip0 (256x256 DXT4_5, tiled) + regenerates its mip chain, recompresses the blob
    at its native window/codec, splices it in (blobs are self-delimiting so size changes are fine),
    and relocates. Returns a status string."""
    game_dir = Path(game_dir)
    loc = resolve(name, game_dir)
    if not loc:
        raise ValueError(f"{name}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); data = bytearray(f.read(size))
    port = [b for b in _walk_blobs(bytes(data), size) if b.get("dec") and len(b["dec"]) >= _PORTRAIT_MIP0]
    by_off = {}                                          # rebuild only the edited blobs
    n = 0
    for e in edits:
        i = e["index"]
        if not (0 <= i < len(port)):
            continue
        b = port[i]
        src = _unmatte(Image.open(e["path"]).convert("RGBA"))             # original (any size)
        dec = bytearray(b["dec"])
        plate = _native_backdrop_plate(i)                # original blob's real studio backdrop
        cur = _portrait_level(src, (256, 256), plate)
        # alpha_aware=False: the canvas holds a valid synthetic backdrop everywhere, so encode
        # it faithfully like a native photo — the encoder's own bleed would re-streak it.
        # _encode_tiled stores blocks pre-swapped for the 8-in-16 fetch (_dxt_endian) on every level
        dec[0:_PORTRAIT_MIP0] = _encode_tiled(cur, "DXT4_5", 1, alpha_aware=False)
        if len(dec) >= 2 * _PORTRAIT_MIP0:
            # regenerate the mip chain — the player-card screens render the small mips (LOD ~1-2),
            # so these levels ARE the in-game image. Levels are successive straight halvings
            # (_straight_resize — Pillow's own RGBA resize premultiplies and zeroes the backdrop).
            # STORAGE LAYOUT (measured from native blobs, quadrant-occupancy proof): every level
            # below 128x128 is stored in its OWN 0x4000 tile, padded to 32x32 blocks and tiled
            # with the 128-wide GTO map, content in the TOP-LEFT corner:
            #   +0x10000 mip1 128x128 (full tile)      +0x14000 mip2 64x64 (TL of tile)
            #   +0x18000 mip3 32x32 (TL of tile)       +0x1C000 mip4 16x16 + packed 8/4 beside it
            # A contiguous per-level packing reads back as SCRAMBLED GARBAGE in-game (the GPU
            # samples mip2 at card scale -> the chunky fringe every import used to have).
            cur = _straight_resize(cur, (128, 128))
            mips = bytearray(_encode_tiled(cur, "DXT4_5", 1, alpha_aware=False))
            levels = {}
            for s in (64, 32, 16, 8, 4):
                cur = _straight_resize(cur, (s, s))
                levels[s] = cur
            for s in (64, 32):
                canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
                canvas.paste(levels[s], (0, 0))
                mips += _encode_tiled(canvas, "DXT4_5", 1, alpha_aware=False)
            canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))   # packed tail tile
            canvas.paste(levels[16], (0, 0))
            canvas.paste(levels[8], (16, 0))
            canvas.paste(levels[4], (24, 0))
            mips += _encode_tiled(canvas, "DXT4_5", 1, alpha_aware=False)
            mips += b"\x00" * max(0, _PORTRAIT_MIP0 - len(mips))
            dec[_PORTRAIT_MIP0:2 * _PORTRAIT_MIP0] = mips[:_PORTRAIT_MIP0]
        blob = EE.encode_payload(bytes(dec), wparam=b["wp"], codec=b["codec"])
        err = _verify_blob(blob, bytes(dec))
        if err:
            raise ValueError(f"{name} portrait #{i}: {err} — replace aborted, game files untouched")
        by_off[b["off"]] = (b["tot"], blob); n += 1
    if not n:
        return None
    # splice edited blobs into the asset (descending offset so earlier splices don't shift later ones)
    new_res = bytearray(data[:size])
    for o in sorted(by_off, reverse=True):
        old_tot, blob = by_off[o]
        new_res[o:o + old_tot] = blob
    _backup_once(game_dir / arc, log)
    log(f"  {name}: replaced {n} portrait(s), asset {size}->{len(new_res)} bytes")
    _sync_portrait_index(bytes(new_res), game_dir, log)
    return _relocate(name, bytes(new_res), idx, game_dir, 256, 256, "DXT4_5", log)


def _portrait_pairs(pack: bytes):
    """[(hdrOff, hdrTot, pixOff, pixTot)] — the (0xE0 header, portrait) blob pairs in a pixel pack.
    Same walk _sync_portrait_index uses to rebuild portrait.iff's read table, so a pack that yields
    PORTRAIT_COUNT pairs here is exactly a pack that can be indexed."""
    blobs = _walk_blobs(pack, len(pack))
    pairs, i = [], 0
    while i + 1 < len(blobs):
        a, b = blobs[i], blobs[i + 1]
        if (a["dec"] is not None and len(a["dec"]) == 0xE0
                and b["dec"] is not None and len(b["dec"]) >= _PORTRAIT_MIP0):
            pairs.append((a["off"], a["tot"], b["off"], b["tot"])); i += 2
        else:
            i += 1
    return pairs


def export_portrait_pack(game_dir=None) -> bytes:
    """The WHOLE portrait pixel pack as the game currently holds it — every portrait, already
    encoded and 0E4837-compressed. Shipping these bytes verbatim (rather than 1478 PNGs to be
    re-encoded on the recipient's machine) makes an imported portrait pack byte-identical to the
    author's, which is the only way to guarantee the mip chains and the portrait.iff read table
    line up. Reads the CURRENT files, not .orig, so applied portrait mods are what you get."""
    game_dir = Path(game_dir) if game_dir else None
    loc = resolve(PORTRAIT_PACK_NAME, game_dir)
    if not loc:
        raise ValueError(f"{PORTRAIT_PACK_NAME}: not found in game TOC")
    arc, off, size, _idx, _f3 = loc
    with open(_arc_file(game_dir, arc), "rb") as f:
        f.seek(off)
        return f.read(size)


def install_portrait_pack(pack: bytes, game_dir, log=print) -> str:
    """Install a WHOLE portrait pixel pack (from export_portrait_pack) over the game's own.

    Not a splice: the incoming bytes REPLACE the asset outright, so the recipient ends up with the
    author's exact portraits regardless of what they had before. Validated before anything is
    written — the pack must walk to PORTRAIT_COUNT header/portrait pairs, because portrait.iff's
    read table at +0xA210 is a fixed 1478-entry array of exact compressed offsets/sizes and the game
    seeks through it instead of walking the pack. Sync that table, then relocate.

    IMPORTANT: applying single-portrait edits afterwards still works (they splice + re-sync), but a
    later `ensure_clean` on this asset throws the whole pack away back to stock."""
    game_dir = Path(game_dir)
    pack = bytes(pack)
    loc = resolve(PORTRAIT_PACK_NAME, game_dir)
    if not loc:
        raise ValueError(f"{PORTRAIT_PACK_NAME}: not found in game TOC")
    arc, _off, _size, idx, _f3 = loc
    pairs = _portrait_pairs(pack)
    if len(pairs) != PORTRAIT_COUNT:
        raise ValueError(f"portrait pack rejected: walks to {len(pairs)} portraits, expected "
                         f"{PORTRAIT_COUNT} — game files untouched")
    _backup_once(game_dir / arc, log)
    log(f"  portrait pack: installing {len(pairs)} portraits ({len(pack)} bytes)")
    _sync_portrait_index(pack, game_dir, log, entries=pairs)
    return _relocate(PORTRAIT_PACK_NAME, pack, idx, game_dir, 256, 256, "DXT4_5", log)


# Stock fetch-constant dwords 3/4 in every portrait's 0xE0 descriptor (fetch constant @0x94;
# verified identical across blobs). d3 bits23-24 = mip_filter (1=linear, 2=BaseMap), d4 bits6-9 =
# mip_max_level. The card screens draw the portrait small, so the GPU footprint picks the 64x64
# mip2; clamping mip_max makes it sample a finer level instead:
#   stock: linear mips, mip_max=8 (whatever the footprint wants — usually 64x64 on the card)
#   mip1 : linear mips, mip_max=1  -> card samples the 128x128 level (2x native detail, mild)
#   mip0 : BaseMap, mip_max=0      -> always the full 256x256 (sharpest, aliases when tiny)
_PORTRAIT_FETCH_D3_OFF = 0x94 + 12
_PORTRAIT_FETCH = {
    "stock": (0x00A80D10, 0x00000203),
    "mip1":  (0x00A80D10, 0x00000043),
    "mip0":  (0x01280D10, 0x00000003),
}


# ── Frontend branding bundles ─────────────────────────────────────────────────
# The team-select / main-menu / loading-screen logos, wordmarks and colour-tint templates do NOT
# come from logo_<code>.iff — they live in two baked bundles (found 2026-07-11 by needle-tracing
# the live team-select atlas back to disk): flat [0xE0 descriptor + texture blob] pair streams
# exactly like the portrait pack, indexed by logos_large/medium/small.iff (120 entries each).
# name -> (label, width, mip0_bytes, dec_bytes, index_iff)
BUNDLE_PACKS = {
    "disc_b6b4e9c8.iff": ("frontend_logos_large", 512, 0x40000, 0x60000, "logos_large.iff"),
    "disc_a300d85f.iff": ("frontend_logos_medium", 256, 0x10000, 0x20000, "logos_medium.iff"),
    "disc_a38365c6.iff": ("frontend_logos_small", 128, 0x4000, 0x10000, "logos_small.iff"),
}
# Only the LARGE bundle is user-facing: replacing a tile there AUTO-SYNCS the same tile in the
# medium + small bundles (same tile indices, verified), so menu logos stay consistent at every
# draw size with a single edit. The children are hidden from the launcher's asset list.
BUNDLE_CASCADE = {"disc_b6b4e9c8.iff": ["disc_a300d85f.iff", "disc_a38365c6.iff"]}
HIDDEN_ASSETS = {"disc_a300d85f.iff", "disc_a38365c6.iff"}
_BUNDLE_CACHE = {}


def _bundle_pairs(name, root):
    """[(hdr_blob, tex_blob)] for a bundle pack read from `root` (cached per (name, root))."""
    key = (name, str(root))
    hit = _BUNDLE_CACHE.get(key)
    if hit is not None:
        return hit
    loc, data, size = _read_asset(name, root)
    if loc is None:
        _BUNDLE_CACHE[key] = []
        return []
    _, w, mip0, dec_sz, _idx = BUNDLE_PACKS[name]
    blobs = _walk_blobs(data, size)
    pairs = []
    i = 0
    while i + 1 < len(blobs):
        a, b = blobs[i], blobs[i + 1]
        if (a["dec"] is not None and len(a["dec"]) == 0xE0
                and b["dec"] is not None and len(b["dec"]) == dec_sz):
            pairs.append((a, b)); i += 2
        else:
            i += 1
    _BUNDLE_CACHE[key] = pairs
    return pairs


def _bundle_pairs_any(name, clean_dir=None):
    """Bundle pairs, preferring the LIVE pack when it holds more tiles than the pristine one.
    Teams added after ship (logos_atlas.add_team) splice extra tiles into the game archives; the
    pristine pack has no idea they exist, so listing/decoding must read live or they'd be invisible.
    Falls back to pristine whenever the live pack has no extra tiles (identical behaviour to before)."""
    ref = _bundle_pairs(name, clean_dir)
    try:
        loc = resolve(name, GAME_DIR)
        if not loc:
            return ref
        with open(_dir(GAME_DIR) / loc[0], "rb") as f:
            f.seek(loc[1]); data = f.read(loc[2])
        _, _w, _m, dec_sz, _idx = BUNDLE_PACKS[name]
        blobs = _walk_blobs(data, len(data))
        live, i = [], 0
        while i + 1 < len(blobs):
            a, b = blobs[i], blobs[i + 1]
            if (a["dec"] is not None and len(a["dec"]) == 0xE0
                    and b["dec"] is not None and len(b["dec"]) == dec_sz):
                live.append((a, b)); i += 2
            else:
                i += 1
        return live if len(live) > len(ref) else ref
    except Exception:
        return ref


def bundle_records(name, clean_dir=None):
    """Tree-style records for a branding bundle — one WxW DXT4_5 texture per pair (its mip0).

    Each tile is labelled with the asset key the game looks it up by (`ana`, `sea`, …), read live
    from the companion logos_*.iff index, so teams added after ship label themselves."""
    _, w, mip0, dec_sz, index_iff = BUNDLE_PACKS[name]
    try:
        from . import logos_atlas as LA
        labels = LA.tile_labels(index_iff, GAME_DIR)
    except Exception:
        labels = {}
    return [{"index": i, "w": w, "h": w, "fmt": "DXT4_5", "bpu": 16, "block": 1, "tiled": 1,
             "mip0": mip0, "tail": dec_sz - mip0, "foot": mip0, "blob_off": p[1]["off"],
             "label": f"{i} ({labels[i]})" if i in labels else str(i)}
            for i, p in enumerate(_bundle_pairs_any(name, clean_dir))]


def decode_bundle(name, index, clean_dir=None):
    """Decode bundle texture `index` (mip0, GPU byte order) -> PIL RGBA, or None."""
    pairs = _bundle_pairs_any(name, clean_dir)
    if not (0 <= index < len(pairs)):
        return None
    _, w, mip0, dec_sz, _idx = BUNDLE_PACKS[name]
    try:
        dec = pairs[index][1]["dec"]
        return T.decode(_dxt_endian(dec[:mip0]), w, w, "DXT4_5", 16, 1, 1, 0).convert("RGBA")
    except Exception:
        return None


def replace_bundles(name, edits, game_dir, log=print) -> str:
    """Replace textures in a frontend branding bundle. Mirrors replace_portraits: rebuild the
    edited tile (mip0 + full mip chain via _rebuild_with_mips, correct 8-in-16 byte order),
    recompress at native window/codec, SPLICE into the pack (blobs are self-delimiting so size
    changes are fine), re-sync the logos_*.iff read table (exact [smallOff,smallSize,bigOff,
    bigSize] entries like portrait.iff), and relocate. edits = [{index, path}]."""
    game_dir = Path(game_dir)
    label, w, mip0, dec_sz, index_iff = BUNDLE_PACKS[name]
    loc = resolve(name, game_dir)
    if not loc:
        raise ValueError(f"{name}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = _walk_blobs(bytes(res), size)
    pairs = []
    i = 0
    while i + 1 < len(blobs):
        a, b = blobs[i], blobs[i + 1]
        if (a["dec"] is not None and len(a["dec"]) == 0xE0
                and b["dec"] is not None and len(b["dec"]) == dec_sz):
            pairs.append((a, b)); i += 2
        else:
            i += 1
    want = _bundle_tile_count(index_iff, game_dir)
    if len(pairs) != want:
        raise ValueError(f"{name}: expected {want} tile pairs (per {index_iff}), found "
                         f"{len(pairs)} — pack damaged? run ensure_clean first")
    ref_pairs = _bundle_pairs_any(name, None)
    by_off = {}
    n = 0
    for e in edits:
        ei = e["index"]
        if not (0 <= ei < len(pairs)):
            continue
        b = pairs[ei][1]
        ref_dec = ref_pairs[ei][1]["dec"] if ei < len(ref_pairs) else b["dec"]
        img = Image.open(e["path"]).convert("RGBA")
        if img.size != (w, w):
            img = _straight_resize(img, (w, w))
        new_dec = _rebuild_with_mips(b["dec"], 0, "DXT4_5", w, w, 1, img, log,
                                     ref_dec=ref_dec, mip_end=dec_sz)
        blob = EE.encode_payload(bytes(new_dec), wparam=b["wp"], codec=b["codec"])
        err = _verify_blob(blob, bytes(new_dec))
        if err:
            raise ValueError(f"{name} tile {ei}: {err} — replace aborted, game files untouched")
        by_off[b["off"]] = (b["tot"], blob); n += 1
    if not n:
        return "no bundle tiles replaced"
    new_res = bytearray(res[:size])
    for o in sorted(by_off, reverse=True):           # descending so earlier splices don't shift
        old_tot, blob = by_off[o]
        new_res[o:o + old_tot] = blob
    _backup_once(game_dir / arc, log)
    log(f"  {name}: replaced {n} bundle tile(s), asset {size}->{len(new_res)} bytes")
    _sync_bundle_index(name, bytes(new_res), game_dir, log)
    _BUNDLE_CACHE.pop((name, str(game_dir)), None)
    status = _relocate(name, bytes(new_res), idx, game_dir, w, w, "DXT4_5", log)
    for child in BUNDLE_CASCADE.get(name, []):        # same tiles at medium/small sizes,
        log(f"  auto-syncing {BUNDLE_PACKS[child][0]} …")   # resized from the same sources
        try:
            replace_bundles(child, edits, game_dir, log)
        except Exception as ce:
            log(f"  WARNING {child} auto-sync failed: {ce}")
    return status


def _bundle_tile_count(index_iff: str, game_dir) -> int:
    """How many tiles the bundle's atlas index says it has (120 as shipped, more once a team has
    been added). Falls back to 120 if the index can't be read."""
    try:
        from . import logos_atlas as LA
    except ImportError:
        import logos_atlas as LA
    try:
        loc = resolve(index_iff, game_dir)
        with open(_dir(game_dir) / loc[0], "rb") as f:
            f.seek(loc[1]); return LA.parse(f.read(loc[2]))["n"]
    except Exception:
        return 120


def _sync_bundle_index(pack_name: str, new_pack: bytes, game_dir: Path, log=print):
    """Rewrite the logos_*.iff read table for a re-spliced bundle. The game seeks each tile via a
    table of [hdrOff, hdrSize, texOff, texSize] (exact compressed offsets/sizes, the same scheme
    portrait.iff uses) — a splice changes the edited blob's size and shifts every later blob, so
    stale entries truncate/corrupt reads. The index is parsed and rebuilt through `logos_atlas`
    (the format is fully reversed and round-trips byte-exactly), which keeps this correct for any
    tile count rather than only the 120 that shipped."""
    try:
        from . import logos_atlas as LA
    except ImportError:
        import logos_atlas as LA
    label, w, mip0, dec_sz, index_iff = BUNDLE_PACKS[pack_name]
    blobs = _walk_blobs(new_pack, len(new_pack))
    entries = []
    i = 0
    while i + 1 < len(blobs):
        a, b = blobs[i], blobs[i + 1]
        if (a["dec"] is not None and len(a["dec"]) == 0xE0
                and b["dec"] is not None and len(b["dec"]) == dec_sz):
            entries.append((a["off"], a["tot"], b["off"], b["tot"])); i += 2
        else:
            i += 1
    iloc = resolve(index_iff, game_dir)
    if not iloc:
        log(f"  WARNING {index_iff} not found in game TOC — not synced")
        return
    with open(game_dir / iloc[0], "rb") as f:
        f.seek(iloc[1]); idat = f.read(iloc[2])
    try:
        info = LA.parse(idat)
    except ValueError as e:
        log(f"  WARNING {index_iff} NOT synced (unparseable: {e})")
        return
    if len(entries) != info["n"]:
        log(f"  WARNING {index_iff} NOT synced ({len(entries)} pairs, index says {info['n']})")
        return
    changed = sum(1 for j, e in enumerate(entries) if tuple(info["readtable"][j]) != e)
    if not changed:
        log(f"  {index_iff} read-table already in sync")
        return
    info["readtable"] = entries
    new_idx = LA.build(info)
    if len(new_idx) == len(idat):                       # same size -> in-place, no relocate
        with open(game_dir / iloc[0], "r+b") as f:
            f.seek(iloc[1]); f.write(new_idx)
    else:
        _relocate(index_iff, new_idx, iloc[3], game_dir, w, w, "INDEX", log)
    log(f"  {index_iff} read-table synced ({changed} entr{'y' if changed == 1 else 'ies'} updated)")


def _sync_portrait_index(new_pack: bytes, game_dir: Path, log=print, entries=None):
    """Rewrite portrait.iff's per-portrait read table to match the (re-spliced) pixel pack. The game
    doesn't walk disc_b9610aac.iff — it seeks each portrait via portrait.iff's blob-order table at
    +0xA210: 1478 x [smallOff, smallSize, bigOff, bigSize] (exact compressed offsets/sizes). A splice
    changes the edited blob's size and SHIFTS every later blob, so stale entries truncate/corrupt
    reads (proven in-game: an over-size blob read only bigSize bytes -> garbage). In-place, same size."""
    # `entries` lets a caller that already walked the pack skip a second ~12s decompress pass
    entries = _portrait_pairs(new_pack) if entries is None else entries
    if len(entries) != PORTRAIT_COUNT:
        log(f"  WARNING portrait.iff NOT synced (found {len(entries)} pairs, expected {PORTRAIT_COUNT})")
        return
    iloc = resolve("portrait.iff", game_dir)
    if not iloc:
        log("  WARNING portrait.iff not found in TOC — index not synced")
        return
    with open(game_dir / iloc[0], "r+b") as f:
        f.seek(iloc[1]); pidat = bytearray(f.read(iloc[2]))
        changed = 0
        for j, ent in enumerate(entries):
            o = 0xA210 + j * 16
            if struct.unpack_from(">IIII", pidat, o) != ent:
                struct.pack_into(">IIII", pidat, o, *ent); changed += 1
        if changed:
            f.seek(iloc[1]); f.write(pidat)
    log(f"  portrait.iff read-table synced ({changed} entr{'y' if changed == 1 else 'ies'} updated)")


def catalog_records(name: str):
    """Tree-style records for a LOADER-REPACKED pack (global.iff, franchise.iff, …). Prefer the captured
    RUNTIME MAP (reliable record→true-offset) when present; else fall back to the live-capture catalog
    (its file_offset recovered by content-match — degenerate on blank textures). vram_off is an offset
    into this asset's texture blob, so it plugs straight into decode_record / replace_at."""
    rm = _runtime_map(name)
    if rm:
        return [{"index": i, "w": e["w"], "h": e["h"], "fmt": e["fmt"], "bpu": e["bpu"],
                 "block": e["block"], "tiled": e["tiled"], "vram_off": e["vram_off"],
                 "mip0": e["mip0"], "tail": e["tail"], "foot": e["foot"], "rec_base": e["rec_base"]}
                for i, e in enumerate(rm)]
    rows = sorted((e for e in _live_catalog() if e.get("iff") == name),
                  key=lambda e: e["file_offset"])
    return [{"index": i, "w": e["w"], "h": e["h"], "fmt": e["fmt"], "bpu": e["bpu"],
             "block": e["block"], "tiled": e["tiled"], "vram_off": e["file_offset"],
             "mip0": e["mip0"], "tail": 0, "foot": e["mip0"], "raw": e.get("raw", 0)}
            for i, e in enumerate(rows)]


# ── Team-select jersey decal components ──────────────────────────────────────
# Each mapped component holds TWO textures (fetch constants at 0x94 / 0x174): the 2048x512 4444
# colour DECAL SHEET and its 2048x512 DXN NORMAL map (the stitching relief). Offsets precomputed
# into fe_components.json; uniform_<team>_<kit> -> component in fe_uniform_map.json.
FE_COMPONENTS_JSON = R.data_path("fe_components.json")
FE_UNIFORM_MAP_JSON = R.data_path("fe_uniform_map.json")
_FE_COMPONENTS = None
_FE_UNIFORM_MAP = None


def _fe_components():
    global _FE_COMPONENTS
    if _FE_COMPONENTS is None:
        try:
            _FE_COMPONENTS = json.loads(FE_COMPONENTS_JSON.read_text())
        except Exception:
            _FE_COMPONENTS = {}
    return _FE_COMPONENTS


def _fe_uniform_map():
    global _FE_UNIFORM_MAP
    if _FE_UNIFORM_MAP is None:
        try:
            _FE_UNIFORM_MAP = {k: v for k, v in json.loads(FE_UNIFORM_MAP_JSON.read_text()).items() if v}
        except Exception:
            _FE_UNIFORM_MAP = {}
    return _FE_UNIFORM_MAP


def _fe_extra_path() -> Path:
    """%APPDATA%\\NHL2K10 Mod Launcher\\fe_components_extra.json — layouts learned at runtime.

    Not in `data/` on purpose (see resources.py): a PyInstaller build wipes the app folder, and
    this file records the decal layout of jerseys that don't exist in the shipped map — an
    expansion team's — which must survive a rebuild exactly like the user's team_fields.json.
    """
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "NHL2K10 Mod Launcher" / "fe_components_extra.json"


_FE_EXTRA = None


def _fe_extra() -> dict:
    global _FE_EXTRA
    if _FE_EXTRA is None:
        try:
            _FE_EXTRA = json.loads(_fe_extra_path().read_text())
        except Exception:
            _FE_EXTRA = {}
    return _FE_EXTRA


def _fe_extra_put(name, entry):
    _fe_extra()[name] = entry
    try:
        p = _fe_extra_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_FE_EXTRA, indent=1))
    except Exception:
        pass                                    # a cache miss next run is not worth failing over


_FE_BY_HEADER = None


def _fe_header_index() -> dict:
    """sha1(first 0x200 bytes) -> component entry, over every sheet in the shipped map.

    The three distinct `dxn_off` values are decided by the colour texture's mip tail, which is
    described by the fetch constants in the resource header — so the header identifies the layout
    even when the pixels have been repainted. Verified a function, not a relation: 224 distinct
    headers across the 288 shipped sheets, zero conflicting dxn_off.
    """
    global _FE_BY_HEADER
    if _FE_BY_HEADER is None:
        _FE_BY_HEADER = {}
        for k, e in _fe_components().items():
            try:
                loc, data, _size = _read_asset(k, None)
            except Exception:
                continue
            if loc is not None:
                _FE_BY_HEADER.setdefault(hashlib.sha1(bytes(data[:0x200])).hexdigest(), e)
    return _FE_BY_HEADER


def fe_component(name):
    """The decal-sheet layout for `name`, or None if it isn't a team-select jersey sheet.

    Three ways in, in order:
      1. the shipped map, keyed by the synthetic `disc_<crc>` name it was built under;
      2. that same entry via the sheet's REAL name — `uniform_frontend_<key>_<slot>.iff` hashes to
         exactly the `disc_<crc>` the map uses, because they are the same TOC entry under two
         names. Without this the real name lists ZERO textures, which is why an expansion team's
         jersey edit silently failed to mirror to the front end (nothing to mirror INTO);
      3. an expansion team's sheet, which is in neither: matched by resource header against the
         shipped sheets (it is a byte clone of its donor, so the layout is the donor's) and then
         remembered, so a later repaint that changes the header can't lose it.
    """
    d = _fe_components()
    if name in d:
        return d[name]
    if not name.startswith("uniform_frontend_"):
        return None
    alias = "disc_%08x.iff" % (zlib.crc32(name.upper().encode("ascii")) & 0xFFFFFFFF)
    if alias in d:
        return d[alias]
    x = _fe_extra()
    if name in x:
        return x[name]
    try:
        loc, data, _size = _read_asset(name, None)
    except Exception:
        return None
    if loc is None:
        return None
    e = _fe_header_index().get(hashlib.sha1(bytes(data[:0x200])).hexdigest())
    if e:
        _fe_extra_put(name, e)
    return e


def fe_component_records(name):
    """Two records for a decal component: the colour sheet + its DXN normal map."""
    e = fe_component(name)
    w, h, nw, nh = e["w"], e["h"], e["nw"], e["nh"]
    # The colour sheet HAS a full mip chain — the gap to the DXN is 700,416 bytes larger than mip0
    # on 254 of the 288 shipped sheets, and larger still on the rest.  Reporting foot == mip0 (what
    # this did before) told replace_many the texture ended at mip0, so an edit rewrote the top level
    # and left every smaller level stock.  The team-select jersey is a 3D render sampling those mips,
    # which is exactly why a repainted jersey looked right in game and wrong on team select.  Derive
    # the real footprint, bounded by where the DXN starts.
    cfoot = _fitted_footprint("4444", w, h, e["dxn_off"] - e["color_off"])
    return [
        {"index": 0, "w": w, "h": h, "fmt": "4444", "bpu": 2, "block": 0, "tiled": 1,
         "vram_off": e["color_off"], "mip0": w * h * 2, "tail": cfoot - w * h * 2, "foot": cfoot,
         "label": "decals"},
        {"index": 1, "w": nw, "h": nh, "fmt": "DXN", "bpu": 16, "block": 1, "tiled": 1,
         "vram_off": e["dxn_off"], "mip0": (nw // 4) * (nh // 4) * 16, "tail": 0,
         "foot": (nw // 4) * (nh // 4) * 16, "label": "decals_normal"},
    ]


# ── player head assets: three per-player maps in one exact-fill VRAM blob ─────
# Every one of the 447 player_head_id_NNNN.iff assets carries an IDENTICAL 983,040-byte VRAM blob
# holding three 512x512 surfaces, each with a full mip tail, packed back-to-back with no slack:
#     0x00000  DXT4_5  colour/diffuse   mip0 262144 + tail 131072 = 393216   (ColorSampler)
#     0x60000  DXN     tangent normal   mip0 262144 + tail 131072 = 393216   (NormalSampler)
#     0xC0000  DXT1    ambient occl.    mip0 131072 + tail  65536 = 196608   (PerPlayerOcclusionSampler)
# 393216+393216+196608 == 983040 exactly, verified across all 447 heads.
# The per-texture descriptors store +0x6C == 1 (loader-placed placeholder), so _texture_tree finds
# NO records and the asset used to fall down the single-primary path — which exposed only the colour
# map and left the normal + AO unreachable. These fixed records restore all three.
_HEAD_VRAM = 983040
_HEAD_MAPS = (("color", "DXT4_5", 393216), ("normal", "DXN", 393216), ("occlusion", "DXT1", 196608))


def player_head_records(name):
    """Three records (colour / normal / occlusion) for a player_head_id_*.iff, else []."""
    if not _player_head_id(name):
        return []
    out, off = [], 0
    for i, (label, fmt, foot) in enumerate(_HEAD_MAPS):
        bpu = _FMT_BPU[fmt]
        mip0 = (512 // 4) * (512 // 4) * bpu
        out.append({"index": i, "w": 512, "h": 512, "fmt": fmt, "bpu": bpu, "block": 1, "tiled": 1,
                    "vram_off": off, "mip0": mip0, "tail": foot - mip0, "foot": foot, "label": label})
        off += foot
    return out


def _fe_component_vram(name, root):
    loc, data, size = _read_asset(name, root)
    if loc is None:
        return None
    b = next((x for x in _walk_blobs(data, size) if x["dec"] and len(x["dec"]) > 0x20000), None)
    return b["dec"] if b else None


def list_textures(name: str, clean_dir: Path = None, live: bool = False):
    """Enumerate every texture in a (possibly multi-texture) asset -> [records]
    (see _texture_tree). Empty for single/primary AND scene assets (those resolve to a
    single texture and use the primary path — only true multi-texture iffs get a sub-list).
    Loader-repacked / scene-graph packs (global.iff, Loading.iff) whose file records are
    runtime-resolved fall back to the live-capture catalog. When the catalog holds MORE
    textures than the file tree parsed (e.g. Loading.iff: the file exposes only a bogus
    runtime-resolved 'primary', but a live capture recovered the real ones), the catalog is
    ground truth -> prefer it."""
    if name in PORTRAIT_PACKS:
        return portrait_records(name, clean_dir)
    if name in BUNDLE_PACKS:
        return bundle_records(name, clean_dir)
    if fe_component(name):
        return fe_component_records(name)
    if _player_head_id(name):                 # colour + normal + occlusion (fixed exact-fill layout)
        return player_head_records(name)
    _vram, recs = _load_tree(name, clean_dir, live)
    cat = catalog_records(name)
    # Multi-sub-package assets (rink_*/arena_presentation_*/led_*/…): the formal count@0x20 tree
    # covers only the FIRST sub-package; the tail textures are appended by the _extra_fetch_records
    # HEURISTIC (packing="scatter") which assumes absolute VRAM offsets and so MISLOCATES them (they
    # decode as noise). When a live capture exists (arena_trace.py content-matched each resident
    # texture to its true file offset), replace ONLY those scatter records with the catalog's
    # corrected offsets — formal records (and their indices/edits) stay untouched. No-op without a
    # capture, so current behavior is unchanged.
    if cat and any(r.get("packing") == "scatter" for r in recs):
        formal = [r for r in recs if r.get("packing") != "scatter"]
        # A formal record can itself be a bogus runtime-resolved primary (Loading.iff: 512x512
        # DXT4_5 @0 decodes as noise, while the capture proved 64x128 8888 lives at 0). When the
        # catalog has the SAME offset with DIFFERENT dims/fmt, the content-matched capture wins;
        # genuine formal records (rink/led) always agree with their capture, so they're untouched.
        cat_by_off = {c["vram_off"]: c for c in cat}
        formal = [cat_by_off[r["vram_off"]]
                  if (c := cat_by_off.get(r["vram_off"])) is not None
                  and (c["w"], c["h"], c["fmt"]) != (r["w"], r["h"], r["fmt"])
                  else r
                  for r in formal]
        formal_offs = {r["vram_off"] for r in formal}
        tail = [c for c in cat if c["vram_off"] not in formal_offs]
        if tail:
            merged = formal + sorted(tail, key=lambda c: c["vram_off"])
            for i, r in enumerate(merged):
                r["index"] = i
            return merged
    if len(cat) > len(recs):
        return cat
    return recs or cat


def decode_record(name: str, rec: dict, clean_dir: Path = None, live: bool = False):
    """Decode one tree record (from list_textures) -> PIL RGBA, or None.
    `live=True` decodes the CURRENT archives (applied edits visible); pass the same flag that was
    passed to list_textures, since a record is only meaningful against the tree it came from."""
    if name in PORTRAIT_PACKS:
        return decode_portrait(name, rec["index"], clean_dir)
    if name in BUNDLE_PACKS:
        return decode_bundle(name, rec["index"], clean_dir)
    if fe_component(name):
        vram = _fe_component_vram(name, clean_dir)
        if vram is None:
            return None
        try:
            return _to_straight(T.decode(_dxt_endian(vram[rec["vram_off"]:rec["vram_off"] + rec["mip0"]], rec["fmt"]),
                                rec["w"], rec["h"], rec["fmt"], rec["bpu"], rec["block"],
                                rec["tiled"], 0).convert("RGBA"), rec["fmt"])
        except Exception:
            return None
    if rec.get("raw"):
        # ff3bef94 scene texture (arena_trace.py --scenes): vram_off is a WHOLE-FILE offset into
        # the scene's raw uncompressed texture tail — no blob decompression involved.
        loc, data, size = _read_asset(name, clean_dir)
        if loc is None:
            return None
        try:
            return _to_straight(T.decode(_dxt_endian(bytes(data[rec["vram_off"]:rec["vram_off"] + rec["mip0"]]), rec["fmt"]),
                                rec["w"], rec["h"], rec["fmt"], rec["bpu"], rec["block"],
                                rec["tiled"], 0).convert("RGBA"), rec["fmt"])
        except Exception:
            return None
    vram, _recs = _load_tree(name, clean_dir, live)
    if vram is None:
        return None
    try:
        return _to_straight(T.decode(_dxt_endian(vram[rec["vram_off"]:rec["vram_off"] + rec["mip0"]], rec["fmt"]),
                            rec["w"], rec["h"], rec["fmt"], rec["bpu"], rec["block"],
                            rec["tiled"], 0).convert("RGBA"), rec["fmt"])
    except Exception:
        return None


def decode_all_textures(name: str, clean_dir: Path = None):
    """Decode every texture of `name` from CLEAN -> [(record, PIL RGBA)]. Iterates the SAME record
    list the browse tree shows (list_textures) so extract == preview. The old direct _load_tree walk
    bypassed the live-capture catalog corrections, so any asset with catalog-corrected offsets
    (Loading.iff, rink/led tails, …) extracted its scatter records as noise while previewing fine."""
    out = []
    for r in list_textures(name, clean_dir):
        img = decode_record(name, r, clean_dir)
        if img is not None:
            out.append((r, img))
    return out


def extract_record(name: str, rec: dict, out_path, clean_dir: Path = None):
    """Decode one tree texture -> uncompressed A8R8G8B8 DDS (keeps alpha)."""
    img = decode_record(name, rec, clean_dir)
    if img is None:
        raise ValueError(f"{name} t{rec['index']}: decode failed")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(_uncompressed_dds(img.tobytes(), rec["w"], rec["h"]))
    return (rec["w"], rec["h"], rec["fmt"])


def _uniform_parts(name: str):
    """('van','home') for uniform_van_home.iff / uniform_base_van_home.iff, else None."""
    import re
    m = re.match(r"uniform_(?:base_)?([a-z]{2,3})_(home|away|alt)\.iff$", name)
    return (m.group(1), m.group(2)) if m else None


def _fe_comp_folder(name: str):
    """Uniform/<TEAM>/<KIT> for a mapped jersey-decal component, else None. Components shared
    by several kits (pre-split mapping) land under the first kit alphabetically."""
    kits = sorted(u for u, c in _fe_uniform_map().items() if c == name)
    if not kits:
        return None
    parts = _uniform_parts(kits[0])
    return f"Uniform/{parts[0].upper()}/{parts[1].upper()}" if parts else None


def _team_asset_folder(name: str):
    """Grouped folder for the per-team arena assets — the SAME <Category>/<TEAM> convention the
    uniforms use, so ice / rink / arena-presentation / zamboni all extract into tidy team folders:
        ice_<code>_playoffs.iff        -> Ice/<TEAM>   (playoffs art == the regular ice)
        ice_<code>_finals.iff          -> Ice/<TEAM>
        rink_<code>.iff                -> Rink/<TEAM>
        arena_<code>.iff               -> Arena/<TEAM>   (the bowl: seats, jumbotron, concourse)
        arena_presentation_<code>.iff  -> Arena_Presentation/<TEAM>
        zamboni_team_<code>.iff        -> Zamboni/<TEAM>   (the textured zamboni; zamboni_<code>
                                                            is the untextured model and is hidden)
    Returns None for anything that isn't one of these."""
    import re
    m = re.match(r"ice_([a-z]+)_(playoffs|finals)\.iff$", name)
    if m:
        return f"Ice/{m.group(1).upper()}"
    m = re.match(r"rink_([a-z]+)\.iff$", name)
    if m:
        return f"Rink/{m.group(1).upper()}"
    m = re.match(r"arena_presentation_([a-z]+)\.iff$", name)
    if m:
        return f"Arena_Presentation/{m.group(1).upper()}"
    m = re.match(r"arena_([a-z]+)\.iff$", name)      # after presentation: [a-z]+ can't eat the "_"
    if m:
        return f"Arena/{m.group(1).upper()}"
    m = re.match(r"zamboni_team_([a-z]+)\.iff$", name)
    if m:
        return f"Zamboni/{m.group(1).upper()}"
    return None


def _player_head_id(name: str):
    """0146 for player_head_id_0146.iff, else None. 447 faces ship, so they get the same
    compacted <Category>/<id> treatment the per-team assets get rather than 447 sibling
    folders each holding one file."""
    import re
    m = re.match(r"player_head_id_(\d{4})\.iff$", name)
    return m.group(1) if m else None


def asset_iff(name: str) -> str:
    """The folder for this asset under Extracted/. Grouped layout:
      logo_<code>.iff                    -> Logos/            (file <code>.dds)
      uniform[_base]_<code>_<kit>.iff    -> Uniform/<TEAM>/<KIT>/
      jersey-decal component             -> Uniform/<TEAM>/<KIT>/  (decals / decals_normal)
      player_head_id_<id>.iff            -> Player_Heads/<id>/  (color/normal/occlusion .dds)
    Scene pseudo-assets map to their host iff; portrait/logo bundles to friendly folders."""
    hid = _player_head_id(name)
    if hid:
        return f"Player_Heads/{hid}"
    if name in SCENE_ASSETS:
        return SCENE_ASSETS[name][0]
    if name in PORTRAIT_PACKS:                # portraits export to a clean, obvious folder
        return "player_portraits"
    if name in BUNDLE_PACKS:
        return BUNDLE_PACKS[name][0]
    if name.startswith("logo_") and name.endswith(".iff"):
        return "Logos"
    up = _uniform_parts(name)
    if up:
        return f"Uniform/{up[0].upper()}/{up[1].upper()}"
    fe = _fe_comp_folder(name)
    if fe:
        return fe
    tf = _team_asset_folder(name)             # ice / rink / arena-presentation / zamboni team folders
    if tf:
        return tf
    return name


def _legacy_asset_iff(name: str) -> str:
    """The pre-2026-07-12 folder naming (per-iff folders) — kept so old edits keep applying."""
    if name in SCENE_ASSETS:
        return SCENE_ASSETS[name][0]
    if name in PORTRAIT_PACKS:
        return "player_portraits"
    if name in BUNDLE_PACKS:
        return BUNDLE_PACKS[name][0]
    return name


def texture_filename(name: str, rec=None) -> str:
    """DDS filename INSIDE the asset's .iff folder (folder = asset_iff(name)):
      • scene pseudo-asset   -> its short label   (titlepage_cover.iff -> cover.dds)
      • multi-texture record -> t{idx}.dds  (just the texture id — dims come from the record)
      • single / primary     -> <stem>.dds."""
    if rec is not None and rec.get("label"):               # scene sub-texture (cover, 2k_logo…)
        return rec["label"] + ".dds"
    if name in SCENE_ASSETS:                                # legacy pseudo-name path
        host = SCENE_ASSETS[name][0][:-4]                  # 'titlepage'
        short = name[:-4]                                   # 'titlepage_cover'
        if short.startswith(host + "_"):
            short = short[len(host) + 1:]                   # 'cover'
        return short + ".dds"
    if rec is not None:
        return f"t{rec['index']:02d}.dds"
    if name.startswith("logo_") and name.endswith(".iff"):
        return name[5:-4] + ".dds"                          # Logos/van.dds
    if name.startswith("uniform_base_") and _uniform_parts(name):
        return "base.dds"                                   # Uniform/VAN/HOME/base.dds
    stem = name[:-4] if name.lower().endswith(".iff") else name
    return stem + ".dds"


# ── Xenia window / taskbar icon (the game's Xbox-360 title icon, embedded in the XEX) ─────
_XEX_PNG_SIG = bytes([0x89, 0x50, 0x4E, 0x47])


def _find_xex_title_icon(data: bytes):
    """(file_offset, max_len) of the XEX title icon — the XDBF image entry id 0x8000, a 64x64 PNG
    that Xenia shows as the window/taskbar icon — or (None, None). Needs a decompressed-basefile
    XEX (the resource bytes are then in the clear). Located dynamically so a rebuilt XEX still works."""
    x = data.find(b"XDBF")
    while x >= 0:
        try:
            _m, _ver, etl, ec, fstl, _fsc = struct.unpack_from(">4sIIIII", data, x)
            if 0 < ec <= 4096 and etl >= ec:
                ent = x + 24; base = ent + etl * 18 + fstl * 8
                for i in range(ec):
                    ns, eid, off, ln = struct.unpack_from(">HQII", data, ent + i * 18)
                    if ns == 2 and eid == 0x8000:                 # image namespace, title-icon id
                        foff = base + off
                        if 0 < ln < 0x100000 and data[foff:foff + 4] == _XEX_PNG_SIG:
                            return foff, ln
        except Exception:
            pass
        x = data.find(b"XDBF", x + 4)
    return None, None


def _encode_icon_fit(src, maxlen):
    """64x64 PNG of `src` (path or PIL image) <= maxlen bytes (palette-reduces if a full-colour PNG
    is too big), or None."""
    import io
    img = (src if isinstance(src, Image.Image) else Image.open(src)).convert("RGBA").resize((64, 64), Image.LANCZOS)
    for n in (None, 256, 128, 64):
        buf = io.BytesIO()
        (img if n is None else img.quantize(n, method=Image.FASTOCTREE)).save(buf, "PNG", optimize=True)
        b = buf.getvalue()
        if len(b) <= maxlen:
            return b
    return None


def ensure_game_icon(xex_path, icon_src, log=print):
    """Make the game's XEX title icon (= the window/taskbar icon Xenia shows) match `icon_src`.
    No-op when it already matches, paths are missing, or the XEX isn't a decompressed basefile.
    One-time .iconbak backup before the first write. Returns a status string, or None if nothing done."""
    import io
    xex = Path(xex_path)
    if not xex.exists() or not Path(icon_src).exists():
        return None
    try:
        raw = xex.read_bytes()
    except Exception:
        return None
    off, maxlen = _find_xex_title_icon(raw)
    if off is None:
        return None
    target = _encode_icon_fit(icon_src, maxlen)
    if target is None:
        return None
    try:                                                          # already the desired icon?
        cur = Image.open(io.BytesIO(raw[off:raw.find(b"IEND", off) + 8])).convert("RGBA").resize((64, 64))
        if cur.tobytes() == Image.open(io.BytesIO(target)).convert("RGBA").tobytes():
            return None
    except Exception:
        pass
    data = bytearray(raw)
    bak = xex.with_suffix(xex.suffix + ".iconbak")
    try:
        if not bak.exists():
            bak.write_bytes(raw)
        data[off:off + maxlen] = target + b"\x00" * (maxlen - len(target))
        xex.write_bytes(data)
    except PermissionError:
        return "game icon: XEX is locked (game running?) — will update once the game is closed"
    except Exception as e:
        return f"game icon: update failed ({e})"
    return f"game icon set to custom art in {xex.name} ({len(target)} B @ 0x{off:X})"


def extract_all_textures(name: str, out_dir, clean_dir: Path = None):
    """Decode + write EVERY texture of `name` as uncompressed DDS into out_dir.
    Returns [(record, Path)] (one read of the asset)."""
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for r, img in decode_all_textures(name, clean_dir):
        p = out_dir / texture_filename(name, r)
        p.write_bytes(_uncompressed_dds(img.tobytes(), r["w"], r["h"]))
        written.append((r, p))
    return written


# ── DDS helpers ──────────────────────────────────────────────────────────────
def _uncompressed_dds(rgba: bytes, w: int, h: int) -> bytes:
    bgra = bytearray(len(rgba))
    bgra[0::4] = rgba[2::4]; bgra[1::4] = rgba[1::4]; bgra[2::4] = rgba[0::4]; bgra[3::4] = rgba[3::4]
    H = bytearray(b"DDS ") + struct.pack("<I", 124)
    H += struct.pack("<I", 0x1 | 0x2 | 0x4 | 0x1000 | 0x8)
    H += struct.pack("<I", h) + struct.pack("<I", w) + struct.pack("<I", w * 4)
    H += struct.pack("<I", 0) + struct.pack("<I", 1) + b"\x00" * 44
    H += struct.pack("<I", 32) + struct.pack("<I", 0x41) + struct.pack("<I", 0) + struct.pack("<I", 32)
    H += struct.pack("<I", 0x00FF0000) + struct.pack("<I", 0x0000FF00)
    H += struct.pack("<I", 0x000000FF) + struct.pack("<I", 0xFF000000)
    H += struct.pack("<I", 0x1000) + struct.pack("<I", 0) * 4
    return bytes(H) + bytes(bgra)


# ── scene assets: texture at a fixed offset in the big VRAM blob ─────────────
def _big_vram_blob(res, size):
    """The blob holding TEXTURE data. Must match what _texture_tree/EXTRACT pick — otherwise a
    REPLACE writes into the wrong blob. The stored-offset packs (overlay_static.iff, HUD/scorebug
    atlases) have their RECORDS blob LARGER than the texture blob, so a plain max() targets the
    metadata and CORRUPTS the asset (froze the game at HUD load)."""
    blobs = [b for b in _walk_blobs(res, size) if b["dec"]]
    if not blobs:
        return None
    dram = blobs[0]["dec"]
    if len(dram) >= 0x24 and _BE(dram, 0x20) == 0:          # contiguous / stored-offset pack
        v2, srecs = _stored_offset_records([b["dec"] for b in blobs], dram)
        if srecs and v2 is not None:
            for b in blobs:
                if b["dec"] is v2:
                    return b
    return max(blobs, key=lambda b: len(b["dec"]))


def _decode_at(dec, vram_off, w, h, fmt, tiled=1):
    bpu = _DXT_BPU[fmt]; mip0 = (w // 4) * (h // 4) * bpu
    return _to_straight(T.decode(_dxt_endian(dec[vram_off:vram_off + mip0], fmt), w, h, fmt, bpu, 1, tiled, 0).convert("RGBA"), fmt)


def extract_dds_at(iff, vram_off, w, h, fmt, out_path, clean_dir=None, tiled=1):
    loc, _cl = resolve_clean(iff, clean_dir)
    if not loc:
        raise ValueError(f"{iff}: not found")
    arc, off, size, idx, f3 = loc
    with open(_arc_file(clean_dir, arc, clean=_cl), "rb") as f:
        f.seek(off); data = f.read(size + 0x200000)
    vb = _big_vram_blob(data, size)
    if not vb:
        raise ValueError(f"{iff}: no VRAM blob")
    img = _decode_at(vb["dec"], vram_off, w, h, fmt, tiled)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(_uncompressed_dds(img.tobytes(), w, h))
    return (w, h, fmt)


def decode_preview_at(iff, vram_off, w, h, fmt, clean_dir=None, tiled=1):
    loc, _cl = resolve_clean(iff, clean_dir)
    if not loc:
        return None
    arc, off, size, idx, f3 = loc
    with open(_arc_file(clean_dir, arc, clean=_cl), "rb") as f:
        f.seek(off); data = f.read(size + 0x200000)
    vb = _big_vram_blob(data, size)
    if not vb:
        return None
    try:
        return _decode_at(vb["dec"], vram_off, w, h, fmt, tiled)
    except Exception:
        return None


def _patch_grown_iff(res: bytearray, vram_blob_off: int, new_blob_len: int):
    """When the (last) VRAM blob grew, fix the IFF header's total_size AND the matching
    section-table size entry — otherwise the game may read the OLD (smaller) size and
    truncate the back of the VRAM (where the cover's mips live)."""
    struct.pack_into(">I", res, 8, len(res))           # IFF total_size @ offset 8
    hdr = _BE(res, 4)
    p = 0x20
    while p + 0x20 <= hdr:
        if _BE(res, p) == 0:
            break
        if _BE(res, p + 0x14) == vram_blob_off:        # section whose data starts at the blob
            struct.pack_into(">I", res, p + 0x18, new_blob_len)
            return True
        p += 0x20
    return False


# Formats replace can re-encode: block DXT (BC1/BC3) + uncompressed linear.
REPLACE_FORMATS = ("DXT1", "DXT4_5", "DXT5", "DXN", "DXT5A", "8888", "8_8", "4444", "565", "1555", "8")
_FMT_BPU = {"DXT1": 8, "DXT4_5": 16, "DXT5": 16, "DXT2_3": 16, "DXN": 16, "DXT3A": 8, "DXT5A": 8,
            "DXT3A1111": 8, "8888": 4, "8_8": 2, "4444": 2, "565": 2, "1555": 2, "8": 1}
_BLOCK_FMTS = {"DXT1", "DXT4_5", "DXT5", "DXT2_3", "DXN", "DXT3A", "DXT5A", "DXT3A1111"}


def _mip0_size(fmt, w, h):
    """Bytes of the mip0 surface for any supported format."""
    bpu = _FMT_BPU[fmt]
    return (w // 4) * (h // 4) * bpu if fmt in _BLOCK_FMTS else w * h * bpu


def _encode_tiled(img, fmt, tiled=1, premultiply=False, alpha_aware=True):
    """Edited PIL image -> game-order, gto-tiled mip0 bytes for the texture's format.
    DXT1 = BC1 (8/block); DXT4_5/DXT5 = BC3 (16/block); else uncompressed (8888/4444/565/…).
    `premultiply` applies only to DXT4_5/DXT5 — pass it ONLY when the original texture is truly
    premultiplied (most DXT4_5 UI/HUD art is straight-alpha; premultiplying that darkens it).
    `alpha_aware=False` (BC3 only) skips the encoder's transparent-RGB bleed + alpha weighting —
    pass it when the image's transparent region already holds VALID colour that must be encoded
    faithfully (portraits carry a synthetic backdrop there, like the native studio photos)."""
    # _dxt_endian: store block data pre-swapped for the Xenos 8-in-16 fetch (see its docstring —
    # this was the root cause of every 'DXT looks off/blocky' issue on clean-source imports).
    if fmt == "DXT1":
        return _dxt_endian(ED.encode_image_dxt1(img), fmt)
    if fmt == "DXT5A":
        return _dxt_endian(ED.encode_image_dxt5a(img), fmt)
    if fmt == "DXN":
        return _dxt_endian(ED.encode_image_dxn(img), fmt)
    if fmt in ("DXT4_5", "DXT5"):
        return _dxt_endian(ED.encode_image(img, alpha_aware=alpha_aware, premultiply=premultiply), fmt)
    return ED.encode_image_linear(img, fmt, tiled)


# ── DDS passthrough (use already-compressed DXT blocks directly, no re-compression) ──────────
_DDS_FOURCC = {b"DXT1": "DXT1", b"DXT5": "DXT4_5", b"DXT3": "DXT2_3"}


def _read_dds_compressed(path):
    """Parse a compressed (DXT1/3/5) DDS -> {fmt,w,h,mips,data(after header)} or None (not a DXT DDS,
    e.g. our uncompressed A8R8G8B8 export, or a PNG). DDS is little-endian."""
    try:
        b = Path(path).read_bytes()
    except Exception:
        return None
    if b[:4] != b"DDS " or len(b) < 128:
        return None
    h = struct.unpack_from("<I", b, 12)[0]; w = struct.unpack_from("<I", b, 16)[0]
    mips = struct.unpack_from("<I", b, 28)[0] or 1
    fmt = _DDS_FOURCC.get(bytes(b[84:88]))
    if not fmt or b[84:88] == b"DX10":          # DX10-extended header unsupported here
        return None
    return {"fmt": fmt, "w": w, "h": h, "mips": mips, "data": b[128:]}


def _swap_retile_dxt(linear: bytes, fmt: str, w: int, h: int) -> bytes:
    """Linear PC-order DXT blocks (LE 565 endpoints) -> game gto-tiled blocks (BE 565 endpoints).
    Only the two colour-endpoint words per block differ (byte-swap); alpha + index bytes match.
    This is the inverse of decode's detiling+endianness, so it PRESERVES the block data exactly
    (no re-compression). Returns (w//4)*(h//4)*bpu bytes."""
    bpu = _FMT_BPU[fmt]; bl = bpu.bit_length() - 1
    bw, bh = w // 4, h // 4; NB = bw * bh
    if len(linear) < NB * bpu:
        raise ValueError(f"DDS has {len(linear)} block bytes, need {NB * bpu}")
    blk = np.frombuffer(linear[:NB * bpu], np.uint8).reshape(NB, bpu).copy()
    # stored = 16-bit byteswap of the WHOLE PC-LE block (the Xenos fetch un-swaps it): colour
    # endpoint words AND alpha/index bytes. (Swapping only the endpoints left index bytes in
    # PC row order -> the GPU pair-swapped every passthrough block\'s pixel rows in-game.)
    tmp = blk[:, 0::2].copy(); blk[:, 0::2] = blk[:, 1::2]; blk[:, 1::2] = tmp
    gmap = np.fromiter((T.gto(bx, by, bw, bl) // bpu for by in range(bh) for bx in range(bw)),
                       np.int64, NB)
    tiled = np.zeros((max(NB, int(gmap.max()) + 1), bpu), np.uint8)
    tiled[gmap] = blk
    return tiled.reshape(-1).tobytes()


def _dds_passthrough_mip0(edited_path, fmt, w, h):
    """If `edited_path` is a compressed DDS ALREADY in `fmt` at `w`x`h`, return its mip0 as game
    gto-tiled bytes (byte-swap + re-tile, NO re-compression) — preserves the user's exact (e.g.
    NVTT/Compressonator) compression. Else None (fall back to normal decode+encode)."""
    if fmt not in ("DXT1", "DXT4_5", "DXT5"):
        return None
    d = _read_dds_compressed(edited_path)
    if not d or d["w"] != w or d["h"] != h:
        return None
    if d["fmt"] != fmt and not (d["fmt"] == "DXT4_5" and fmt == "DXT5"):
        return None
    try:
        return _swap_retile_dxt(d["data"], fmt, w, h)
    except Exception:
        return None


_MIN_TILE = 32          # Xenos tile = 32x32 ADDRESSED units (texels linear, 4x4 blocks for DXT/DXN)


def _surf_h(fmt, sw, nbytes):
    """Height of a `sw`-wide surface of exactly `nbytes` bytes, or 0 if it doesn't divide."""
    bpu = _FMT_BPU[fmt]
    if fmt in _BLOCK_FMTS:
        row = (sw // 4) * bpu                          # bytes per 4-texel block row
        return 4 * (nbytes // row) if row and nbytes % row == 0 else 0
    row = sw * bpu
    return (nbytes // row) if row and nbytes % row == 0 else 0


def _mip_tail_plan(fmt, w, h, tail_bytes):
    """Layout of the mip levels that follow the naive (both dims >= min tile) chain.

    MEASURED from pristine shipped packs (uniform_{ana,bos,cgy}_home, records 0/1/2) — the
    generic walk below assumes every level is stored at its naive size, which is true only
    while BOTH dimensions are at least one Xenos tile.  Past that point:
      * a level keeps its own surface, each dimension padded up to a whole tile, image at (0,0) —
        but only while that still leaves a tile for the packed surface;
      * everything from there down is PACKED into one final surface, each level at (0, its own
        texel height).  Verified 64x16@(0,16), 32x8@(0,8), 16x4@(0,4) on three independent
        packs (10-30x MSE margin over the (0,0) reading), and the DXN record's 64x16@(0,16).

    Sizes predicted by this rule land EXACTLY on the shipped footprint for every pack checked —
    4444 2048x512 and 1024x256, DXN 2048x512, DXT1 256x128 (overlay), 565 1024x1024 and 512x512
    (uniform_base) — and the mismatch check below is what makes a wrong guess a no-op rather than
    corruption.  Placement is measured for the >=32x32 levels; below that a downscale of mip0 no
    longer predicts stored content well enough to discriminate, so those few hundred bytes rest on
    the size fit alone (they are sub-16x16 mips — not observable in game).

    Returns [(surf_w, surf_h, nbytes, [(lvl_w, lvl_h, x, y), ...]), ...], or None when the plan
    does not account for EXACTLY `tail_bytes` — an unrecognised layout, which the caller must
    leave untouched rather than corrupt.  On the two measured records the prediction is exact
    (4,096 and 65,536).
    """
    unit = 4 if fmt in _BLOCK_FMTS else 1
    mt = _MIN_TILE * unit                              # min tile edge, in TEXELS
    mw, mh = w // 2, h // 2
    while mw >= mt and mh >= mt:                       # skip what the naive walk already covers
        mw //= 2; mh //= 2

    plan, left = [], tail_bytes
    while mw >= 4 and mh >= 4 and left > 0:
        more = (mw // 2) >= 4 and (mh // 2) >= 4
        sw, sh = max(mw, mt), max(mh, mt)              # own surface, padded up to a whole tile
        need = _mip0_size(fmt, sw, sh)
        if need <= left and (not more or left - need >= _mip0_size(fmt, mt, mt)):
            plan.append((sw, sh, need, [(mw, mh, 0, 0)]))
            left -= need
            mw //= 2; mh //= 2
            continue
        sw = max(mw, mt)                               # packed surface takes ALL that remains
        sh = _surf_h(fmt, sw, left)
        if sh < mt:
            return None
        lv, a, b = [], mw, mh
        while a >= 4 and b >= 4 and a <= sw and 2 * b <= sh:
            lv.append((a, b, 0, b))                    # level of height b sits at y = b
            a //= 2; b //= 2
        if not lv:
            return None
        plan.append((sw, sh, left, lv))
        left = 0
        break
    return plan if left == 0 and plan else None


def _fitted_footprint(fmt, w, h, limit):
    """The stored footprint the native layout accounts for EXACTLY, within `limit` bytes of room.

    For assets with a texture-record tree the footprint is read from the record (`foot`).  The
    front-end jersey sheets have no tree — fe_components.json gives only the two textures' start
    offsets — so their footprint has to be DERIVED, and getting it wrong in the generous direction
    would let a mip write run into the next texture.  Build it the same way the layout is built:
    mip0, then full-size levels while both dims are at least a tile, then the SMALLEST tail
    `_mip_tail_plan` accepts.

    Smallest, not largest.  254 of the 288 shipped sheets leave exactly 700,416 bytes between the
    two textures and 33 leave 8,192 more, which reads like a second layout — it isn't.  Decoding
    the 64x16 level out of both groups puts it at (0,16) of a packed 64x32 surface either way (MSE
    ~800 there against a downscale of mip0, vs ~12,000 at (0,0) — a 10-20x margin, measured on ten
    sheets across both groups).  So the extra 8,192 is slack and the tail is the same everywhere.
    Taking the largest fit instead would have claimed that slack as three own surfaces and written
    those levels to the wrong place.

    Everything is bounded by `limit`, and a limit that fits nothing past mip0 returns mip0's size —
    the old behaviour — so this can only add correctly-described levels, never overrun.
    """
    unit = 4 if fmt in _BLOCK_FMTS else 1
    mt = _MIN_TILE * unit
    base = _mip0_size(fmt, w, h)
    if base > limit:
        return base
    mw, mh = w // 2, h // 2
    while mw >= mt and mh >= mt and base + _mip0_size(fmt, mw, mh) <= limit:
        base += _mip0_size(fmt, mw, mh)
        mw //= 2; mh //= 2
    if mw >= mt and mh >= mt:                          # ran out of room mid-chain — no tail
        return base
    step = _mip0_size(fmt, mt, mt)                     # the tail is a whole number of min tiles
    for k in range(1, (limit - base) // step + 1):
        if _mip_tail_plan(fmt, w, h, k * step):
            return base + k * step
    return base


def _rebuild_with_mips(dec, vram_off, fmt, w, h, tiled, edited_img, log=print, ref_dec=None,
                       mip_end=None, pt_mip0=None):
    """Splice the edited image into mip0 AND every following mip level — otherwise the
    GPU samples the ORIGINAL smaller mips at distance and the old art reappears.

    NHL2k10 stores mip levels contiguously after mip0 until the Xenos 'mip tail' (small
    levels packed together). We walk the chain and, before overwriting each level, confirm
    its current data really IS that mip (decodes to a downscale of the reference mip0, low
    MSE); the moment that check fails we STOP — so we never corrupt the tail / neighbours.
    `ref_dec` = the PRISTINE (CLEAN) blob used as the mip-chain reference, so replacing OVER
    an already-modified texture still finds the real chain (defaults to `dec`).
    `mip_end` = absolute byte offset where THIS texture's data ends (vram_off + footprint).
    When known we bound the walk by it (hard safety against touching the next packed texture)
    and use a looser MSE gate — emboss/normal mips downscale with more error than colour, so
    the old 3000 cutoff stopped after mip0. Without it (scene assets) keep the strict gate.
    Returns the new decompressed blob (same length). Raises if mip0 doesn't fit/encode."""
    ref = ref_dec if (ref_dec is not None and len(ref_dec) == len(dec)) else dec
    out = bytearray(dec)
    block = fmt in _BLOCK_FMTS; bpu = _FMT_BPU[fmt]
    mip0_sz = _mip0_size(fmt, w, h)
    cap = min(mip_end, len(ref)) if mip_end is not None else len(ref)
    thresh = 12000 if mip_end is not None else 3000
    if vram_off + mip0_sz > len(dec):
        raise ValueError("mip0 out of VRAM range")
    # Detect per-texture whether the ORIGINAL is premultiplied (most DXT4_5 are NOT — they're
    # straight-alpha; premultiplying those darkens every partial-alpha pixel = the "low-res"
    # look). mip0 and every mip level then use the same setting.
    premf = _orig_is_premult(ref, vram_off, w, h, fmt, tiled)
    if pt_mip0 is not None and len(pt_mip0) == mip0_sz:   # DDS passthrough (no re-compression)
        new0 = pt_mip0; log("  mip0: DDS passthrough (already-compressed blocks, no re-encode)")
    else:
        new0 = _encode_tiled(edited_img, fmt, tiled, premultiply=premf)
    if len(new0) != mip0_sz:
        raise ValueError(f"encoded mip0 {len(new0)} != {mip0_sz}")
    out[vram_off:vram_off + mip0_sz] = new0
    try:
        orig0 = T.decode(_dxt_endian(bytes(ref[vram_off:vram_off + mip0_sz]), fmt), w, h, fmt, bpu, block, tiled, 0).convert("RGB")
    except Exception:
        orig0 = None
    n = 0
    # Premultiplied textures: downscale mips in PREMULTIPLIED space (correct edge filtering);
    # straight textures: downscale straight. encode_image(premultiply=False) then fits as-is.
    mip_src = _premult_pil(edited_img) if premf else edited_img

    def _enc_lvl(im):                                    # exact per-level encode used below
        # premf: image is already premultiplied; _encode_tiled (not bare ED.encode_image) so the
        # 8-in-16 fetch-endian swap is applied — bare encode stored mips unswapped and the GPU
        # scrambled every level below mip0 ("small logos boxy/noisy" root cause, 2026-07-27).
        return (_encode_tiled(im, fmt, tiled, premultiply=False, alpha_aware=False)
                if premf else _encode_tiled(im, fmt, tiled))

    # SQUARE branding / logo mip tail (verified on the real logo_*.iff packs, and identical to the
    # portrait tail): every level is an own tile down to 32x32 — each level BELOW 128 padded top-left
    # into its OWN 128-min-tile — then the 16/8/4 levels PACKED into one final 128-min-tile
    # (16@(0,0) · 8@(16,0) · 4@(24,0)). The old generic walk wrote bare _encode_tiled sizes, which
    # UNDER-size sub-128 DXT levels (64 -> 0x3800 not the stored 0x4000); that desynced the offset
    # walk so it stopped at ~64x64 and left 32/16/8/4 as STALE old-logo bytes. The menu samples the
    # top levels (fine) but the scorebug / in-game overlays sample these tiny mips -> "small logos
    # look pixelated". Build the native tail explicitly for square block textures (logos); non-square
    # packs (uniforms/overlay) and non-block formats keep the safe MSE-gated walk below unchanged.
    if orig0 is not None and block and w == h:
        min_tile = (128 // 4) * (128 // 4) * bpu
        o = vram_off + mip0_sz; mw = w // 2
        while mw >= 32:                                  # own tiles: full-size >=128, else padded 128-tile
            try:
                lvl = _box_lvl(mip_src, (mw, mw))
                if mw >= 128:
                    enc = _enc_lvl(lvl)
                else:
                    canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0)); canvas.paste(lvl, (0, 0))
                    enc = _enc_lvl(canvas)
            except Exception:
                break
            if o + len(enc) > cap or o + len(enc) > len(out):
                break
            out[o:o + len(enc)] = enc
            o += len(enc); n += 1; mw //= 2
        if o + min_tile <= cap and o + min_tile <= len(out) and w >= 16:   # packed 16/8/4 tail tile
            try:
                canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
                for s, x in ((16, 0), (8, 16), (4, 24)):
                    if s <= w:
                        canvas.paste(_box_lvl(mip_src, (s, s)), (x, 0))
                enc = _enc_lvl(canvas)
                if o + len(enc) <= cap and o + len(enc) <= len(out):
                    out[o:o + len(enc)] = enc; n += 1
            except Exception:
                pass
        log(f"  replaced mip0 + {n} mip level(s) [square tail]")
        return bytes(out)

    # The naive "every level is stored at its raw size, back to back" assumption holds only while
    # BOTH dimensions are at least one Xenos tile. Past that levels are padded, then packed — so the
    # old walk read the WRONG bytes there, the MSE gate blew up, and it STOPPED, leaving the donor's
    # small mips in place. That is the "sharp up close, wrong art at distance" LOD bug. When the
    # naive chain plus _mip_tail_plan account for the footprint EXACTLY, the layout is fully known:
    # write every level deterministically and skip the gate entirely (the gate also mis-fires when
    # re-importing over an already-damaged pack, which made the damage stick across re-imports).
    plan = None
    if orig0 is not None and mip_end is not None:
        unit = 4 if block else 1
        mt = _MIN_TILE * unit
        nat, mw, mh = [], w // 2, h // 2
        while mw >= mt and mh >= mt:
            nat.append((mw, mh, _mip0_size(fmt, mw, mh)))
            mw //= 2; mh //= 2
        rest = cap - (vram_off + mip0_sz) - sum(s for _, _, s in nat)
        plan = _mip_tail_plan(fmt, w, h, rest) if rest > 0 else ([] if rest == 0 else None)

    if plan is not None:
        o = vram_off + mip0_sz
        try:
            for mw, mh, msz in nat:                          # full levels, own surfaces
                out[o:o + msz] = _enc_lvl(_box_lvl(mip_src, (mw, mh)))
                o += msz; n += 1
            for sw, sh, nbytes, levels in plan:              # padded / packed tail surfaces
                try:
                    # seed from the REFERENCE so padding and the sub-4 scraps we don't regenerate
                    # keep their original bytes instead of going blank
                    canvas = T.decode(_dxt_endian(bytes(ref[o:o + nbytes]), fmt),
                                      sw, sh, fmt, bpu, block, tiled, 0).convert("RGBA")
                except Exception:
                    canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
                for lw, lh, x, y in levels:
                    canvas.paste(_box_lvl(mip_src, (lw, lh)), (x, y))
                enc = _enc_lvl(canvas)
                if len(enc) != nbytes:
                    raise ValueError(f"tail surface {sw}x{sh} encoded {len(enc)} != {nbytes}")
                out[o:o + nbytes] = enc
                o += nbytes; n += len(levels)
            log(f"  replaced mip0 + {n} mip level(s) [native layout, "
                f"{len(nat)} full + {len(plan)} tail surface(s)]")
            return bytes(out)
        except Exception as e:
            log(f"  native mip layout failed ({e}) — falling back to the gated walk")
            out = bytearray(dec); out[vram_off:vram_off + mip0_sz] = new0; n = 0

    if orig0 is not None:
        o = vram_off + mip0_sz; mw, mh = w // 2, h // 2
        while mw >= 4 and mh >= 4:
            # encode the downscaled edit FIRST: its length is the level's stored (gto-padded)
            # tiled size — small mips (<128 wide DXT) are stored padded, not at the raw mip size.
            try:
                # BOX (2x2 average) is the game's own mip filter: matches native compressibility
                # (~native size -> fits in-place) and avoids LANCZOS ringing/overshoot at edges.
                lvl_img = _box_lvl(mip_src, (mw, mh))
                new_lvl = (_encode_tiled(lvl_img, fmt, tiled, premultiply=False, alpha_aware=False)
                           if premf else _encode_tiled(lvl_img, fmt, tiled))
            except Exception:
                break
            msz = len(new_lvl)
            if o + msz > cap or o + msz > len(out):
                break
            try:
                cur = T.decode(_dxt_endian(bytes(ref[o:o + msz]), fmt), mw, mh, fmt, bpu, block, tiled, 0).convert("RGB")
            except Exception:
                break
            dref = orig0.resize((mw, mh), Image.BOX)
            mse = float(np.mean((np.asarray(cur, np.float32) - np.asarray(dref, np.float32)) ** 2))
            if mse > thresh:                     # hit the Xenos packed mip tail / next texture -> stop
                break
            out[o:o + msz] = new_lvl
            n += 1; o += msz; mw //= 2; mh //= 2
    log(f"  replaced mip0 + {n} mip level(s)")
    return bytes(out)


def _clean_ref_blob(name, vram_off):
    """The pristine CLEAN decompressed VRAM blob for `name` (mip-chain reference), or None."""
    try:
        loc, _cl = resolve_clean(name, None)
        if not loc:
            return None
        arc, off, size, idx, f3 = loc
        with open(_arc_file(None, arc, clean=_cl), "rb") as f:
            f.seek(off); data = f.read(size + 0x400000)
        vb = _big_vram_blob(data, size)
        return vb["dec"] if vb else None
    except Exception:
        return None


def replace_at(iff, vram_off, w, h, fmt, edited_path, game_dir, log=print, tiled=1) -> str:
    if not tiled:
        raise ValueError(f"{iff}: replace of non-tiled textures is not supported")
    game_dir = Path(game_dir)
    loc = resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    if res[:4] == b"\xff\x3b\xef\x94" and \
            len([b for b in _walk_blobs(res, size) if b["dec"]]) < 2:
        # RAW ff3bef94 scene (disc_* arenas): a lone compressed DRAM tree + raw texture tail. Its
        # textures live in the RAW tail (catalog raw=1 records), not in a compressed VRAM blob —
        # this path would corrupt the DRAM tree. Raw replace TBD. (Loading.iff shares the magic but
        # has a real texture blob, so the blob count keeps it on the normal path.)
        raise ValueError(f"{iff}: scene raw-tail texture replace is not supported yet (view/extract only)")
    vb = _big_vram_blob(res, size)
    if not vb:
        raise ValueError(f"{iff}: no VRAM blob")
    # bound the mip walk by this texture's footprint -> replaces its whole mip chain without
    # ever touching the next packed texture (multi-texture assets) or running off the end.
    mip_end = None
    try:
        for _r in _texture_tree(res, size)[1]:
            if _r["vram_off"] == vram_off and _r.get("foot"):
                mip_end = vram_off + _r["foot"]; break
    except Exception:
        pass
    img = Image.open(edited_path).convert("RGBA")
    if img.size != (w, h):
        if img.size[0] * h != img.size[1] * w:
            log(f"  WARNING {iff}: source aspect {img.size} != {w}x{h} — it will be stretched")
        log(f"  {iff}: fitting source {img.size} -> {w}x{h} (LANCZOS downscale)")
        img = img.resize((w, h), Image.LANCZOS)
    vo = vb["off"]; old_tot = vb["tot"]
    ref = _clean_ref_blob(iff, vram_off)
    pt = _dds_passthrough_mip0(edited_path, fmt, w, h)   # use pre-compressed DDS blocks if given

    def _encode(levels, quiet):
        src = _posterize(img, levels)
        nd = _rebuild_with_mips(vb["dec"], vram_off, fmt, w, h, tiled, src,
                                (lambda *a: None) if quiet else log, ref_dec=ref, mip_end=mip_end,
                                pt_mip0=pt)
        if len(nd) != len(vb["dec"]):
            raise ValueError(f"{iff}: VRAM size changed (offset/size out of range)")
        nb = EE.encode_payload(nd, wparam=vb["wp"], codec=vb["codec"])   # native window
        e = _verify_blob(nb, nd)
        if e:
            raise ValueError(f"{iff}: {e} — replace aborted, game files untouched")
        return nb, nd

    new_blob, new_dec = _encode(0, quiet=False)
    used = 0
    if len(new_blob) > old_tot:
        # Multi-texture / stored-offset packs (overlay_static, global, uniforms, HUD, …) CANNOT be
        # relocated (it corrupts the pack + freezes the game). Instead of refusing a slightly-too-
        # big edit, AUTO-FIT: gently posterize this one texture until it compresses back into its
        # slot. Highest quality that fits wins (levels descend = smaller).
        log(f"  edit is {len(new_blob)-old_tot} bytes over the in-place slot — auto-fitting (posterize)…")
        for lv in (64, 48, 32, 24, 16):
            new_blob, new_dec, used = (*_encode(lv, quiet=True), lv)
            if len(new_blob) <= old_tot:
                break

    if len(new_blob) <= old_tot:
        if used:
            log(f"  auto-fit OK: posterized to {used} colour levels ({len(new_blob)}/{old_tot} bytes)")
        new_res = bytearray(res)
        new_res[vo:vo + old_tot] = new_blob + b"\x00" * (old_tot - len(new_blob))
        _backup_once(game_dir / arc, log)
        with open(game_dir / arc, "r+b") as f:
            f.seek(off); f.write(new_res)
        tag = f", posterized {used}lvl" if used else ""
        return (f"IN-PLACE: {iff} @VRAM 0x{vram_off:X} -> {arc}:0x{off:X} "
                f"(blob {len(new_blob)}/{old_tot}, {w}x{h} {fmt}{tag})")

    # Still over even at 16 levels. Before degrading further / refusing, try the WHOLE-PACK RELOCATE:
    # the uncompressed layout is unchanged (same dims, same format, same offsets) — only the
    # COMPRESSED blob outgrew its slot — so moving the whole resource to the end of 1B and repointing
    # the TOC is safe and keeps FULL quality. Restricted to relocatable, non-sequential packs whose
    # VRAM blob is last; _whole_pack_relocate_vram returns None for anything else.
    if not _is_sequential_pack(iff, game_dir):
        try:
            full, _fd = _encode(0, quiet=True)            # back to un-posterized before relocating
            reloc = _whole_pack_relocate_vram(iff, res, size, vb, full, idx, game_dir, log)
        except Exception as we:
            log(f"  whole-pack relocate aborted ({we})"); reloc = None
        if reloc:
            return (f"RELOCATED (full quality): {iff} @VRAM 0x{vram_off:X} — blob {len(full)} bytes "
                    f"(was {old_tot}); layout preserved. {reloc}")

    over = len(new_blob) - old_tot
    raise ValueError(
        f"edit too large to fit in place even after posterizing: compressed {len(new_blob)} bytes "
        f"vs the {old_tot}-byte slot (+{over}). Multi-texture packs can't be relocated (it corrupts "
        f"the asset and freezes the game), so {iff} was NOT changed. Use a flatter/simpler image "
        f"(less fine detail / sharp noise) for this texture. (These assets are in-place only.)")


def _grow_many(iff, arc, off, idx, res, size, tex_b, items, game_dir, log, prefer_lossless=False):
    """Relocate-grow path for apply-all: APPEND each edited texture to the end of the texture
    blob + redirect its record's stored offset (+0x6c), re-encode both blobs once, and relocate
    the resource (append to 1B + repoint TOC). Frees the in-place size cap so detailed/hi-res
    edits keep FULL quality (no posterize). Old slots become dead space; unedited textures are
    untouched. Returns a status string, or None when the pack can't be grown safely — loader-
    repacked records (global.iff), the texture blob isn't last (overlay_static), or a record's
    dims don't match (coincidental record). Modelled on the verified replace_multitex_grow."""
    blobs = _walk_blobs(res, size)
    if len(blobs) < 2 or tex_b["off"] != blobs[-1]["off"]:
        return None                                       # need [descriptor … texture(last)]
    dram_b = blobs[0]
    dram = bytearray(dram_b["dec"]); tex = bytearray(tex_b["dec"])
    plan = []
    if _is_scatter_pack(iff):               # arena presentation: group-relative offsets, in-place only
        return None
    for (e, img, _me) in items:
        rec = _find_multitex_rec(bytes(dram), e["vram_off"], w=e["w"], h=e["h"])
        if rec is None:
            return None
        rw = struct.unpack_from(">H", dram, rec + 0x60)[0]
        rh = struct.unpack_from(">H", dram, rec + 0x62)[0]
        if rw != e["w"] or rh != e["h"]:                  # coincidental record -> not a grow target
            return None
        plan.append((e, img, rec))
    n8 = 0
    for (e, img, rec) in plan:
        upg = _lossless_target(e["fmt"]) if prefer_lossless else None   # 8888 (colour) / 8_8 (normal)
        nf = upg or e["fmt"]
        if upg:                                           # uncompressed: no premult, straight channels
            chain = bytearray(_encode_tiled(img, nf, 1))
            mw, mh = e["w"] // 2, e["h"] // 2
            while mw >= 4 and mh >= 4:
                chain += _encode_tiled(img.resize((mw, mh), Image.BOX).convert("RGBA"), nf, 1)
                mw //= 2; mh //= 2
            n8 += 1
        else:
            premf = _orig_is_premult(tex_b["dec"], e["vram_off"], e["w"], e["h"], e["fmt"], 1)
            mip_src = _premult_pil(img) if premf else img
            pt = _dds_passthrough_mip0(e.get("path"), e["fmt"], e["w"], e["h"])
            chain = bytearray(pt if pt else _encode_tiled(img, e["fmt"], 1, premultiply=premf))
            mw, mh = e["w"] // 2, e["h"] // 2
            while mw >= 4 and mh >= 4:
                lvl = mip_src.resize((mw, mh), Image.BOX).convert("RGBA")
                chain += (_encode_tiled(lvl, e["fmt"], 1, premultiply=False, alpha_aware=False)
                          if premf else _encode_tiled(lvl, e["fmt"], 1))
                mw //= 2; mh //= 2
        mip0 = _mip0_size(nf, e["w"], e["h"]); tail = len(chain) - mip0
        new_voff = (len(tex) + 0xFFF) & ~0xFFF            # page-aligned append point
        tex += b"\x00" * (new_voff - len(tex)) + chain
        struct.pack_into(">I", dram, rec + 0x6C, new_voff + 1)    # redirect the stored offset
        struct.pack_into(">I", dram, rec + 0x70, mip0)
        struct.pack_into(">I", dram, rec + 0x74, tail)
        if nf != e["fmt"]:                                # format upgraded -> rewrite the record fields
            for o in (0x08, 0x0C, 0x1C):
                struct.pack_into(">I", dram, rec + o, _FMT_DESCRIPTOR[nf])
            f1 = struct.unpack_from(">I", dram, rec + 0x98)[0]
            struct.pack_into(">I", dram, rec + 0x98, (f1 & ~0xFFF) | _FMT_F1_LOW[nf])
            f3v = struct.unpack_from(">I", dram, rec + 0xA0)[0]
            struct.pack_into(">I", dram, rec + 0xA0, (f3v & ~0xFFF) | _FMT_F3_LOW[nf])
            struct.pack_into(">I", dram, rec + 0xA8, (mip0 & ~0xFFF) | 0xA00)
    if n8:
        log(f"  {n8} texture(s) stored UNCOMPRESSED (8888 colour / 8_8 normal — no block artifacts)")
    dram_c = EE.encode_payload(bytes(dram), wparam=dram_b["wp"], codec=dram_b["codec"])
    tex_c = EE.encode_payload(bytes(tex), wparam=tex_b["wp"], codec=tex_b["codec"])
    for blob_c, dec in ((dram_c, bytes(dram)), (tex_c, bytes(tex))):
        ve = _verify_blob(blob_c, dec)
        if ve:
            raise ValueError(f"{iff}: {ve} — grow aborted, game files untouched")
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + tex_c
    _patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    _patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(tex_c), dec_size=len(tex))
    struct.pack_into(">I", new_res, 8, len(new_res))      # IFF total_size
    _backup_once(game_dir / arc, log)
    e0 = plan[0][0]
    log(f"  {iff}: relocate-grow — appended {len(plan)} texture(s) at full quality, "
        f"blob {len(tex_b['dec'])}->{len(tex)} bytes (old slots now dead space)")
    return _relocate(iff, bytes(new_res), idx, game_dir, e0["w"], e0["h"], e0["fmt"], log)


# Experimental: repack SCATTER packs (arena_presentation) so their textures can go uncompressed too.
# Scatter records store GROUP-RELATIVE offsets (+0x6c = group_rel + 1; a group starts where rel resets
# to 0; runtime base = cumulative). Growing one texture shifts everything after it, so we rebuild the
# WHOLE blob in DRAM-record order and recompute every record's group-relative +0x6c. Set False to fall
# back to native in-place DXT for these packs if the repack renders wrong in-game.
REPACK_SCATTER = True

# SEQUENTIAL LOADER-PLACED packs (global.iff, gamedata.iff, …): +0x6c = 1 (placeholder); the game streams
# textures to VRAM at a runtime cursor in record order. The file blob IS a record-order concatenation, so
# splicing bigger uncompressed data in "looks" right offline (436/446 records decode) — BUT IN-GAME it
# FAILS: enlarging one texture shifts the cursor for every texture after it and the whole pack renders
# garbage (confirmed by in-game test, 2026-07-07). So this convert is DISABLED; sequential packs only get
# same-dimensions/same-format (DXT) in-place edits (replace_many/smart_replace_record force prefer_lossless
# =False for them). Leave False — the splice desyncs the loader.
CONVERT_SEQUENTIAL = False


def _sequential_records(fdram, ftex):
    """Enumerate a sequential loader-placed pack's records in DRAM order with their record-order blob
    position (cumulative footprint). Returns [recs] (each + base, pos) or [] if the pack isn't a clean
    record-order sequential blob (majority of records must decode coherently at their cumulative pos)."""
    recs = []; b = 0; cum = 0
    while b + 0xE0 <= len(fdram):
        r = _parse_multi_rec(fdram, b)
        if r is not None and r["fetch"]:
            recs.append(dict(r, base=b, pos=cum)); cum += r["foot"]; b += 0xE0
        else:
            b += 4
    if len(recs) < 8:
        return []
    # validate: are these placeholder-offset records (+0x6c<=1) laid out sequentially? sample-decode.
    placeholder = sum(1 for r in recs if _BE(fdram, r["base"] + 0x6C) <= 1)
    if placeholder < len(recs) * 0.8:                  # not a placeholder/sequential pack
        return []
    ok = 0; checked = 0
    for r in recs[::max(1, len(recs) // 24)]:          # sample ~24 records across the pack
        seg = ftex[r["pos"]:r["pos"] + r["mip0"]]
        if len(seg) < r["mip0"]:
            break
        checked += 1
        try:
            import numpy as _np
            a = _np.asarray(T.decode(_dxt_endian(seg, r["fmt"]), r["w"], r["h"], r["fmt"], r["bpu"], r["block"], r["tiled"], 0).convert("RGB"))
            if a.var() > 20:
                ok += 1
        except Exception:
            pass
    return recs if (checked and ok >= checked * 0.7) else []


def _is_sequential_pack(iff, clean_dir=None):
    """True if `iff` is a sequential loader-placed pack (global.iff-style) convertible by splice."""
    try:
        loc, data, size = _read_asset(iff, clean_dir)
        if loc is None:
            return False
        blobs = [b["dec"] for b in _walk_blobs(data, size) if b["dec"]]
        if len(blobs) < 2:
            return False
        return bool(_sequential_records(min(blobs, key=len), max(blobs, key=len)))
    except Exception:
        return False


def _repack_scatter(iff, arc, off, idx, res, size, tex_b, items, game_dir, new_fmt, log):
    """Rebuild a scatter pack's VRAM blob with the edited textures stored as `new_fmt` (uncompressed),
    recomputing all group-relative offsets. Copies every UNedited texture's original bytes verbatim
    (lossless) and substitutes freshly-encoded bytes for the edited ones. Self-validates the new
    group layout before writing. Returns a status string, or None if the pack can't be repacked
    (records don't enumerate cleanly / footprints don't fill the blob / an edit isn't matched)."""
    if not REPACK_SCATTER:
        return None
    blobs = _walk_blobs(res, size)
    if len(blobs) < 2 or tex_b["off"] != blobs[-1]["off"]:
        return None
    dram_b = blobs[0]
    dram = bytearray(dram_b["dec"]); tex = bytes(tex_b["dec"])

    # 1) enumerate every scatter record in DRAM-base order (base, dims, foot, original group-rel)
    seq = []; b = 0
    while b + 0xE0 <= len(dram):
        r = _parse_multi_rec(dram, b)
        if r is not None and r["fetch"]:
            vv = _BE(dram, b + 0x6C); flags = _BE(dram, b + 0x5C)
            align = 0x10 if (flags & 0xC0000000) == 0xC0000000 else 0x1000
            rel = 0 if vv <= 1 else ((vv - 1) // align) * align
            seq.append(dict(r, base=b, rel0=rel)); b += 0xE0
        else:
            b += 4
    if len(seq) < 2:
        return None
    # original cumulative offsets (DRAM-base order == packing order) must fill the blob exactly
    cum = 0
    for r in seq:
        r["cum0"] = cum; cum += r["foot"]
    if cum != len(tex):
        return None

    # 2) match each edit (by resolved VRAM offset + dims) to its record
    edits_by = {}
    for (e, img, _me) in items:
        want = e["vram_off"]
        hit = next((r for r in seq if r["cum0"] == want and r["w"] == e["w"] and r["h"] == e["h"]), None)
        if hit is None:
            return None
        edits_by[hit["base"]] = (e, img)

    # 3) rebuild the blob + recompute group-relative offsets
    new_tex = bytearray(); group_base = 0; nconv = 0
    for r in seq:
        if r["rel0"] == 0:                                   # a group starts here
            group_base = len(new_tex)
        new_rel = len(new_tex) - group_base
        if new_rel % 0x1000:                                 # keep records page-aligned (they always are)
            pad = 0x1000 - (new_rel % 0x1000); new_tex += b"\x00" * pad; new_rel += pad
        if r["base"] in edits_by:                            # edited -> store uncompressed
            e, img = edits_by[r["base"]]
            tgt = _lossless_target(r["fmt"]) or new_fmt      # colour DXT->8888, DXN->8_8
            if img.size != (r["w"], r["h"]):
                img = img.resize((r["w"], r["h"]), Image.LANCZOS)
            data = _encode_tiled(img, tgt, 1)                # mip0 only (scatter textures ship mip0-only)
            mip0 = _mip0_size(tgt, r["w"], r["h"]); tail = len(data) - mip0
            struct.pack_into(">I", dram, r["base"] + 0x70, mip0)
            struct.pack_into(">I", dram, r["base"] + 0x74, tail)
            for o2 in (0x08, 0x0C, 0x1C):
                struct.pack_into(">I", dram, r["base"] + o2, _FMT_DESCRIPTOR[tgt])
            f1 = _BE(dram, r["base"] + 0x98); struct.pack_into(">I", dram, r["base"] + 0x98, (f1 & ~0xFFF) | _FMT_F1_LOW[tgt])
            f3 = _BE(dram, r["base"] + 0xA0); struct.pack_into(">I", dram, r["base"] + 0xA0, (f3 & ~0xFFF) | _FMT_F3_LOW[tgt])
            struct.pack_into(">I", dram, r["base"] + 0xA8, (mip0 & ~0xFFF) | 0xA00)
            nconv += 1
        else:                                                # unchanged -> copy original bytes verbatim
            data = tex[r["cum0"]:r["cum0"] + r["foot"]]
        struct.pack_into(">I", dram, r["base"] + 0x6C, new_rel + 1)   # group-relative offset (+1 encoded)
        new_tex += data
    if not nconv:
        return None

    # 4) self-validate the new layout the way _scatter_records does (base order, group-rel consistent)
    v2 = 0; gb = 0
    for r in seq:
        vv = _BE(dram, r["base"] + 0x6C); flags = _BE(dram, r["base"] + 0x5C)
        align = 0x10 if (flags & 0xC0000000) == 0xC0000000 else 0x1000
        rel = 0 if vv <= 1 else ((vv - 1) // align) * align
        foot = _BE(dram, r["base"] + 0x70) + _BE(dram, r["base"] + 0x74)
        if rel == 0:
            gb = v2
        if v2 - gb != rel:
            log(f"  scatter repack self-check FAILED (rec@0x{r['base']:X}: {v2-gb:#x} != rel {rel:#x}) — aborted")
            return None
        v2 += foot
    if v2 != len(new_tex):
        log(f"  scatter repack self-check FAILED (blob {v2:#x} != {len(new_tex):#x}) — aborted")
        return None

    # 5) encode both blobs, patch section sizes, relocate (same recipe as _grow_many)
    dram_c = EE.encode_payload(bytes(dram), wparam=dram_b["wp"], codec=dram_b["codec"])
    tex_c = EE.encode_payload(bytes(new_tex), wparam=tex_b["wp"], codec=tex_b["codec"])
    for blob_c, dec in ((dram_c, bytes(dram)), (tex_c, bytes(new_tex))):
        ve = _verify_blob(blob_c, dec)
        if ve:
            raise ValueError(f"{iff}: {ve} — scatter repack aborted, game files untouched")
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + tex_c
    _patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    _patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(tex_c), dec_size=len(new_tex))
    struct.pack_into(">I", new_res, 8, len(new_res))
    _backup_once(game_dir / arc, log)
    e0 = items[0][0]
    log(f"  {iff}: SCATTER REPACK — {nconv} texture(s) -> {new_fmt}, blob {len(tex)}->{len(new_tex)} "
        f"(group offsets recomputed; VERIFY the arena presentation renders correctly in-game)")
    return _relocate(iff, bytes(new_res), idx, game_dir, e0["w"], e0["h"], new_fmt, log)


def _whole_pack_relocate_vram(iff, res, size, vb, new_blob, idx, game_dir, log):
    """Relocate the ENTIRE resource with the VRAM blob recompressed to `new_blob` — same
    UNCOMPRESSED texture layout (same-dim edits only), so no fetch/record offset shifts — while
    keeping blob0 (e.g. the scoreclock scene/layout) byte-verbatim. Mirrors overlay_editor.apply_dram
    but edits the texture blob instead of the scene, so a bigger-compressing edit doesn't have to be
    posterized when the in-place slot is too small. Safe ONLY for relocatable, non-sequential packs
    whose VRAM blob is the LAST 0E4837 blob. Returns a status string, or None if it can't be done."""
    vo = vb["off"]
    blobs = _walk_blobs(res, size)
    if not blobs or blobs[-1]["off"] != vo:                # VRAM blob must be LAST (else offsets shift)
        return None
    # header + small sections + blob0 (all verbatim) + the recompressed edited blob1. Trailing bytes
    # after blob1 (dead VRAM tail: section dec-size == blob1 dec) are dropped, exactly as apply_dram does.
    new_res = bytearray(res[:vo]) + bytearray(new_blob)
    hdr = _BE(new_res, 4); p = 0x20; sectype = None       # find the VRAM section entry (dataoff == vo)
    while p + 0x20 <= hdr:
        t = _BE(new_res, p)
        if t == 0:
            break
        if _BE(new_res, p + 0x14) == vo:
            sectype = t; break
        p += 0x20
    if sectype is None:
        return None
    _patch_iff_section(new_res, sectype, vo, len(new_blob), dec_size=len(vb["dec"]))
    struct.pack_into(">I", new_res, 8, len(new_res))       # total resource size
    return _relocate(iff, bytes(new_res), idx, game_dir, 0, 0, "DXT4_5", log)


def replace_many(iff, edits, game_dir, log=print, prefer_lossless=True) -> str:
    """Apply MANY sub-texture edits to one multi-texture asset in a SINGLE re-encode — splice
    every edit into the one decompressed VRAM blob, then compress ONCE (N separate replace_at
    calls would re-read + re-decompress + re-compress the whole blob N times). Prefers in-place;
    if the edits don't fit, tries a SAFE relocate-grow (full quality, no posterize) on stored-
    offset packs via _grow_many, else falls back to posterize-to-fit, else refuses.
    edits = [{vram_off, w, h, fmt, tiled, path, foot?}]. Returns a status string."""
    game_dir = Path(game_dir)
    if iff in PORTRAIT_PACKS:                             # portrait pack -> per-blob portrait replace
        return replace_portraits(iff, [{"index": e["index"], "path": e["path"]} for e in edits], game_dir, log)
    if iff in BUNDLE_PACKS:                               # frontend branding bundle -> in-place tiles
        return replace_bundles(iff, [{"index": e["index"], "path": e["path"]} for e in edits], game_dir, log)
    # Front-end jersey decal sheets carry FIXED offsets supplied by fe_components.json (the resource
    # has no texture-record tree), so the two record-rewriting growth paths are off the table: no
    # lossless format upgrade and no _grow_many (there is no +0x6C to redirect). Everything else --
    # the single-pass splice, and crucially the WHOLE-PACK RELOCATE fallback -- applies normally,
    # because that path keeps the uncompressed layout byte-for-byte identical and only moves the
    # resource. This used to loop replace_at instead, which predates both growth paths and is
    # therefore in-place-ONLY: a team whose stock sheet has no compression slack (ANA/SEA/VGK ship a
    # 774,848-byte resource; VAN ships 945,164) failed outright on art that VAN swallowed.
    fe = fe_component(iff) is not None
    if fe:
        prefer_lossless = False
    # SEQUENTIAL loader-placed packs (global.iff, gamedata.iff): the game streams these to VRAM at a
    # runtime cursor in record order, so RESIZING or format-CONVERTING any one texture (8888/grow) shifts
    # the cursor for every texture after it and the whole pack desyncs in-game (confirmed: global.iff
    # convert breaks every image). ONLY same-dimensions, same-format (DXT) in-place is safe -> force it.
    if _is_sequential_pack(iff, game_dir):
        prefer_lossless = False
    # uniform_base_*.iff textures carry LOAD-assigned offsets (+0x6C=1), so relocating/growing them
    # (e.g. the DXN base normal -> 8_8) would corrupt the pack. Replace them same-dim, same-format,
    # IN-PLACE only — exactly how the overlay uniform normal is edited.
    if re.match(r"uniform_base_", str(iff)):
        prefer_lossless = False
    # player_head_id_*.iff is the same shape: three surfaces packed back-to-back with +0x6C==1 and
    # ZERO slack (393216+393216+196608 == the whole 983,040-byte blob). Any resize or format upgrade
    # would push the loader's cursor and desync the normal + occlusion maps -> same-dim, in-place only.
    if _player_head_id(str(iff)):
        prefer_lossless = False
    loc = resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    vb = _big_vram_blob(res, size)
    if not vb:
        raise ValueError(f"{iff}: no VRAM blob")
    ref = _clean_ref_blob(iff, 0)
    foot_by_off = {}
    try:
        for _r in _texture_tree(res, size)[1]:
            if _r.get("foot"):
                foot_by_off[_r["vram_off"]] = _r["foot"]
    except Exception:
        pass
    items = []
    for e in edits:
        if e["fmt"] not in REPLACE_FORMATS or not e.get("tiled", 1):
            log(f"  skip t#{e.get('index', '?')} ({e['fmt']}) — unsupported format"); continue
        img = Image.open(e["path"]).convert("RGBA")
        if img.size != (e["w"], e["h"]):
            img = img.resize((e["w"], e["h"]), Image.LANCZOS)
        # Guard: some tiled block textures (notably scatter DXT whose height gto-tiles to a PADDED
        # footprint, e.g. 256x64 DXT4_5 -> 30720B) can't be written into their stored mip0 slot
        # (16384B) — the encode overruns it and _rebuild_with_mips raises. Rather than abort the
        # WHOLE Apply-All on one such texture, skip just it (extract/view-only) so the rest apply.
        _exp = _mip0_size(e["fmt"], e["w"], e["h"])
        _pt = _dds_passthrough_mip0(e["path"], e["fmt"], e["w"], e["h"])
        _fits = (_pt is not None and len(_pt) == _exp)
        if not _fits:
            try:
                _fits = (len(_encode_tiled(img, e["fmt"], e.get("tiled", 1))) == _exp)
            except Exception:
                _fits = False
        if not _fits:
            log(f"  skip t#{e.get('index', '?')} ({e['w']}x{e['h']} {e['fmt']}"
                f"{', ' + e['packing'] if e.get('packing') else ''}) — tiled footprint doesn't fit "
                f"its stored slot; not replaceable (extract/view only). Other edits still apply.")
            continue
        foot = e.get("foot") or foot_by_off.get(e["vram_off"])
        items.append((e, img, (e["vram_off"] + foot) if foot else None))
    if not items:
        raise ValueError(f"{iff}: no applicable modified textures to apply")

    # LOSSLESS-first: if any edit is a compressed format we can upgrade (colour DXT->8888, DXN->8_8)
    # and the pack can grow, store everything via the relocate-grow path (no block artifacts). Falls
    # through to the in-place / posterize path when the pack can't grow (loader-repacked / non-last blob).
    if prefer_lossless and any(_lossless_target(e["fmt"]) for (e, _i, _m) in items):
        try:
            grown = _grow_many(iff, arc, off, idx, res, size, vb, items, game_dir, log,
                               prefer_lossless=True)
        except Exception as ge:
            log(f"  lossless relocate-grow aborted ({ge}); trying in-place"); grown = None
        if grown:
            return grown

    # SCATTER packs (arena_presentation) can't grow-append, but they CAN be fully repacked (rebuild the
    # blob + recompute every group-relative offset) to store uncompressed — try that before in-place DXT.
    if prefer_lossless and _is_scatter_pack(iff) and any(_lossless_target(e["fmt"]) for (e, _i, _m) in items):
        try:
            packed = _repack_scatter(iff, arc, off, idx, res, size, vb, items, game_dir, "8888", log)
        except Exception as pe:
            log(f"  scatter repack aborted ({pe}); trying in-place"); packed = None
        if packed:
            return packed

    def _encode(levels):
        cur = vb["dec"]
        for (e, img, mip_end) in items:
            src = _posterize(img, levels) if levels else img
            pt = _dds_passthrough_mip0(e["path"], e["fmt"], e["w"], e["h"])
            cur = _rebuild_with_mips(cur, e["vram_off"], e["fmt"], e["w"], e["h"],
                                     e.get("tiled", 1), src, (lambda *a: None),
                                     ref_dec=ref, mip_end=mip_end, pt_mip0=pt)
            if len(cur) != len(vb["dec"]):
                raise ValueError(f"{iff}: VRAM size changed (t#{e.get('index', '?')} out of range)")
        nb = EE.encode_payload(cur, wparam=vb["wp"], codec=vb["codec"])    # native window
        err = _verify_blob(nb, cur)
        if err:
            raise ValueError(f"{iff}: {err} — replace aborted, game files untouched")
        return nb

    old_tot = vb["tot"]; vo = vb["off"]
    new_blob = _encode(0); used = 0
    if len(new_blob) > old_tot:
        # Too big for the in-place slot. First try a SAFE relocate-grow — append the edits to the
        # texture-blob end + redirect their records, then relocate the resource (append to 1B +
        # repoint TOC). Keeps FULL quality (NO posterize) for stored-offset packs
        # (rink/led/uniform/scene/…). Returns None for packs that can't grow safely — loader-
        # repacked like global.iff, or a non-last big blob like overlay_static -> posterize-to-fit.
        grown = None
        # player heads are loader-placed (+0x6C==1): a redirected record is IGNORED, the surface is
        # streamed from the cursor, so a grow silently leaves the stock texture in the game and
        # inflates the pack. Their only safe overflow path is the whole-pack relocate below, which
        # keeps the uncompressed layout byte-for-byte and only moves the resource.
        if not fe and not _player_head_id(str(iff)):       # FE sheets have no records to redirect
            try:
                grown = _grow_many(iff, arc, off, idx, res, size, vb, items, game_dir, log)
            except Exception as ge:
                log(f"  relocate-grow aborted ({ge}); falling back to posterize"); grown = None
        if grown:
            return grown
        # WHOLE-PACK RELOCATE (full quality, no posterize): the edited blob1 keeps the SAME
        # uncompressed texture layout (same-dim edits) — only its COMPRESSED size outgrew the
        # in-place slot. For a relocatable, non-sequential pack whose VRAM blob is LAST
        # (overlay_static scorebug), relocate the WHOLE resource with the recompressed blob1 while
        # keeping blob0 (scoreclock layout) verbatim — instead of degrading quality via posterize.
        if not _is_sequential_pack(iff, game_dir):
            try:
                reloc = _whole_pack_relocate_vram(iff, res, size, vb, new_blob, idx, game_dir, log)
            except Exception as we:
                log(f"  whole-pack relocate aborted ({we}); falling back to posterize"); reloc = None
            if reloc:
                return (f"RELOCATED (full quality): {iff} — {len(items)} texture(s) applied, VRAM blob "
                        f"{len(new_blob)} bytes (was {old_tot}); blob0/layout preserved. {reloc}")
        log(f"  {len(items)} edit(s) {len(new_blob) - old_tot} bytes over the in-place slot — "
            f"can't relocate this pack; auto-fitting (posterize)…")
        for lv in (64, 32, 16):
            new_blob = _encode(lv); used = lv
            if len(new_blob) <= old_tot:
                break
    if len(new_blob) > old_tot:
        raise ValueError(
            f"{iff}: the {len(items)} edits don't fit in place (+{len(new_blob) - old_tot} bytes) "
            f"even after posterizing, and this pack can't be relocated. Apply the heaviest "
            f"texture(s) individually, or simplify them. (In-place only here.)")
    new_res = bytearray(res)
    new_res[vo:vo + old_tot] = new_blob + b"\x00" * (old_tot - len(new_blob))
    _backup_once(game_dir / arc, log)
    with open(game_dir / arc, "r+b") as f:
        f.seek(off); f.write(new_res)
    tag = f", posterized {used}lvl" if used else ""
    return (f"IN-PLACE: {iff} — {len(items)} textures applied in ONE pass "
            f"(blob {len(new_blob)}/{old_tot} bytes{tag})")


def replace_chain(iff, fmt, chain, edited_path, game_dir, log=print) -> str:
    """Replace mip0 AND every following mip level (chain = [(off,w,h),...], mip0 first)
    from the edited image, so the GPU never blends in original mips."""
    game_dir = Path(game_dir)
    loc = resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    vb = _big_vram_blob(res, size)
    if not vb:
        raise ValueError(f"{iff}: no VRAM blob")
    bpu = _DXT_BPU[fmt]
    img = Image.open(edited_path).convert("RGBA")
    m0_off, w, h = chain[0]
    if img.size != (w, h):
        if img.size[0] * h != img.size[1] * w:
            log(f"  WARNING {iff}: source aspect {img.size} != {w}x{h} — it will be stretched")
        log(f"  {iff}: fitting source {img.size} -> {w}x{h} (LANCZOS downscale)")
        img = img.resize((w, h), Image.LANCZOS)
    new_dec = bytearray(vb["dec"])
    premf = _orig_is_premult(vb["dec"], m0_off, w, h, fmt, 1)   # only premult truly-premult art
    for (mo, mw, mh) in chain:
        mimg = img if (mw, mh) == (w, h) else img.resize((mw, mh), Image.LANCZOS)
        mt = _encode_tiled(mimg, fmt, premultiply=premf)
        msize = (mw // 4) * (mh // 4) * bpu
        if len(mt) != msize:
            raise ValueError(f"{iff}: mip {mw}x{mh} encoded {len(mt)} != {msize}")
        if mo + msize > len(new_dec):
            raise ValueError(f"{iff}: mip {mw}x{mh} @0x{mo:X} out of VRAM")
        new_dec[mo:mo + msize] = mt
    new_blob = EE.encode_payload(bytes(new_dec), wparam=vb["wp"], codec=vb["codec"])  # native window
    err = _verify_blob(new_blob, bytes(new_dec))
    if err:
        raise ValueError(f"{iff}: {err} — replace aborted, game files untouched")
    vo = vb["off"]; old_tot = vb["tot"]
    if len(new_blob) <= old_tot:
        new_res = bytearray(res)
        new_res[vo:vo + old_tot] = new_blob + b"\x00" * (old_tot - len(new_blob))
        _backup_once(game_dir / arc, log)
        with open(game_dir / arc, "r+b") as f:
            f.seek(off); f.write(new_res)
        return (f"IN-PLACE: {iff} ({len(chain)} mip levels) -> {arc}:0x{off:X} "
                f"(blob {len(new_blob)}/{old_tot}, {w}x{h} {fmt})")
    new_res = bytearray(res[:vo] + new_blob + res[vo + old_tot:])
    _patch_grown_iff(new_res, vo, len(new_blob))       # fix IFF total_size + section size (grown blob)
    return _relocate(iff, bytes(new_res), idx, game_dir, w, h, fmt, log)


# ── HIGHER-RESOLUTION redirect (scene assets) ────────────────────────────────
def _patch_iff_section(res: bytearray, sectype: int, dataoff: int, size: int, dec_size=None) -> bool:
    """Set a section-table entry by section TYPE (0xBB05A9C1 DRAM, 0x411536D5 VRAM). Entries
    at 0x20, stride 0x20: type@+0, **dec/alloc size@+0xC**, dataoff@+0x14, compressed size@+0x18.
    The game allocates the section buffer from +0xC, so growing a blob MUST bump it too."""
    hdr = _BE(res, 4); p = 0x20
    while p + 0x20 <= hdr:
        if _BE(res, p) == sectype:
            if dec_size is not None:
                struct.pack_into(">I", res, p + 0xC, dec_size)   # decompressed/allocation size
            struct.pack_into(">I", res, p + 0x14, dataoff)
            struct.pack_into(">I", res, p + 0x18, size)          # compressed size in the file
            return True
        if _BE(res, p) == 0:
            break
        p += 0x20
    return False


def _find_scene_record(dram: bytes, cur_off: int):
    """Find the 0xE0 texture record whose stored VRAM offset (@+0x6c) == cur_off (the scene
    asset's current offset). The embedded fetch sits at record+0x94. Returns rec base or None."""
    for o in range(0, len(dram) - 24, 4):
        d0, d1, d2 = struct.unpack_from(">III", dram, o)
        if (d0 & 3) == 2 and (d1 & 0x3F) in _FETCH_FMT:
            rec = o - 0x94
            if 0 <= rec and rec + 0xE0 <= len(dram) and (_BE(dram, rec + 0x6C) - 1) == cur_off:
                return rec
    return None


def _scene_rec_base(host: str, cur_off: int, game_dram_len: int):
    """Locate a scene texture record base AFTER it's already been hi-res-redirected (its +0x6c no
    longer == cur_off). The PRISTINE CLEAN dram always has the record at cur_off, and the
    decompressed DRAM record layout is identical across redirects (only field VALUES change), so
    the rec base from CLEAN matches the game's. Returns rec base or None."""
    loc = resolve(host, None, clean=True)
    if not loc:
        return None
    arc, off, size, idx, f3 = loc
    with open(_arc_file(None, arc, clean=True), "rb") as f:
        f.seek(off); data = f.read(size + 0x400000)
    blobs = _walk_blobs(data, size)
    if not blobs or not blobs[0]["dec"]:
        return None
    cdram = blobs[0]["dec"]
    if len(cdram) != game_dram_len:                    # structure must match to reuse the base
        return None
    return _find_scene_record(cdram, cur_off)


def _compute_fetch_ext(nw: int, nh: int, fmt: str):
    """Compute the size-dependent fetch dwords d4(+0xA4)/d5(+0xA8) for a (nw,nh) tiled DXT texture
    when no native same-size template exists in the iff (needed for 2048+ scene hi-res). Derived
    from the Xenos fetch constant (rexglue xenos.h):
      d4: mip_max_level (bits 6-9) = log2(max(nw,nh)); low 0x03 = vol_mag/min_filter (as native).
      d5: mip_address (bits 12-31) = mip0_size>>12 (mips start mip0_size after base, ENGINE-relative
          like the template's — it's relocated at bind); | packed_mips(0x800) | dimension k2D(0x200).
    VERIFIED to reproduce native exactly: 256x128->(0x203,0x8A00), 1024x512->(0x283,0x80A00)."""
    bpu = _FMT_BPU.get(fmt, 16)
    mip0 = (nw // 4) * (nh // 4) * bpu
    mip_max = max(nw, nh).bit_length() - 1                 # log2 of the larger dimension
    d4 = (mip_max << 6) | 0x03
    d5 = (mip0 & ~0xFFF) | 0xA00                           # mip0_size(4KB-aligned) | packed | k2D
    return d4, d5


def _find_ref_template(dram: bytes, nw: int, nh: int, fmt_code: int, skip_rec: int = -1):
    """Find another 0xE0 record in the SAME dram whose embedded fetch already describes a texture
    of (nw,nh) in the same pixel format, and return its size-dependent fetch extension dwords
    (d4 @+0xA4, d5-template @+0xA8). When we upscale a texture we must NOT keep the old (smaller)
    size's d4/d5 — they encode size-dependent fields (verified: titlepage logo 256x128 d4=0x203/
    d5tmpl=0x8A00 vs the game's own 1024x512 EULA d4=0x283/d5tmpl=0x80A00). Copying a real same-
    size texture's template makes the redirected texture set up EXACTLY like a native one of that
    resolution (incl. that texture-class's mip behaviour). Returns (d4, d5tmpl) or None."""
    for o in range(0, len(dram) - 24, 4):
        d0, d1, d2 = struct.unpack_from(">III", dram, o)
        if (d0 & 3) != 2 or (d1 & 0x3F) != fmt_code:
            continue
        rec = o - 0x94
        if rec == skip_rec or rec < 0 or rec + 0xE0 > len(dram):
            continue
        if ((d2 & 0x1FFF) + 1) == nw and (((d2 >> 13) & 0x1FFF) + 1) == nh:
            return _BE(dram, rec + 0xA4), _BE(dram, rec + 0xA8)
    return None


def replace_scene_hires(scene_name: str, edited_path, game_dir, scale: int = 4, log=print) -> str:
    """HIGHER-RES replace for a scene asset (titlepage_*): encode the edited image at scale×
    the original size, append it to a NEW spot at the end of the VRAM blob, and patch the
    texture record's dims/pitch/offset/sizes to point there (menu UVs are normalised, so a
    bigger texture renders sharper at the same on-screen size). Grows the .iff -> relocate."""
    if scene_name not in SCENE_ASSETS:
        raise ValueError(f"{scene_name}: not a scene asset")
    host, cur_off, ow, oh, fmt = SCENE_ASSETS[scene_name]
    if fmt != "DXT4_5":
        raise ValueError(f"{scene_name}: hi-res currently supports DXT4_5 scene assets only")
    NW, NH = ow * scale, oh * scale
    game_dir = Path(game_dir)
    loc = resolve(host, game_dir)
    if not loc:
        raise ValueError(f"{host}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = _walk_blobs(res, size)
    if not blobs:
        raise ValueError(f"{host}: no blobs")
    dram_b = blobs[0]; vram_b = max(blobs, key=lambda b: len(b["dec"]) if b["dec"] else 0)
    dram = bytearray(dram_b["dec"]); vram = bytearray(vram_b["dec"])
    rec = _find_scene_record(bytes(dram), cur_off)
    if rec is None:                                    # already hi-res'd: find via pristine layout
        rec = _scene_rec_base(host, cur_off, len(dram))
        if rec is not None:
            log(f"  {scene_name}: already hi-res — re-located record via pristine layout (rec @0x{rec:X})")
    if rec is None:
        raise ValueError(f"{scene_name}: texture record (@+0x6c==0x{cur_off:X}) not found")
    # Detect the ORIGINAL scene texture's alpha mode from the pristine CLEAN copy (premultiplied
    # vs straight) so the upscaled version matches it (logos=premult, cover/EULA=straight).
    premf = False
    if fmt in _PREMULT_FMTS:
        try:
            cl = resolve(host, None, clean=True)
            with open(_arc_file(None, cl[0], clean=True), "rb") as cf:
                cf.seek(cl[1]); cdata = cf.read(cl[2] + 0x800000)
            cvb = _big_vram_blob(cdata, cl[2])
            premf = _orig_is_premult(cvb["dec"], cur_off, ow, oh, fmt, 1)
        except Exception:
            premf = False
    log(f"  {scene_name}: original alpha = {'premultiplied' if premf else 'straight'}")
    img = Image.open(edited_path).convert("RGBA")
    if img.size != (NW, NH):
        img = img.resize((NW, NH), Image.LANCZOS)
    mip_src = _premult_pil(img) if premf else img      # premult textures downscale in premult space
    chain = bytearray(); mw, mh = NW, NH
    while mw >= 4 and mh >= 4:
        lvl = mip_src.resize((mw, mh), Image.BOX if premf else Image.LANCZOS).convert("RGBA")
        chain += (_encode_tiled(lvl, fmt, 1, premultiply=False, alpha_aware=False) if premf
                  else _encode_tiled(lvl, fmt, 1))
        mw //= 2; mh //= 2
    mip0 = _mip0_size(fmt, NW, NH); tail = len(chain) - mip0
    new_off = (len(vram) + 0xFFF) & ~0xFFF              # align the new spot to a page
    vram += b"\x00" * (new_off - len(vram)) + chain
    struct.pack_into(">H", dram, rec + 0x60, NW); struct.pack_into(">H", dram, rec + 0x62, NH)
    struct.pack_into(">I", dram, rec + 0x94, 0x80000000 | ((NW // 32) << 22) | 0x48FE)  # tiled DXT4_5
    struct.pack_into(">I", dram, rec + 0x9C, (NW - 1) | ((NH - 1) << 13))
    struct.pack_into(">I", dram, rec + 0x6C, new_off + 1)
    struct.pack_into(">I", dram, rec + 0x70, mip0); struct.pack_into(">I", dram, rec + 0x74, tail)
    # Fix the size-dependent fetch-extension dwords d4(+0xA4)/d5-template(+0xA8): the upscaled
    # record still carries the ORIGINAL (smaller) size's values, which is wrong. Copy them from a
    # real same-(NW,NH,fmt) texture so the redirect is set up exactly like a native texture of the
    # new resolution (this also inherits that texture-class's mip_max_level — UI textures = 0, i.e.
    # mip0-only, so a MINIFIED upscaled UI element will still alias: enabling mips needs the Xenos
    # packed-mip layout, still open. The redirect is sharp for textures drawn at/above native size).
    fmt_code = _BE(dram, rec + 0x98) & 0x3F
    ref_tmpl = _find_ref_template(bytes(dram), NW, NH, fmt_code, skip_rec=rec)
    if ref_tmpl:
        struct.pack_into(">I", dram, rec + 0xA4, ref_tmpl[0])
        struct.pack_into(">I", dram, rec + 0xA8, ref_tmpl[1])
        log(f"  fetch d4/d5 template <- native {NW}x{NH} ref (d4=0x{ref_tmpl[0]:X} d5=0x{ref_tmpl[1]:X})")
    else:
        d4, d5 = _compute_fetch_ext(NW, NH, fmt)          # no native template (e.g. 2048) -> compute
        struct.pack_into(">I", dram, rec + 0xA4, d4)
        struct.pack_into(">I", dram, rec + 0xA8, d5)
        log(f"  fetch d4/d5 COMPUTED for {NW}x{NH} (no native ref): d4=0x{d4:X} d5=0x{d5:X}")
    dram_c = EE.encode_payload(bytes(dram)); vram_c = EE.encode_payload(bytes(vram))
    for blob_c, dec in ((dram_c, bytes(dram)), (vram_c, bytes(vram))):
        err = _verify_blob(blob_c, dec)
        if err:
            raise ValueError(f"{scene_name}: {err} — hi-res aborted, game files untouched")
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + vram_c
    _patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    _patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(vram_c), dec_size=len(vram))
    struct.pack_into(">I", new_res, 8, len(new_res))    # IFF total_size
    _backup_once(game_dir / arc, log)
    log(f"  {scene_name}: hi-res {ow}x{oh} -> {NW}x{NH} (new spot @VRAM 0x{new_off:X})")
    return _relocate(host, bytes(new_res), idx, game_dir, NW, NH, fmt, log)


def _find_multitex_rec(dram: bytes, vram_off: int, align: int = 0x1000, w=None, h=None):
    """Find the 0xE0 texture record in a multi-texture pack whose stored offset (@+0x6c, aligned)
    resolves to vram_off. When w/h are given, ALSO require the record's dims (@+0x60/+0x62) to
    match — essential to disambiguate: several records can carry a placeholder +0x6c that resolves
    to the same (often 0) offset, so an offset-only match grabs a coincidental record (e.g. a 1x0
    stub). Returns rec base or None."""
    for o in range(0, len(dram) - 24, 4):
        d0, d1, d2 = struct.unpack_from(">III", dram, o)
        if (d0 & 3) == 2 and (d1 & 0x3F) in _FETCH_FMT:
            rec = o - 0x94
            if 0 <= rec and rec + 0xE0 <= len(dram):
                if w is not None:                       # dims filter (disambiguate)
                    rw = struct.unpack_from(">H", dram, rec + 0x60)[0]
                    rh = struct.unpack_from(">H", dram, rec + 0x62)[0]
                    if rw != w or rh != h:
                        continue
                v = struct.unpack_from(">I", dram, rec + 0x6C)[0]
                if v and ((v - 1) // align) * align == vram_off:
                    return rec
    return None


def replace_multitex_grow(iff, vram_off, w, h, fmt, edited_path, game_dir, scale: int = 1, log=print) -> str:
    """Replace a sub-texture of a MULTI-TEXTURE pack (overlay_static, HUD atlases, …) by APPENDING
    the new texture to the END of the texture blob, redirecting that record's stored offset (+0x6c)
    to it, growing the blob (patching the section alloc-size so the engine allocates the bigger
    buffer), then RELOCATING the iff. This frees the in-place size cap — the edit no longer has to
    compress back into its original slot. Other sub-textures are untouched (the old slot becomes
    dead space). scale>1 also upscales (WARNING: a HUD widget's on-screen size may follow texture
    dims, so a bigger texture can render larger / mis-positioned — use scale=1 for HUD textures
    unless verified otherwise; scene/UI logos with normalised UVs are fine via replace_scene_hires)."""
    if fmt not in ("DXT4_5", "DXT5"):
        raise ValueError(f"{iff}: multitex grow currently supports DXT4_5 only")
    game_dir = Path(game_dir)
    loc = resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = _walk_blobs(res, size)
    dram_b = blobs[0]
    tex_b = _big_vram_blob(res, size)
    if not tex_b or tex_b["off"] != blobs[-1]["off"]:    # compare by offset (different _walk_blobs call)
        raise ValueError(f"{iff}: texture blob isn't the last blob — unsupported layout for grow")
    dram = bytearray(dram_b["dec"]); tex = bytearray(tex_b["dec"])
    rec = _find_multitex_rec(bytes(dram), vram_off, w=w, h=h)
    if rec is None:
        raise ValueError(f"{iff}: texture record for VRAM 0x{vram_off:X} ({w}x{h}) not found")

    NW, NH = w * scale, h * scale
    img = Image.open(edited_path).convert("RGBA")
    if img.size != (NW, NH):
        img = img.resize((NW, NH), Image.LANCZOS)
    premf = _orig_is_premult(tex_b["dec"], vram_off, w, h, fmt, 1)   # detect from the original
    mip_src = _premult_pil(img) if premf else img
    pt = _dds_passthrough_mip0(edited_path, fmt, NW, NH)   # pre-compressed DDS -> no re-encode
    chain = bytearray(pt if pt else _encode_tiled(img, fmt, 1, premultiply=premf))   # mip0
    if pt:
        log("  mip0: DDS passthrough (already-compressed blocks)")
    mw, mh = NW // 2, NH // 2
    while mw >= 4 and mh >= 4:                             # premult-space BOX mips (clean edges)
        lvl = mip_src.resize((mw, mh), Image.BOX).convert("RGBA")
        chain += (_encode_tiled(lvl, fmt, 1, premultiply=False, alpha_aware=False) if premf
                  else _encode_tiled(lvl, fmt, 1))
        mw //= 2; mh //= 2
    mip0 = _mip0_size(fmt, NW, NH); tail = len(chain) - mip0

    new_off = (len(tex) + 0xFFF) & ~0xFFF                 # page-aligned append (record offset align)
    tex += b"\x00" * (new_off - len(tex)) + chain
    struct.pack_into(">I", dram, rec + 0x6C, new_off + 1)            # redirect the stored offset
    struct.pack_into(">I", dram, rec + 0x70, mip0)
    struct.pack_into(">I", dram, rec + 0x74, tail)
    if scale > 1:                                          # upscale -> also patch dims + fetch
        struct.pack_into(">H", dram, rec + 0x60, NW); struct.pack_into(">H", dram, rec + 0x62, NH)
        struct.pack_into(">I", dram, rec + 0x94, 0x80000000 | ((NW // 32) << 22) | 0x48FE)
        struct.pack_into(">I", dram, rec + 0x9C, (NW - 1) | ((NH - 1) << 13))
        d4, d5 = _compute_fetch_ext(NW, NH, fmt)
        struct.pack_into(">I", dram, rec + 0xA4, d4); struct.pack_into(">I", dram, rec + 0xA8, d5)
        log(f"  upscaled record -> {NW}x{NH} (d4=0x{d4:X} d5=0x{d5:X})")

    dram_c = EE.encode_payload(bytes(dram), wparam=dram_b["wp"], codec=dram_b["codec"])
    tex_c = EE.encode_payload(bytes(tex), wparam=tex_b["wp"], codec=tex_b["codec"])
    for blob_c, dec in ((dram_c, bytes(dram)), (tex_c, bytes(tex))):
        err = _verify_blob(blob_c, dec)
        if err:
            raise ValueError(f"{iff}: {err} — grow aborted, game files untouched")
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + tex_c
    _patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    _patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(tex_c), dec_size=len(tex))
    struct.pack_into(">I", new_res, 8, len(new_res))      # IFF total_size
    _backup_once(game_dir / arc, log)
    log(f"  {iff}: appended {NW}x{NH} @VRAM 0x{new_off:X}, blob grew {len(tex_b['dec'])}->{len(tex)} "
        f"(redirect +0x6c, the old slot is now dead space)")
    return _relocate(iff, bytes(new_res), idx, game_dir, NW, NH, fmt, log)


# Serialized resource descriptor (@rec+0x08/0x0C/0x1C), fetch format id (f1&0x3F) and f3 low-12
# per texture format — ground-truthed by diffing 8888 vs DXT4_5 records in overlay_static.iff.
# 4444 (RGBA4444, 2 bytes/px = half of 8888): uncompressed → no DXT block artifacts, lighter storage.
# Ground-truthed from shipped 4444 textures (disc_f73bf9f4 etc.): DESC=0x1828014F (8888's 0x18280186
# with the format nibble = f1low), f3low=0xC14 (same as 8888), f1low=0x04F (fmt 15 + 8-in-16 endian).
# 8_8 (two 8-bit channels, uncompressed, 2 bytes/px): the block-free target for DXN NORMAL maps —
# BC4's 8-level-per-block ladder can't hold fine normal detail (interior "noise"); 8_8 stores X=R, Y=G
# at full 8-bit and the shader rebuilds Z, same as DXN. No standalone 8_8 texture ships (8_8 only lives
# inside uniform packs), so the descriptor is DERIVED from the airtight 8888/4444 pattern
# (DESC = 0x18280100 | f1low) with f1low=0x00A (fmt 10, endian 0, as seen in the uniform 8_8 records).
# UNCONFIRMED in-game (needs a render test): does the normal shader sample an 8_8 map like a DXN.
_FMT_DESCRIPTOR = {"8888": 0x18280186, "4444": 0x1828014F, "8_8": 0x1828010A, "DXT4_5": 0x1A200154, "DXT1": 0x1A200152, "DXT5": 0x1A200154}
_FMT_FETCH_ID   = {"8888": 6, "4444": 15, "8_8": 10, "DXT4_5": 20, "DXT5": 20, "DXT1": 18}
_FMT_F3_LOW     = {"8888": 0xC14, "4444": 0xC14, "8_8": 0xC14, "DXT4_5": 0xD10, "DXT5": 0xD10, "DXT1": 0xD10}
# f1 low 12 bits = format id (bits 0-5) + GPU ENDIAN (bits 6-11): 8888 = 8-in-32 swap (0x086),
# DXT = 8-in-16 (0x054/0x052). Keeping the DXT endian on 8888 data byte-swaps the pixels wrong
# (green read as alpha). MUST set the full low 12 bits, not just the format id.
_FMT_F1_LOW     = {"8888": 0x086, "4444": 0x04F, "8_8": 0x00A, "DXT4_5": 0x054, "DXT5": 0x054, "DXT1": 0x052}


def replace_multitex_convert(iff, vram_off, w, h, new_fmt, edited_path, game_dir,
                             log=print, rec_off=None) -> str:
    """Replace a multi-texture sub-texture AND CHANGE ITS PIXEL FORMAT (e.g. DXT4_5 -> 8888 for a
    lossless, block-artifact-free UI logo). Same grow mechanism as replace_multitex_grow (append to
    the END of the texture blob, redirect +0x6c, patch the section alloc-size, relocate), but also
    rewrites the record's format fields so the GPU samples the new format: the serialized descriptor
    @+0x08/+0x0C/+0x1C, the fetch format id (f1@+0x98), f3 (@+0xA0) and f5 (@+0xA8, the mip0 size).
    f0/f2/f4/flags(@+0x5C)/dims stay (they depend on dimensions, which are unchanged). 8888 is 4x the
    bytes of DXT so this only works where the texture blob can grow (blob is LAST). Keeps the same
    dims. Verified recipe (see _FMT_* tables). Returns the relocate status string."""
    if new_fmt not in _FMT_DESCRIPTOR:
        raise ValueError(f"convert to {new_fmt} not supported (have {list(_FMT_DESCRIPTOR)})")
    game_dir = Path(game_dir)
    loc = resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = _walk_blobs(res, size); dram_b = blobs[0]; tex_b = _big_vram_blob(res, size)
    if not tex_b or tex_b["off"] != blobs[-1]["off"]:
        raise ValueError(f"{iff}: texture blob isn't the last blob — can't grow for format convert")
    dram = bytearray(dram_b["dec"]); tex = bytearray(tex_b["dec"])
    rec = rec_off if rec_off is not None else _find_multitex_rec(bytes(dram), vram_off, w=w, h=h)
    if rec is None:
        raise ValueError(f"{iff}: texture record for VRAM 0x{vram_off:X} ({w}x{h}) not found")
    rw = struct.unpack_from(">H", dram, rec + 0x60)[0]
    rh = struct.unpack_from(">H", dram, rec + 0x62)[0]
    if rw != w or rh != h:      # coincidental / loader-repacked record (global.iff) -> refuse
        raise ValueError(f"{iff}: record dims {rw}x{rh} != {w}x{h} — loader-repacked/coincidental, "
                         f"can't safely convert format")

    img = Image.open(edited_path).convert("RGBA")
    if img.size != (w, h):
        img = _straight_resize(img, (w, h))              # NOT Image.resize — Pillow premultiplies
    chain = bytearray(_encode_tiled(img, new_fmt, 1))                 # mip0
    mw, mh = w // 2, h // 2
    cur = img
    while mw >= 4 and mh >= 4:                                        # mips down to 4x4 (as DXT)
        cur = _straight_resize(cur, (mw, mh))
        chain += _encode_tiled(cur, new_fmt, 1)
        mw //= 2; mh //= 2
    mip0 = _mip0_size(new_fmt, w, h); tail = len(chain) - mip0

    new_off = (len(tex) + 0xFFF) & ~0xFFF
    tex += b"\x00" * (new_off - len(tex)) + chain

    def W32(o, v): struct.pack_into(">I", dram, rec + o, v)
    def R32(o):    return struct.unpack_from(">I", dram, rec + o)[0]
    W32(0x6C, new_off + 1); W32(0x70, mip0); W32(0x74, tail)
    for o in (0x08, 0x0C, 0x1C):                                      # serialized format descriptor
        W32(o, _FMT_DESCRIPTOR[new_fmt])
    W32(0x98, (R32(0x98) & ~0xFFF) | _FMT_F1_LOW[new_fmt])          # fetch format id + ENDIAN
    W32(0xA0, (R32(0xA0) & ~0xFFF) | _FMT_F3_LOW[new_fmt])           # f3
    W32(0xA8, (mip0 & ~0xFFF) | 0xA00)                               # f5 = mip0 size | 0xA00

    dram_c = EE.encode_payload(bytes(dram), wparam=dram_b["wp"], codec=dram_b["codec"])
    tex_c = EE.encode_payload(bytes(tex), wparam=tex_b["wp"], codec=tex_b["codec"])
    for blob_c, dec in ((dram_c, bytes(dram)), (tex_c, bytes(tex))):
        err = _verify_blob(blob_c, dec)
        if err:
            raise ValueError(f"{iff}: {err} — convert aborted, game files untouched")
    new_res = bytearray(res[:dram_b["off"]]) + dram_c + tex_c
    _patch_iff_section(new_res, 0xBB05A9C1, dram_b["off"], len(dram_c), dec_size=len(dram))
    _patch_iff_section(new_res, 0x411536D5, dram_b["off"] + len(dram_c), len(tex_c), dec_size=len(tex))
    struct.pack_into(">I", new_res, 8, len(new_res))
    _backup_once(game_dir / arc, log)
    log(f"  {iff}: converted {w}x{h} -> {new_fmt} @VRAM 0x{new_off:X}, blob "
        f"{len(tex_b['dec'])}->{len(tex)} (lossless, no DXT block artifacts)")
    st = _relocate(iff, bytes(new_res), idx, game_dir, w, h, new_fmt, log)
    # Only the PRIMARY's own record can be re-derived from the pack, so this readback is meaningful
    # for the single-primary case (rec_off=0); a sub-texture convert is verified by its caller.
    if VERIFY_WRITES and rec_off == 0 and verify_written_primary(iff, game_dir, bytes(chain[:mip0]), log):
        st += "  [!] POST-WRITE VERIFY FAILED — see log"
    return st


def replace_primary_convert(iff, edited_path, game_dir, new_fmt="8888", log=print) -> str:
    """Replace a SINGLE-PRIMARY texture (goalie mask, single-texture pack) AND change its pixel format
    to `new_fmt` (default 8888 = uncompressed, no DXT block artifacts / jagged diagonals). The primary's
    fetch/descriptor record sits at offset 0 of the small descriptor blob (blobs[0]) with the exact same
    field layout as a multitex record, so reuse replace_multitex_convert with rec_off=0. Grows the VRAM
    blob (8888 is 4x DXT), which relocates — fine on renderable shells."""
    game_dir = Path(game_dir)
    loc = resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size + 0x200000))
    vram, fetch = _find_primary(_walk_blobs(res, size))
    if not vram or not fetch:
        raise ValueError(f"{iff}: primary texture not locatable for convert")
    _fmt, bpu, block, w, h, tiled, mip0 = fetch
    return replace_multitex_convert(iff, vram["off"], w, h, new_fmt, edited_path, game_dir, log, rec_off=0)


# Color block-DXT formats worth auto-upgrading to lossless 8888 (already-uncompressed formats are
# left as-is; DXT5A stays single-channel).
_LOSSY_COLOR_DXT = ("DXT1", "DXT4_5", "DXT5")

# Experimental: store DXN normal maps uncompressed as 8_8 (block-free). The 8_8 descriptor is DERIVED
# (no standalone 8_8 texture ships to copy), so this is unconfirmed in-game — if normals render wrong,
# set this False (one line) to fall back to native DXN everywhere.
DXN_TO_8_8 = True


def _lossless_target(fmt):
    """The uncompressed format to upgrade a compressed `fmt` to so it has NO block artifacts, or None
    to keep `fmt` as-is. Colour DXT -> 8888 (RGBA, 4 B/px). DXN normal maps -> 8_8 (X=R,Y=G at full
    8-bit, 2 B/px; the shader rebuilds Z, exactly as it does from DXN) — kills the BC4 8-level-per-block
    interior noise. (8_8's descriptor is derived, not ground-truthed — confirm the normal renders.)"""
    if fmt in _LOSSY_COLOR_DXT:
        return "8888"
    if fmt == "DXN" and DXN_TO_8_8:
        return "8_8"
    return None


def _is_scatter_pack(iff, rec=None) -> bool:
    """Packed assets whose blob offsets are group-relative / build-time-permuted and therefore NOT
    safe to grow or convert to 8888 (redirecting +0x6c would corrupt the packing) — arena
    presentation. Detected by the scatter record tag OR the pack name (covers single-texture arenas
    that don't reach the >=2-record scatter path)."""
    return (rec is not None and rec.get("packing") == "scatter") \
        or str(iff).startswith("arena_presentation")


def ensure_clean(iff, game_dir, log=print) -> bool:
    """Reset the game copy of `iff` to its PRISTINE clean content when it's currently modified, so
    an apply always starts from a fresh base (never stacks grows / relocations / format-conversions
    on top of a previous edit). No-op (returns False) if the asset is already pristine. The pristine
    bytes come from the game folder's own <arc>.orig backup; we relocate them back over the game
    TOC entry."""
    game_dir = Path(game_dir)
    # Deliberately NOT resolve_clean: this is the reset-to-shipped path, and an expansion team's
    # asset has no shipped version to reset to. Falling back to the live archive would compare the
    # asset against itself and always report "already pristine" — same no-op, extra full read.
    cl = resolve(iff, None, clean=True); gl = resolve(iff, game_dir)
    if not cl or not gl:
        return False
    with open(_arc_file(None, cl[0], clean=True), "rb") as f:
        f.seek(cl[1]); clean = f.read(cl[2])
    with open(game_dir / gl[0], "rb") as f:
        f.seek(gl[1]); cur = f.read(gl[2])
    if clean == cur:
        return False                                   # already pristine — nothing to reset
    _relocate(iff, clean, gl[3], game_dir, 0, 0, "DXT4_5", log)
    log(f"  reset {iff} to clean (applying fresh)")
    return True


def smart_replace_record(iff, rec, edited_path, game_dir, log=print, prefer_lossless=True) -> str:
    """Replace a multi-texture sub-texture, PREFERRING an uncompressed store (no block artifacts)
    whenever the pack can grow — colour DXT -> 8888, DXN normal maps -> 8_8. Falls back to the
    original format (in-place, else grow) for in-place-only packs or non-upgradeable formats. Returns
    the relocate/replace status string."""
    fmt = rec["fmt"]
    scatter = _is_scatter_pack(iff, rec)
    target = _lossless_target(fmt)
    # sequential loader-placed pack (global.iff): resizing/format-converting any texture shifts the
    # runtime VRAM cursor and desyncs the WHOLE pack in-game -> only same-format in-place is safe.
    if _is_sequential_pack(iff, game_dir):
        prefer_lossless = False
    # scatter-packed (arena presentation) records store GROUP-RELATIVE offsets — never grow/convert
    # them (would corrupt the packing); replace in-place at native format instead.
    if prefer_lossless and target and not scatter:
        try:
            st = replace_multitex_convert(iff, rec["vram_off"], rec["w"], rec["h"],
                                          target, edited_path, game_dir, log)
            if target == "8_8":
                log("  (stored UNCOMPRESSED as 8_8 — removes BC4 block noise on the normal; the 8_8 "
                    "descriptor is DERIVED, so verify the normal renders correctly in-game)")
            else:
                log("  (stored LOSSLESS as 8888 — no DXT block artifacts)")
            return st
        except ValueError as e:
            log(f"  (uncompressed {target} unavailable here — {e}; keeping {fmt})")
    try:
        return replace_at(iff, rec["vram_off"], rec["w"], rec["h"], fmt, edited_path,
                          game_dir, log, rec["tiled"])
    except ValueError as e:
        if "too large to fit in place" not in str(e):
            raise
        if scatter:                           # can't relocate a packed arena texture — keep native
            raise ValueError(f"{iff}: this is a packed (arena presentation) texture — replace it at "
                             f"its native {rec['w']}x{rec['h']} {fmt} (hi-res/lossless isn't possible "
                             f"for this pack).")
        log("  in-place slot too small — relocating the pack (append + redirect)…")
        return replace_multitex_grow(iff, rec["vram_off"], rec["w"], rec["h"], fmt,
                                      edited_path, game_dir, 1, log)


# ── EXTRACT (from CLEAN) ─────────────────────────────────────────────────────
def extract_dds(name: str, out_path: Path, clean_dir: Path = None) -> tuple:
    """Decode the primary texture of `name` from CLEAN -> uncompressed DDS. Returns
    (w, h, fmt) or raises."""
    if name in SCENE_ASSETS:
        return extract_dds_at(*SCENE_ASSETS[name][:5], out_path, clean_dir)
    loc, _cl = resolve_clean(name, clean_dir)
    if not loc:
        raise ValueError(f"{name}: not found in TOC")
    arc, off, size, idx, f3 = loc
    with open(_arc_file(clean_dir, arc, clean=_cl), "rb") as f:
        f.seek(off); data = f.read(size + 0x200000)
    blobs = _walk_blobs(data, size)
    vram, fetch = _find_primary(blobs)
    if not vram:
        raise ValueError(f"{name}: no decodable primary texture (packed/scene asset?)")
    fmt, bpu, block, w, h, tiled, mip0 = fetch
    # Honour the record's +0x6c redirect exactly as decode_preview does. A grow-replace APPENDS the
    # new pixels and points the record at them, so mip0 is no longer at the head of the blob —
    # slicing from 0 decodes the stale ORIGINAL texture (in the NEW format) and yields a shifted,
    # garbled image. Preview did this right and extract did not, which is why an asset could look
    # correct in the tab and come out wrong on disk.
    vo = _primary_vram_off(blobs, len(vram["dec"]), mip0)
    img = T.decode(_dxt_endian(vram["dec"][vo:vo + mip0], fmt), w, h, fmt, bpu, block, tiled, 0).convert("RGBA")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(_uncompressed_dds(img.tobytes(), w, h))
    return (w, h, fmt)


# ── REPLACE (into GAME archives) ─────────────────────────────────────────────
# team code -> tile index in the frontend logo bundles (identical across large/medium/small).
#
# ⚠ This table is a LAST RESORT, not the answer: a tile index is a POSITION in the atlas, and
# the atlas is not fixed.  Adding Seattle and Vegas spliced two tiles in at 67 and 42, which
# pushed 22 of these 30 down by one or two — every one of them was silently overwriting its
# neighbour's logo.  `_logo_tile()` reads the real order off disk; this only covers the case
# where that read fails.
LOGO_BUNDLE_TILE = {"col": 9, "lak": 12, "chi": 20, "phi": 21, "fla": 28, "buf": 29, "pit": 31,
                    "cbj": 35, "njd": 44, "car": 52, "nyi": 53, "ana": 54, "van": 56, "edm": 62,
                    "dal": 65, "bos": 73, "ott": 74, "min": 75, "stl": 76, "mtl": 78, "cgy": 83,
                    "atl": 87, "tbl": 90, "pho": 101, "wsh": 104, "tor": 110, "nsh": 115,
                    "nyr": 117, "det": 118, "sjs": 119}


def _logo_tile(code: str, game_dir, log=print):
    """The atlas tile `logo_<code>.iff` really occupies, read off disk.

    The bundles are keyed by crc32 of the asset key and stored in hash order, so a team added
    after ship (Seattle, Vegas) lands in the middle and shifts everything below it.  Only the
    file knows the current order — see logos_atlas.team_tiles(), which inverts the hashes.
    """
    try:
        from . import logos_atlas as LA
    except ImportError:
        import logos_atlas as LA
    try:
        t = LA.team_tiles(game_dir).get(code)
        if t is not None:
            return t
        log(f"  no atlas tile for asset key '{code}' — front-end logo not cascaded")
        return None
    except Exception as e:
        log(f"  WARNING could not read the logo atlas ({e}); falling back to the shipped map")
        return LOGO_BUNDLE_TILE.get(code)


VERIFY_WRITES = True                 # set False only to benchmark; costs ~0.02s (logo) - 1.1s (ice)


def verify_written_primary(name, game_dir, expect_mip0: bytes, log=print) -> str | None:
    """Read `name` back the way the GAME will and confirm the GPU will sample `expect_mip0`.

    The point is that this resolves the surface offset INDEPENDENTLY — through the record's stored
    `+0x6c`, exactly as the loader does — instead of trusting the offset the writer used. That is
    the one thing neither `_verify_blob` (blob round-trips, but says nothing about where it sits)
    nor an in-memory assertion can check, and it is precisely what went wrong with the ice: a
    perfectly valid blob, written 2.8 MB away from where the record points.

    Decompress only, no pixel decode — ~1s on the biggest asset in the game. Returns None when the
    write is good, else an error string (also logged as a WARNING)."""
    try:
        loc = resolve(name, game_dir)
        if not loc:
            return None                                # nothing to check against
        arc, off, size, _idx, _f3 = loc
        with open(_arc_file(game_dir, arc, clean=False), "rb") as f:
            f.seek(off); data = f.read(size + 0x200000)
        blobs = _walk_blobs(data, size)
        vram, fetch = _find_primary(blobs)
        if not vram or not fetch:
            return None
        vo = _primary_vram_off(blobs, len(vram["dec"]), fetch[6])
        got = bytes(vram["dec"][vo:vo + len(expect_mip0)])
    except Exception as e:
        log(f"  (post-write verify skipped for {name}: {type(e).__name__}: {e})")
        return None
    if got == expect_mip0:
        return None
    where = next((i for i, (a, b) in enumerate(zip(got, expect_mip0)) if a != b), len(got))
    err = (f"VERIFY FAILED: {name} — the game will sample from VRAM offset {vo:,} but the pixels we "
           f"wrote are not there (first mismatch at byte {where:,} of {len(expect_mip0):,}). "
           f"The art on disk will render wrong.")
    log(f"  WARNING {err}")
    return err


def replace(name: str, edited_path: Path, game_dir: Path, log=print, prefer_lossless=True) -> str:
    """Permanently splice an edited image into the GAME archives (returns a status string).
    logo_<code>.iff replacements CASCADE into the frontend logo bundles (menu / team-select /
    loading logos, all three sizes) so one edit covers every spot the logo appears."""
    st = _replace_impl(name, edited_path, game_dir, log, prefer_lossless)
    code = name[5:-4] if name.startswith("logo_") and name.endswith(".iff") else None
    tile = _logo_tile(code, game_dir, log) if code else None
    if tile is not None:
        log(f"  cascading logo_{code} into the frontend logo bundles (tile {tile}) …")
        try:
            replace_bundles("disc_b6b4e9c8.iff", [{"index": tile, "path": str(edited_path)}],
                            game_dir, log)
        except Exception as ce:
            log(f"  WARNING logo bundle cascade failed: {ce}")
    return st


def _replace_impl(name: str, edited_path: Path, game_dir: Path, log=print, prefer_lossless=True) -> str:
    """Permanently splice an edited image into the GAME archives. Returns a status string."""
    if name in SCENE_ASSETS:
        iff, vo, w, h, fmt = SCENE_ASSETS[name]
        chain = MIP_CHAINS.get(name)
        if chain:
            return replace_chain(iff, fmt, chain, edited_path, game_dir, log)
        return replace_at(iff, vo, w, h, fmt, edited_path, game_dir, log)
    game_dir = Path(game_dir)
    loc = resolve(name, game_dir)
    if not loc:
        raise ValueError(f"{name}: not found in game TOC")
    arc, off, size, idx, f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off); res = bytearray(f.read(size))
    blobs = _walk_blobs(res, size)
    vram, fetch = _find_primary(blobs)
    if not vram:
        raise ValueError(f"{name}: primary texture not locatable for replace")
    fmt, bpu, block, w, h, tiled, mip0 = fetch
    if fmt not in REPLACE_FORMATS:
        raise ValueError(f"{name}: replace not supported for format {fmt}")

    # LOSSLESS-first (same as the multi-texture path): a compressed single-primary — team logos, cover
    # art, goalie masks are all 512x512 DXT4_5 — re-imports blocky if re-compressed to DXT. Store it
    # UNCOMPRESSED instead (colour DXT -> 8888, DXN -> 8_8): no block artifacts, sharp edges. Grows/
    # relocates (fine for single-primary). Falls back to native in-place if the pack can't grow.
    # logo_* stay native DXT4_5: their MIP CHAIN is what the menu/team-select screens sample, and
    # the native in-place path rebuilds it faithfully (the old "DXT re-imports blocky" reason for
    # 8888 was the 8-in-16 endian bug, fixed 2026-07-11 — DXT round-trips cleanly now).
    target = _lossless_target(fmt) if (prefer_lossless and not name.startswith("logo_")) else None
    if target and not _is_scatter_pack(name):
        try:
            st = replace_primary_convert(name, edited_path, game_dir, target, log)
            log(f"  (stored UNCOMPRESSED as {target} — no block artifacts / sharp edges)")
            return st
        except ValueError as e:
            log(f"  (uncompressed {target} unavailable here — {e}; keeping {fmt})")

    img = Image.open(edited_path).convert("RGBA")
    if img.size != (w, h):
        if img.size[0] * h != img.size[1] * w:
            log(f"  WARNING {name}: source aspect {img.size} != {w}x{h} — it will be stretched")
        log(f"  {name}: fitting source {img.size} -> {w}x{h} (LANCZOS downscale)")
        img = img.resize((w, h), Image.LANCZOS)
    # The surface is NOT always at the head of the VRAM blob: when the record carries a real stored
    # offset (+0x6c > 1) the loader samples from THERE, not 0. Writing mip0 at 0 leaves the GPU
    # reading 2.8 MB into our new pixels — the art comes out rolled by that many bytes (this was the
    # ice_* bug: ice packs keep mip0 at 0x2B0000, and extract honoured it while replace did not).
    svo = _primary_vram_off(blobs, len(vram["dec"]), mip0)
    new_dec = _rebuild_with_mips(vram["dec"], svo, fmt, w, h, tiled, img, log,
                                 ref_dec=_clean_ref_blob(name, svo),  # mip0 + mip chain (CLEAN ref)
                                 mip_end=len(vram["dec"]))            # single texture = to blob end
    new_blob = EE.encode_payload(new_dec, wparam=vram["wp"], codec=vram["codec"])  # native window
    err = _verify_blob(new_blob, new_dec)
    if err:
        raise ValueError(f"{name}: {err} — replace aborted, game files untouched")
    old_tot = vram["tot"]
    vo = vram["off"]

    expect0 = bytes(new_dec[svo:svo + _mip0_size(fmt, w, h)])   # what the GPU must end up sampling

    if len(new_blob) <= old_tot:                      # IN-PLACE: same resource size
        new_res = bytearray(res)
        new_res[vo:vo + old_tot] = new_blob + b"\x00" * (old_tot - len(new_blob))
        _backup_once(game_dir / arc, log)
        with open(game_dir / arc, "r+b") as f:
            f.seek(off); f.write(new_res)
        st = (f"IN-PLACE: {name} -> {arc}:0x{off:X} "
              f"(blob {len(new_blob)}/{old_tot} bytes, {w}x{h} {fmt})")
        if VERIFY_WRITES and verify_written_primary(name, game_dir, expect0, log):
            st += "  [!] POST-WRITE VERIFY FAILED — see log"
        return st

    # RELOCATE: build new resource (bigger VRAM blob), append to end of 1B, repoint TOC
    new_res = bytearray(res[:vo] + new_blob + res[vo + old_tot:])
    _patch_grown_iff(new_res, vo, len(new_blob))       # fix IFF total_size + section size (grown blob)
    st = _relocate(name, bytes(new_res), idx, game_dir, w, h, fmt, log)
    if VERIFY_WRITES and verify_written_primary(name, game_dir, expect0, log):
        st += "  [!] POST-WRITE VERIFY FAILED — see log"
    return st


def _backup_once(path: Path, log):
    bak = path.with_suffix(path.suffix + ".orig")
    if not bak.exists():
        log(f"  backing up {path.name} -> {bak.name} (one-time)")
        shutil.copy2(path, bak)


def _verify_blob(new_blob: bytes, expect_dec: bytes):
    """SAFETY: the re-encoded LZ blob MUST decompress back to exactly the bytes we
    intend (using the codec/wp written in its own header, i.e. exactly what the game
    will do), or the game gets corrupt data. Returns an error string, or None if OK."""
    wp = struct.unpack_from(">I", new_blob, 16)[0]
    try:
        chk = DF.decompress_codec(new_blob[20:], len(expect_dec), (1 << wp) - 1, wp)
    except Exception as e:
        return f"re-encoded blob failed to decompress ({type(e).__name__})"
    if chk != expect_dec:
        return "re-encoded blob did not round-trip"
    return None


def _orig_1b_size(game_dir, align=0x800):
    """The 1B size BEFORE any relocation (= where appended copies start), from the .orig backup.
    Aligned. 0 if no backup (then auto-reuse/compaction are disabled — always append)."""
    o = Path(game_dir) / "1B.orig"
    if not o.exists():
        return 0
    return (o.stat().st_size + align - 1) & ~(align - 1)


# Appends roll into the LAST archive named by the 0A header. When one more asset would push that
# archive past this ceiling, a fresh archive is minted instead ("1C", "1D", ...) and the append goes
# there, so no single archive ever approaches the ~2 GiB wall. The shipped 1B is already 1.91 GiB,
# so in practice the next append spills immediately.
SPILL_CEILING = 2_000_000_000


def _read_header(a0: Path):
    """0A's header + the whole entry table + 16 trailing bytes of slack.

    The slack is what a new archive row is carved out of: inserting a 16-byte row slides the entry
    table 16 bytes later, and consuming 16 zero bytes behind it keeps 0A's total length -- and
    therefore every asset offset in it -- unchanged."""
    head = open(a0, "rb").read(0x18)
    narc = _BE(head, 0x08)
    cnt = _BE(head, 0x10)
    need = 0x18 + narc * 16 + cnt * 16 + 16
    buf = bytearray(open(a0, "rb").read(need))
    if len(buf) != need:
        raise RuntimeError(f"0A is shorter ({len(buf)}) than its own header claims ({need})")
    tbl = arc_table(buf)
    return buf, [n for n, _ in tbl], [s for _, s in tbl], 0x18 + narc * 16, cnt


def _write_header(a0: Path, buf: bytes):
    with open(a0, "r+b") as f:
        f.seek(0); f.write(buf)


def _next_arc_name(names, game_dir):
    taken = set(names)
    for d in "123456789":
        for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            n = d + c
            if n not in taken and not (Path(game_dir) / n).exists():
                return n
    raise RuntimeError("no free archive name left")


def _add_archive(game_dir, buf: bytearray, names, log):
    """Mint a new empty archive and register it as the last row of 0A's archive table.

    The name field is 8 bytes of UTF-16BE with a NUL terminator, so a name is at most 3 characters
    -- "1C" fits, "Expanded_Textures" does not. Verified in-game: the loader takes the archive
    count, the file names and the cumulative bounds walk entirely from this table, for both the
    synchronous front-end load path and the gameplay streaming path."""
    narc = len(names)
    cnt = _BE(buf, 0x10)
    eend = 0x18 + narc * 16 + cnt * 16
    if any(buf[eend:eend + 16]):
        raise RuntimeError("no zero slack behind 0A's entry table -- adding an archive would have "
                           "to move asset data; refusing")
    name = _next_arc_name(names, game_dir)
    row = struct.pack(">II", 0, 0) + name.encode("utf-16-be").ljust(8, b"\0")
    buf[0x18 + narc * 16:0x18 + narc * 16] = row       # insert the row; entry table slides +16
    struct.pack_into(">I", buf, 0x08, narc + 1)
    del buf[eend + 16:eend + 32]                       # ... and 16 B of slack pays for it
    (Path(game_dir) / name).write_bytes(b"")
    log(f"  minted archive {name} (now {narc + 1} archives; "
        f"entry table at 0x{0x18 + (narc + 1) * 16:x})")
    return name


def _appended_start(game_dir, arc_name, align=0x800):
    """Byte offset inside `arc_name` where OUR appended data begins -- everything at or above it is
    ours to reuse or repack. For a shipped archive that is the size of its .orig backup (None when
    there is no backup, which disables reuse and compaction for it); an archive we minted is ours in
    its entirety."""
    if arc_name in ARCS:
        o = Path(game_dir) / (arc_name + ".orig")
        if not o.exists():
            return None
        return (o.stat().st_size + align - 1) & ~(align - 1)
    return 0


def _backup_if_shipped(p: Path, log):
    if p.name in ARCS:
        _backup_once(p, log)


def _relocate(name, new_res: bytes, toc_idx: int, game_dir: Path, w, h, fmt, log) -> str:
    """Write `new_res` over `name`'s TOC entry (reusing its slot when it fits, else appending).

    Afterwards, top up the STREAMING-POOL headroom for whatever asset family `name` belongs to: the
    game sizes each class's pool slot to the biggest SHIPPED member of that family and silently
    refuses to load anything larger (forever-"Loading"). Growing an asset past that ceiling is
    exactly what a replace does, so the check belongs here — every asset write funnels through it."""
    st = _relocate_raw(name, new_res, toc_idx, game_dir, w, h, fmt, log)
    try:
        from . import streaming_pool as _SP
    except ImportError:
        try:
            import streaming_pool as _SP
        except ImportError:
            _SP = None
    if _SP is not None:
        try:
            _SP.ensure_headroom(name, game_dir, log)
        except Exception as e:                       # headroom is a safety net, never a blocker
            log(f"  (streaming-pool headroom check skipped: {type(e).__name__}: {e})")
    return st


def _relocate_raw(name, new_res: bytes, toc_idx: int, game_dir: Path, w, h, fmt, log) -> str:
    """Point `name`'s TOC entry at `new_res`, reusing its slot when the data fits and otherwise
    appending to the LAST archive -- minting a new one when that would cross SPILL_CEILING.

    The archive set is read from 0A's header, never assumed. One invariant keeps that safe: an
    archive's declared size is the base of every archive after it, so only the last archive may ever
    change size. Appends always go to the last archive, so growth never moves anything."""
    ALIGN = 0x800
    game_dir = Path(game_dir)
    a0 = game_dir / "0A"
    _backup_once(a0, log)
    buf, names, sizes, ebase, cnt = _read_header(a0)
    bases, acc = [], 0
    for s in sizes:
        bases.append(acc); acc += s

    eo = ebase + toc_idx * 16
    cur_off = _BE(buf, eo + 12) * ALIGN
    cur_size = _BE(buf, eo + 4)
    ci = max(k for k in range(len(names)) if bases[k] <= cur_off)
    cur_arc, cur_local = names[ci], cur_off - bases[ci]

    # AUTO-REUSE: if this asset is ALREADY in appended (relocated) space and the new data fits its
    # existing slot, overwrite in place — no append, no growth, no orphan. This makes re-applying
    # the same asset free instead of leaking dead space.
    astart = _appended_start(game_dir, cur_arc, ALIGN)
    if astart is not None and cur_local >= astart and len(new_res) <= cur_size:
        p = game_dir / cur_arc
        _backup_if_shipped(p, log)
        with open(p, "r+b") as f:
            f.seek(cur_local); f.write(new_res)
            if len(new_res) < cur_size:                        # wipe the stale tail (keeps it clean)
                f.write(b"\x00" * (cur_size - len(new_res)))
        struct.pack_into(">I", buf, eo + 4, len(new_res))      # size only; f3 unchanged
        _write_header(a0, buf)
        return (f"IN-PLACE (reused slot): {name} -> {cur_arc}:0x{cur_local:X} "
                f"({len(new_res)}/{cur_size} bytes, no growth) ({w}x{h} {fmt})")

    spill = names[-1]
    spill_path = game_dir / spill
    spill_size = spill_path.stat().st_size if spill_path.exists() else 0
    new_local = (spill_size + ALIGN - 1) & ~(ALIGN - 1)        # 0x800-align the append point

    # ROLLING SPILL: never let one archive approach the ~2 GiB wall. A brand-new archive is exempt
    # from the check, so an asset larger than the ceiling still lands somewhere instead of looping.
    if spill_size and new_local + len(new_res) > SPILL_CEILING:
        log(f"  {spill} would reach {new_local + len(new_res):,} B, past the "
            f"{SPILL_CEILING:,} B ceiling — spilling to a new archive")
        spill = _add_archive(game_dir, buf, names, log)
        names.append(spill); sizes.append(0); bases.append(acc)
        spill_path = game_dir / spill
        spill_size = 0
        new_local = 0
        ebase += 16                                            # the entry table just moved
        eo = ebase + toc_idx * 16

    si = len(names) - 1
    _backup_if_shipped(spill_path, log)
    with open(spill_path, "r+b" if spill_path.exists() else "wb") as f:
        if new_local > spill_size:
            f.seek(spill_size); f.write(b"\x00" * (new_local - spill_size))
        f.seek(new_local); f.write(new_res)
    total = new_local + len(new_res)
    struct.pack_into(">I", buf, 0x18 + si * 16, (total + ALIGN - 1) // ALIGN)   # last archive's size
    struct.pack_into(">I", buf, eo + 4, len(new_res))                          # size
    struct.pack_into(">I", buf, eo + 12, (bases[si] + new_local) // ALIGN)     # f3
    _write_header(a0, buf)
    orphan = (f" (old slot @{cur_arc}:0x{cur_local:X} orphaned — run compact_1b to reclaim)"
              if (astart is not None and cur_local >= astart) else "")
    return (f"RELOCATED: {name} -> {spill}:0x{new_local:X} ({len(new_res)} bytes), "
            f"TOC#{toc_idx} repointed, {spill} grown ({w}x{h} {fmt}){orphan}")


def compact_1b(game_dir, log=print) -> str:
    """Reclaim dead space in EVERY archive we have appended to. Kept under the old name because a
    dozen call sites use it.

    For each such archive: keep the original region (everything an un-relocated TOC entry still
    points into) plus every LIVE relocated entry, packed with no gaps, dropping orphaned copies
    (superseded relocations + reverted assets) and repointing each moved entry's f3.

    One rule matters for correctness now that there can be more than one archive: an archive's
    DECLARED size is the base of every archive after it, so shrinking a non-last archive would
    silently move all of them. Non-last archives are therefore truncated on disk but KEEP their
    declared size — declared > actual is already true of the shipped 1B, and only dead entries ever
    point past the end. Only the last archive may shrink its declared size. Run with the game
    CLOSED."""
    ALIGN = 0x800
    game_dir = Path(game_dir)
    a0 = game_dir / "0A"
    buf, names, sizes, ebase, cnt = _read_header(a0)
    bases, acc = [], 0
    for s in sizes:
        bases.append(acc); acc += s

    per = {k: [] for k in range(len(names))}            # archive index -> [(toc_idx, local, size)]
    for i in range(cnt):
        _fl, s, _f2, f3 = struct.unpack_from(">4I", buf, ebase + i * 16)
        off = f3 * ALIGN
        k = max(j for j in range(len(names)) if bases[j] <= off)
        per[k].append((i, off - bases[k], s))

    notes, saved_total, kept_total = [], 0, 0
    for k, arc in enumerate(names):
        start = _appended_start(game_dir, arc, ALIGN)
        p = game_dir / arc
        if start is None or not p.exists():
            continue                                   # no .orig -> boundary unknown, leave alone
        cur_sz = p.stat().st_size
        if cur_sz <= start:
            continue
        live = sorted((e for e in per[k] if e[2] > 0 and start <= e[1] < cur_sz),
                      key=lambda e: e[1])
        blobs = {}
        with open(p, "rb") as f:                       # read live bytes BEFORE truncating
            for i, local, s in live:
                f.seek(local); blobs[i] = f.read(s)
        with open(p, "r+b") as f:
            f.truncate(start)
            cur = start
            for i, local, s in live:
                at = (cur + ALIGN - 1) & ~(ALIGN - 1)
                f.seek(at); f.write(blobs[i])
                struct.pack_into(">I", buf, ebase + i * 16 + 12, (bases[k] + at) // ALIGN)
                cur = at + s
        if k == len(names) - 1:                        # only the last archive may shrink its bound
            struct.pack_into(">I", buf, 0x18 + k * 16, (cur + ALIGN - 1) // ALIGN)
        saved_total += cur_sz - cur
        kept_total += len(live)
        if cur_sz != cur:
            notes.append(f"{arc} {cur_sz:,}->{cur:,} ({len(live)} kept)")

    # a minted archive that ended up empty, with nothing at all pointing into it, can be dropped
    while len(names) > len(ARCS) and names[-1] not in ARCS and not per[len(names) - 1]:
        arc = names[-1]
        p = game_dir / arc
        if p.exists() and p.stat().st_size:
            break
        narc = len(names)
        eend = 0x18 + narc * 16 + cnt * 16
        del buf[0x18 + (narc - 1) * 16:0x18 + narc * 16]       # drop the row; table slides back 16
        struct.pack_into(">I", buf, 0x08, narc - 1)
        buf[eend - 16:eend - 16] = b"\0" * 16                  # ... and the slack is handed back
        if p.exists():
            p.unlink()
        names.pop(); ebase -= 16
        notes.append(f"dropped empty archive {arc}")

    _write_header(a0, buf)
    if not notes:
        return "nothing to compact (no dead space in any archive)"
    log(f"compacted: reclaimed {saved_total:,} B ({saved_total // 1024 // 1024}MB), "
        f"{kept_total} live relocated entries repacked — " + "; ".join(notes))
    return (f"compacted: reclaimed {saved_total // 1024 // 1024}MB "
            f"({kept_total} live entries kept across {len(notes)} change(s))")


if __name__ == "__main__":
    # self-test: extract logo_buf, re-encode unchanged, check in-place fit + re-decode (NO write)
    import io, contextlib
    out = Path("_at_selftest.dds")
    wh = extract_dds("logo_buf.iff", out)
    print("extract logo_buf.iff:", wh, "->", out, out.stat().st_size, "bytes")
    # dry replace against CLEAN copy in memory
    loc = resolve("logo_buf.iff", None, clean=True); arc, off, size, idx, f3 = loc
    with open(_arc_file(None, arc, clean=True), "rb") as f:
        f.seek(off); res = f.read(size)
    blobs = _walk_blobs(res, size); vram, fetch = _find_primary(blobs)
    fmt, bpu, block, w, h, tiled, mip0 = fetch
    img = Image.open(out).convert("RGBA")
    nt = _encode_tiled(img, fmt); nd = bytes(nt) + vram["dec"][mip0:]
    with contextlib.redirect_stdout(io.StringIO()):
        nb = EE.encode_payload(nd)                     # encoder is codec-8 / 9-bit offset
    print(f"re-encode: new blob {len(nb)} vs orig blob {vram['tot']}  -> "
          f"{'IN-PLACE fits' if len(nb) <= vram['tot'] else 'RELOCATE needed'}")
    print("roundtrip decompress ok:", _verify_blob(nb, nd) is None, "| mip0 bytes:", mip0)
