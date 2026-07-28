#!/usr/bin/env python3
"""
NHL 2k10 Mod Launcher — Audio + Textures + Arena Music
Unified modding tool for NHL 2k10 (Xbox 360 / Xenia)
"""

import hashlib
import json
import mmap
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import winsound
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tkinter as tk
from tkinter import *
from tkinter import filedialog, messagebox, simpledialog, ttk

# ── Module path setup ─────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "launcher"))

from launcher import roster_editor as rost
from launcher import team_colors as tcol
from launcher import archive_textures as archtex
from launcher import bank_parser as bankparse
from launcher import audio_names as audnames
from launcher import modpack as mp
from launcher import scorebug_anchors as sbanchor

try:
    from PIL import Image, ImageTk
    _PIL = True
except ImportError:
    _PIL = False

# ═══════════════════════════════════════════════════════════════════════════════
# Constants & Global Executor
# ═══════════════════════════════════════════════════════════════════════════════
APP_TITLE   = "NHL 2k10 Mod Launcher"
APP_VERSION = "1.0.0"

# Shared Thread Pool Executor for background non-blocking tasks
EXECUTOR = ThreadPoolExecutor(max_workers=os.cpu_count() or 4)

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).parent

# Bundled resources (logo, window icon) — PyInstaller onefile extracts datas to sys._MEIPASS,
# not next to the exe, so read them from there when frozen.
_RES = Path(getattr(sys, "_MEIPASS", _BASE))

# Windows explicit App User Model ID
try:
    import ctypes
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NHL2K10.ModLauncher")
except Exception:
    pass

def check_and_update_xenia_resolution(xenia_exe_path, config_settings=None, parent=None):
    """Checks xenia-canary.config.toml for draw_resolution_scale_x and y."""
    if config_settings is None:
        config_settings = {}

    if config_settings.get("never_check_xenia_res", False):
        return True

    if os.path.isdir(xenia_exe_path):
        xenia_dir = xenia_exe_path
    else:
        xenia_dir = os.path.dirname(xenia_exe_path)

    config_path = os.path.join(xenia_dir, "xenia-canary.config.toml")
    if not os.path.exists(config_path):
        return True

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"Error reading Xenia config: {e}")
        return True

    scale_x_match = re.search(r'^\s*draw_resolution_scale_x\s*=\s*(\d+)', content, re.MULTILINE)
    scale_y_match = re.search(r'^\s*draw_resolution_scale_y\s*=\s*(\d+)', content, re.MULTILINE)

    scale_x = int(scale_x_match.group(1)) if scale_x_match else 1
    scale_y = int(scale_y_match.group(1)) if scale_y_match else 1

    if scale_x >= 2 and scale_y >= 2:
        return True

    dialog = tk.Toplevel(parent)
    dialog.title("Resolution Check")
    dialog.resizable(False, False)
    dialog_width, dialog_height = 450, 160

    if parent:
        parent.update_idletasks()
        parent_x, parent_y = parent.winfo_rootx(), parent.winfo_rooty()
        parent_w, parent_h = parent.winfo_width(), parent.winfo_height()
        center_x = int(parent_x + (parent_w / 2) - (dialog_width / 2))
        center_y = int(parent_y + (parent_h / 2) - (dialog_height / 2))
        dialog.geometry(f"{dialog_width}x{dialog_height}+{center_x}+{center_y}")
    else:
        dialog.geometry(f"{dialog_width}x{dialog_height}")

    msg = (
        "Xenia is only rendering at 1x resolution.\n\n"
        "Texture replacements that have mipmaps will show artifacts if it is lower than 2.\n\n"
        "Would you like to update your resolution to 2?"
    )
    label = tk.Label(dialog, text=msg, justify="left", wraplength=420, padx=15, pady=15)
    label.pack(side="top", fill="both", expand=True)

    btn_frame = tk.Frame(dialog, pady=10)
    btn_frame.pack(side="bottom", fill="x")

    def on_yes():
        new_content = content
        if scale_x_match:
            new_content = re.sub(r'^(\s*draw_resolution_scale_x\s*=\s*)\d+', r'\g<1>2', new_content, flags=re.MULTILINE)
        else:
            new_content += "\ndraw_resolution_scale_x = 2\n"

        if scale_y_match:
            new_content = re.sub(r'^(\s*draw_resolution_scale_y\s*=\s*)\d+', r'\g<1>2', new_content, flags=re.MULTILINE)
        else:
            new_content += "\ndraw_resolution_scale_y = 2\n"

        try:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to update Xenia config file: {e}")
        dialog.destroy()

    def on_not_this_time():
        dialog.destroy()

    def on_never():
        config_settings["never_check_xenia_res"] = True
        dialog.destroy()

    tk.Button(btn_frame, text="Yes", width=12, command=on_yes).pack(side="left", padx=(20, 5))
    tk.Button(btn_frame, text="Not this time", width=12, command=on_not_this_time).pack(side="left", padx=5)
    tk.Button(btn_frame, text="Never", width=12, command=on_never).pack(side="left", padx=5)

    dialog.grab_set()
    dialog.wait_window()
    return True

def _resolve_config_path():
    """Persist settings in %APPDATA%\\NHL2K10 Mod Launcher\\ — a stable, always-writable, per-user
    location that SURVIVES rebuilds (the onedir app folder is wiped by `pyinstaller --noconfirm`, so
    a config sitting next to the exe would be lost every build) and is the right place for a shipped
    app. On first run, migrate an existing config from any legacy location so settings carry over
    automatically (no manual copy)."""
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    cfg_dir = Path(appdata) / "NHL2K10 Mod Launcher" if appdata else _BASE
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        cfg_dir = _BASE
    cfg = cfg_dir / "nhl2k10_launcher_config.json"
    if not cfg.exists():
        legacy = [
            _BASE / "nhl2k10_launcher_config.json",                    # next to the exe (onedir)
            Path(sys.executable).parent.parent / "nhl2k10_launcher_config.json",  # old onefile dist/
            _HERE / "nhl2k10_launcher_config.json",                    # dev / source tree
        ]
        for src in legacy:
            try:
                if src.exists() and src.resolve() != cfg.resolve():
                    shutil.copy2(src, cfg)
                    break
            except Exception:
                pass
    return cfg


CONFIG_FILE = _resolve_config_path()

PACKET_SIZE     = 2048
SAMPLE_RATE     = 48000
SECS_PER_PACKET = 7.07 / 39

FILE_IDS = ["0A", "0B", "1A", "1B"]

CATEGORY_FOLDER: dict = {
    "Goal_Horns":          "Goal_Horns",
    "Goal_Songs":          "Goal_Songs",
    "Goal_SFX":            "Goal_SFX",
    "Arena_Music":         "Arena_Music",
    "BIP_Music":           "BIP_Music",
    "Organ_Crowd":         "Organ_Crowd",
    "PP_SFX":              "SFX",
    "PA_English":          "PA",
    "PA_French":           "PA",
    "Crowd_Ambient":       "Crowd_Ambient",
    "Whistle":             "SFX",
    "Commentary":          "Commentary",
    "Pre_Game_Faceoff":    "Pre_Game_Faceoff",
    "Menu_Music":          "Menu_Music",
    "Unknown":             "Unknown",
    "Crowd_Ambient_Short": "Crowd_Ambient",
    "BIP_Music_or_Crowd":  "BIP_Music",
    "SFX_Mid":             "SFX",
    "SFX_High":            "SFX",
    "SFX_VeryShort":       "SFX",
    "SFX_Short":           "SFX",
    "SFX":                 "SFX",
    "Arena_SFX":           "SFX",
    "PA_or_Commentary":    "PA",
    "ArenaMusic":          "Arena_Music",
    "ArenaMusic_Short":    "Arena_Music",
}

CATEGORY_LABELS: dict = {
    "Goal_Horns":       "Goal Horns",
    "Goal_Songs":       "Goal Songs",
    "Goal_SFX":         "Goal SFX",
    "Arena_Music":      "Arena Music",
    "BIP_Music":        "BIP Music",
    "Organ_Crowd":      "Organ / Crowd",
    "SFX":              "Sound Effects",
    "PA":               "PA Announcer",
    "Crowd_Ambient":    "Crowd Ambient",
    "Commentary":       "Commentary",
    "Pre_Game_Faceoff": "Pre-Game / Faceoff",
    "Menu_Music":       "Menu Music",
    "Unknown":          "Unknown",
}

STEM_RE = re.compile(r"^([0-9A-Fa-f]{8})_(\d+)ch_(\d+)p$")

TEAMS = [
    "Anaheim Ducks", "Atlanta Thrashers", "Boston Bruins", "Buffalo Sabres",
    "Calgary Flames", "Carolina Hurricanes", "Chicago Blackhawks",
    "Colorado Avalanche", "Columbus Blue Jackets", "Dallas Stars",
    "Detroit Red Wings", "Edmonton Oilers", "Florida Panthers",
    "Los Angeles Kings", "Minnesota Wild", "Montreal Canadiens",
    "Nashville Predators", "New Jersey Devils", "NY Islanders", "NY Rangers",
    "Ottawa Senators", "Philadelphia Flyers", "Phoenix Coyotes",
    "Pittsburgh Penguins", "San Jose Sharks", "St. Louis Blues",
    "Tampa Bay Lightning", "Toronto Maple Leafs", "Vancouver Canucks",
    "Washington Capitals",
]

ARENA_EVENTS = [
    ("intro",       "Intro / Skate-Out"),
    ("warmup",      "Warmup"),
    ("pregame",     "Pre-Game"),
    ("bip",         "Break in Play (BIP)"),
    ("goal",        "Goal Celebration"),
    ("pp",          "Power Play"),
    ("penalty_end", "Penalty End"),
    ("intermission","Intermission"),
    ("overtime",    "Overtime"),
    ("shootout",    "Shootout"),
]

# ═══════════════════════════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════════════════════════

def bundled_tool(rel: str) -> str:
    """Path to a tool shipped WITH the launcher (tools/ next to the exe, or the dev launcher dir,
    or the PyInstaller bundle), or '' if not present. Lets the launcher be self-contained."""
    for base in (_BASE, _RES):
        p = base / "tools" / rel
        if p.exists():
            return str(p)
    return ""

DEFAULT_CONFIG: dict = {
    "root_path":        "",
    "xenia_path":       "",
    "game_path":        "",
    "xma2encode":       bundled_tool("xma2encode.exe"),
    "ffmpeg":           bundled_tool("ffmpeg/ffmpeg.exe"),
    "arena_music":      {},
}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception:
            pass
    # Prefer the tools SHIPPED with the launcher when the configured path is empty or missing
    # (e.g. a shared config points at the author's personal ffmpeg that the recipient lacks).
    for key, rel in (("xma2encode", "xma2encode.exe"), ("ffmpeg", "ffmpeg/ffmpeg.exe")):
        if not cfg.get(key) or not Path(cfg[key]).exists():
            b = bundled_tool(rel)
            if b:
                cfg[key] = b
    return cfg

def save_config(cfg: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

# ═══════════════════════════════════════════════════════════════════════════════
# Path helpers
# ═══════════════════════════════════════════════════════════════════════════════

def catalog_path(root: Path, fid: str) -> Path:
    return root / f"{fid}_Audio_Catalog.json"

def names_path(root: Path, fid: str) -> Path:
    return root / f"{fid}_Audio_Names.json"

def archive_path(root: Path, fid: str) -> Path:
    return root.parent / fid

def audio_dir(root: Path) -> Path:
    return root / "Audio"

def modified_audio_dir(root: Path) -> Path:
    return root / "Modified" / "Audio"

def category_audio_dir(root: Path, category: str) -> Path:
    folder = CATEGORY_FOLDER.get(category or "", "Unknown")
    return audio_dir(root) / folder

# ═══════════════════════════════════════════════════════════════════════════════
# Audio data helpers
# ═══════════════════════════════════════════════════════════════════════════════

def load_catalog(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def load_names_map(path: Path) -> tuple:
    if not path.exists():
        return {}, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        default_cat = raw.get("_default_category")
        entries = {k: v for k, v in raw.items()
                   if not k.startswith("_") and isinstance(v, dict)}
        return entries, default_cat
    except Exception:
        return {}, None

def save_catalog(path: Path, catalog: dict) -> None:
    path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")

def wav_info(wav_path: Path) -> tuple:
    try:
        with open(wav_path, "rb") as f:
            if f.read(4) != b"RIFF":
                return 0.0, 0
            f.seek(8)
            if f.read(4) != b"WAVE":
                return 0.0, 0
            ch = rate = bits = data_size = 0
            while True:
                chunk_id   = f.read(4)
                if len(chunk_id) < 4:
                    break
                chunk_size = struct.unpack("<I", f.read(4))[0]
                chunk_end  = f.tell() + chunk_size + (chunk_size & 1)
                if chunk_id == b"fmt " and chunk_size >= 16:
                    f.seek(2, 1)
                    ch   = struct.unpack("<H", f.read(2))[0]
                    rate = struct.unpack("<I", f.read(4))[0]
                    f.seek(6, 1)
                    bits = struct.unpack("<H", f.read(2))[0]
                elif chunk_id == b"data":
                    data_size = chunk_size
                    break
                f.seek(chunk_end)
        if rate and ch and bits and data_size:
            return data_size / (rate * ch * (bits // 8)), rate
    except Exception:
        pass
    return 0.0, 0

def wav_duration(wav_path: Path) -> float:
    return wav_info(wav_path)[0]

def _scan_wav_set(base: Path) -> set:
    """All .wav paths under `base` as a set of os.path.normcase'd strings — one directory
    walk instead of tens of thousands of per-file os.stat() calls. normcase makes the
    lookups case-insensitive, matching Windows' Path.exists() semantics exactly."""
    out = set()
    for dirpath, _dirs, files in os.walk(base):
        for fn in files:
            if fn.endswith(".wav"):
                out.add(os.path.normcase(os.path.join(dirpath, fn)))
    return out


def load_all_audio(root: Path) -> list:
    aud_dir   = audio_dir(root)
    mod_dir   = modified_audio_dir(root)
    audio_set = _scan_wav_set(aud_dir)        # ~80k files in one walk (~0.2s) vs 80k stats
    mod_set   = _scan_wav_set(mod_dir)
    rows = []
    for fid in FILE_IDS:
        cat = load_catalog(catalog_path(root, fid))
        for stem, entry in cat.items():
            friendly    = entry.get("friendly_name") or stem
            category    = entry.get("category") or "Unknown"
            folder      = CATEGORY_FOLDER.get(category, "Unknown")
            duration    = entry.get("duration", 0.0)
            channels    = entry.get("channels", 1)
            sample_rate = entry.get("sample_rate", SAMPLE_RATE)
            wav_rel     = entry.get("wav", "")
            off_hex     = entry.get("offset_hex", "")
            max_pkts    = entry.get("packets", 0)

            wav_p      = (root / wav_rel) if wav_rel else (aud_dir / folder / f"{friendly}.wav")
            wav_exists = os.path.normcase(str(wav_p)) in audio_set
            if wav_exists and not sample_rate:
                actual_dur, actual_sr = wav_info(wav_p)
                if actual_sr:
                    sample_rate = actual_sr
                if actual_dur > 0:
                    duration = actual_dur
            elif wav_exists and sample_rate != SAMPLE_RATE and not duration:
                actual_dur, _ = wav_info(wav_p)
                if actual_dur > 0:
                    duration = actual_dur

            mod_p    = mod_dir / folder / f"{friendly}.wav"
            has_mod  = os.path.normcase(str(mod_p)) in mod_set
            if not has_mod:
                mod_p2  = mod_dir / folder / f"{stem}.wav"
                if os.path.normcase(str(mod_p2)) in mod_set:
                    has_mod = True
                    mod_p = mod_p2

            bank_disp, bank_hay = audnames.bank_info(fid, entry.get("offset"))
            rows.append({
                "stem":        stem,
                "name":        friendly,
                "category":    category,
                "folder":      folder,
                "duration":    duration,
                "channels":    channels,
                "sample_rate": sample_rate,
                "source":      entry.get("source_file", fid),
                "wav_path":    str(wav_p) if wav_exists else "",
                "mod_path":    str(mod_p) if has_mod else "",
                "has_mod":     has_mod,
                "off_hex":     off_hex,
                "offset":      entry.get("offset"),
                "max_pkts":    max_pkts,
                "file_id":     fid,
                "banks":       bank_disp,
                "banks_hay":   bank_hay,
            })
    return rows

# ═══════════════════════════════════════════════════════════════════════════════
# XMA2 helpers
# ═══════════════════════════════════════════════════════════════════════════════

# Run console tools (xma2encode, ffmpeg) WITHOUT flashing a console window or stealing
# focus from the foreground app. CREATE_NO_WINDOW suppresses the console; the hidden
# STARTUPINFO is a belt-and-suspenders fallback for tools that try to show one anyway.
if sys.platform == "win32":
    _NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW
    _NO_WINDOW_STARTUPINFO = subprocess.STARTUPINFO()
    _NO_WINDOW_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _NO_WINDOW_STARTUPINFO.wShowWindow = subprocess.SW_HIDE
else:
    _NO_WINDOW_FLAGS = 0
    _NO_WINDOW_STARTUPINFO = None

# ═══════════════════════════════════════════════════════════════════════════════
# Audio Processing Optimizations (Automatic Tempdir Cleanup)
# ═══════════════════════════════════════════════════════════════════════════════
if sys.platform == "win32":
    _NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW
    _NO_WINDOW_STARTUPINFO = subprocess.STARTUPINFO()
    _NO_WINDOW_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _NO_WINDOW_STARTUPINFO.wShowWindow = subprocess.SW_HIDE
else:
    _NO_WINDOW_FLAGS = 0
    _NO_WINDOW_STARTUPINFO = None

def _run(cmd, **kw):
    """subprocess.run wrapper that prevents popping console windows on Windows."""
    kw["creationflags"] = kw.get("creationflags", 0) | _NO_WINDOW_FLAGS
    kw.setdefault("startupinfo", _NO_WINDOW_STARTUPINFO)
    return subprocess.run(cmd, **kw)


def make_riff_xma2(raw: bytes, channels: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    n_packets = len(raw) // PACKET_SIZE
    n_samples = n_packets * 2 * 512
    ch_mask   = 4 if channels == 1 else 3
    def le16(v): return struct.pack("<H", v & 0xFFFF)
    def le32(v): return struct.pack("<I", v & 0xFFFFFFFF)
    avg_bps = max(1, (len(raw) * sample_rate) // max(1, n_samples))
    fmt  = le16(0x0166) + le16(channels) + le32(sample_rate) + le32(avg_bps)
    fmt += le16(PACKET_SIZE) + le16(16) + le16(34)
    fmt += le16(1) + le32(ch_mask) + le32(n_samples)
    fmt += le32(PACKET_SIZE) + le32(0) + le32(n_samples)
    fmt += le32(0) * 2 + bytes([0, 4]) + le16(n_packets)
    hdr  = b"RIFF" + le32(4 + 8 + len(fmt) + 8 + len(raw)) + b"WAVE"
    hdr += b"fmt " + le32(len(fmt)) + fmt
    hdr += b"data" + le32(len(raw))
    return hdr + raw

def decode_xma2(raw: bytes, channels: int, out_wav: Path, xma2encode: str, sample_rate: int = 48000) -> float:
    """Decodes XMA2 raw bytes to PCM WAV using a temporary directory context manager."""
    with tempfile.TemporaryDirectory(prefix="nhl_dec_") as tmpdir:
        work = Path(tmpdir)
        xma = work / "t.xma"
        xma.write_bytes(make_riff_xma2(raw, channels, sample_rate))

        try:
            r = _run([xma2encode, str(xma), "/DecodeToPCM", str(out_wav)], capture_output=True, timeout=120)
            if r.returncode == 0 and out_wav.exists() and out_wav.stat().st_size > 512:
                dur, _ = wav_info(out_wav)
                return dur
        except Exception:
            pass
    return 0.0

def _xma2_data_from_riff(xma_data: bytes) -> bytes:
    pos = 12
    while pos < len(xma_data) - 8:
        cid = xma_data[pos:pos+4]
        csz = struct.unpack_from("<I", xma_data, pos+4)[0]
        if cid == b"data":
            raw = xma_data[pos+8: pos+8+csz]
            return raw[:(len(raw) // PACKET_SIZE) * PACKET_SIZE]
        pos += 8 + csz + (csz & 1)
    raise ValueError("No data chunk in encoded XMA2")

def encode_wav_to_xma2(wav: Path, channels: int, ffmpeg: str, xma2encode: str, sample_rate: int = 48000, quality: int = 60) -> bytes:
    """Encodes WAV file to XMA2 with guaranteed temporary file cleanup."""
    with tempfile.TemporaryDirectory(prefix="nhl_enc_") as tmpdir:
        work = Path(tmpdir)
        pcm_wav = work / "pcm.wav"
        xma_out = work / "out.xma"
        speaker = "C" if channels == 1 else "F,R"

        _run([ffmpeg, "-y", "-i", str(wav), "-acodec", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels), str(pcm_wav)], capture_output=True, check=True)

        base = [xma2encode, str(pcm_wav), "/TargetFile", str(xma_out), "/Quality", str(quality), "/Speaker", speaker]
        for cmd in [base + ["/UseLoopPoints"], base, [xma2encode, str(pcm_wav), "/TargetFile", str(xma_out), "/Quality", str(quality)]]:
            if xma_out.exists():
                xma_out.unlink()
            r = _run(cmd, capture_output=True)
            if r.returncode == 0:
                return _xma2_data_from_riff(xma_out.read_bytes())

        err = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
        raise RuntimeError(f"xma2encode failed (rc={r.returncode}): {err or 'no output'}")

def encode_wav_to_fit(wav: Path, channels: int, ffmpeg: str, xma2encode: str, sample_rate: int, max_packets: int, log=None) -> tuple:
    """Iteratively encodes audio to fit target packet constraints with auto-cleanup."""
    with tempfile.TemporaryDirectory(prefix="nhl_enc_fit_") as tmpdir:
        work = Path(tmpdir)
        pcm_wav = work / "pcm.wav"
        xma_out = work / "out.xma"
        speaker = "C" if channels == 1 else "F,R"

        _run([ffmpeg, "-y", "-i", str(wav), "-acodec", "pcm_s16le", "-ar", str(sample_rate), "-ac", str(channels), str(pcm_wav)], capture_output=True, check=True)

        def _try(q: int) -> bytes:
            base = [xma2encode, str(pcm_wav), "/TargetFile", str(xma_out), "/Quality", str(q), "/Speaker", speaker]
            for cmd in [base + ["/UseLoopPoints"], base, [xma2encode, str(pcm_wav), "/TargetFile", str(xma_out), "/Quality", str(q)]]:
                if xma_out.exists():
                    xma_out.unlink()
                r = _run(cmd, capture_output=True)
                if r.returncode == 0:
                    return _xma2_data_from_riff(xma_out.read_bytes())
            err = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
            raise RuntimeError(f"xma2encode failed (rc={r.returncode}): {err or 'no output'}")

        best = b""
        for q in [60, 50, 40, 30, 20, 10]:
            raw = _try(q)
            packets = len(raw) // 2048
            if max_packets <= 0 or packets <= max_packets:
                return raw, q
            best = raw

        return best, 10

# ═══════════════════════════════════════════════════════════════════════════════
# XMA2 stream scanner
# ═══════════════════════════════════════════════════════════════════════════════

def scan_streams(mm, file_size: int, min_pkts: int = 4) -> list:
    starts = []
    pos = 0
    while pos + PACKET_SIZE * 3 <= file_size:
        b0 = mm[pos]
        if (b0 & 0xF0) == 0x00:
            b1 = mm[pos + PACKET_SIZE]
            if (b1 & 0xF0) == 0x10:
                b2 = mm[pos + PACKET_SIZE * 2]
                if (b2 & 0xF0) == 0x20:
                    if (b0 & 0x0F) == (b1 & 0x0F) == (b2 & 0x0F):
                        if mm[pos + 6] == 0xFC:
                            if pos < PACKET_SIZE or (mm[pos - PACKET_SIZE] & 0xF0) != 0xF0:
                                starts.append(pos)
        pos += PACKET_SIZE
    streams = []
    for i, start in enumerate(starts):
        end  = starts[i+1] if i+1 < len(starts) else file_size
        pkts = (end - start) // PACKET_SIZE
        if pkts < min_pkts:
            continue
        ch = 1 if mm[start + 7] == 0x03 else 2
        streams.append({"offset": start, "packets": pkts, "channels": ch})
    return streams

# ═══════════════════════════════════════════════════════════════════════════════
# Background audio operations  (unchanged from modtool)
# ═══════════════════════════════════════════════════════════════════════════════

def op_extract(root: Path, file_ids: list, xma2encode: str, log):
    for fid in file_ids:
        arc = archive_path(root, fid)
        if not arc.exists():
            log(f"[{fid}] SKIP: archive not found at {arc}"); continue
        nm_map, default_cat = load_names_map(names_path(root, fid))
        cat       = load_catalog(catalog_path(root, fid))
        file_size = arc.stat().st_size
        log(f"[{fid}] Scanning {file_size // 1024 // 1024} MB…")
        with open(arc, "rb") as fh:
            mm      = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            streams = scan_streams(mm, file_size)
            total   = len(streams)
            log(f"[{fid}] Found {total} streams")
            ok = fail = 0
            for idx, s in enumerate(streams, 1):
                off  = s["offset"]; pkts = s["packets"]; ch = s["channels"]
                stem = f"{off:08X}_{ch}ch_{pkts}p"; hex_key = f"0x{off:08X}"
                ne   = nm_map.get(hex_key, {})
                fname = ne.get("name") if ne else None
                cat_  = ne.get("category") if ne else default_cat
                folder = CATEGORY_FOLDER.get(cat_ or "", "Unknown")
                label  = fname or stem
                dest_d = audio_dir(root) / folder
                dest_d.mkdir(parents=True, exist_ok=True)
                wav = dest_d / f"{label}.wav"
                if stem in cat and wav.exists():
                    ok += 1; continue
                raw = bytes(mm[off: off + pkts * PACKET_SIZE])
                dur = decode_xma2(raw, ch, wav, xma2encode)
                if dur > 0:
                    _, sr = wav_info(wav)
                    cat[stem] = {
                        "offset": off, "offset_hex": hex_key,
                        "packets": pkts, "channels": ch,
                        "duration": round(dur, 3), "stem": stem,
                        "friendly_name": fname, "category": cat_,
                        "source_file": fid, "sample_rate": sr,
                        "wav": str(wav.relative_to(root)),
                    }
                    ok += 1
                else:
                    fail += 1
                if idx % 50 == 0 or idx == total:
                    log(f"  [{fid}] {idx}/{total}  ok={ok} fail={fail}")
            scanned = {s["offset"] for s in streams}
            for hk, ne in nm_map.items():
                try: off = int(hk, 16)
                except ValueError: continue
                if off in scanned: continue
                m = STEM_RE.match(ne.get("stem", ""))
                if not m: continue
                ch = int(m.group(2)); pkts = int(m.group(3))
                stem = f"{off:08X}_{ch}ch_{pkts}p"
                if stem in cat: continue
                folder = CATEGORY_FOLDER.get(ne.get("category") or "", "Unknown")
                dest_d = audio_dir(root) / folder
                dest_d.mkdir(parents=True, exist_ok=True)
                wav = dest_d / f"{(ne.get('name') or stem)}.wav"
                raw = bytes(mm[off: off + pkts * PACKET_SIZE])
                dur = decode_xma2(raw, ch, wav, xma2encode)
                if dur > 0:
                    _, sr = wav_info(wav)
                    cat[stem] = {
                        "offset": off, "offset_hex": hk,
                        "packets": pkts, "channels": ch,
                        "duration": round(dur, 3), "stem": stem,
                        "friendly_name": ne.get("name"), "category": ne.get("category"),
                        "source_file": fid, "sample_rate": sr,
                        "wav": str(wav.relative_to(root)),
                    }
            mm.close()
        save_catalog(catalog_path(root, fid), cat)
        log(f"[{fid}] Done: {ok} extracted, {fail} failed")


def op_reimport(root: Path, ffmpeg: str, xma2encode: str,
                force_truncate: bool, log) -> tuple:
    mod  = modified_audio_dir(root)
    wavs = sorted(mod.rglob("*.wav")) if mod.exists() else []
    if not wavs:
        log("No files in Modified/Audio/")
        return 0, 0, 0
    all_cat: dict = {}; friendly_idx: dict = {}
    for fid in FILE_IDS:
        cat = load_catalog(catalog_path(root, fid))
        for stem, entry in cat.items():
            if not entry.get("source_file"):
                entry["source_file"] = fid
            all_cat[stem] = entry
            fn = entry.get("friendly_name")
            if fn:
                friendly_idx[fn] = stem
    pending: dict = {fid: [] for fid in FILE_IDS}
    not_found = []
    for wp in wavs:
        sk    = wp.stem
        entry = all_cat.get(sk) or all_cat.get(friendly_idx.get(sk, ""))
        if not entry:
            not_found.append(wp); continue
        sf = entry.get("source_file")
        if not sf or sf not in FILE_IDS:
            not_found.append(wp); continue
        pending[sf].append((wp, entry))
    for fid in FILE_IDS:
        n = len(pending[fid])
        if n:
            arc    = archive_path(root, fid)
            status = "archive found" if arc.exists() else "ARCHIVE NOT FOUND"
            log(f"  [{fid}] {n} file(s) queued — {status}")
    if not_found:
        log(f"WARNING: {len(not_found)} file(s) not in catalog (skipped)")
    patched = skipped_trunc = 0
    for fid, items in pending.items():
        if not items: continue
        arc = archive_path(root, fid)
        if not arc.exists():
            log(f"[{fid}] SKIP: archive not found"); continue
        bak = arc.with_suffix(".bak")
        if not bak.exists():
            log(f"[{fid}] Creating backup…")
            shutil.copy2(arc, bak)
        log(f"[{fid}] Patching {len(items)} file(s)…")
        with open(arc, "r+b") as f:
            for wp, entry in items:
                off = entry["offset"]; max_pkts = entry["packets"]
                ch  = entry["channels"]; sr = entry.get("sample_rate") or SAMPLE_RATE
                display = entry.get("friendly_name") or entry.get("stem") or wp.stem
                log(f"  {display}  @ 0x{off:08X}  slot={max_pkts}p {ch}ch")
                try:
                    raw_new, q_used = encode_wav_to_fit(
                        wp, ch, ffmpeg, xma2encode,
                        sample_rate=sr, max_packets=max_pkts, log=log)
                except Exception as e:
                    log(f"    FAILED encode: {e}"); continue
                if q_used < 60:
                    log(f"    Lowered quality to {q_used} to fit slot")
                n_new  = len(raw_new) // PACKET_SIZE
                excess = n_new - max_pkts
                if excess > 0:
                    if force_truncate:
                        raw_new = raw_new[:max_pkts * PACKET_SIZE]; n_new = max_pkts
                        log(f"    TRUNCATED {excess} excess packets")
                    else:
                        log(f"    SKIPPED: {excess} pkts too long")
                        skipped_trunc += 1; continue
                elif excess < 0:
                    raw_new = raw_new + bytes((-excess) * PACKET_SIZE)
                    log(f"    Padded {-excess} spare packets")
                f.seek(off); f.write(raw_new)
                log(f"    Written {n_new} pkts"); patched += 1
    return patched, skipped_trunc, len(not_found)


def op_reload_names(root: Path, log):
    for fid in FILE_IDS:
        nm_map, default_cat = load_names_map(names_path(root, fid))
        if not nm_map and default_cat is None: continue
        cat_p = catalog_path(root, fid); cat = load_catalog(cat_p)
        if not cat: continue
        updated = moved = 0
        for stem, entry in cat.items():
            off = entry.get("offset")
            if off is None: continue
            hk  = f"0x{off:08X}"; ne = nm_map.get(hk)
            old_wav_rel = entry.get("wav", "")
            old_wav     = root / old_wav_rel if old_wav_rel else None
            changed = False
            if ne:
                new_name = ne.get("name"); new_cat = ne.get("category")
                if new_name and entry.get("friendly_name") != new_name:
                    entry["friendly_name"] = new_name; changed = True
                if new_cat and entry.get("category") != new_cat:
                    entry["category"] = new_cat; changed = True
            elif default_cat and not entry.get("category"):
                entry["category"] = default_cat; changed = True
            if changed:
                updated += 1
                if old_wav and old_wav.exists():
                    cur_cat    = entry.get("category") or "Unknown"
                    cur_folder = CATEGORY_FOLDER.get(cur_cat, "Unknown")
                    cur_name   = entry.get("friendly_name") or stem
                    new_wav    = audio_dir(root) / cur_folder / f"{cur_name}.wav"
                    if old_wav.resolve() != new_wav.resolve():
                        new_wav.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            shutil.move(str(old_wav), str(new_wav))
                            entry["wav"] = str(new_wav.relative_to(root)); moved += 1
                        except Exception as e:
                            log(f"  [{fid}] Move failed ({old_wav.name}): {e}")
        if updated:
            save_catalog(cat_p, cat)
        log(f"[{fid}] Reloaded names: {updated} updated, {moved} files moved")


def op_patch_single(root: Path, ffmpeg: str, xma2encode: str,
                    wav_path: Path, entry: dict, log) -> bool:
    fid = entry.get("source_file")
    if not fid or fid not in FILE_IDS:
        log(f"ERROR: invalid source_file {fid!r}"); return False
    arc = archive_path(root, fid)
    if not arc.exists():
        log(f"Archive not found: {arc}"); return False
    bak = arc.with_suffix(".bak")
    if not bak.exists():
        log(f"[{fid}] Creating backup…"); shutil.copy2(arc, bak)
    off = entry["offset"]; max_pkts = entry["packets"]
    ch  = entry["channels"]; sr = entry.get("sample_rate") or SAMPLE_RATE
    display = entry.get("friendly_name") or entry.get("stem") or wav_path.stem
    log(f"{display}  @ 0x{off:08X}  slot={max_pkts}p {ch}ch")
    try:
        raw_new, q_used = encode_wav_to_fit(
            wav_path, ch, ffmpeg, xma2encode,
            sample_rate=sr, max_packets=max_pkts, log=log)
    except Exception as e:
        log(f"  FAILED encode: {e}"); return False
    if q_used < 60:
        log(f"  Lowered quality to {q_used}")
    n_new  = len(raw_new) // PACKET_SIZE; excess = n_new - max_pkts
    if excess > 0:
        raw_new = raw_new[:max_pkts * PACKET_SIZE]; n_new = max_pkts
        log(f"  Truncated to slot ({n_new} pkts)")
    elif excess < 0:
        raw_new = raw_new + bytes((-excess) * PACKET_SIZE)
        log(f"  Padded {-excess} pkts")
    with open(arc, "r+b") as f:
        f.seek(off); f.write(raw_new)
    log(f"  Written {n_new} pkts → OK"); return True


def op_set_sample_rate(root: Path, game_root: Path, fid: str,
                       offset: int, packets: int, channels: int,
                       wav_path: Path, new_rate: int,
                       xma2encode: str, log) -> bool:
    arc = game_root / fid
    if not arc.exists():
        log(f"Archive not found: {arc}"); return False
    raw_size = packets * PACKET_SIZE
    with open(arc, "rb") as f:
        f.seek(offset); raw = f.read(raw_size)
    if len(raw) < raw_size:
        log("Read error: file too short"); return False
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    log(f"Decoding at {new_rate} Hz …")
    dur = decode_xma2(raw, channels, wav_path, xma2encode, sample_rate=new_rate)
    if dur > 0:
        log(f"OK — {dur:.2f}s at {new_rate} Hz → {wav_path.name}")
        stem = f"{offset:08X}_{channels}ch_{packets}p"
        cat_p = catalog_path(root, fid); cat = load_catalog(cat_p)
        if stem in cat:
            cat[stem]["sample_rate"] = new_rate
            cat[stem]["duration"]    = round(dur, 3)
            save_catalog(cat_p, cat)
        return True
    log("Decode failed"); return False

# ═══════════════════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════════════════

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg        = load_config()
        self.audio_rows: list = []
        self.filtered:   list = []
        self._log_q:     queue.Queue = queue.Queue()
        self._op_thread: threading.Thread | None = None
        self._playing   = False
        self._pending_name_changes: dict = {}
        self._pending_rate_changes: dict = {}
        self._loading_dlg      = None
        self._ld_bar = self._ld_pct = self._ld_note = None   # loading-dialog progress widgets
        self._preview_img      = None
        self._cancel_event     = threading.Event()
        self._op_done_callback = None

        self.title(APP_TITLE)
        self.geometry("1620x900")
        self.minsize(1300, 680)
        self._set_app_icon()

        self._apply_style()
        self._build_ui()
        self.after(100, self._poll_log)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(400, self._sync_game_icon_async)   # keep the game's Xenia window icon = our art
        self.after(10, self._check_bundled_data)      # a broken install must say so up front

        if self.cfg.get("root_path") and Path(self.cfg["root_path"]).is_dir():
            self.after(50, self._reload_all)
        else:
            self._log("Welcome!  Set the game folder in the Settings tab to get started.")
            self._nb.select(self._tab_settings)

    def _check_bundled_data(self):
        """Report a broken install immediately instead of letting it surface as a mystery.

        Without this, a data file missing from the bundle degrades into a silent wrong answer deep
        in a tab — a missing discovered_assets.csv just made every portrait 'fail to decode', with
        nothing pointing at the real cause. The .spec refuses to build without these, so this only
        fires for a damaged/partial install."""
        try:
            from launcher import resources as res
            gone = res.missing()
        except Exception as e:
            self._log(f"[data] could not verify bundled data: {e}")
            return
        if not gone:
            return
        self._log(f"[data] MISSING bundled data in {res.data_dir()}: {', '.join(gone)}")
        messagebox.showwarning(
            "Missing bundled data",
            "This install is missing data file(s) the launcher needs:\n\n"
            + "".join(f"  • {f}\n" for f in gone)
            + f"\nExpected in:\n{res.data_dir()}\n\n"
              "Features that use them will fail (e.g. without discovered_assets.csv no portrait "
              "or disc_* texture can be decoded). Reinstall/rebuild to restore them.")

    # ── Style ─────────────────────────────────────────────────────────────────

    def _sync_game_icon_async(self):
        """On startup, ensure the game's Xenia window/taskbar icon is our bundled NHL 2K27 art
        (writes into the configured game XEX only if it isn't already that icon). Silent/background."""
        game_path = self.cfg.get("game_path", "").strip()
        icon = _RES / "NHL 2k27 Game Icon.png"
        if not game_path or not icon.exists():
            return
        def work():
            try:
                st = archtex.ensure_game_icon(game_path, str(icon), self._log_q.put)
                if st:
                    self._log_q.put(st)
            except Exception as e:
                self._log_q.put(f"(game icon sync skipped: {e})")
        threading.Thread(target=work, daemon=True).start()

    def _set_app_icon(self):
        """Window title-bar + taskbar icon from the bundled NHL_2k_Launcher_Icon."""
        try:
            ico = _RES / "app_icon.ico"
            if ico.exists():
                self.iconbitmap(default=str(ico))
        except Exception:
            pass
        try:                                   # PhotoImage fallback (keeps a ref alive)
            png = _RES / "NHL_2k_Launcher_Icon.png"
            if png.exists():
                self._app_icon_img = PhotoImage(file=str(png))
                self.iconphoto(True, self._app_icon_img)
        except Exception:
            pass

    # NHL 2K27 icon palette — charcoal/black shield, bright red "2K", silver bevel, white text.
    _COL = {
        "bg0":   "#141519",   # window base (near-black charcoal)
        "bg1":   "#1c1e23",   # panels / frames / notebook body
        "bg2":   "#24272e",   # inputs, tree rows, log
        "bg3":   "#2f333c",   # hover / active surface
        "fg":    "#e8eaed",   # primary text (off-white)
        "muted": "#9aa0a8",   # secondary / help text
        "border":"#3a3f4a",   # subtle borders
        "silver":"#aeb4bb",   # metallic accent (headings, separators)
        "red":   "#e0111a",   # NHL 2K red accent
        "red_hi":"#ff2a33",   # accent hover
        "red_lo":"#a80b12",   # accent pressed
    }

    def _apply_style(self):
        C = self._COL
        self.configure(bg=C["bg0"])
        # tk (non-ttk) widget defaults: Menus, Combobox popdown list.
        self.option_add("*Menu.background", C["bg2"])
        self.option_add("*Menu.foreground", C["fg"])
        self.option_add("*Menu.activeBackground", C["red"])
        self.option_add("*Menu.activeForeground", "#ffffff")
        self.option_add("*Menu.relief", "flat")
        self.option_add("*TCombobox*Listbox.background", C["bg2"])
        self.option_add("*TCombobox*Listbox.foreground", C["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", C["red"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")

        s = ttk.Style(self)
        s.theme_use("clam")
        # base — every ttk widget inherits these unless overridden below.
        s.configure(".", background=C["bg1"], foreground=C["fg"],
                    fieldbackground=C["bg2"], bordercolor=C["border"],
                    lightcolor=C["bg1"], darkcolor=C["bg1"], troughcolor=C["bg2"],
                    focuscolor=C["red"], insertcolor=C["fg"], font=("Segoe UI", 9))
        s.configure("TFrame",      background=C["bg1"])
        s.configure("TLabel",      background=C["bg1"], foreground=C["fg"])
        s.configure("TLabelframe", background=C["bg1"], bordercolor=C["border"])
        s.configure("TLabelframe.Label", background=C["bg1"], foreground=C["silver"],
                    font=("Segoe UI", 9, "bold"))
        s.configure("TPanedwindow", background=C["bg0"])
        s.configure("Sash", background=C["border"], gripcount=0)
        s.configure("TSeparator", background=C["border"])
        s.configure("Status.TLabel", background=C["bg2"], foreground=C["muted"])
        # buttons
        s.configure("TButton", background=C["bg2"], foreground=C["fg"],
                    bordercolor=C["border"], padding=[8, 4], font=("Segoe UI", 9))
        s.map("TButton",
              background=[("active", C["bg3"]), ("pressed", C["bg3"]), ("disabled", C["bg1"])],
              foreground=[("disabled", C["muted"])])
        s.configure("Accent.TButton", background=C["red"], foreground="#ffffff",
                    bordercolor=C["red"], font=("Segoe UI", 9, "bold"), padding=[10, 4])
        s.map("Accent.TButton",
              background=[("active", C["red_hi"]), ("pressed", C["red_lo"]), ("disabled", "#5a3a3d")],
              foreground=[("disabled", "#cbbcbd")])
        # notebook tabs
        s.configure("TNotebook", background=C["bg0"], bordercolor=C["border"], tabmargins=[2, 4, 2, 0])
        s.configure("TNotebook.Tab", background=C["bg1"], foreground=C["muted"],
                    padding=[12, 6], bordercolor=C["border"])
        s.map("TNotebook.Tab",
              background=[("selected", C["bg2"]), ("active", C["bg3"])],
              foreground=[("selected", C["fg"]), ("active", C["fg"])])
        # treeview (lists)
        s.configure("Treeview", background=C["bg2"], fieldbackground=C["bg2"],
                    foreground=C["fg"], rowheight=22, bordercolor=C["border"], font=("Segoe UI", 9))
        s.map("Treeview", background=[("selected", C["red"])], foreground=[("selected", "#ffffff")])
        s.configure("Treeview.Heading", background=C["bg3"], foreground=C["silver"],
                    font=("Segoe UI", 9, "bold"), relief="flat", bordercolor=C["border"])
        s.map("Treeview.Heading", background=[("active", C["bg3"])])
        # entry / combobox / spinbox
        s.configure("TEntry", fieldbackground=C["bg2"], foreground=C["fg"],
                    bordercolor=C["border"], insertcolor=C["fg"])
        s.configure("TCombobox", fieldbackground=C["bg2"], background=C["bg2"], foreground=C["fg"],
                    bordercolor=C["border"], arrowcolor=C["silver"])
        s.map("TCombobox", fieldbackground=[("readonly", C["bg2"])],
              foreground=[("readonly", C["fg"])], arrowcolor=[("active", C["fg"])])
        s.configure("TSpinbox", fieldbackground=C["bg2"], foreground=C["fg"],
                    bordercolor=C["border"], arrowcolor=C["silver"])
        # checkbutton / radiobutton
        for w in ("TCheckbutton", "TRadiobutton"):
            s.configure(w, background=C["bg1"], foreground=C["fg"])
            s.map(w, background=[("active", C["bg1"])], foreground=[("disabled", C["muted"])])
        # scrollbars
        s.configure("TScrollbar", background=C["bg2"], troughcolor=C["bg0"],
                    bordercolor=C["border"], arrowcolor=C["silver"])
        s.map("TScrollbar", background=[("active", C["bg3"])])
        # progress bar (red on dark trough)
        s.configure("Horizontal.TProgressbar", background=C["red"], troughcolor=C["bg2"],
                    bordercolor=C["border"], lightcolor=C["red"], darkcolor=C["red"])

    # ── Top-level layout ──────────────────────────────────────────────────────

    def _build_ui(self):
        top = ttk.Frame(self, padding=(8, 6, 8, 4))
        top.pack(fill=X)
        # Header brand — the NHL 2K Mod Launcher logo (crisp PIL down-scale), tinted to blend
        # with the dark bar. Falls back silently if the asset is missing.
        for _brand in ("NHL_2k_Launcher_Icon.png", "NHL 2k27 Game Icon.png"):
            _lp = _RES / _brand
            if not _lp.exists():
                continue
            try:
                im = Image.open(_lp).convert("RGBA")
                h = 46; w = max(1, int(im.width * h / im.height))
                im = im.resize((w, h), Image.LANCZOS)
                self._brand_img = ImageTk.PhotoImage(im)
                Label(top, image=self._brand_img, bd=0,
                      bg=self._COL["bg1"]).pack(side=LEFT, padx=(0, 14))
                break
            except Exception:
                pass
        # Canonical game-folder var — the field itself lives in Settings (single source of truth).
        self._v_root = StringVar(value=self.cfg.get("root_path", ""))
        # Top bar = the four primary actions.
        ttk.Button(top, text="Reload All", command=self._reload_all).pack(side=LEFT, padx=3)
        ttk.Button(top, text="Apply All Mods", style="Accent.TButton",
                   command=self._apply_all_mods).pack(side=LEFT, padx=3)
        ttk.Button(top, text="Import Mod Pack…",
                   command=self._import_modpack).pack(side=LEFT, padx=3)
        ttk.Separator(top, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=8)
        self._btn_launch = ttk.Button(top, text="▶  Launch NHL 2k10",
                                      style="Accent.TButton",
                                      command=self._launch_and_apply)
        self._btn_launch.pack(side=LEFT, padx=3)

        self._v_status = StringVar(value="Ready")
        ttk.Label(self, textvariable=self._v_status, anchor=W, style="Status.TLabel",
                  relief=FLAT, padding=(8, 3)).pack(fill=X, side=BOTTOM)

        pane = ttk.PanedWindow(self, orient=VERTICAL)
        pane.pack(fill=BOTH, expand=True, padx=6, pady=2)

        nb_frame = ttk.Frame(pane)
        pane.add(nb_frame, weight=4)

        self._nb = ttk.Notebook(nb_frame)
        self._nb.pack(fill=BOTH, expand=True)

        self._tab_audio    = ttk.Frame(self._nb)
        self._tab_iff      = ttk.Frame(self._nb)
        self._tab_banks    = ttk.Frame(self._nb)
        self._tab_arena    = ttk.Frame(self._nb)
        self._tab_teams    = ttk.Frame(self._nb)
        self._tab_goalie   = ttk.Frame(self._nb)
        self._tab_portrait = ttk.Frame(self._nb)
        self._tab_scorebug = ttk.Frame(self._nb)
        self._tab_gameplay = ttk.Frame(self._nb)
        self._tab_settings = ttk.Frame(self._nb)

        self._nb.add(self._tab_audio,    text="  Audio  ")
        self._nb.add(self._tab_iff,      text="  IFF Textures  ")
        # WIP — Audio Banks & Arena Music tabs are HIDDEN for the 1.0 release (not yet finished).
        # The frames are still created and built below so nothing else breaks; re-add these two
        # lines to bring the tabs back once they're ready. TODO: finish Audio Banks + Arena Music.
        # self._nb.add(self._tab_banks,    text="  Audio Banks  ")
        # self._nb.add(self._tab_arena,    text="  Arena Music  ")
        self._nb.add(self._tab_teams,    text="  Teams  ")
        self._nb.add(self._tab_goalie,   text="  Goalie Equipment  ")
        self._nb.add(self._tab_portrait, text="  Portraits  ")
        self._nb.add(self._tab_scorebug, text="  Scoreclock  ")
        self._nb.add(self._tab_gameplay, text="  Gameplay  ")
        self._nb.add(self._tab_settings, text="  Settings  ")

        self._build_audio_tab()
        self._build_iff_tab()
        self._build_banks_tab()
        self._build_arena_tab()
        self._build_teams_tab()
        self._build_goalie_tab()
        self._build_portrait_tab()
        self._build_scorebug_tab()
        self._build_gameplay_tab()
        self._build_settings_tab()

        log_frame = ttk.LabelFrame(pane, text="Operation Log", padding=4)
        pane.add(log_frame, weight=1)

        self._progress = ttk.Progressbar(log_frame, mode="indeterminate", length=180)
        self._progress.pack(side=LEFT, padx=(0, 6))

        self._log_box = Text(
            log_frame, height=5, state=DISABLED, wrap=WORD,
            font=("Consolas", 8), bg=self._COL["bg2"], fg="#c9ccd1",
            insertbackground=self._COL["fg"], relief=FLAT, bd=0,
            selectbackground=self._COL["red"], selectforeground="#ffffff")
        sb = ttk.Scrollbar(log_frame, command=self._log_box.yview)
        self._log_box.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        self._log_box.pack(fill=BOTH, expand=True)
        self._flush_early_log()          # anything the tabs logged while being built
        self._log_ctx = Menu(self._log_box, tearoff=0)
        self._log_ctx.add_command(label="Clear Log", command=self._clear_log)
        self._log_box.bind("<Button-3>", lambda e: (
            self._log_ctx.tk_popup(e.x_root, e.y_root),
            self._log_ctx.grab_release()))

    # ── Audio tab ─────────────────────────────────────────────────────────────

    def _build_audio_tab(self):
        t = self._tab_audio
        bar = ttk.Frame(t, padding=(4, 4, 4, 2))
        bar.pack(fill=X)

        ttk.Label(bar, text="Category:").pack(side=LEFT)
        self._v_cat = StringVar(value="All")
        self._cat_cb = ttk.Combobox(bar, textvariable=self._v_cat,
                                     state="readonly", width=20)
        self._cat_cb.pack(side=LEFT, padx=(4, 8))
        self._cat_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_audio_filter())

        ttk.Label(bar, text="Team:").pack(side=LEFT)
        self._v_audio_team = StringVar(value="Any")
        self._audio_team_cb = ttk.Combobox(
            bar, textvariable=self._v_audio_team,
            values=["Any"] + TEAMS, state="readonly", width=18)
        self._audio_team_cb.pack(side=LEFT, padx=(4, 8))
        self._audio_team_cb.bind("<<ComboboxSelected>>",
                                  lambda _: self._apply_audio_filter())

        ttk.Label(bar, text="Search:").pack(side=LEFT)
        self._v_search = StringVar()
        self._v_search.trace_add("write", lambda *_: self._apply_audio_filter())
        ttk.Entry(bar, textvariable=self._v_search, width=24).pack(side=LEFT, padx=(4, 8))

        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        ttk.Button(bar, text="Extract…",     command=self._open_extract_dlg).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Check All",     command=self._run_check).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Reload Names",  command=self._run_reload_names).pack(side=LEFT, padx=2)
        self._btn_apply_changes = ttk.Button(bar, text="Apply Changes",
                                             command=self._apply_pending_changes,
                                             state=DISABLED)
        self._btn_apply_changes.pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Patch Game",
                   style="Accent.TButton",
                   command=self._run_reimport).pack(side=LEFT, padx=2)

        lf = ttk.Frame(t)
        lf.pack(fill=BOTH, expand=True, padx=4, pady=(0, 4))

        cols = ("name", "category", "banks", "duration", "rate", "source", "ch", "modified")
        self._a_tree = ttk.Treeview(lf, columns=cols, show="headings",
                                     selectmode="extended")
        self._a_tree.heading("name",     text="Name",        command=lambda: self._sort_audio("name"))
        self._a_tree.heading("category", text="Category",    command=lambda: self._sort_audio("category"))
        self._a_tree.heading("banks",    text="Bank / Team", command=lambda: self._sort_audio("banks"))
        self._a_tree.heading("duration", text="Duration",    command=lambda: self._sort_audio("duration"))
        self._a_tree.heading("rate",     text="Sample Rate", command=lambda: self._sort_audio("rate"))
        self._a_tree.heading("source",   text="Source",      command=lambda: self._sort_audio("source"))
        self._a_tree.heading("ch",       text="Ch",          command=lambda: self._sort_audio("ch"))
        self._a_tree.heading("modified", text="Mod",         command=lambda: self._sort_audio("modified"))
        self._a_tree.column("name",     width=320, minwidth=180)
        self._a_tree.column("category", width=130, minwidth=80)
        self._a_tree.column("banks",    width=110, minwidth=70)
        self._a_tree.column("duration", width=75,  minwidth=55,  anchor=E)
        self._a_tree.column("rate",     width=80,  minwidth=60,  anchor=CENTER)
        self._a_tree.column("source",   width=55,  minwidth=45,  anchor=CENTER)
        self._a_tree.column("ch",       width=55,  minwidth=40,  anchor=CENTER)
        self._a_tree.column("modified", width=40,  minwidth=35,  anchor=CENTER)

        vsb = ttk.Scrollbar(lf, orient=VERTICAL,   command=self._a_tree.yview)
        hsb = ttk.Scrollbar(lf, orient=HORIZONTAL, command=self._a_tree.xview)
        self._a_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._a_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        lf.rowconfigure(0, weight=1); lf.columnconfigure(0, weight=1)

        self._a_tree.tag_configure("modified", foreground="#4fc3f7")
        self._a_tree.tag_configure("missing",  foreground="#888888")
        self._a_tree.tag_configure("pending",  foreground="#FFB74D")

        self._a_tree.bind("<<TreeviewSelect>>", self._on_audio_select)
        self._a_tree.bind("<Double-1>",          self._on_audio_double)
        self._a_tree.bind("<Button-3>",          self._show_audio_ctx)
        self._a_ctx = None        # context menu is rebuilt per right-click (selection-aware)

        ctrl = ttk.Frame(t, padding=(4, 2, 4, 4))
        ctrl.pack(fill=X)
        self._btn_play     = ttk.Button(ctrl, text="▶  Play",          command=self._play,          state=DISABLED)
        self._btn_stop     = ttk.Button(ctrl, text="■  Stop",          command=self._stop,          state=DISABLED)
        self._btn_replace  = ttk.Button(ctrl, text="Replace…",         command=self._replace,       state=DISABLED)
        self._btn_showfile = ttk.Button(ctrl, text="Show in Explorer",  command=self._show_in_explorer, state=DISABLED)
        self._btn_openmod  = ttk.Button(ctrl, text="Open Modified Folder",
                                         command=self._open_modified_folder)
        self._btn_play.pack(side=LEFT, padx=2)
        self._btn_stop.pack(side=LEFT, padx=2)
        ttk.Separator(ctrl, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=8)
        self._btn_replace.pack(side=LEFT, padx=2)
        self._btn_showfile.pack(side=LEFT, padx=2)
        self._btn_openmod.pack(side=RIGHT, padx=2)
        self._lbl_sel = ttk.Label(ctrl, text="No file selected",
                                   foreground="#888888", font=("Segoe UI", 8))
        self._lbl_sel.pack(side=LEFT, padx=10)
    def _build_arena_tab(self):
        t = self._tab_arena
        outer = ttk.Frame(t, padding=(12, 8))
        outer.pack(fill=BOTH, expand=True)

        ttk.Label(outer, text="Custom Arena Music",
                  font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=4, sticky=W, pady=(0, 4))
        ttk.Label(outer, text=(
            "Point each in-game music event to a folder on your PC.\n"
            "Xenia will pick tracks from the folder when that event fires.\n"
            "This feature requires additional game-file patching (coming soon)."),
                  foreground="#888888").grid(
            row=1, column=0, columnspan=4, sticky=W, pady=(0, 14))

        ttk.Label(outer, text="Team / Arena:").grid(row=2, column=0, sticky=W, pady=4)
        self._v_arena_team = StringVar(value="All Arenas")
        ttk.Combobox(outer, textvariable=self._v_arena_team,
                     values=["All Arenas"] + TEAMS,
                     state="readonly", width=24).grid(
            row=2, column=1, sticky=W, padx=(6, 0), pady=4)

        ttk.Separator(outer).grid(row=3, column=0, columnspan=4, sticky=EW, pady=10)

        self._arena_vars: dict = {}
        for r_idx, (key, label) in enumerate(ARENA_EVENTS, start=4):
            ttk.Label(outer, text=f"{label}:").grid(
                row=r_idx, column=0, sticky=W, pady=3)
            v = StringVar(
                value=self.cfg.get("arena_music", {}).get(key, ""))
            self._arena_vars[key] = v
            entry = ttk.Entry(outer, textvariable=v, width=48)
            entry.grid(row=r_idx, column=1, padx=(6, 4), sticky=EW, pady=3)
            def _browse(var=v):
                p = filedialog.askdirectory(title="Select music folder")
                if p:
                    var.set(p)
            ttk.Button(outer, text="Browse…", command=_browse).grid(
                row=r_idx, column=2, padx=(0, 4), pady=3)

        outer.columnconfigure(1, weight=1)

        ttk.Separator(outer).grid(
            row=4 + len(ARENA_EVENTS), column=0, columnspan=4, sticky=EW, pady=12)

        def _save_arena():
            am = {k: v.get().strip() for k, v in self._arena_vars.items()}
            self.cfg["arena_music"] = am
            save_config(self.cfg)
            messagebox.showinfo("Saved", "Arena music paths saved to config.")

        ttk.Button(outer, text="Save Paths", style="Accent.TButton",
                   command=_save_arena).grid(
            row=5 + len(ARENA_EVENTS), column=1, sticky=W)

    # ── Audio Banks tab — parse IFF sound banks (sound -> wave-offset directory) ─
    def _build_banks_tab(self):
        t = self._tab_banks
        self._bank_records = []; self._bank_filtered = []; self._bank_meta = None
        bar = ttk.Frame(t, padding=(4, 4, 4, 2)); bar.pack(fill=X)
        ttk.Label(bar, text="Bank:").pack(side=LEFT)
        self._v_bank = StringVar()
        self._bank_menu = bankparse.bank_menu()                    # [(name, label), ...]
        self._bank_cb = ttk.Combobox(bar, textvariable=self._v_bank,
                                     values=[lbl for _n, lbl in self._bank_menu], width=34)
        self._bank_cb.pack(side=LEFT, padx=(4, 6))
        self._bank_cb.bind("<<ComboboxSelected>>", lambda _: self._bank_parse())
        ttk.Button(bar, text="Parse", style="Accent.TButton",
                   command=self._bank_parse).pack(side=LEFT, padx=2)
        ttk.Label(bar, text="Search:").pack(side=LEFT, padx=(8, 0))
        self._v_bank_search = StringVar()
        self._v_bank_search.trace_add("write", lambda *_: self._bank_apply_filter())
        ttk.Entry(bar, textvariable=self._v_bank_search, width=18).pack(side=LEFT, padx=(4, 8))
        ttk.Button(bar, text="Export Bank…", command=self._bank_export).pack(side=RIGHT, padx=2)

        ttk.Label(t, foreground="#888888", font=("Segoe UI", 8),
                  text="A bank is the game's sound directory: each record points at a wave "
                       "OFFSET in 1A/1B. Arena banks (arena_<code>.iff) link to named sounds; "
                       "crowd/bootup banks point into their own wave region (shown unlinked)."
                  ).pack(anchor=W, padx=6)

        lf = ttk.Frame(t); lf.pack(fill=BOTH, expand=True, padx=4, pady=(2, 4))
        cols = ("bankoff", "wave", "arc", "rate", "sound", "dur", "ch")
        self._bk_tree = ttk.Treeview(lf, columns=cols, show="headings", selectmode="browse")
        for c, txt, w, anc in (("bankoff", "Bank Offset", 95, CENTER),
                               ("wave", "Wave Offset", 100, CENTER), ("arc", "Arc", 45, CENTER),
                               ("rate", "Rate", 70, CENTER), ("sound", "Linked Sound", 320, W),
                               ("dur", "Dur", 60, E), ("ch", "Ch", 60, CENTER)):
            self._bk_tree.heading(c, text=txt); self._bk_tree.column(c, width=w, anchor=anc)
        vsb = ttk.Scrollbar(lf, orient=VERTICAL, command=self._bk_tree.yview)
        self._bk_tree.configure(yscrollcommand=vsb.set)
        self._bk_tree.grid(row=0, column=0, sticky="nsew"); vsb.grid(row=0, column=1, sticky="ns")
        lf.rowconfigure(0, weight=1); lf.columnconfigure(0, weight=1)
        self._bk_tree.tag_configure("linked", foreground="#4fc3f7")
        self._bk_tree.tag_configure("unlinked", foreground="#888888")
        self._bk_tree.tag_configure("modified", foreground="#81c784")
        self._bk_tree.bind("<Double-1>", lambda e: self._bank_play())

        ctrl = ttk.Frame(t, padding=(4, 2, 4, 4)); ctrl.pack(fill=X)
        ttk.Button(ctrl, text="▶  Play Linked", command=self._bank_play).pack(side=LEFT, padx=2)
        ttk.Button(ctrl, text="Replace Linked…", command=self._bank_replace).pack(side=LEFT, padx=2)
        ttk.Button(ctrl, text="Show in Audio Tab",
                   command=self._bank_show_in_audio).pack(side=LEFT, padx=2)
        ttk.Button(ctrl, text="Patch Game", style="Accent.TButton",
                   command=self._run_reimport).pack(side=RIGHT, padx=2)
        self._bk_status = ttk.Label(ctrl, text="Select a bank and press Parse.",
                                    foreground="#888888", font=("Segoe UI", 8))
        self._bk_status.pack(side=LEFT, padx=10)

    def _bank_name_from_label(self, label: str) -> str:
        for n, lbl in self._bank_menu:
            if label in (lbl, n):
                return n
        return label.split()[0]                       # free-typed: first token is the name

    def _bank_parse(self):
        if self._op_busy(): return
        label = self._v_bank.get().strip()
        if not label:
            messagebox.showinfo("No Bank", "Pick or type a bank (e.g. arena_van.iff)."); return
        if not self.audio_rows:
            messagebox.showinfo("No Catalog", "Load the game folder / extract audio first."); return
        name = self._bank_name_from_label(label)
        oba: dict = {}
        for r in self.audio_rows:
            o = r.get("offset")
            if o is not None:
                oba.setdefault(r["file_id"], set()).add(o)
        self._log(f"─── Parse bank: {name} ───")
        def work():
            try:
                meta, recs = bankparse.parse_bank(name, oba)
            except Exception as e:
                self._log_q.put(f"ERROR parsing {name}: {e}")
                self._bank_records = []; self._bank_meta = None; return
            if meta is None:
                self._log_q.put(f"{name}: not found in TOC (not a loadable bank).")
                self._bank_records = []; self._bank_meta = None
            else:
                self._bank_records = recs; self._bank_meta = meta
                self._log_q.put(f"{name}: {meta['decompressed']} bytes, {len(recs)} records, "
                                f"{meta.get('linked', 0)} linked to streams.")
        self._run_in_thread(work, op_label=f"Parsing {name}…", on_done=self._bank_populate)

    def _bank_populate(self):
        idx = {(r["file_id"], r["offset"]): r
               for r in self.audio_rows if r.get("offset") is not None}
        for r in self._bank_records:
            row = idx.get((r["archive"], r["wave_off"])) if r["archive"] else None
            r["_row"]   = row
            r["_sound"] = row["name"] if row else ""
            r["_dur"]   = row["duration"] if row else 0
            r["_ch"]    = row["channels"] if row else 0
        self._bank_apply_filter()
        if self._bank_meta:
            m = self._bank_meta
            self._bk_status.config(
                text=f"{m['name']}  |  {m['archive']}  |  {len(self._bank_records)} records  |  "
                     f"{m.get('linked', 0)} link to streams")

    def _bank_apply_filter(self):
        q = self._v_bank_search.get().lower().strip()
        self._bank_filtered = [r for r in self._bank_records
                               if not q or q in r.get("_sound", "").lower()
                               or q in f"{r['wave_off']:08x}"]
        self._bk_tree.delete(*self._bk_tree.get_children())
        for r in self._bank_filtered:
            rate  = f"{r['sample_rate'] // 1000} kHz" if r.get("sample_rate") else "—"
            dur   = f"{r['_dur']:.1f}s" if r.get("_dur") else ""
            ch    = ("Mono" if r["_ch"] == 1 else "Stereo") if r.get("_ch") else ""
            row   = r.get("_row")
            modded = bool(row and row.get("has_mod"))
            tag   = "modified" if modded else ("linked" if row else "unlinked")
            sound = (r.get("_sound") or "(unlinked)") + ("   ✓ replaced" if modded else "")
            self._bk_tree.insert("", END, tags=(tag,), values=(
                f"0x{r['bank_off']:06X}", f"0x{r['wave_off']:08X}", r["archive"] or "?",
                rate, sound, dur, ch))

    def _bank_selected(self):
        sel = self._bk_tree.selection()
        if not sel: return None
        i = self._bk_tree.index(sel[0])
        return self._bank_filtered[i] if i < len(self._bank_filtered) else None

    def _bank_play(self):
        r = self._bank_selected()
        if not r or not r.get("_row"):
            messagebox.showinfo("No Linked Sound",
                "This record's wave isn't in the audio catalog (crowd/bootup banks point "
                "into their own wave region)."); return
        row = r["_row"]; wav = row.get("mod_path") or row.get("wav_path")
        if not wav:
            messagebox.showinfo("Not Extracted",
                f"Extract {row['file_id']} audio first to hear it."); return
        try:
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as e:
            messagebox.showerror("Playback Error", str(e))

    def _bank_replace(self):
        r = self._bank_selected()
        if not r or not r.get("_row"):
            messagebox.showinfo("No Linked Sound",
                "Select a record linked to a catalogued sound (blue rows) to replace it.\n\n"
                "Unlinked records (crowd/bootup) point outside the audio catalog."); return
        self._replace_row(r["_row"])

    def _bank_show_in_audio(self):
        r = self._bank_selected()
        if not r or not r.get("_row"): return
        self._v_cat.set("All"); self._v_audio_team.set("Any")
        self._v_search.set(r["_row"]["name"])
        self._apply_audio_filter()
        self._nb.select(self._tab_audio)

    def _bank_export(self):
        if not self._bank_meta:
            messagebox.showinfo("No Bank", "Parse a bank first."); return
        name = self._bank_meta["name"]
        dec, _ = bankparse.decompress_bank(name)
        if dec is None:
            messagebox.showerror("Error", "Could not decompress bank."); return
        p = filedialog.asksaveasfilename(
            title="Export decompressed bank", defaultextension=".bin",
            initialfile=name.replace(".iff", "_bank.bin"),
            filetypes=[("Binary", "*.bin"), ("All files", "*.*")])
        if not p: return
        Path(p).write_bytes(dec)
        self._log(f"Exported {name} ({len(dec)} bytes) -> {p}")

    # ── Goalie Equipment tab (live in-memory mask assignment — Option B) ────────
    # Masks are assigned by patching the running game's memory (player+0xB4 shell / +0xB8 pattern);
    # the roster/game files are NOT modified (that field is scrambled on disk). So this works only
    # with the launcher attached to Xenia, and re-applies on each launch. See launcher/goalie_equipment.py.
    def _build_goalie_tab(self):
        t = self._tab_goalie
        self._goalie_rows = []; self._ga_mask_map = {}
        self._ga_prev_cache = {}; self._ga_prev_imgs = []
        outer = ttk.Frame(t, padding=(12, 8)); outer.pack(fill=BOTH, expand=True)
        ttk.Label(outer, text="Goalie Equipment", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        ttk.Label(outer, text=(
            "Repaint an existing goalie-mask slot with your own design, then assign it to goalies. "
            "(The game's mask set is a fixed grid — new slots can't be added, so custom masks repaint "
            "shipped patterns in place.) Assignments are SAVED in the launcher and re-applied every time "
            "you press Launch. Assigning needs the game running only while you set it up (the roster's "
            "mask field is scrambled on disk, so the launcher writes the live copy instead)."),
            foreground="#bdbdbd", wraplength=800, justify=LEFT).pack(anchor=W, pady=(2, 10))

        # 1 ── Create ───────────────────────────────────────────────────────────
        cm = ttk.LabelFrame(outer, text="1.  Repaint a mask slot  (in-place — overwrites an existing design)", padding=8)
        cm.pack(fill=X, pady=(0, 10))
        r1 = ttk.Frame(cm); r1.pack(fill=X, pady=(0, 5))
        ttk.Label(r1, text="Name").pack(side=LEFT)
        self._v_gm_name = StringVar()
        ttk.Entry(r1, textvariable=self._v_gm_name, width=22).pack(side=LEFT, padx=(4, 12))
        ttk.Label(r1, text="Style  g").pack(side=LEFT)
        self._v_gm_shell = StringVar(value="01")
        ttk.Combobox(r1, textvariable=self._v_gm_shell, width=4, state="readonly",
                     values=["01", "02", "03", "04", "05", "06"]).pack(side=LEFT, padx=(2, 12))
        ttk.Label(r1, text="Pattern").pack(side=LEFT)
        self._v_gm_pat = StringVar(value="1")
        ttk.Spinbox(r1, from_=1, to=31, width=4, textvariable=self._v_gm_pat).pack(side=LEFT, padx=(2, 12))
        ttk.Label(r1, text="Quality").pack(side=LEFT)
        self._v_gm_fmt = StringVar(value="8888 (best)")
        ttk.Combobox(r1, textvariable=self._v_gm_fmt, width=14, state="readonly",
                     values=["8888 (best)", "4444 (half size)"]).pack(side=LEFT, padx=(2, 0))
        r2 = ttk.Frame(cm); r2.pack(fill=X)
        self._v_gm_img = StringVar()
        ttk.Entry(r2, textvariable=self._v_gm_img, width=52).pack(side=LEFT, padx=(0, 4))
        ttk.Button(r2, text="Image…", command=self._goalie_pick_image).pack(side=LEFT)
        ttk.Button(cm, text="Repaint Slot", style="Accent.TButton", command=self._goalie_create_mask).pack(anchor=W, pady=(8, 0))
        ttk.Label(cm, text="Overwrites that pattern's design — pick one no goalie wears. Styles: g01/g04, g02/g03, "
                           "g05/g06 are the 3 mask shapes (g01 matches the Substance project). Stored uncompressed "
                           "(crisp edges, no DXT blockiness) — 8888 is pristine, 4444 is half the file size with faint "
                           "gradient banding. Any detail/resolution is fine — it grows as needed.",
                  foreground="#888", font=("Segoe UI", 8), wraplength=800, justify=LEFT).pack(anchor=W, pady=(4, 0))

        # 2 ── Assign ───────────────────────────────────────────────────────────
        asg = ttk.LabelFrame(outer, text="2.  Assign a mask to goalies", padding=8)
        asg.pack(fill=BOTH, expand=True)
        bar = ttk.Frame(asg); bar.pack(fill=X)
        ttk.Label(bar, text="Mask:").pack(side=LEFT)
        self._v_ga_mask = StringVar()
        self._ga_mask_cb = ttk.Combobox(bar, textvariable=self._v_ga_mask, width=36, state="readonly")
        self._ga_mask_cb.pack(side=LEFT, padx=(4, 14))
        ttk.Button(bar, text="Refresh goalies", command=self._goalie_refresh).pack(side=LEFT)
        ttk.Label(bar, text="Filter:").pack(side=LEFT, padx=(12, 2))
        self._v_gm_filter = StringVar()
        self._v_gm_filter.trace_add("write", lambda *a: self._goalie_populate())
        ttk.Entry(bar, textvariable=self._v_gm_filter, width=16).pack(side=LEFT)
        ttk.Label(bar, text="  (Ctrl/Shift-click to multi-select)", foreground="#888",
                  font=("Segoe UI", 8)).pack(side=LEFT)
        self._ga_mask_cb.bind("<<ComboboxSelected>>", lambda e: self._goalie_show_mask_preview())
        mid = ttk.Frame(asg); mid.pack(fill=BOTH, expand=True, pady=(6, 0))
        tvf = ttk.Frame(mid); tvf.pack(side=LEFT, fill=BOTH, expand=True)
        tv = ttk.Treeview(tvf, columns=("name", "mask", "saved"), show="headings", height=11, selectmode="extended")
        for c, w, txt, anc in (("name", 250, "Goalie", W),
                               ("mask", 160, "Current mask", W), ("saved", 170, "Assigned (saved)", W)):
            tv.heading(c, text=txt); tv.column(c, width=w, anchor=anc)
        tv.tag_configure("assigned", foreground="#4ec26b")
        sb = ttk.Scrollbar(tvf, command=tv.yview); tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); tv.pack(side=LEFT, fill=BOTH, expand=True)
        self._goalie_tv = tv
        prev = ttk.LabelFrame(mid, text="Selected mask", padding=6); prev.pack(side=LEFT, fill=Y, padx=(10, 0))
        self._ga_mask_preview = ttk.Label(prev, text="(select a mask)", anchor="center", width=22)
        self._ga_mask_preview.pack()
        self._ga_mask_prev_lbl = ttk.Label(prev, text="", foreground="#888", font=("Segoe UI", 8),
                                           wraplength=160, justify="center")
        self._ga_mask_prev_lbl.pack(pady=(4, 0))
        ab = ttk.Frame(asg); ab.pack(fill=X, pady=(8, 0))
        ttk.Button(ab, text="Assign selected mask → selected goalies", style="Accent.TButton",
                   command=self._goalie_assign).pack(side=LEFT)
        ttk.Button(ab, text="Clear selected", command=self._goalie_clear_selected).pack(side=LEFT, padx=(8, 0))
        ttk.Button(ab, text="Clear ALL saved", command=self._goalie_clear_saved).pack(side=LEFT, padx=(8, 0))
        self._goalie_status = ttk.Label(asg, text="Press “Refresh goalies” with the game running to list goalies.",
                                        foreground="#888", font=("Consolas", 8))
        self._goalie_status.pack(anchor=W, pady=(4, 0))
        self._goalie_refresh_masklist()

    # ── mask list / free-slot helpers ───────────────────────────────────────────
    def _goalie_mask_choices(self):
        """[(label, filename_shell, filename_pattern)] — created customs first, then built-ins."""
        import re
        choices, seen = [], set()
        for cmk in self.cfg.get("custom_masks", []):
            s, p = int(cmk.get("shell", 0)), int(cmk.get("pattern", 0))
            nm = cmk.get("name") or cmk.get("file", "")
            choices.append((f"★ {nm}   (g{s:02d} · slot {p})", s, p)); seen.add((s, p))
        rx = re.compile(r"helmet_g0*(\d+)_pattern_0*(\d+)\.iff$", re.I)
        built = set()
        for r in getattr(self, "_iff_catalog", []):
            m = rx.match(r.get("iff", ""))
            if m:
                sp = (int(m.group(1)), int(m.group(2)))
                if sp not in seen:
                    built.add(sp)
        for s, p in sorted(built):
            choices.append((f"Built-in   g{s:02d} pattern {p:02d}", s, p))
        return choices

    def _goalie_refresh_masklist(self):
        if not hasattr(self, "_ga_mask_cb"):
            return
        ch = self._goalie_mask_choices()
        self._ga_mask_map = {lbl: (s, p) for lbl, s, p in ch}
        self._ga_mask_cb["values"] = [lbl for lbl, _, _ in ch]
        if ch and self._v_ga_mask.get() not in self._ga_mask_map:
            self._v_ga_mask.set(ch[0][0])
        if hasattr(self, "_ga_mask_preview"):
            self._goalie_show_mask_preview()

    def _goalie_show_mask_preview(self, *a):
        """Decode + show the currently-selected mask (from the game files so repaints show; falls back
        to the shipped CLEAN design). Threaded + cached so the UI never blocks."""
        sp = self._ga_mask_map.get(self._v_ga_mask.get())
        lbl = self._v_ga_mask.get()
        if not sp:
            self._ga_mask_preview.config(image="", text="(no mask)")
            self._ga_mask_prev_lbl.config(text=""); return
        shell, pat = sp
        name = f"helmet_g{shell:02d}_pattern_{pat:02d}.iff"
        if name in self._ga_prev_cache:
            self._ga_set_mask_preview(self._ga_prev_cache[name], lbl); return
        self._ga_mask_preview.config(image="", text="loading…")
        import threading
        def work():
            img = None
            try:
                gd = self._get_game_root()
                if gd:
                    img = archtex.decode_preview(name, gd)
            except Exception:
                img = None
            if img is None:
                try:
                    img = archtex.decode_preview(name)
                except Exception:
                    img = None
            def done():
                self._ga_prev_cache[name] = img
                self._ga_set_mask_preview(img, lbl)
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _ga_set_mask_preview(self, img, lbl):
        if img is None:
            self._ga_mask_preview.config(image="", text="(preview\nunavailable)")
            self._ga_mask_prev_lbl.config(text=lbl); return
        try:
            bg = Image.new("RGBA", img.size, (64, 64, 64, 255))
            im = Image.alpha_composite(bg, img.convert("RGBA")).convert("RGB")
            im.thumbnail((150, 150))
            pi = ImageTk.PhotoImage(im)
            self._ga_prev_imgs.append(pi)
            if len(self._ga_prev_imgs) > 40:
                self._ga_prev_imgs = self._ga_prev_imgs[-40:]
            self._ga_mask_preview.config(image=pi, text="")
            self._ga_mask_preview.image = pi
            self._ga_mask_prev_lbl.config(text=lbl)
        except Exception:
            self._ga_mask_preview.config(image="", text="(preview error)")

    def _goalie_pick_image(self):
        p = filedialog.askopenfilename(title="Mask image",
                                       filetypes=[("Images", "*.png *.dds *.tga *.jpg *.jpeg *.bmp"), ("All", "*.*")])
        if p:
            self._v_gm_img.set(p)

    def _restore_mask_clean_inplace(self, name, game_dir):
        """Restore a mask asset to its pristine CLEAN bytes at its ORIGINAL in-bounds location and
        repoint the TOC there. Reliable in-place — unlike archtex.ensure_clean, which can RELOCATE to
        1B (appended/relocated data does not load under Xenia)."""
        import struct, zlib, os
        game_dir = str(game_dir)
        c = archtex.resolve(name, game_dir, clean=True)     # pristine toc (0A.orig)
        if not c:
            return
        arc, coff, csize, cf3 = c[0], c[1], c[2], c[4]
        with open(archtex._arc_file(game_dir, arc, clean=True), "rb") as f:
            f.seek(coff); data = f.read(csize)
        with open(os.path.join(game_dir, arc), "r+b") as f:
            f.seek(coff); f.write(data)
        h = zlib.crc32(name.upper().encode("ascii")) & 0xffffffff
        a0 = os.path.join(game_dir, "0A")
        d = bytearray(open(a0, "rb").read(0x9800))
        for i in range(archtex._BE(d, 0x10)):
            e = 0x58 + i * 16
            if archtex._BE(d, e + 8) == h:
                struct.pack_into(">IIII", d, e, archtex._BE(d, e + 0), csize, h, cf3); break
        with open(a0, "r+b") as f:
            f.seek(0); f.write(d)

    def _goalie_create_mask(self):
        if self._op_busy():
            return
        img = self._v_gm_img.get().strip()
        game_dir = self._get_game_root()
        if not img or not Path(img).exists():
            messagebox.showerror("Repaint Slot", "Pick a valid image first."); return
        if not game_dir:
            messagebox.showerror("Repaint Slot", "Set the game-files folder in Settings first."); return
        try:
            shell = int(self._v_gm_shell.get()); pat = int(self._v_gm_pat.get())
        except ValueError:
            messagebox.showerror("Repaint Slot", "Style / pattern must be numbers."); return
        if not (1 <= shell <= 6) or not (1 <= pat <= 31):
            messagebox.showerror("Repaint Slot", "Style must be g01–g06 (only those render), pattern 1–31."); return
        name = f"helmet_g{shell:02d}_pattern_{pat:02d}.iff"
        friendly = self._v_gm_name.get().strip() or f"Repaint g{shell:02d} p{pat}"
        if not messagebox.askyesno("Repaint Slot",
                f"Repaint {name} with:\n{Path(img).name}\n\nThis overwrites that pattern's design — "
                f"any goalie already wearing it will change too.  Continue?"):
            return
        new_fmt = "4444" if self._v_gm_fmt.get().startswith("4444") else "8888"
        self._gm_pending = {"name": friendly, "shell": shell, "pattern": pat, "file": name}
        self._gm_ok = False
        def work():
            # reset to a clean in-bounds base so re-repaints don't stack grows; then splice as the chosen
            # uncompressed format (no DXT block artifacts / jagged diagonals). 8888 = pristine (4 B/px);
            # 4444 = half the storage (2 B/px), block-free too, ~4-bit gradient banding. Grows/relocates
            # fine on g01–g06.
            self._restore_mask_clean_inplace(name, game_dir)
            status = archtex.replace_primary_convert(name, img, game_dir, new_fmt,
                                                     log=lambda m: self._log_q.put(m))
            self._log_q.put(status)
            self._gm_ok = True
        self._run_in_thread(work, op_label=f"Repainting {name}…", on_done=self._goalie_create_done)

    def _goalie_create_done(self):
        if not getattr(self, "_gm_ok", False):
            messagebox.showerror("Repaint Slot", "Repaint failed — see the Operation Log for details.")
            return
        masks = self.cfg.setdefault("custom_masks", [])
        f = self._gm_pending["file"]
        masks[:] = [m for m in masks if m.get("file") != f] + [self._gm_pending]   # de-dupe by slot
        self._ga_prev_cache.pop(f, None)                 # repainted -> re-decode its preview
        save_config(self.cfg)
        self._goalie_refresh_masklist()
        self._v_ga_mask.set(f"★ {self._gm_pending['name']}   "
                            f"(g{self._gm_pending['shell']:02d} · pattern {self._gm_pending['pattern']})")
        self._goalie_show_mask_preview()
        self._v_gm_name.set(""); self._v_gm_img.set("")
        messagebox.showinfo("Repaint Slot",
                            f"Repainted “{self._gm_pending['name']}” (g{self._gm_pending['shell']:02d} "
                            f"pattern {self._gm_pending['pattern']}).\nIt's selected in the Assign list "
                            f"below — check goalies, then Assign.")

    def _goalie_refresh(self):
        self._goalie_status.config(text="Reading roster from the running game…")
        import threading
        def work():
            try:
                from launcher import goalie_equipment as ge
            except ImportError:
                import goalie_equipment as ge
            gs, err = ge.list_goalies()
            self.after(0, lambda: self._goalie_loaded(gs, err))
        threading.Thread(target=work, daemon=True).start()

    def _goalie_loaded(self, gs, err):
        self._goalie_rows = gs or []
        if err:
            self._goalie_status.config(text=err)
        else:
            self._goalie_status.config(text=f"{len(gs)} goalies in the loaded roster.")
        self._goalie_populate()

    def _goalie_populate(self):
        tv = getattr(self, "_goalie_tv", None)
        if tv is None:
            return
        prev = set(tv.selection())
        tv.delete(*tv.get_children())
        q = self._v_gm_filter.get().strip().lower()
        saved = self.cfg.get("goalie_masks", {})
        seen = set()
        for g in self._goalie_rows:
            key = f"{g['first']}|{g['last']}"
            if key in seen:                          # one row per goalie (names repeat across pools)
                continue
            if q and q not in g["name"].lower():
                continue
            seen.add(key)
            mask = f"g{g['shell'] + 1:02d}  pattern {g['pattern']:02d}"
            sv = saved.get(key)
            svtxt = f"→ g{sv[0] + 1:02d} slot {sv[1]}" if sv else ""
            tv.insert("", END, iid=key, values=(g["name"] or "(unnamed)", mask, svtxt),
                      tags=("assigned",) if sv else ())
        restore = [k for k in prev if tv.exists(k)]
        if restore:
            tv.selection_set(restore)

    def _goalie_assign(self):
        sel = list(self._goalie_tv.selection())
        if not sel:
            messagebox.showinfo("Assign", "Select one or more goalies in the list first "
                                          "(click, or Ctrl/Shift-click for several)."); return
        sp = self._ga_mask_map.get(self._v_ga_mask.get())
        if not sp:
            messagebox.showinfo("Assign", "Pick a mask from the “Mask:” list first "
                                          "(create one above, or choose a built-in)."); return
        shell, pat = sp                              # filename shell / pattern
        mem_shell = max(0, shell - 1)                # roster/memory shell is 0-based (filename = shell+1)
        label = self._v_ga_mask.get()
        identity = 1 if label.startswith("★") else 0  # ★ = a repainted custom mask → RGB-identity colors
        n_sel = len(sel)
        saved = dict(self.cfg.get("goalie_masks", {}))
        for key in sel:
            saved[key] = [mem_shell, pat, identity]
        self.cfg["goalie_masks"] = saved
        save_config(self.cfg)
        import threading
        def work():
            try:
                from launcher import goalie_equipment as ge
            except ImportError:
                import goalie_equipment as ge
            assigns = {k: tuple(v) for k, v in self.cfg.get("goalie_masks", {}).items()}
            n, err = ge.apply_masks(assigns)
            def done():
                if err:
                    self._goalie_status.config(text=f"Saved to {n_sel} goalie(s). Not applied live "
                                                    f"({err}) — it'll apply automatically on next Launch.")
                else:
                    self._goalie_status.config(text=f"Assigned “{label}” to {n_sel} goalie(s) — "
                                                    f"wrote {n} record(s) live + saved for every Launch. "
                                                    f"Masks show when the goalie next loads (start/reload a game).")
                self._goalie_refresh()
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _goalie_clear_selected(self):
        sel = list(self._goalie_tv.selection())
        if not sel:
            messagebox.showinfo("Clear", "Select the goalies whose assignment you want to remove."); return
        saved = dict(self.cfg.get("goalie_masks", {}))
        removed = sum(1 for k in sel if saved.pop(k, None) is not None)
        self.cfg["goalie_masks"] = saved
        save_config(self.cfg)
        self._goalie_status.config(text=f"Removed {removed} saved assignment(s). "
                                        f"Their live mask stays until the game reloads the roster.")
        self._goalie_populate()

    def _goalie_clear_saved(self):
        if self.cfg.get("goalie_masks") and not messagebox.askyesno(
                "Clear ALL saved", "Remove every saved goalie-mask assignment?"):
            return
        self.cfg["goalie_masks"] = {}
        save_config(self.cfg)
        self._goalie_status.config(text="Cleared all saved assignments (live game state is unchanged until reload).")
        self._goalie_populate()

    def _goalie_apply_saved_async(self, tries=18):
        """Auto-apply saved goalie masks to the running game after launch. The roster isn't in
        memory until the game boots to a roster-loaded state, so poll (~20 s apart) until it
        applies — or give up after `tries`."""
        saved = self.cfg.get("goalie_masks", {})
        if not saved:
            return
        import threading
        def work():
            try:
                from launcher import goalie_equipment as ge
            except ImportError:
                import goalie_equipment as ge
            assigns = {k: tuple(v) for k, v in saved.items()}
            n, err = ge.apply_masks(assigns)
            def done():
                if n > 0:
                    self._log(f"[goalie] auto-applied {n} saved mask assignment(s) to the running game")
                elif tries > 1:
                    self.after(20000, lambda: self._goalie_apply_saved_async(tries - 1))
                elif err:
                    self._log(f"[goalie] auto-apply gave up ({err})")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ── Player Portraits tab (live in-memory portrait assignment) ───────────────
    # A player's UI portrait (shoulders-up headshot) is chosen by the u16 at player+0x1C ('portrait
    # key'): the game loads crc32('%04d_image' % key) and each portrait blob's header carries that crc,
    # so a player shows portrait key_blob[key]. We reassign by writing that key in the running game
    # (Roster.ROS stores players in a different, mostly-empty on-disk layout where the key isn't plainly
    # at +0x1C), saved and re-applied on every Launch — same model as Goalie Equipment. RE:
    # Function_83D32188 / FUN_840a69e0. Mapping: archive_textures.portrait_key_blob_map().
    def _build_portrait_tab(self):
        t = self._tab_portrait
        self._portrait_rows = []             # live roster players
        self._pa_key_name = {}               # portrait key -> a player name (for labels)
        self._pa_sources = []                # [(key, blob, name)] for the picker
        self._pa_thumb_cache = {}            # blob -> PhotoImage
        outer = ttk.Frame(t, padding=(12, 8)); outer.pack(fill=BOTH, expand=True)
        ttk.Label(outer, text="Player Portraits", font=("Segoe UI", 13, "bold")).pack(anchor=W)
        ttk.Label(outer, text=(
            "Give any player a different UI portrait (the shoulders-up headshot on roster / player "
            "screens). Pick target players on the left, pick the portrait to give them on the right, "
            "then Assign. Assignments are SAVED and re-applied every time you press Launch. Assigning "
            "needs the game running only while you set it up — the launcher writes the live roster "
            "(the portrait field is stored differently on disk, so it can't be edited in the file)."),
            foreground="#bdbdbd", wraplength=820, justify=LEFT).pack(anchor=W, pady=(2, 10))

        bar = ttk.Frame(outer); bar.pack(fill=X)
        ttk.Button(bar, text="Refresh players", command=self._portrait_refresh).pack(side=LEFT)
        ttk.Label(bar, text="Filter players:").pack(side=LEFT, padx=(12, 2))
        self._v_pa_filter = StringVar()
        self._v_pa_filter.trace_add("write", lambda *a: self._portrait_populate())
        ttk.Entry(bar, textvariable=self._v_pa_filter, width=16).pack(side=LEFT)
        ttk.Label(bar, text="  (Ctrl/Shift-click to multi-select players)", foreground="#888",
                  font=("Segoe UI", 8)).pack(side=LEFT)

        # Reserve the Assign row + status at the very BOTTOM (packed before the body) so the two lists /
        # previews can never push them off-view; the body fills the middle above them.
        self._portrait_status = ttk.Label(outer, text="Press “Refresh players” with the game running "
                                                      "(at a roster / menu screen) to list players.",
                                           foreground="#888", font=("Consolas", 8))
        self._portrait_status.pack(side=BOTTOM, anchor=W, pady=(4, 0))
        ab = ttk.Frame(outer); ab.pack(side=BOTTOM, fill=X, pady=(10, 2))
        ttk.Button(ab, text="Assign portrait → selected player(s)", style="Accent.TButton",
                   command=self._portrait_assign).pack(side=LEFT)
        ttk.Button(ab, text="Clear selected", command=self._portrait_clear_selected).pack(side=LEFT, padx=(8, 0))
        ttk.Button(ab, text="Clear ALL saved", command=self._portrait_clear_saved).pack(side=LEFT, padx=(8, 0))
        ttk.Label(ab, text="  ← pick player(s) left + a portrait right, then Assign", foreground="#888",
                  font=("Segoe UI", 8)).pack(side=LEFT, padx=(10, 0))

        # ── Online: fetch the player's real NHL headshot and write it into their slot ─────────
        nhl = ttk.LabelFrame(outer, text="Get real portraits from NHL.com (online)", padding=(8, 6))
        nhl.pack(side=BOTTOM, fill=X, pady=(10, 0))
        nrow = ttk.Frame(nhl); nrow.pack(fill=X)
        ttk.Button(nrow, text="Fetch → selected player(s)", style="Accent.TButton",
                   command=self._portrait_nhl_fetch_selected).pack(side=LEFT)
        ttk.Button(nrow, text="Auto-fill ALL roster portraits…",
                   command=self._portrait_nhl_autofill).pack(side=LEFT, padx=(8, 0))
        ttk.Label(nrow, text="Jersey season:").pack(side=LEFT, padx=(14, 2))
        seasons = self._pd().season_list()                    # [(season_id, label)]
        self._pd_season_labels = {lbl: sid for sid, lbl in seasons}
        self._v_pd_season = StringVar(value=seasons[0][1])
        cb = ttk.Combobox(nrow, textvariable=self._v_pd_season, width=15, state="readonly",
                          values=[lbl for _, lbl in seasons])
        cb.pack(side=LEFT)
        # Manual reclaim (portrait applies now auto-compact, but this is here if 1B ever bloats again).
        ttk.Button(nrow, text="Compact game files", command=self._iff_compact).pack(side=RIGHT)
        ttk.Label(nhl, text=(
            "Downloads each player's official NHL headshot by name, reframes it to sit like the game's "
            "portrait (top-of-head / eyes / chin aligned), and writes it into the slot they already use — "
            "then deletes the temp image. No match (after searching active then retired players) → a gray "
            "silhouette. Duplicate names (e.g. two Elias Petterssons) prompt you to pick for a single "
            "player; bulk auto-picks and lists them in the log. “Jersey season” pulls that year's mug "
            "(e.g. recreate a 2013-14 league)."),
            foreground="#888", font=("Segoe UI", 8), wraplength=780, justify=LEFT).pack(anchor=W, pady=(4, 0))

        body = ttk.Frame(outer); body.pack(side=TOP, fill=BOTH, expand=True, pady=(8, 0))

        # LEFT — target players. Reserve the preview strip at the BOTTOM (fixed height, side=BOTTOM) so
        # the list shrinks into the middle when a thumbnail appears instead of shoving controls off-view.
        left = ttk.LabelFrame(body, text="1.  Players", padding=6)
        left.pack(side=LEFT, fill=BOTH, expand=True)
        tfoot = ttk.Frame(left, height=150); tfoot.pack(side=BOTTOM, fill=X, pady=(6, 0)); tfoot.pack_propagate(False)
        self._pa_target_thumb = ttk.Label(tfoot); self._pa_target_thumb.pack(side=LEFT)
        self._pa_target_lbl = ttk.Label(tfoot, text="select a player →", foreground="#888")
        self._pa_target_lbl.pack(side=LEFT, padx=8)
        ptv = ttk.Treeview(left, columns=("name", "cur", "saved"), show="headings", height=13, selectmode="extended")
        for c, w, txt in (("name", 180, "Player"), ("cur", 120, "Current portrait"), ("saved", 120, "Assigned")):
            ptv.heading(c, text=txt); ptv.column(c, width=w, anchor=W)
        ptv.tag_configure("assigned", foreground="#4ec26b")
        psb = ttk.Scrollbar(left, command=ptv.yview); ptv.configure(yscrollcommand=psb.set)
        psb.pack(side=RIGHT, fill=Y); ptv.pack(side=TOP, fill=BOTH, expand=True)
        ptv.bind("<<TreeviewSelect>>", lambda e: self._portrait_show_target_thumb())
        self._portrait_tv = ptv

        # RIGHT — source portrait picker. Bottom controls packed bottom-up (hint, buttons, preview) so
        # they're always reserved; the list fills the middle above them.
        right = ttk.LabelFrame(body, text="2.  Portrait to assign", padding=6)
        right.pack(side=LEFT, fill=BOTH, expand=True, padx=(10, 0))
        sbar = ttk.Frame(right); sbar.pack(side=TOP, fill=X)
        ttk.Label(sbar, text="Search:").pack(side=LEFT)
        self._v_pa_src_filter = StringVar()
        self._v_pa_src_filter.trace_add("write", lambda *a: self._portrait_populate_sources())
        ttk.Entry(sbar, textvariable=self._v_pa_src_filter, width=18).pack(side=LEFT, padx=(4, 0))
        ttk.Label(right, text="Previews show what's CURRENTLY in the game files (mods included). Extract saves "
                              "the selected portrait as a PNG; Import replaces its pixels in the game files (any "
                              "size — resized to 256×256 DXT4_5). Shows in-game after the portrait pack reloads "
                              "(restart / reopen the screen).",
                  foreground="#888", font=("Segoe UI", 8), wraplength=260, justify=LEFT).pack(side=BOTTOM, anchor=W, pady=(4, 0))
        sbtn = ttk.Frame(right); sbtn.pack(side=BOTTOM, fill=X, pady=(6, 0))
        ttk.Button(sbtn, text="Extract PNG…", command=self._portrait_extract).pack(side=LEFT)
        ttk.Button(sbtn, text="Import image…", command=self._portrait_import).pack(side=LEFT, padx=(6, 0))
        sfoot = ttk.Frame(right, height=150); sfoot.pack(side=BOTTOM, fill=X, pady=(6, 0)); sfoot.pack_propagate(False)
        self._pa_source_thumb = ttk.Label(sfoot); self._pa_source_thumb.pack(side=LEFT)
        self._pa_source_lbl = ttk.Label(sfoot, text="select a portrait →", foreground="#888")
        self._pa_source_lbl.pack(side=LEFT, padx=8)
        stv = ttk.Treeview(right, columns=("name", "key"), show="headings", height=13, selectmode="browse")
        stv.heading("name", text="Portrait (player)"); stv.column("name", width=190, anchor=W)
        stv.heading("key", text="key"); stv.column("key", width=55, anchor=W)
        ssb = ttk.Scrollbar(right, command=stv.yview); stv.configure(yscrollcommand=ssb.set)
        ssb.pack(side=RIGHT, fill=Y); stv.pack(side=TOP, fill=BOTH, expand=True)
        stv.bind("<<TreeviewSelect>>", lambda e: self._portrait_show_source_thumb())
        self._portrait_src_tv = stv
        self._portrait_load_sources_async()   # build the portrait list (map load is instant via bundled JSON)

    def _pa_map(self):
        try:
            return archtex.portrait_key_blob_map()
        except Exception:
            return {}

    def _portrait_thumb(self, blob, size=140):
        if blob is None:
            return None
        pi = self._pa_thumb_cache.get(blob)
        if pi is not None:
            return pi
        try:
            # show what's CURRENTLY in the game files (mods included); fall back to the clean
            # (.orig) decode only if the current archive can't be read
            img = archtex.decode_portrait_current(blob) or archtex.decode_portrait("disc_b9610aac.iff", blob)
            if img is None:
                return None
            # keep RGBA: the alpha channel is the head-silhouette mask, so the studio backdrop
            # disappears and the cut-out renders straight over the UI background
            img.thumbnail((size, size))
            pi = ImageTk.PhotoImage(img)
            self._pa_thumb_cache[blob] = pi
            return pi
        except Exception:
            return None

    def _portrait_load_sources_async(self):
        import threading
        def work():
            m = self._pa_map()
            src = sorted(m.items())            # [(key, blob)]
            self.after(0, lambda: self._portrait_sources_ready(src))
        threading.Thread(target=work, daemon=True).start()

    def _portrait_sources_ready(self, src):
        self._pa_sources = [(k, b, self._pa_key_name.get(k, f"(portrait {k})")) for k, b in src]
        self._portrait_populate_sources()

    def _portrait_populate_sources(self):
        stv = getattr(self, "_portrait_src_tv", None)
        if stv is None:
            return
        q = self._v_pa_src_filter.get().strip().lower()
        stv.delete(*stv.get_children())
        for k, b, nm in self._pa_sources:
            if q and q not in nm.lower() and q != str(k):
                continue
            stv.insert("", END, iid=str(k), values=(nm, k))

    def _portrait_refresh(self):
        self._portrait_status.config(text="Reading roster from the running game…")
        import threading
        def work():
            try:
                from launcher import portrait_assign as pa
            except ImportError:
                import portrait_assign as pa
            ps, err = pa.list_players()
            self.after(0, lambda: self._portrait_loaded(ps, err))
        threading.Thread(target=work, daemon=True).start()

    def _portrait_loaded(self, ps, err):
        self._portrait_rows = ps or []
        m = {}                                 # portrait key -> player name (for labels)
        for p in self._portrait_rows:
            if p["key"] not in m and p["name"]:
                m[p["key"]] = p["name"]
        self._pa_key_name = m
        if err:
            self._portrait_status.config(text=err)
        else:
            uniq = len({(p["first"], p["last"]) for p in ps})
            self._portrait_status.config(text=f"{uniq} players in the loaded roster.")
        if self._pa_sources:                   # relabel portraits with the newly-known names
            self._pa_sources = [(k, b, m.get(k, f"(portrait {k})")) for k, b, _ in self._pa_sources]
            self._portrait_populate_sources()
        self._portrait_populate()

    def _portrait_populate(self):
        tv = getattr(self, "_portrait_tv", None)
        if tv is None:
            return
        prev = set(tv.selection())
        tv.delete(*tv.get_children())
        q = self._v_pa_filter.get().strip().lower()
        saved = self.cfg.get("player_portraits", {})
        m = self._pa_map()
        seen = set()
        for p in self._portrait_rows:
            key = f"{p['first']}|{p['last']}"
            if key in seen:                    # one row per player (names repeat across pools)
                continue
            if q and q not in p["name"].lower():
                continue
            seen.add(key)
            cur = p["key"]
            cur_txt = (self._pa_key_name.get(cur, f"#{cur}") if cur in m else f"#{cur} (no photo)")
            sv = saved.get(key)
            sv_txt = f"→ {self._pa_key_name.get(sv, '#'+str(sv))}" if sv is not None else ""
            tv.insert("", END, iid=key, values=(p["name"] or "(unnamed)", cur_txt, sv_txt),
                      tags=("assigned",) if sv is not None else ())
        restore = [k for k in prev if tv.exists(k)]
        if restore:
            tv.selection_set(restore)

    def _portrait_show_target_thumb(self):
        sel = self._portrait_tv.selection()
        if not sel:
            return
        key = sel[-1]
        p = next((x for x in self._portrait_rows if f"{x['first']}|{x['last']}" == key), None)
        if not p:
            return
        pk = self.cfg.get("player_portraits", {}).get(key, p["key"])   # saved assignment wins
        blob = self._pa_map().get(pk)
        pi = self._portrait_thumb(blob)
        self._pa_target_thumb.config(image=pi or ""); self._pa_target_thumb.image = pi
        self._pa_target_lbl.config(text=p["name"] + (f"  ·  portrait #{pk}" if blob is not None else "  ·  no photo"))

    def _portrait_show_source_thumb(self):
        sel = self._portrait_src_tv.selection()
        if not sel:
            return
        k = int(sel[0])
        blob = self._pa_map().get(k)
        pi = self._portrait_thumb(blob)
        self._pa_source_thumb.config(image=pi or ""); self._pa_source_thumb.image = pi
        self._pa_source_lbl.config(text=f"{self._pa_key_name.get(k, f'portrait {k}')}  ·  key {k}")

    def _portrait_selected_blob(self):
        sel = self._portrait_src_tv.selection()
        if not sel:
            return None, None
        k = int(sel[0])
        return k, self._pa_map().get(k)

    def _portrait_extract(self):
        k, blob = self._portrait_selected_blob()
        if blob is None:
            messagebox.showinfo("Extract portrait", "Pick a portrait on the right first."); return
        img = archtex.decode_portrait_current(blob) or archtex.decode_portrait("disc_b9610aac.iff", blob)
        if img is None:
            messagebox.showerror("Extract portrait", "Couldn't decode that portrait."); return
        nm = self._pa_key_name.get(k, f"portrait_{k}").strip().replace(" ", "_") or f"portrait_{k}"
        p = filedialog.asksaveasfilename(title="Save portrait PNG", defaultextension=".png",
                                         initialfile=f"{nm}_{k}.png",
                                         filetypes=[("PNG image", "*.png"), ("All files", "*.*")])
        if not p:
            return
        try:
            img.save(p)
            self._portrait_status.config(text=f"Extracted {nm} (key {k}, blob {blob}) → {p}")
        except Exception as e:
            messagebox.showerror("Extract portrait", f"Save failed: {e}")

    def _portrait_import(self):
        if self._op_busy():
            return
        k, blob = self._portrait_selected_blob()
        if blob is None:
            messagebox.showinfo("Import portrait", "Pick a portrait on the right first."); return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Game folder",
                                 "Set the game files folder (with 0A/0B/1A/1B) in Settings first."); return
        img_path = filedialog.askopenfilename(
            title="Portrait image (square works best — resized to 256×256)",
            filetypes=[("Images", "*.png *.dds *.tga *.jpg *.jpeg *.bmp"), ("All files", "*.*")])
        if not img_path:
            return
        nm = self._pa_key_name.get(k, f"portrait {k}")
        if not messagebox.askyesno("Import portrait",
                f"Replace the portrait for {nm} (key {k}, blob {blob}) with:\n{Path(img_path).name}\n\n"
                f"This permanently writes the game files (a one-time .orig backup is made). Continue?"):
            return
        self._log(f"─── Import portrait: key {k} (blob {blob}) ← {Path(img_path).name} ───")
        def work():
            try:
                status = archtex.replace_portraits("disc_b9610aac.iff",
                                                   [{"index": blob, "path": img_path}], game_dir, self._log_q.put)
                self._log_q.put(f"  {status}")
                self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
                self._pa_imp = (blob, img_path)
            except Exception as e:
                self._log_q.put(f"  ERROR: {e}"); self._pa_imp = None
        self._run_in_thread(work, op_label="Importing portrait…", on_done=self._portrait_import_done)

    def _portrait_import_done(self):
        imp = getattr(self, "_pa_imp", None)
        if not imp:
            messagebox.showerror("Import portrait", "Import failed — see the Operation Log for details."); return
        blob, img_path = imp
        try:                                    # show the imported image in the preview immediately
            im = Image.open(img_path).convert("RGBA"); im.thumbnail((140, 140))
            self._pa_thumb_cache[blob] = ImageTk.PhotoImage(im)
        except Exception:
            self._pa_thumb_cache.pop(blob, None)
        self._portrait_show_source_thumb()
        self._portrait_status.config(text=f"Imported portrait into blob {blob} — it shows in-game once the "
                                          f"portrait pack reloads (restart the game / reopen the screen).")

    def _portrait_assign(self):
        sel = list(self._portrait_tv.selection())
        if not sel:
            messagebox.showinfo("Assign", "Select one or more players on the left first."); return
        ssel = self._portrait_src_tv.selection()
        if not ssel:
            messagebox.showinfo("Assign", "Pick a portrait on the right first."); return
        pkey = int(ssel[0])
        label = self._pa_key_name.get(pkey, f"portrait {pkey}")
        saved = dict(self.cfg.get("player_portraits", {}))
        for k in sel:
            saved[k] = pkey
        self.cfg["player_portraits"] = saved
        save_config(self.cfg)
        n_sel = len(sel)
        import threading
        def work():
            try:
                from launcher import portrait_assign as pa
            except ImportError:
                import portrait_assign as pa
            n, err = pa.apply_portraits({k: v for k, v in self.cfg.get("player_portraits", {}).items()})
            def done():
                if err:
                    self._portrait_status.config(text=f"Saved to {n_sel} player(s). Not applied live ({err}) — "
                                                      f"applies automatically on next Launch.")
                else:
                    self._portrait_status.config(text=f"Assigned “{label}” to {n_sel} player(s) — wrote {n} record(s) "
                                                      f"live + saved for every Launch. The portrait updates when that "
                                                      f"player next loads (reopen the roster / reload a game).")
                self._portrait_populate()
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _portrait_clear_selected(self):
        sel = list(self._portrait_tv.selection())
        if not sel:
            messagebox.showinfo("Clear", "Select the players whose assignment you want to remove."); return
        saved = dict(self.cfg.get("player_portraits", {}))
        removed = sum(1 for k in sel if saved.pop(k, None) is not None)
        self.cfg["player_portraits"] = saved
        save_config(self.cfg)
        self._portrait_status.config(text=f"Removed {removed} saved assignment(s). The live portrait stays until reload.")
        self._portrait_populate()

    def _portrait_clear_saved(self):
        if self.cfg.get("player_portraits") and not messagebox.askyesno(
                "Clear ALL saved", "Remove every saved portrait assignment?"):
            return
        self.cfg["player_portraits"] = {}
        save_config(self.cfg)
        self._portrait_status.config(text="Cleared all saved portrait assignments (live game state unchanged until reload).")
        self._portrait_populate()

    def _portrait_apply_saved_async(self, tries=18):
        """Auto-apply saved portrait assignments to the running game after launch (poll until the
        roster is in memory, or give up after `tries`)."""
        saved = self.cfg.get("player_portraits", {})
        if not saved:
            return
        import threading
        def work():
            try:
                from launcher import portrait_assign as pa
            except ImportError:
                import portrait_assign as pa
            n, err = pa.apply_portraits({k: v for k, v in saved.items()})
            def done():
                if n > 0:
                    self._log(f"[portrait] auto-applied {n} saved portrait assignment(s) to the running game")
                elif tries > 1:
                    self.after(20000, lambda: self._portrait_apply_saved_async(tries - 1))
                elif err:
                    self._log(f"[portrait] auto-apply gave up ({err})")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ── NHL.com online portrait fetch ───────────────────────────────────────────
    # Download a player's official NHL headshot by name, reframe it to the game's portrait framing
    # (portrait_download.py), and overwrite the pixels of the portrait blob the player already points
    # at (their +0x1C key -> blob). Single players get an interactive dialog (disambiguate duplicate
    # names, override the jersey/season, preview); bulk auto-fills the whole roster in one batched
    # replace_portraits write. Reframed images are written to a temp dir and deleted after applying.
    def _pd(self):
        try:
            from launcher import portrait_download as pd
        except ImportError:
            import portrait_download as pd
        return pd

    def _pd_season(self):
        return self._pd_season_labels.get(self._v_pd_season.get(), "current")

    def _pd_thumb(self, img, size=180):
        """PhotoImage of a reframed portrait (alpha kept — cut-out shows over the UI background)."""
        if img is None:
            return None
        try:
            im = img.convert("RGBA"); im.thumbnail((size, size))
            return ImageTk.PhotoImage(im)
        except Exception:
            return None

    def _portrait_unique_rows(self):
        """One row per unique roster player (name), each with the portrait blob it currently points
        at ({key,name,first,last,blob}); blob is None when the player has no photo slot."""
        m = self._pa_map()
        seen, rows = set(), []
        for p in self._portrait_rows:
            key = f"{p['first']}|{p['last']}"
            if key in seen or not p["name"]:
                continue
            seen.add(key)
            rows.append({"key": key, "name": p["name"], "first": p["first"],
                         "last": p["last"], "blob": m.get(p["key"])})
        return rows

    def _portrait_free_slots(self):
        """(portrait_key, blob) pairs for slots NOT used by any loaded roster player — reusable to give
        'no-photo' players a portrait. Sorted; empty if the roster isn't loaded. (Creating brand-new
        slots isn't safe — the portrait manager holds a fixed slot arena — so we recycle unused ones.)"""
        m = self._pa_map()                              # {portrait_key: blob}
        used = {p["key"] for p in self._portrait_rows}
        return [(k, b) for k, b in sorted(m.items()) if k not in used]

    def _portrait_assign_keys(self, assignments):
        """Point players at portrait keys: save to cfg (re-applied on Launch) + live-write if the game
        is running. assignments = {player_id 'first|last': portrait_key}. Returns (n_live, err)."""
        if not assignments:
            return 0, None
        saved = dict(self.cfg.get("player_portraits", {}))
        saved.update(assignments)
        self.cfg["player_portraits"] = saved
        save_config(self.cfg)
        try:
            from launcher import portrait_assign as pa
        except ImportError:
            import portrait_assign as pa
        try:
            return pa.apply_portraits(assignments)
        except Exception as e:
            return 0, str(e)

    def _portrait_nhl_fetch_selected(self):
        if self._op_busy():
            return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Game folder",
                                 "Set the game files folder (with 0A/0B/1A/1B) in Settings first."); return
        sel = list(self._portrait_tv.selection())
        if not sel:
            messagebox.showinfo("Fetch from NHL", "Select one or more players on the left first."); return
        by_key = {r["key"]: r for r in self._portrait_unique_rows()}
        chosen = [by_key[k] for k in sel if k in by_key]
        if not chosen:
            return
        if len(chosen) == 1:
            self._portrait_nhl_single(chosen[0], game_dir)
        elif messagebox.askyesno("Fetch from NHL",
                f"Fetch official NHL portraits for the {len(chosen)} selected player(s) automatically?\n\n"
                "Duplicate names are auto-picked (and listed in the Operation Log)."):
            self._portrait_nhl_bulk(chosen, game_dir, scope=f"{len(chosen)} selected")

    # ── single-player interactive dialog ────────────────────────────────────────
    def _portrait_nhl_single(self, row, game_dir):
        pd = self._pd()
        if row["blob"] is None:                          # no-photo player: recycle a free portrait slot
            free = self._portrait_free_slots()
            if not free:
                messagebox.showinfo("Fetch from NHL",
                    f"{row['name']} has no portrait slot, and every slot is already used by another player "
                    "— so there's no free one to give them (new slots can't be created safely). Free one "
                    "up by clearing another player's portrait, or assign an existing portrait.")
                return
            row = dict(row); row["alloc_key"], row["blob"] = free[0]
            self._log(f"[portrait] {row['name']} had no photo — recycling free slot key {row['alloc_key']} "
                      f"(blob {row['blob']}).")
        dlg = Toplevel(self); dlg.title(f"NHL portrait — {row['name']}")
        dlg.transient(self); dlg.configure(bg=self._COL["bg1"]); dlg.resizable(False, False)
        f = ttk.Frame(dlg, padding=12); f.pack(fill=BOTH, expand=True)
        ttk.Label(f, text=row["name"], font=("Segoe UI", 12, "bold")).pack(anchor=W)
        info = ttk.Label(f, text="Searching NHL.com…", foreground="#bbb", wraplength=420, justify=LEFT)
        info.pack(anchor=W, pady=(2, 8))
        body = ttk.Frame(f); body.pack(fill=BOTH, expand=True)
        prev = ttk.Label(body, width=26); prev.pack(side=LEFT)
        side = ttk.Frame(body); side.pack(side=LEFT, padx=(12, 0), fill=BOTH, expand=True)
        btns = ttk.Frame(f); btns.pack(fill=X, pady=(10, 0))
        st = {"cand": None, "image": None, "want_team": None, "team_var": None,
              "team_labels": {}, "head_frac": pd._TARGET_HEAD_FRAC, "closed": False}
        dlg.protocol("WM_DELETE_WINDOW", lambda: (st.update(closed=True), dlg.destroy()))

        def set_preview(img):
            pi = self._pd_thumb(img, 200)
            prev.config(image=pi or ""); prev.image = pi

        def resolve_async(cand=None, want_team=None):
            st["want_team"] = want_team
            info.config(text="Fetching headshot…")
            set_preview(None)
            season = self._pd_season()
            hf = st.get("head_frac")
            def work():
                try:
                    res = pd.resolve_image(row["name"], season=season, cand=cand,
                                           want_team=want_team, head_frac=hf)
                except Exception as e:
                    res = {"status": "error", "error": str(e)}
                if not st["closed"]:
                    self.after(0, lambda: on_result(res))
            threading.Thread(target=work, daemon=True).start()

        def on_result(res):
            if st["closed"]:
                return
            if res.get("status") == "error":
                info.config(text=f"Network error: {res.get('error')} — press Refresh to retry.")
                return build_controls(res)
            if res["status"] == "ambiguous":
                return show_candidates(res["candidates"])
            st["cand"] = res.get("chosen")
            st["image"] = res["image"]
            set_preview(res["image"])
            if res["status"] == "silhouette":
                info.config(text="No NHL headshot found — a gray silhouette will be applied.")
            elif res["status"] == "composited":
                wt = res.get("team") or "?"
                info.config(text=f"No real {wt} photo of {row['name']} — grafted his head onto a {wt} "
                                 f"jersey (composite). Lower quality than a real mug.")
            else:
                src = "retired" if res.get("source") == "inactive" else "active"
                tm = res.get("team") or "?"; ssn = res.get("season") or "?"
                info.config(text=f"Matched {src} player · jersey {tm} · season {ssn}")
            build_controls(res)

        def show_candidates(cands):
            for w in side.winfo_children():
                w.destroy()
            for w in btns.winfo_children():
                w.destroy()
            info.config(text="Multiple players share this name — pick the right one:")
            lb = Listbox(side, height=min(6, len(cands)), width=42,
                         bg=self._COL["bg2"], fg=self._COL["fg"], selectmode="browse",
                         exportselection=False)
            for c in cands:
                lb.insert(END, pd.describe(c))
            lb.selection_set(0); lb.pack(fill=X)
            def use_it():
                i = (lb.curselection() or [0])[0]
                resolve_async(cand=cands[i])
            ttk.Button(btns, text="Use this player", style="Accent.TButton", command=use_it).pack(side=LEFT)
            ttk.Button(btns, text="Cancel", command=lambda: (st.update(closed=True), dlg.destroy())).pack(side=RIGHT)

        def refresh():
            wt = (st["team_labels"].get(st["team_var"].get())
                  if st.get("team_var") is not None else None)
            resolve_async(cand=st["cand"], want_team=wt) if st["cand"] is not None else resolve_async()

        def build_controls(res):
            for w in side.winfo_children():
                w.destroy()
            for w in btns.winfo_children():
                w.destroy()
            st["team_var"] = None
            # Full jersey/team picker — change to ANY team (a mug only exists for teams the player
            # actually played for; others come back as a silhouette). Only shown once we have a player.
            if st["cand"] is not None:
                try:
                    hist = [ab for ab, _tn, _s in pd.team_history(pd.landing(pd.player_id(st["cand"])))]
                except Exception:
                    hist = []
                choices = list(pd.CURRENT_TEAMS)
                known = {ab for ab, _ in choices}
                for ab in hist:                                # add historical teams the player wore
                    if ab not in known:
                        choices.append((ab, ab)); known.add(ab)
                choices.sort(key=lambda t: t[0])
                labels = {f"{ab}  ·  {nm}": ab for ab, nm in choices}
                rev = {ab: lbl for lbl, ab in labels.items()}
                ttk.Label(side, text="Jersey / team:").pack(anchor=W)
                v = StringVar(value=rev.get(st.get("want_team") or res.get("team"), next(iter(labels))))
                jcb = ttk.Combobox(side, values=list(labels), width=30, state="readonly", textvariable=v)
                jcb.pack(anchor=W, pady=(0, 3))
                st["team_var"], st["team_labels"] = v, labels
                jcb.bind("<<ComboboxSelected>>",
                         lambda e: resolve_async(cand=st["cand"], want_team=labels.get(v.get())))
                ttk.Label(side, text="any team — real photo if they played there, else a jersey composite",
                          foreground="#888", font=("Segoe UI", 8), wraplength=210, justify=LEFT).pack(anchor=W)
            # Head-size fine-tune (per player): smaller = more neck/shoulder; release the slider to re-render.
            hrow = ttk.Frame(side); hrow.pack(anchor=W, fill=X, pady=(8, 0))
            ttk.Label(hrow, text="Head size:").pack(side=LEFT)
            hlbl = ttk.Label(hrow, text=f"{st['head_frac']:.2f}", width=5, foreground="#bbb")
            hlbl.pack(side=RIGHT)

            def on_head(v):
                st["head_frac"] = round(float(v), 3)
                hlbl.config(text=f"{st['head_frac']:.2f}")
            sc = ttk.Scale(side, from_=0.44, to=0.74, value=st["head_frac"], command=on_head)
            sc.pack(anchor=W, fill=X)
            sc.bind("<ButtonRelease-1>", lambda e: refresh())
            ttk.Button(side, text="↻ Refresh portrait", command=refresh).pack(anchor=W, pady=(6, 0))
            ap = ttk.Button(btns, text=f"Apply to {row['name']}", style="Accent.TButton", command=do_apply)
            ap.pack(side=LEFT)
            if st["image"] is None:
                ap.state(["disabled"])
            ttk.Button(btns, text="Cancel",
                       command=lambda: (st.update(closed=True), dlg.destroy())).pack(side=RIGHT)

        def do_apply():
            img = st["image"]
            if img is None:
                return
            st.update(closed=True)
            try:
                dlg.destroy()
            except Exception:
                pass
            self._portrait_nhl_apply_one(row, img, game_dir)

        resolve_async()

    def _portrait_nhl_apply_one(self, row, image, game_dir):
        blob = row["blob"]
        self._log(f"─── NHL portrait: {row['name']} → blob {blob} ───")
        tmpdir = tempfile.mkdtemp(prefix="nhlport_")
        tmp = os.path.join(tmpdir, f"{blob}.png")
        try:
            image.save(tmp)
        except Exception as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            messagebox.showerror("Fetch from NHL", f"Couldn't stage the image: {e}"); return

        alloc_key = row.get("alloc_key")

        def work():
            try:
                if alloc_key is not None:               # recycled slot: point the player at it (live+saved)
                    self._portrait_assign_keys({row["key"]: alloc_key})
                    self._log_q.put(f"  gave {row['name']} portrait slot key {alloc_key} (live + saved).")
                status = archtex.replace_portraits("disc_b9610aac.iff",
                                                   [{"index": blob, "path": tmp}], game_dir, self._log_q.put)
                self._log_q.put(f"  {status}")
                # reclaim the orphaned old pack copy so 1B doesn't creep toward the 2 GB file cap
                self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
                self._pd_applied = (blob, image)
            except Exception as e:
                self._log_q.put(f"  ERROR: {e}"); self._pd_applied = None
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        def done():
            ap = getattr(self, "_pd_applied", None)
            if not ap:
                messagebox.showerror("Fetch from NHL", "Apply failed — see the Operation Log."); return
            b, img = ap
            try:
                self._pa_thumb_cache[b] = self._pd_thumb(img, 140)
            except Exception:
                self._pa_thumb_cache.pop(b, None)
            self._portrait_show_target_thumb()
            self._portrait_status.config(text=f"Applied NHL portrait for {row['name']} — shows in-game once "
                                              "the portrait pack reloads (restart / reopen the screen).")
        self._run_in_thread(work, op_label=f"Applying {row['name']}'s portrait…", on_done=done)

    # ── bulk auto-fill ──────────────────────────────────────────────────────────
    def _portrait_nhl_autofill(self):
        if self._op_busy():
            return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Game folder",
                                 "Set the game files folder (with 0A/0B/1A/1B) in Settings first."); return
        rows = self._portrait_unique_rows()
        if not rows:
            messagebox.showinfo("Auto-fill portraits",
                "No roster players are loaded. Press “Refresh players” with the game running first."); return
        noblob = sum(1 for r in rows if r["blob"] is None)
        if not messagebox.askyesno("Auto-fill ALL portraits",
                f"Download official NHL portraits for {len(rows)} roster player(s) and write them into the "
                f"game files?\n\n"
                f"•  Jersey season:  {self._v_pd_season.get()}\n"
                f"•  No NHL match  →  all share one silhouette slot (frees their slot for matched players)\n"
                + (f"•  {noblob} player(s) currently have no photo — they'll be given a freed-up slot when "
                   "one is matched\n" if noblob else "")
                + "\nA one-time .orig backup is made. This downloads a lot of images and can take a few "
                "minutes; you can Cancel mid-run. Continue?"):
            return
        self._portrait_nhl_bulk(rows, game_dir, scope="whole roster")

    @staticmethod
    def _pd_autopick(cands):
        """Pick a candidate for an ambiguous name in bulk: prefer an active player, else the first."""
        return next((c for c in cands if c.get("active")), cands[0]) if cands else None

    def _portrait_nhl_bulk(self, rows, game_dir, scope="", unplaced=0):
        pd = self._pd()
        season = self._pd_season()
        pa_map = self._pa_map()                          # {portrait_key: blob}
        inv = {}                                         # blob -> a portrait_key that renders it
        for k, b in sorted(pa_map.items()):
            inv.setdefault(b, k)
        free_pool = list(self._portrait_free_slots())    # [(portrait_key, blob)] unused by any player
        # De-duplicate players that already share ONE real blob (both can't paint the same slot).
        # No-photo players (blob is None) never collide — they're all kept and placed from freed slots.
        by_blob, collisions, noblob = {}, [], []
        for r in rows:
            if r["blob"] is None:
                noblob.append(r)
            elif r["blob"] in by_blob:
                collisions.append(r["name"])
            else:
                by_blob[r["blob"]] = r
        items = list(by_blob.values()) + noblob
        rep = {"matched": 0, "retired": 0, "silhouette": 0, "composited": 0, "ambiguous": [], "errors": [],
               "collisions": collisions, "applied": 0, "assigned_slots": 0, "freed": 0,
               "shared_silhouette": 0, "unplaced": 0, "cancelled": False}

        def work():
            tmpdir = tempfile.mkdtemp(prefix="nhlport_")
            total = len(items) or 1
            done = 0
            self._log_q.put(f"[portrait] auto-fill ({scope}): resolving {len(items)} player(s), season "
                            f"{self._v_pd_season.get()}…")
            ex = ThreadPoolExecutor(max_workers=8)
            futs = {ex.submit(self._pd_resolve_row, pd, r, season): r for r in items}
            resolved = []                                # [(row, res)] classified below
            try:
                for fut in as_completed(futs):
                    if self._cancel_event.is_set():
                        rep["cancelled"] = True
                        break
                    r = futs[fut]
                    try:
                        res = fut.result()
                    except Exception as e:
                        res = {"status": "error", "error": str(e), "image": None}
                    stt = res.get("status")
                    if stt == "error":
                        rep["errors"].append(f"{r['name']}: {res.get('error')}")
                    if res.get("ambiguous"):
                        rep["ambiguous"].append(r["name"])
                    resolved.append((r, res))
                    done += 1
                    self._log_q.put(f"__PROGRESS__{done / total}|{r['name']} · {stt}")
                # stop any still-queued downloads (snappy Cancel) before the write step
                ex.shutdown(wait=False, cancel_futures=True)
                if rep["cancelled"] or not resolved:
                    self._pd_bulk_report = rep
                    return

                # ── partition: real headshot vs no-match silhouette ──────────────
                def is_match(res):
                    return res.get("status") in ("matched", "composited") and res.get("image") is not None
                matched = [(r, res) for r, res in resolved if is_match(res)]
                nomatch = [(r, res) for r, res in resolved if not is_match(res)]
                for _r, res in matched:
                    if res.get("status") == "composited":
                        rep["composited"] += 1
                    else:
                        rep["matched"] += 1
                        if res.get("source") == "inactive":
                            rep["retired"] += 1
                rep["silhouette"] = len(nomatch)

                # ── one shared "no photo" silhouette slot ────────────────────────
                # Prefer recycling a slot a no-match player already owns (costs nothing); else a free
                # slot. Every OTHER no-match player's blob is then freed for a matched-but-no-slot player.
                nomatch_slots = [(inv.get(r["blob"], None), r["blob"])
                                 for r, _ in nomatch if r["blob"] is not None and inv.get(r["blob"]) is not None]
                shared_key = shared_blob = None
                pool = list(free_pool)                    # (key, blob) reusable for matched players
                if nomatch_slots:
                    shared_key, shared_blob = nomatch_slots[0]
                    pool += nomatch_slots[1:]             # the rest are freed for matched players
                elif pool:
                    shared_key, shared_blob = pool.pop(0)
                if nomatch and shared_blob is None:
                    # no slot anywhere to host the silhouette — leave those players untouched
                    rep["unplaced"] += len(nomatch)

                edits = []                                # [{index: blob, path}]
                assigns = {}                              # {player 'first|last': portrait_key}
                # paint the shared silhouette once
                if shared_blob is not None and nomatch:
                    try:
                        sp = os.path.join(tmpdir, f"sil_{shared_blob}.png")
                        pd.silhouette_placeholder().save(sp)
                        edits.append({"index": shared_blob, "path": sp})
                        rep["shared_silhouette"] = 1
                    except Exception as e:
                        rep["errors"].append(f"silhouette: save {e}")
                    for r, _res in nomatch:
                        if r["key"] != shared_key:        # only rewrite players not already on it
                            assigns[r["key"]] = shared_key

                # place the matched headshots: paint into the player's own slot, or recycle a freed one
                for r, res in matched:
                    blob = r["blob"]
                    if blob is None:
                        if not pool:
                            rep["unplaced"] += 1
                            continue
                        alloc_key, blob = pool.pop(0)
                        assigns[r["key"]] = alloc_key
                        rep["assigned_slots"] += 1
                    p = os.path.join(tmpdir, f"{blob}.png")
                    try:
                        res["image"].save(p)
                        edits.append({"index": blob, "path": p})
                    except Exception as e:
                        rep["errors"].append(f"{r['name']}: save {e}")

                if edits:
                    self._log_q.put(f"__PROGRESS__-1|writing {len(edits)} portrait(s) into the game files…")
                    self._log_q.put(f"[portrait] writing {len(edits)} portrait(s) into disc_b9610aac.iff…")
                    status = archtex.replace_portraits("disc_b9610aac.iff", edits, game_dir, self._log_q.put)
                    self._log_q.put(f"  {status}")
                    rep["applied"] = len(edits)
                    # ONE compaction after the whole batch (not per-portrait) — reclaims the orphaned
                    # old 66 MB pack copy so 1B stays well under the 2 GB file cap
                    self._log_q.put(f"__PROGRESS__-1|compacting game files…")
                    self._log_q.put(f"[portrait] {archtex.compact_1b(game_dir, self._log_q.put)}")
                if assigns:
                    rep["freed"] = rep["assigned_slots"]
                    _n, err = self._portrait_assign_keys(assigns)
                    self._log_q.put(f"[portrait] re-pointed {len(assigns)} player(s): "
                                    f"{rep['assigned_slots']} matched→freed slot, "
                                    f"{len(assigns) - rep['assigned_slots']} no-match→shared silhouette"
                                    + (f" (saved; live apply: {err})" if err else " (live + saved)"))
            finally:
                ex.shutdown(wait=False, cancel_futures=True)
                shutil.rmtree(tmpdir, ignore_errors=True)
            self._pd_bulk_report = rep
        self._run_in_thread(work, op_label="Auto-filling NHL portraits…", on_done=self._portrait_nhl_bulk_done)

    @staticmethod
    def _pd_resolve_row(pd, row, season):
        """Resolve one roster row to a reframed image (worker-thread safe). Auto-picks duplicate
        names and flags them via res['ambiguous']."""
        res = pd.resolve_image(row["name"], season=season)
        if res["status"] == "ambiguous":
            cand = App._pd_autopick(res["candidates"])
            res = pd.resolve_image(row["name"], season=season, cand=cand)
            res["ambiguous"] = True
        return res

    def _portrait_nhl_bulk_done(self):
        rep = getattr(self, "_pd_bulk_report", None)
        if not rep:
            self._portrait_status.config(text="Auto-fill finished (see the Operation Log)."); return
        # invalidate preview thumbnails for the blobs we just overwrote
        self._pa_thumb_cache.clear()
        head = "Cancelled — partial apply." if rep["cancelled"] else "Auto-fill complete."
        lines = [head, "",
                 f"Matched real headshots : {rep['matched']}  (of which retired: {rep['retired']})",
                 f"No match → silhouette  : {rep['silhouette']}",
                 f"Written to game files  : {rep['applied']}"]
        if rep.get("composited"):
            lines.append(f"Jersey composites      : {rep['composited']}")
        if rep.get("shared_silhouette"):
            lines.append(f"Silhouette players     : share 1 slot (freeing their own for matches)")
        if rep.get("assigned_slots"):
            lines.append(f"Matched → freed slot   : {rep['assigned_slots']} (had no photo; given a recycled slot)")
        if rep.get("unplaced"):
            lines.append(f"Couldn't be placed     : {rep['unplaced']} (no free portrait slot left)")
        if rep["ambiguous"]:
            lines += ["", f"Auto-picked duplicate names ({len(rep['ambiguous'])}) — review with a single "
                          "Fetch if wrong:", "  " + ", ".join(rep["ambiguous"][:40])
                      + (" …" if len(rep["ambiguous"]) > 40 else "")]
        if rep["collisions"]:
            lines += ["", f"Skipped (share a portrait slot): {', '.join(rep['collisions'][:30])}"
                      + (" …" if len(rep["collisions"]) > 30 else "")]
        if rep["errors"]:
            lines += ["", f"Errors ({len(rep['errors'])}):"] + ["  " + e for e in rep["errors"][:20]]
        for ln in lines:
            self._log(ln)
        self._portrait_status.config(
            text=f"{head} Matched {rep['matched']}, silhouette {rep['silhouette']}, written "
                 f"{rep['applied']}. Shows in-game after the portrait pack reloads (restart).")
        self._portrait_populate()
        win = Toplevel(self); win.title("Auto-fill NHL portraits — summary")
        win.transient(self); win.configure(bg=self._COL["bg1"])
        fr = ttk.Frame(win, padding=12); fr.pack(fill=BOTH, expand=True)
        txt = Text(fr, width=74, height=min(26, len(lines) + 2), wrap="word",
                   bg=self._COL["bg2"], fg=self._COL["fg"], relief="flat")
        txt.insert("1.0", "\n".join(lines)); txt.config(state="disabled")
        txt.pack(fill=BOTH, expand=True)
        ttk.Button(fr, text="Close", command=win.destroy).pack(anchor=E, pady=(8, 0))

    # ── Scoreclock tab ────────────────────────────────────────────────────────

    def _sb_xex(self) -> Path | None:
        """The XEX the game actually boots (Settings 'Game' entry), falling back to
        <game files folder>/default.xex. None when neither resolves to a .xex file."""
        p = self.cfg.get("game_path", "").strip()
        if p:
            gp = Path(p)
            if gp.is_file() and gp.suffix.lower() == ".xex":
                return gp
            if gp.is_dir() and (gp / "default.xex").is_file():
                return gp / "default.xex"
        root = self._get_game_root()
        if root and (root / "default.xex").is_file():
            return root / "default.xex"
        return None

    def _build_scorebug_tab(self):
        """Primary content = the per-element layout/scale/color editor with a live preview.
        Whole-scoreclock screen placement (the XEX anchor) is a secondary dialog."""
        t = self._tab_scorebug
        from launcher import scorebug_layout as sblay
        self._sblay = sblay
        self._sbl_rows = []                 # last-read element rows (current on-disk state)
        self._sbl_pending = {}              # {name: {dx,dy,sx,sy,size,color}}
        self._sbl_factory = False           # staged "reset to default" (preview only until Apply)

        head = ttk.Frame(t, padding=(12, 10, 12, 4)); head.pack(fill=X)
        ttk.Label(head, text="Scoreclock Element Editor",
                  font=("Segoe UI", 13, "bold")).pack(side=LEFT)
        ttk.Button(head, text="Whole-Scoreclock Position (screen anchor)…",
                   command=self._sb_anchor_dialog).pack(side=RIGHT)
        self._sbl_shadow_btn = ttk.Button(head, text="Hide Scorebug Logo",
                                          command=self._sbl_toggle_shadow)
        self._sbl_shadow_btn.pack(side=RIGHT, padx=(0, 6))
        self._sbl_teal_btn = ttk.Button(head, text="Hide Bottom Teal Bar",
                                        command=self._sbl_toggle_teal)
        self._sbl_teal_btn.pack(side=RIGHT, padx=(0, 6))
        ttk.Label(t, foreground="#999", font=("Segoe UI", 8), justify=LEFT, wraplength=940,
                  text="Move, resize and recolour each part of the in-game scoreclock. Pick an "
                       "element in the list or preview, queue changes, then Apply — writes "
                       "overlay_static.iff; shows on the NEXT game launch. Axes: +X right, +Y up. "
                       "“1st” and clock digit 4 are re-anchored by the game and may ignore moves."
                  ).pack(fill=X, padx=12)

        # Preview canvas
        pv = ttk.LabelFrame(t, text="Preview (schematic, to scale — dashed = stock)", padding=4)
        pv.pack(fill=X, padx=12, pady=(6, 4))
        self._sbl_canvas = Canvas(pv, height=150, bg="#0e0f12", highlightthickness=0)
        self._sbl_canvas.pack(fill=X)
        self._sbl_canvas.bind("<Configure>", lambda e: self._sbl_render())
        self._sbl_canvas.bind("<Button-1>", self._sbl_canvas_click)

        body = ttk.Frame(t); body.pack(fill=BOTH, expand=True, padx=12, pady=4)

        # Element list (left)
        left = ttk.Frame(body); left.pack(side=LEFT, fill=BOTH, expand=True)
        cols = ("element", "x", "y", "fontsize", "width", "height", "color", "pending")
        tv = ttk.Treeview(left, columns=cols, show="headings", height=12,
                          selectmode="extended")     # multi-select for group actions
        for c, w, a in (("element", 196, W), ("x", 56, W), ("y", 56, W),
                        ("fontsize", 66, W), ("width", 58, W), ("height", 58, W),
                        ("color", 70, W), ("pending", 150, W)):
            tv.heading(c, text={"x": "X", "y": "Y", "fontsize": "Font Size",
                                "width": "Width", "height": "Height",
                                "color": "Color", "pending": "Pending"}.get(c, c.title()))
            tv.column(c, width=w, anchor=a)
        sb = ttk.Scrollbar(left, command=tv.yview); tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); tv.pack(side=LEFT, fill=BOTH, expand=True)
        tv.bind("<<TreeviewSelect>>", lambda e: self._sbl_render())
        tv.bind("<Double-1>", self._sbl_tv_double)     # double-click a cell to edit X/Y/Size/Color
        self._sbl_tv = tv

        # Edit controls (right)
        ctl = ttk.Frame(body, padding=(12, 0, 0, 0)); ctl.pack(side=LEFT, fill=Y)
        self._sbl_selvar = StringVar(value="(no element selected)")
        ttk.Label(ctl, textvariable=self._sbl_selvar, font=("Segoe UI", 9, "bold"),
                  wraplength=210).pack(anchor=W, pady=(0, 4))

        mv = ttk.LabelFrame(ctl, text="Move", padding=8); mv.pack(fill=X)
        pad = ttk.Frame(mv); pad.pack()
        ttk.Label(pad, text="Step").grid(row=0, column=0, padx=(0, 4))
        self._sbl_step = StringVar(value="5")
        ttk.Entry(pad, textvariable=self._sbl_step, width=5).grid(row=0, column=1, sticky=W)
        ttk.Button(pad, text="↑", width=3, command=lambda: self._sbl_nudge(0, 1)).grid(row=1, column=1)
        ttk.Button(pad, text="←", width=3, command=lambda: self._sbl_nudge(-1, 0)).grid(row=2, column=0)
        ttk.Button(pad, text="→", width=3, command=lambda: self._sbl_nudge(1, 0)).grid(row=2, column=2)
        ttk.Button(pad, text="↓", width=3, command=lambda: self._sbl_nudge(0, -1)).grid(row=3, column=1)
        exact = ttk.Frame(mv); exact.pack(pady=(6, 0))
        ttk.Label(exact, text="dX").grid(row=0, column=0)
        self._sbl_dx = StringVar(); ttk.Entry(exact, textvariable=self._sbl_dx, width=6).grid(row=0, column=1, padx=2)
        ttk.Label(exact, text="dY").grid(row=0, column=2)
        self._sbl_dy = StringVar(); ttk.Entry(exact, textvariable=self._sbl_dy, width=6).grid(row=0, column=3, padx=2)
        ttk.Button(exact, text="Set move", command=self._sbl_set_move).grid(
            row=1, column=0, columnspan=4, sticky=EW, pady=(3, 0))

        sc = ttk.LabelFrame(ctl, text="Scale — text, logos & bars (independent X, Y)",
                            padding=8); sc.pack(fill=X, pady=(8, 0))
        srow = ttk.Frame(sc); srow.pack()
        ttk.Label(srow, text="×X").grid(row=0, column=0)
        self._sbl_sx = StringVar(value="1.0")
        ttk.Entry(srow, textvariable=self._sbl_sx, width=6).grid(row=0, column=1, padx=2)
        ttk.Label(srow, text="×Y").grid(row=0, column=2)
        self._sbl_sy = StringVar(value="1.0")
        ttk.Entry(srow, textvariable=self._sbl_sy, width=6).grid(row=0, column=3, padx=2)
        ttk.Button(sc, text="Set scale (× stock)", command=self._sbl_set_scale).pack(
            fill=X, pady=(4, 0))
        ttk.Label(sc, text="1.0 = stock. 2 = double, 0.5 = half. Works on text glyphs too now "
                           "(scores/period/abbrevs) — the clip box widens with it so bigger text "
                           "won't truncate.", foreground="#888", font=("Segoe UI", 7),
                  wraplength=210, justify=LEFT).pack(anchor=W)

        fs = ttk.LabelFrame(ctl, text="Text — font size & colour", padding=8); fs.pack(fill=X, pady=(8, 0))
        ffr = ttk.Frame(fs); ffr.pack(fill=X)
        ttk.Label(ffr, text="Font (size)").pack(side=LEFT)
        self._sbl_font = StringVar(value="Normal")
        ttk.Combobox(ffr, textvariable=self._sbl_font, state="readonly", width=15,
                     values=list(self._sblay.FONTS.keys())).pack(side=LEFT, padx=3)
        ttk.Button(ffr, text="Set", width=5, command=self._sbl_set_font).pack(side=LEFT)
        ttk.Label(fs, text="Per-element FONT = per-element SIZE (the real size lever — there's no scale "
                           "field). Small/Large use the game's built-in Avenir 24/40. EXPERIMENTAL: "
                           "apply + relaunch to check, and tell me if an element resizes — then I'll "
                           "add named custom sizes.",
                  foreground="#888", font=("Segoe UI", 7), wraplength=210,
                  justify=LEFT).pack(anchor=W, pady=(3, 0))
        frow = ttk.Frame(fs); frow.pack(fill=X, pady=(5, 0))
        ttk.Label(frow, text="Width").grid(row=0, column=0)
        self._sbl_size = StringVar(value="")
        ttk.Entry(frow, textvariable=self._sbl_size, width=6).grid(row=0, column=1, padx=2)
        ttk.Button(frow, text="Set", width=5,
                   command=self._sbl_set_size).grid(row=0, column=2)
        ttk.Label(fs, text="Width = the text's clip box (NOT glyph size): shrink it too far and the "
                           "game truncates with \"…\".",
                  foreground="#888", font=("Segoe UI", 7), wraplength=210,
                  justify=LEFT).pack(anchor=W, pady=(2, 0))
        ttk.Button(fs, text="Colour…", command=self._sbl_pick_color).pack(fill=X, pady=(6, 0))

        hb = ttk.Frame(ctl); hb.pack(fill=X, pady=(10, 0))
        ttk.Button(hb, text="Hide", command=lambda: self._sbl_set_hidden(True)).pack(
            side=LEFT, expand=True, fill=X, padx=(0, 2))
        ttk.Button(hb, text="Show", command=lambda: self._sbl_set_hidden(False)).pack(
            side=LEFT, expand=True, fill=X, padx=(2, 0))
        ttk.Button(ctl, text="Clear this element",
                   command=self._sbl_clear_sel).pack(fill=X, pady=(6, 0))
        ttk.Separator(ctl, orient=HORIZONTAL).pack(fill=X, pady=8)
        ttk.Button(ctl, text="Edit Textures (logos, bars…)",
                   command=self._sbl_edit_textures).pack(fill=X)
        ttk.Label(ctl, text="Opens the IFF Textures tab for the scoreclock art.",
                  foreground="#888", font=("Segoe UI", 7), wraplength=210).pack(anchor=W)

        # Preset bar — full editor edit-set, saved to APPDATA. "Default" = factory/stock.
        pr = ttk.Frame(t, padding=(12, 4, 12, 0)); pr.pack(fill=X)
        ttk.Label(pr, text="Preset:").pack(side=LEFT)
        self._sbl_preset_var = StringVar()
        self._sbl_preset_cb = ttk.Combobox(pr, textvariable=self._sbl_preset_var,
                                           state="readonly", width=26, values=[])
        self._sbl_preset_cb.pack(side=LEFT, padx=(4, 6))
        ttk.Button(pr, text="Load", width=7, command=self._sbl_preset_load).pack(side=LEFT, padx=(0, 3))
        ttk.Button(pr, text="Save", width=7, command=self._sbl_preset_save).pack(side=LEFT, padx=(0, 3))
        ttk.Button(pr, text="Save As…", width=9, command=self._sbl_preset_save_as).pack(side=LEFT, padx=(0, 3))
        ttk.Button(pr, text="Delete", width=7, command=self._sbl_preset_delete).pack(side=LEFT)
        self._sbl_preset_refresh()

        # Bottom action bar
        act = ttk.Frame(t, padding=(12, 4, 12, 10)); act.pack(fill=X)
        ttk.Button(act, text="Apply to Game Files", style="Accent.TButton",
                   command=self._sbl_apply).pack(side=LEFT, padx=(0, 6))
        ttk.Button(act, text="Apply All Shown",
                   command=self._sbl_apply_all).pack(side=LEFT, padx=(0, 6))
        ttk.Button(act, text="Discard Unapplied",
                   command=self._sbl_clear_all).pack(side=LEFT, padx=(0, 6))
        ttk.Button(act, text="Reload", command=lambda: self._sbl_load()).pack(side=LEFT)
        self._sbl_statusvar = StringVar(value="")
        ttk.Label(act, textvariable=self._sbl_statusvar,
                  foreground="#999").pack(side=RIGHT)

        self.after(700, lambda: self._sbl_load(initial=True))

    # ── Scoreclock element editor: data + preview ─────────────────────────────

    def _sbl_load(self, initial=False, rows=None):
        game_dir = self._get_game_root()
        if not game_dir:
            self._sbl_statusvar.set("Set the game files folder in Settings.")
            return
        if rows is not None:
            self._sbl_after_load(rows); return
        self._sbl_statusvar.set("Reading scoreclock scene…")
        def work():
            try:
                r = self._sblay.list_elements(game_dir); err = None
            except Exception as e:
                r, err = None, str(e)
            self.after(0, lambda: (
                self._sbl_statusvar.set(f"Couldn't read scene: {err}")
                if err else self._sbl_after_load(r)))
        threading.Thread(target=work, daemon=True).start()

    def _sbl_after_load(self, rows):
        self._sbl_rows = rows
        self._sbl_refresh_table()
        self._sbl_render()
        self._sbl_statusvar.set(f"{len(rows)} elements")
        self._sbl_sync_shadow_btn()

    def _sbl_sync_shadow_btn(self):
        """Reflect whether the grey 2K backdrop shadow / teal bar are currently hidden."""
        game_dir = self._get_game_root()
        def work():
            try:
                sh = self._sblay.scorebug_logo_hidden(game_dir) if game_dir else False
            except Exception:
                sh = False
            try:
                tl = self._sblay.teal_bar_hidden(game_dir) if game_dir else False
            except Exception:
                tl = False
            def done():
                if hasattr(self, "_sbl_shadow_btn"):
                    self._sbl_shadow_btn.config(
                        text="Show Scorebug Logo" if sh else "Hide Scorebug Logo")
                if hasattr(self, "_sbl_teal_btn"):
                    self._sbl_teal_btn.config(
                        text="Show Bottom Teal Bar" if tl else "Hide Bottom Teal Bar")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _sbl_toggle_teal(self):
        """Hide/show the bottom teal glow bar (glow_cylinder_color's cyan texture, blanked
        transparent in place). Writes overlay_static.iff; shows on next game launch."""
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Scoreclock", "Set the game files folder in Settings."); return
        self._sbl_statusvar.set("Updating teal bar…")
        def work():
            try:
                hidden = self._sblay.teal_bar_hidden(game_dir)
                status = self._sblay.set_teal_bar_hidden(not hidden, game_dir, self._log_q.put)
            except Exception as e:
                self.after(0, lambda: (self._sbl_statusvar.set(f"Failed: {e}"),
                                       messagebox.showerror("Scoreclock", f"Failed:\n{e}")))
                return
            def done():
                self._sbl_sync_shadow_btn()
                self._log_q.put(f"[scoreclock] bottom teal bar "
                                f"{'hidden' if not hidden else 'shown'} — {status}")
                self._sbl_statusvar.set(f"Teal bar {'hidden' if not hidden else 'shown'}. "
                                        "Relaunch the game to see it.")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _sbl_toggle_shadow(self):
        """Hide/show the scorebug 2K logo (grey backdrop + branding = the logo_2k_mesh geometry,
        not a texture — which is why editing 2K logo textures never removed it). Hidden by zeroing
        its index buffer, the same proven way the glow/teal meshes are hidden. Writes overlay_static.iff."""
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Scoreclock", "Set the game files folder in Settings."); return
        self._sbl_statusvar.set("Updating scorebug logo…")
        def work():
            try:
                hidden = self._sblay.scorebug_logo_hidden(game_dir)
                status = self._sblay.set_scorebug_logo_hidden(not hidden, game_dir, self._log_q.put)
            except Exception as e:
                self.after(0, lambda: (self._sbl_statusvar.set(f"Failed: {e}"),
                                       messagebox.showerror("Scoreclock", f"Failed:\n{e}")))
                return
            def done():
                self._sbl_sync_shadow_btn()
                self._log_q.put(f"[scoreclock] scorebug logo "
                                f"{'hidden' if not hidden else 'shown'} — {status}")
                self._sbl_statusvar.set(f"Scorebug logo {'hidden' if not hidden else 'shown'}. "
                                        "Relaunch the game to see it.")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _sbl_refresh_table(self):
        tv = self._sbl_tv
        keep = set(tv.selection())
        tv.delete(*tv.get_children())
        for r in self._sbl_rows:
            nm = r["name"]
            ed = self._sbl_pending.get(nm, {})
            # show the EFFECTIVE value (file + pending) so double-click edits are reflected
            eff_x = r["x"] + float(ed.get("dx", 0))
            eff_y = r["y"] + float(ed.get("dy", 0))
            if r["kind"] == "text":
                sz = float(ed.get("size", r.get("size", 0)))
                fontsize_txt = f"{sz:.0f}"
                width_txt = height_txt = "—"        # text rows have no bbox (greyed placeholder)
                col = ("#%02X%02X%02X" % tuple(int(c) & 0xFF for c in ed["color"])
                       if "color" in ed else r.get("color", ""))
            else:
                w = r.get("w", 0) * float(ed.get("sx", 1))
                h = r.get("h", 0) * float(ed.get("sy", 1))
                fontsize_txt = "—"                  # meshes have no font size
                width_txt = f"{w:.0f}"; height_txt = f"{h:.0f}"
                col = ""
            bits = []
            if ed.get("hidden"):
                bits.append("HIDDEN")
            if ed.get("dx") or ed.get("dy"):
                bits.append(f"move {ed.get('dx', 0):+g},{ed.get('dy', 0):+g}")
            if ed.get("sx", 1) != 1 or ed.get("sy", 1) != 1:
                bits.append(f"×{ed.get('sx', 1):g}/{ed.get('sy', 1):g}")
            if "size" in ed:
                bits.append(f"width {ed['size']:g}")
            if "font" in ed:
                bits.append(f"font:{self._sblay._HASH_FONT.get(int(ed['font']), 'custom')}")
            if "color" in ed:
                bits.append("recolour")
            tv.insert("", END, iid=nm, values=(
                r["label"], f"{eff_x:.1f}", f"{eff_y:.1f}",
                fontsize_txt, width_txt, height_txt, col,
                ", ".join(bits)))
        for nm in keep:
            if tv.exists(nm):
                tv.selection_add(nm)

    def _sbl_selected(self):
        sel = self._sbl_tv.selection()
        return sel[0] if sel else None

    def _sbl_selection(self):
        """All currently-selected element names (multi-select), in tree order."""
        return list(self._sbl_tv.selection())

    def _sbl_render(self):
        """Draw the calibrated scoreclock schematic (strip fractions -> canvas), with pending
        edits applied and a dashed stock ghost for anything changed."""
        c = self._sbl_canvas
        c.delete("all")
        rows = self._sbl_rows
        if not rows:
            return
        cw = c.winfo_width() or 940
        ch = c.winfo_height() or 150
        # The strip keeps the real ~9.4:1 aspect, centred, with side padding for the SN logo area.
        m = 8
        sw = cw - 2 * m
        sh = min(ch - 2 * m, sw / 9.4)
        ox, oy = m, (ch - sh) / 2
        c.create_rectangle(ox, oy, ox + sw, oy + sh, fill="#15171c", outline="#2a2d34")
        # a faint "SN logo" placeholder box on the far left (not editable, for orientation)
        c.create_rectangle(ox + sw * 0.01, oy + sh * 0.2, ox + sw * 0.11, oy + sh * 0.8,
                           outline="#3a3f4a", dash=(2, 2))
        c.create_text(ox + sw * 0.06, oy + sh * 0.5, text="SN", fill="#5a6068",
                      font=("Segoe UI", max(7, int(sh * 0.16)), "bold"))

        def PX(fx): return ox + fx * sw
        def PY(fy): return oy + fy * sh
        selset = set(self._sbl_tv.selection())     # highlight every selected element
        self._sbl_sync_selbar(self._sbl_selected())
        self._sbl_hit = []                        # (name, x0,y0,x1,y1) for click-select
        cap = int(sh * 0.92)                       # let big fonts grow up to the strip height
        for e in self._sblay.preview_layout(rows, self._sbl_pending, factory=self._sbl_factory):
            nm = e["name"]
            is_sel = nm in selset
            if e.get("hidden"):                   # draw only a faint ghost outline at the stock spot
                gx0, gy0 = PX(e["gcx"] - e["gw"] / 2), PY(e["gcy"] - e["gh"] / 2)
                gx1, gy1 = PX(e["gcx"] + e["gw"] / 2), PY(e["gcy"] + e["gh"] / 2)
                c.create_rectangle(gx0, gy0, gx1, gy1,
                                   outline="#ff2a33" if is_sel else "#444", dash=(1, 3))
                c.create_text((gx0 + gx1) / 2, (gy0 + gy1) / 2, text="hidden",
                              fill="#555", font=("Segoe UI", 7))
                self._sbl_hit.append((nm, min(gx0, gx1), min(gy0, gy1),
                                      max(gx0, gx1), max(gy0, gy1)))
                continue
            x0, y0 = PX(e["cx"] - e["w"] / 2), PY(e["cy"] - e["h"] / 2)
            x1, y1 = PX(e["cx"] + e["w"] / 2), PY(e["cy"] + e["h"] / 2)
            if e["changed"]:                      # dashed stock ghost
                gx0, gy0 = PX(e["gcx"] - e["gw"] / 2), PY(e["gcy"] - e["gh"] / 2)
                gx1, gy1 = PX(e["gcx"] + e["gw"] / 2), PY(e["gcy"] + e["gh"] / 2)
                c.create_rectangle(gx0, gy0, gx1, gy1, outline="#667", dash=(2, 2))
            if e["kind"] == "mesh":
                c.create_rectangle(x0, y0, x1, y1, fill=e["color"],
                                   outline="#ff2a33" if is_sel else "#0e0f12",
                                   width=2 if is_sel else 1)
            else:
                px = max(6, min(cap, int((y1 - y0) * 0.95)))     # font size drives token height
                c.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=e["token"] or "T",
                              fill=e["color"], font=("Segoe UI", px, "bold"))
                if is_sel:
                    bb = c.bbox(c.find_all()[-1])
                    if bb:
                        c.create_rectangle(*bb, outline="#ff2a33", width=1)
            self._sbl_hit.append((nm, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))

    def _sbl_canvas_click(self, ev):
        """Select the element whose box contains the click (else the nearest centre)."""
        hits = getattr(self, "_sbl_hit", None)
        if not hits:
            return
        inside = [nm for nm, x0, y0, x1, y1 in hits if x0 <= ev.x <= x1 and y0 <= ev.y <= y1]
        if inside:
            best = inside[-1]                     # topmost drawn
        else:
            best = min(hits, key=lambda h: ((h[1] + h[3]) / 2 - ev.x) ** 2
                       + ((h[2] + h[4]) / 2 - ev.y) ** 2)[0]
        if self._sbl_tv.exists(best):
            self._sbl_tv.selection_set(best); self._sbl_tv.see(best)

    def _sb_write(self, edits, what):
        """Shared write path: validate XEX, patch `edits` {slot: (x,y)}, log, resync UI."""
        xex = self._sb_xex()
        if not xex:
            messagebox.showerror("Scoreclock",
                "Game XEX not found — set the Game path in Settings."); return
        try:
            n = sbanchor.write_modes(xex, edits, self._log_q.put)
        except (ValueError, OSError) as e:
            messagebox.showerror("Scoreclock", str(e)); return
        self._log_q.put(f"[scoreclock] {what}: "
                        + (f"{n} slot(s) patched — takes effect on next game launch"
                           if n else "no change (already set)"))
        self._sb_reload()

    def _sb_apply(self, stock=False):
        if stock:
            x, y = sbanchor.STOCK[sbanchor.SCOREBUG_MODE]
        elif self._sb_pos.get():
            x, y = (int(v) for v in self._sb_pos.get().split(","))
        else:
            return
        self._sb_write({sbanchor.SCOREBUG_MODE: (x, y)},
                       f"scoreclock -> {sbanchor.pos_name(x, y)}")

    def _sb_apply_table(self):
        try:
            edits = {m: (sbanchor.X_VALS[vx.get()], sbanchor.Y_VALS[vy.get()])
                     for m, (vx, vy) in self._sb_mode_vars.items()}
        except KeyError:
            messagebox.showerror("Scoreclock",
                "Table not loaded yet — press Reload first."); return
        self._sb_write(edits, "anchor slot table")

    def _sb_restore_all(self):
        if not messagebox.askyesno("Scoreclock",
                "Restore ALL 9 anchor slots to stock?\n(This also puts the scoreclock back "
                "at Bottom-Left.)"):
            return
        self._sb_write(dict(sbanchor.STOCK), "restore all slots to stock")

    # ── Scoreclock element editor: edit actions ───────────────────────────────

    def _sbl_ped(self, nm):
        return self._sbl_pending.setdefault(nm, {})

    def _sbl_row(self, nm):
        return next((x for x in self._sbl_rows if x["name"] == nm), None)

    def _sbl_sync_selbar(self, nm):
        """Update the 'selected element' label + prime the size/scale entries from its state."""
        if not hasattr(self, "_sbl_selvar"):
            return
        r = self._sbl_row(nm) if nm else None
        if not r:
            self._sbl_selvar.set("(no element selected)"); return
        ed = self._sbl_pending.get(nm, {})
        if r["kind"] == "text":
            self._sbl_selvar.set(f"{r['label']}  ·  text")
            self._sbl_size.set(f"{ed.get('size', r.get('size', 0)):g}")
        else:
            self._sbl_selvar.set(f"{r['label']}  ·  logo/bar")
            self._sbl_sx.set(f"{ed.get('sx', 1):g}"); self._sbl_sy.set(f"{ed.get('sy', 1):g}")

    def _sbl_nudge(self, dx, dy):
        names = self._sbl_selection()
        if not names:
            return
        try:
            step = float(self._sbl_step.get())
        except ValueError:
            step = 5.0
        for nm in names:                           # nudge every selected element
            ed = self._sbl_ped(nm)
            ed["dx"] = ed.get("dx", 0) + dx * step
            ed["dy"] = ed.get("dy", 0) + dy * step
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_set_move(self):
        names = self._sbl_selection()
        if not names:
            return
        try:
            dx = float(self._sbl_dx.get() or 0); dy = float(self._sbl_dy.get() or 0)
        except ValueError:
            messagebox.showerror("Scoreclock", "dX / dY must be numbers."); return
        for nm in names:                           # same move to every selected element
            ed = self._sbl_ped(nm); ed["dx"], ed["dy"] = dx, dy
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_set_scale(self):
        names = self._sbl_selection()
        if not names:
            return
        try:
            sx = float(self._sbl_sx.get()); sy = float(self._sbl_sy.get())
        except ValueError:
            messagebox.showerror("Scoreclock", "Scale factors must be numbers."); return
        if sx <= 0 or sy <= 0:
            messagebox.showerror("Scoreclock", "Scale factors must be positive."); return
        applied = 0
        for nm in names:                           # scale now applies to BOTH meshes (vertex scale)
            r = self._sbl_row(nm)                  # and text (glyph scale via the transform matrix)
            if not r:
                continue
            ed = self._sbl_ped(nm)
            ed["sx"], ed["sy"] = sx, sy            # keep sx/sy even at 1.0 so it resets a prior scale
            applied += 1
        if not applied:
            return
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_edit_textures(self):
        """In-tab texture gallery: thumbnails of the scoreclock's overlay_static textures with
        identifying labels + direct Replace (preserves the layout edits in the same file)."""
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Scoreclock", "Set the game files folder in Settings."); return
        win = Toplevel(self)
        win.title("Scoreclock Textures — overlay_static.iff")
        win.geometry("860x600"); win.configure(bg=self._COL["bg1"])
        win.transient(self)
        ttk.Label(win, foreground="#999", font=("Segoe UI", 8), justify=LEFT, wraplength=820,
                  text="The scoreclock's textures (bars, glints, panels, 2K/SN logos, glow). "
                       "Replace one with a PNG/DDS — your layout edits in this file are preserved. "
                       "Team logos are per-matchup (logo_<team>.iff), not here. ★ = appears on the "
                       "scoreclock strip.").pack(fill=X, padx=10, pady=(10, 4))
        only_sc = BooleanVar(value=True)
        ttk.Checkbutton(win, text="Show only scoreclock textures", variable=only_sc,
                        command=lambda: fill()).pack(anchor=W, padx=10)

        canv = Canvas(win, bg=self._COL["bg1"], highlightthickness=0)
        vsb = ttk.Scrollbar(win, orient=VERTICAL, command=canv.yview)
        canv.configure(yscrollcommand=vsb.set)
        vsb.pack(side=RIGHT, fill=Y); canv.pack(fill=BOTH, expand=True, padx=10, pady=6)
        holder = ttk.Frame(canv)
        canv.create_window((0, 0), window=holder, anchor="nw")
        holder.bind("<Configure>", lambda e: canv.configure(scrollregion=canv.bbox("all")))

        self._sbl_texthumbs = []          # keep PhotoImage refs alive

        def do_replace(rec, label):
            p = filedialog.askopenfilename(
                title=f"Replace “{label}”",
                filetypes=[("Images", "*.png *.dds *.tga *.bmp"), ("All files", "*.*")])
            if not p:
                return
            self._sbl_statusvar.set(f"Replacing {label}…")
            def work():
                try:
                    # NO ensure_clean: that would wipe the layout edits sharing this file. Replace
                    # in place on the current archive; the layout blob is preserved.
                    status = archtex.replace_many("overlay_static.iff",
                                                  [{**rec, "path": p}], game_dir, self._log_q.put,
                                                  prefer_lossless=True)
                    self._log_q.put(f"  {status}")
                    self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror(
                        "Scoreclock", f"Replace failed:\n{e}", parent=win)); return
                self.after(0, lambda: (fill(),
                           self._sbl_statusvar.set(f"Replaced {label}. Relaunch to see it.")))
            threading.Thread(target=work, daemon=True).start()

        def do_extract(rec, label):
            p = filedialog.asksaveasfilename(
                title=f"Extract “{label}”", defaultextension=".png",
                initialfile=f"{label.split('(')[0].strip().replace(' ', '_')}.png",
                filetypes=[("PNG", "*.png"), ("DDS", "*.dds")])
            if not p:
                return
            def work():
                try:
                    img = archtex.decode_record("overlay_static.iff", rec, game_dir)
                    if p.lower().endswith(".dds"):
                        archtex.extract_record("overlay_static.iff", rec, p, game_dir)
                    else:
                        img.save(p)
                    self._log_q.put(f"[scoreclock] extracted {label} -> {p}")
                except Exception as e:
                    self.after(0, lambda: messagebox.showerror(
                        "Scoreclock", f"Extract failed:\n{e}", parent=win))
            threading.Thread(target=work, daemon=True).start()

        def fill():
            for w in holder.winfo_children():
                w.destroy()
            self._sbl_texthumbs.clear()
            try:
                recs = archtex.list_textures("overlay_static.iff", game_dir)
            except Exception as e:
                ttk.Label(holder, text=f"Couldn't read textures: {e}").grid(row=0, column=0); return
            cols = 3; r = c = 0
            for i, rec in enumerate(recs):
                label, is_sc = self._sblay.texture_label(i, rec)
                if only_sc.get() and not is_sc:
                    continue
                cell = ttk.Frame(holder, padding=6)
                cell.grid(row=r, column=c, sticky=W, padx=4, pady=4)
                # checkerboard thumbnail
                try:
                    img = archtex.decode_record("overlay_static.iff", rec, game_dir)
                except Exception:
                    img = None
                thumb = self._sbl_make_thumb(img, 150)
                if thumb:
                    self._sbl_texthumbs.append(thumb)
                    Label(cell, image=thumb, bd=1, relief=SOLID,
                          bg=self._COL["bg2"]).pack()
                star = "★ " if is_sc else ""
                ttk.Label(cell, text=f"{star}{label}", font=("Segoe UI", 8, "bold"),
                          wraplength=150).pack(anchor=W, pady=(3, 0))
                ttk.Label(cell, text=f"#{i} · {rec['w']}×{rec['h']} {rec['fmt']}",
                          foreground="#888", font=("Segoe UI", 7)).pack(anchor=W)
                bb = ttk.Frame(cell); bb.pack(anchor=W, pady=(2, 0))
                ttk.Button(bb, text="Replace…", width=9,
                           command=lambda rr=rec, ll=label: do_replace(rr, ll)).pack(side=LEFT)
                ttk.Button(bb, text="Extract…", width=9,
                           command=lambda rr=rec, ll=label: do_extract(rr, ll)).pack(side=LEFT, padx=2)
                c += 1
                if c >= cols:
                    c = 0; r += 1
        fill()

    def _sbl_make_thumb(self, img, size):
        """RGBA PIL -> PhotoImage over a checkerboard (fast tiled build), or None."""
        if img is None:
            return None
        from PIL import Image as _I
        img = img.convert("RGBA"); img.thumbnail((size, size))
        w, h = img.size
        light = _I.new("RGBA", (16, 16), (150, 150, 150, 255))
        dark = _I.new("RGBA", (16, 16), (90, 90, 90, 255))
        bg = _I.new("RGBA", (w, h), (90, 90, 90, 255))
        for yy in range(0, h, 16):
            for xx in range(0, w, 16):
                bg.paste(light if (xx // 16 + yy // 16) % 2 else dark, (xx, yy))
        return ImageTk.PhotoImage(_I.alpha_composite(bg, img))

    def _sbl_set_size(self):
        names = self._sbl_selection()
        if not names:
            return
        try:
            s = float(self._sbl_size.get())
        except ValueError:
            messagebox.showerror("Scoreclock", "Font size must be a number."); return
        applied = 0
        for nm in names:                           # font size is text-only; skip meshes silently
            r = self._sbl_row(nm)
            if not r or r["kind"] != "text":
                continue
            self._sbl_ped(nm)["size"] = s
            applied += 1
        if not applied:
            messagebox.showinfo("Scoreclock", "Width applies to text elements. Use Scale "
                                "for logos and bars."); return
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_set_font(self):
        """Repoint the selected text element(s) to a different font resource = per-element SIZE."""
        names = self._sbl_selection()
        if not names:
            return
        fh = self._sblay._FONT_HASH.get(self._sbl_font.get())
        if fh is None:
            return
        applied = 0
        for nm in names:                           # font is text-only; skip meshes silently
            r = self._sbl_row(nm)
            if not r or r["kind"] != "text":
                continue
            self._sbl_ped(nm)["font"] = fh
            applied += 1
        if not applied:
            messagebox.showinfo("Scoreclock", "Font applies to text elements (scores, clock, "
                                "abbreviations)."); return
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_pick_color(self):
        """Right-panel swatch picker — applies one chosen colour to every selected text
        element (meshes in the selection are skipped)."""
        names = self._sbl_selection()
        if not names:
            return
        text_names = [nm for nm in names
                      if (self._sbl_row(nm) or {}).get("kind") == "text"]
        if not text_names:
            messagebox.showinfo("Scoreclock", "Colour applies to text elements (scores, clock, "
                                "abbreviations)."); return
        from tkinter.colorchooser import askcolor
        nm0 = text_names[0]; r0 = self._sbl_row(nm0)     # seed from the first text element
        init = self._sbl_pending.get(nm0, {}).get("color")
        init = "#%02X%02X%02X" % tuple(init) if init else r0.get("color", "#FFFFFF")
        title = (f"Colour for {r0['label']}" if len(text_names) == 1
                 else f"Colour for {len(text_names)} elements")
        rgb, _hexv = askcolor(color=init, title=title)
        if rgb:
            col = tuple(int(c) for c in rgb)
            for nm in text_names:
                self._sbl_ped(nm)["color"] = col
            self._sbl_refresh_table(); self._sbl_render()

    @staticmethod
    def _sbl_parse_hex(s):
        """'#RGB' / '#RRGGBB' / same without '#' -> (r, g, b) 0-255, or None if invalid."""
        s = (s or "").strip().lstrip("#").strip()
        hexdig = "0123456789abcdefABCDEF"
        if len(s) == 3 and all(ch in hexdig for ch in s):
            return tuple(int(ch * 2, 16) for ch in s)
        if len(s) == 6 and all(ch in hexdig for ch in s):
            return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
        return None

    def _sbl_edit_color_hex(self, nm):
        """Double-click the Color cell: type a hex colour (#RGB / #RRGGBB / no '#'). Validated —
        invalid input shows an error and changes nothing."""
        r = self._sbl_row(nm)
        if not r or r["kind"] != "text":
            messagebox.showinfo("Scoreclock", "Colour applies to text elements (scores, clock, "
                                "abbreviations)."); return
        ed = self._sbl_pending.get(nm, {})
        cur = ("#%02X%02X%02X" % tuple(int(c) & 0xFF for c in ed["color"])
               if "color" in ed else r.get("color", "#FFFFFF"))
        val = simpledialog.askstring("Colour", f"Hex colour for “{r['label']}” (e.g. #1E90FF):",
                                     initialvalue=cur, parent=self)
        if val is None:
            return
        rgb = self._sbl_parse_hex(val)
        if rgb is None:
            messagebox.showerror("Colour", "Not a valid hex colour (e.g. #1E90FF)."); return
        self._sbl_ped(nm)["color"] = rgb
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_set_hidden(self, hide):
        """Hide (shove off-screen) or show every selected element. Reversible."""
        names = self._sbl_selection()
        if not names:
            return
        for nm in names:
            ed = self._sbl_ped(nm)
            if hide:
                ed["hidden"] = True
            else:
                ed.pop("hidden", None)
                if not ed:
                    self._sbl_pending.pop(nm, None)
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_clear_sel(self):
        changed = False
        for nm in self._sbl_selection():           # clear pending for every selected element
            if nm in self._sbl_pending:
                del self._sbl_pending[nm]; changed = True
        if changed:
            self._sbl_refresh_table(); self._sbl_render()

    def _sbl_clear_all(self):
        """Discard everything not yet written to the files -> preview returns to current state."""
        self._sbl_pending.clear()
        self._sbl_factory = False
        self._sbl_refresh_table(); self._sbl_render()
        self._sbl_statusvar.set("")

    def _sbl_tv_double(self, ev):
        """Double-click a cell (single row): X / Y open a number prompt; Font Size (text) and
        Width / Height (mesh) open a number prompt; Color opens the hex-entry prompt. Everything
        writes into self._sbl_pending, then re-renders."""
        tv = self._sbl_tv
        rowid = tv.identify_row(ev.y)
        colid = tv.identify_column(ev.x)          # '#1'..'#8'
        if not rowid:
            return
        nm = rowid
        r = self._sbl_row(nm)
        if not r:
            return
        cols = ("element", "x", "y", "fontsize", "width", "height", "color", "pending")
        try:
            cname = cols[int(colid.replace("#", "")) - 1]
        except (ValueError, IndexError):
            return
        tv.selection_set(nm); self._sbl_render()
        if cname == "color":
            self._sbl_edit_color_hex(nm); return
        if cname not in ("x", "y", "fontsize", "width", "height"):
            return
        is_text = r["kind"] == "text"
        if cname == "fontsize" and not is_text:
            messagebox.showinfo("Scoreclock", "Meshes have no font size — edit Width/Height."); return
        if cname in ("width", "height") and is_text:
            messagebox.showinfo("Scoreclock", "Text has no width/height — edit Font Size."); return
        ed = self._sbl_pending.get(nm, {})
        if cname == "x":
            cur = r["x"] + float(ed.get("dx", 0)); title = "X position"
        elif cname == "y":
            cur = r["y"] + float(ed.get("dy", 0)); title = "Y position"
        elif cname == "fontsize":
            cur = float(ed.get("size", r.get("size", 0))); title = "Font size"
        elif cname == "width":
            cur = r.get("w", 0) * float(ed.get("sx", 1)); title = "Width"
        else:  # height
            cur = r.get("h", 0) * float(ed.get("sy", 1)); title = "Height"
        val = simpledialog.askstring("Scoreclock", f"{title} for “{r['label']}”:",
                                     initialvalue=f"{cur:g}", parent=self)
        if val is None:
            return
        try:
            f = float(val.strip())                 # digits / decimal / minus only
        except ValueError:
            messagebox.showerror("Scoreclock", "Enter a number (digits, an optional decimal point "
                                 "and minus sign)."); return
        e = self._sbl_ped(nm)
        if cname == "x":
            e["dx"] = f - r["x"]                    # delta from the on-disk value
        elif cname == "y":
            e["dy"] = f - r["y"]
        elif cname == "fontsize":
            e["size"] = f                          # absolute font size
        elif cname == "width":
            base = r.get("w", 0)                    # target width -> scale factor about centroid
            if base <= 0:
                messagebox.showerror("Scoreclock", "This mesh has no measurable width."); return
            e["sx"] = f / base                      # preserves any existing pending sy
        else:  # height
            base = r.get("h", 0)
            if base <= 0:
                messagebox.showerror("Scoreclock", "This mesh has no measurable height."); return
            e["sy"] = f / base                      # preserves any existing pending sx
        self._sbl_refresh_table(); self._sbl_render()

    def _sbl_apply_all(self):
        """Apply the ABSOLUTE current editor value for EVERY element (not just changed ones):
        pending moves/scales + a forced absolute size & colour for each text element, so a loaded
        preset commits fully. Background thread, same pattern as _sbl_apply."""
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Scoreclock", "Set the game files folder in Settings."); return
        pending = {n: dict(e) for n, e in self._sbl_pending.items()}
        self._sbl_statusvar.set("Applying all shown…")
        def work():
            try:
                edits = {}
                for r in self._sblay.list_elements(game_dir):
                    nm = r["name"]
                    ed = dict(pending.get(nm, {}))
                    if r["kind"] == "text":            # force absolute size + colour to the shown value
                        if "size" not in ed and r.get("size") is not None:
                            ed["size"] = r["size"]
                        if "color" not in ed:
                            c = (r.get("color") or "#FFFFFF").lstrip("#")
                            ed["color"] = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
                    if ed:
                        edits[nm] = ed
                note = (self._sblay.apply_edits(edits, game_dir, self._log_q.put)
                        if edits else "nothing to apply")
                rows = self._sblay.list_elements(game_dir)
            except Exception as e:
                self.after(0, lambda: (self._sbl_statusvar.set(f"Apply failed: {e}"),
                                       messagebox.showerror("Scoreclock", f"Apply failed:\n{e}")))
                return
            def done():
                self._sbl_pending.clear(); self._sbl_factory = False
                self._sbl_after_load(rows)
                self._sbl_autosave(rows)
                self._log_q.put(f"[scoreclock] applied ALL shown ({len(edits)} element(s)) — {note}")
                self._sbl_statusvar.set("Applied all shown (auto-backed-up). Relaunch the game to see it.")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ── Scoreclock presets (full editor edit-set, saved to APPDATA) ────────────

    # Reserved preset auto-written after every Apply so an accidental reset (or a launcher/game
    # rebuild that re-cleans overlay_static) is always ONE "Load" away from your last look.
    _SBL_AUTOSAVE = "↺ Last Applied (auto-backup)"

    def _sbl_preset_file(self):
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "NHL2k10ModLauncher")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "scoreclock_presets.json")

    def _sbl_presets_read(self):
        """{name: edit-set} of USER presets ('Default' is synthetic and never stored)."""
        try:
            with open(self._sbl_preset_file(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.pop("Default", None)
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _sbl_presets_write(self, data):
        try:
            with open(self._sbl_preset_file(), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            messagebox.showerror("Scoreclock", f"Couldn't save presets:\n{e}")

    def _sbl_pending_serializable(self):
        """JSON-safe copy of the pending edit-set (colour tuples -> lists)."""
        out = {}
        for nm, ed in self._sbl_pending.items():
            e = dict(ed)
            if "color" in e:
                e["color"] = [int(c) & 0xFF for c in e["color"]]
            out[nm] = e
        return out

    def _sbl_effective_snapshot(self, rows=None, pending=None):
        """ABSOLUTE layout of every element = file rows + pending edits, JSON-safe. This is what a
        preset stores now (not disposable relative deltas) so it survives Apply + any reset.
        text -> {kind,x,y,size,color:[r,g,b],hidden}; mesh -> {kind,x,y,w,h,hidden}."""
        rows = self._sbl_rows if rows is None else rows
        pending = self._sbl_pending if pending is None else pending
        snap = {}
        for r in rows or []:
            nm = r["name"]; ed = pending.get(nm, {})
            hidden = bool(ed.get("hidden"))
            x = r["x"] + float(ed.get("dx", 0)); y = r["y"] + float(ed.get("dy", 0))
            if r["kind"] == "text":
                size = float(ed.get("size", r.get("size", 0) or 0))
                if "color" in ed:
                    col = [int(c) & 0xFF for c in ed["color"]]
                else:
                    cc = (r.get("color") or "#FFFFFF").lstrip("#")
                    col = [int(cc[i:i + 2], 16) for i in (0, 2, 4)] if len(cc) >= 6 else [255, 255, 255]
                snap[nm] = {"kind": "text", "x": x, "y": y, "size": size, "color": col, "hidden": hidden}
            else:
                w = (r.get("w", 0) or 0) * float(ed.get("sx", 1))
                h = (r.get("h", 0) or 0) * float(ed.get("sy", 1))
                snap[nm] = {"kind": "mesh", "x": x, "y": y, "w": w, "h": h, "hidden": hidden}
        return snap

    def _sbl_snapshot_to_pending(self, snap, rows=None):
        """Minimal pending deltas that turn the CURRENT file rows into an absolute snapshot — so
        loading a preset restores the exact look no matter what base it lands on (e.g. pristine)."""
        rows = self._sbl_rows if rows is None else rows
        by = {r["name"]: r for r in (rows or [])}
        pend = {}
        for nm, a in snap.items():
            r = by.get(nm)
            if not r:
                continue
            ed = {}
            dx = float(a.get("x", r["x"])) - r["x"]
            dy = float(a.get("y", r["y"])) - r["y"]
            if abs(dx) > 1e-4: ed["dx"] = dx
            if abs(dy) > 1e-4: ed["dy"] = dy
            if a.get("hidden"): ed["hidden"] = True
            if r["kind"] == "text":
                if "size" in a and abs(float(a["size"]) - float(r.get("size", 0) or 0)) > 1e-4:
                    ed["size"] = float(a["size"])
                if "color" in a:
                    col = tuple(int(c) & 0xFF for c in a["color"])
                    cur = (r.get("color") or "#FFFFFF").lstrip("#")
                    curc = tuple(int(cur[i:i + 2], 16) for i in (0, 2, 4)) if len(cur) >= 6 else (255, 255, 255)
                    if col != curc:
                        ed["color"] = col
            else:
                rw = (r.get("w", 0) or 0.0); rh = (r.get("h", 0) or 0.0)
                if rw > 1e-6 and abs(float(a.get("w", rw)) / rw - 1) > 1e-4: ed["sx"] = float(a["w"]) / rw
                if rh > 1e-6 and abs(float(a.get("h", rh)) / rh - 1) > 1e-4: ed["sy"] = float(a["h"]) / rh
            if ed:
                pend[nm] = ed
        return pend

    def _sbl_autosave(self, rows):
        """Write the just-applied absolute layout to the reserved auto-backup preset."""
        try:
            if not rows:
                return
            data = self._sbl_presets_read()
            data[self._SBL_AUTOSAVE] = {"__abs__": self._sbl_effective_snapshot(rows=rows, pending={})}
            self._sbl_presets_write(data)
            self._sbl_preset_refresh(select=self._sbl_preset_var.get() or "Default")
        except Exception as e:
            self._log_q.put(f"[scoreclock] auto-backup failed: {e}")

    def _sbl_preset_refresh(self, select=None):
        data = self._sbl_presets_read()
        names = ["Default"]
        if self._SBL_AUTOSAVE in data:
            names.append(self._SBL_AUTOSAVE)
        names += sorted(n for n in data if n != self._SBL_AUTOSAVE)
        self._sbl_preset_cb.config(values=names)
        if select and select in names:
            self._sbl_preset_var.set(select)
        elif self._sbl_preset_var.get() not in names:
            self._sbl_preset_var.set("Default")

    def _sbl_preset_load(self):
        name = self._sbl_preset_var.get()
        if not name:
            return
        if name == "Default":                     # factory/stock — what the old "Reset to Default" did
            game_dir = self._get_game_root()
            if not game_dir:
                messagebox.showerror("Scoreclock", "Set the game files folder in Settings."); return
            self._sbl_statusvar.set("Loading default…")
            def work():
                try:
                    fe = dict(self._sblay.factory_edits(game_dir))
                except Exception as e:
                    self.after(0, lambda: self._sbl_statusvar.set(f"Couldn't load default: {e}"))
                    return
                def done():
                    self._sbl_pending = fe
                    self._sbl_factory = False
                    self._sbl_refresh_table(); self._sbl_render()
                    self._sbl_statusvar.set("Loaded DEFAULT — Apply to write it, or Discard to keep "
                                            "your current look.")
                self.after(0, done)
            threading.Thread(target=work, daemon=True).start()
            return
        ed = self._sbl_presets_read().get(name)
        if ed is None:
            messagebox.showinfo("Scoreclock", f"No saved preset named “{name}”."); return
        if not self._sbl_rows:
            messagebox.showinfo("Scoreclock", "The scene is still loading — try Load again in a "
                                "moment."); return
        if isinstance(ed, dict) and "__abs__" in ed:      # new absolute-snapshot format
            self._sbl_pending = self._sbl_snapshot_to_pending(ed["__abs__"])
        elif isinstance(ed, dict) and ed:                 # legacy relative-delta format
            self._sbl_pending = {n: dict(e) for n, e in ed.items()}
        else:                                             # empty preset -> nothing to restore
            messagebox.showinfo("Scoreclock", f"Preset “{name}” is empty (saved with no layout). "
                                "Nothing to load."); return
        self._sbl_factory = False
        self._sbl_refresh_table(); self._sbl_render()
        n = len(self._sbl_pending)
        self._sbl_statusvar.set(f"Loaded preset “{name}” ({n} element(s)) — Apply to write it.")

    def _sbl_preset_save(self):
        name = self._sbl_preset_var.get()
        if not name:
            messagebox.showinfo("Scoreclock", "Pick a preset to overwrite, or use Save As…"); return
        if name.strip().lower() == "default" or name == self._SBL_AUTOSAVE:
            messagebox.showinfo("Presets", "That preset is reserved and can't be overwritten — use "
                                "Save As… to make your own."); return
        if not self._sbl_rows:
            messagebox.showinfo("Scoreclock", "The scene isn't loaded yet — nothing to save."); return
        data = self._sbl_presets_read()
        data[name] = {"__abs__": self._sbl_effective_snapshot()}
        self._sbl_presets_write(data)
        self._sbl_preset_refresh(select=name)
        self._sbl_statusvar.set(f"Saved preset “{name}” (full layout of {len(self._sbl_rows)} elements).")

    def _sbl_preset_save_as(self):
        name = simpledialog.askstring("Scoreclock", "New preset name:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()
        if name.lower() == "default" or name == self._SBL_AUTOSAVE:
            messagebox.showerror("Presets", "That name is reserved — pick another."); return
        if not self._sbl_rows:
            messagebox.showinfo("Scoreclock", "The scene isn't loaded yet — nothing to save."); return
        data = self._sbl_presets_read()
        if name in data and not messagebox.askyesno(
                "Scoreclock", f"Preset “{name}” already exists — overwrite it?"):
            return
        data[name] = {"__abs__": self._sbl_effective_snapshot()}
        self._sbl_presets_write(data)
        self._sbl_preset_refresh(select=name)
        self._sbl_statusvar.set(f"Saved preset “{name}” (full layout of {len(self._sbl_rows)} elements).")

    def _sbl_preset_delete(self):
        name = self._sbl_preset_var.get()
        if not name or name.strip().lower() == "default":
            messagebox.showinfo("Presets", "The built-in “Default” preset can't be deleted."); return
        data = self._sbl_presets_read()
        if name not in data:
            messagebox.showinfo("Scoreclock", f"No saved preset named “{name}”."); return
        if not messagebox.askyesno("Scoreclock", f"Delete preset “{name}”?"):
            return
        data.pop(name, None)
        self._sbl_presets_write(data)
        self._sbl_preset_refresh()
        self._sbl_statusvar.set(f"Deleted preset “{name}”.")

    def _sbl_apply(self):
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Scoreclock", "Set the game files folder in Settings."); return
        factory = self._sbl_factory
        pending = {n: e for n, e in self._sbl_pending.items() if e}
        if not factory and not pending:
            messagebox.showinfo("Scoreclock", "No pending changes."); return
        self._sbl_statusvar.set("Applying…")
        def work():
            try:
                notes = []
                if factory:
                    notes.append(self._sblay.reset_to_factory(game_dir, self._log_q.put))
                if pending:
                    notes.append(self._sblay.apply_edits(pending, game_dir, self._log_q.put))
                rows = self._sblay.list_elements(game_dir)
            except Exception as e:
                self.after(0, lambda: (self._sbl_statusvar.set(f"Apply failed: {e}"),
                                       messagebox.showerror("Scoreclock", f"Apply failed:\n{e}")))
                return
            def done():
                self._sbl_pending.clear(); self._sbl_factory = False
                self._sbl_after_load(rows)
                self._sbl_autosave(rows)
                what = ("reset to default" if factory else "")
                if pending:
                    what = (what + " + " if what else "") + f"{len(pending)} edit(s)"
                self._log_q.put(f"[scoreclock] applied {what} — shows on next game launch")
                for n in notes:
                    self._log_q.put(f"  {n}")
                self._sbl_statusvar.set(f"Applied {what}. Relaunch the game to see it.")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ── Whole-scoreclock screen anchor (secondary dialog) ─────────────────────

    def _sb_anchor_dialog(self):
        """Whole-scoreclock placement: the 3×3 screen anchor (XEX patch) + the advanced
        shared-slot table. Secondary to the per-element editor."""
        xex = self._sb_xex()
        win = Toplevel(self)
        win.title("Whole-Scoreclock Position")
        win.configure(bg=self._COL["bg1"])
        win.transient(self); win.resizable(False, False)
        pad = ttk.Frame(win, padding=16); pad.pack(fill=BOTH, expand=True)

        ttk.Label(pad, text="Whole-Scoreclock Screen Position",
                  font=("Segoe UI", 12, "bold")).pack(anchor=W)
        ttk.Label(pad, foreground="#999", font=("Segoe UI", 8), justify=LEFT, wraplength=560,
                  text="Moves the entire scoreclock by patching its screen anchor in the game XEX "
                       "(stock = Bottom-Left). Applies immediately (game must be closed); shows on "
                       "next launch. The instant-replay watermark shares this anchor.").pack(
            anchor=W, pady=(2, 8))

        bar = ttk.Frame(pad); bar.pack(anchor=W, pady=(0, 8))
        ttk.Label(bar, text="Game XEX:").pack(side=LEFT)
        self._sb_xexlbl = StringVar(value=str(xex) if xex else "not found — set Game path in Settings")
        ttk.Label(bar, textvariable=self._sb_xexlbl, foreground="#999").pack(side=LEFT, padx=(4, 0))

        grid_f = ttk.LabelFrame(pad, text="On-screen anchor", padding=10); grid_f.pack(anchor=W)
        self._sb_pos = StringVar(value="")
        stock = sbanchor.STOCK[sbanchor.SCOREBUG_MODE]
        for r, (yname, y) in enumerate((("Top", 3), ("Middle", 5), ("Bottom", 4))):
            for c, (xname, x) in enumerate((("Left", 1), ("Center", 2), ("Right", 5))):
                txt = f"{yname}-{xname}" + ("  (stock)" if (x, y) == stock else "")
                ttk.Radiobutton(grid_f, text=txt, value=f"{x},{y}",
                                variable=self._sb_pos).grid(row=r, column=c, sticky=W,
                                                            padx=12, pady=6)
        btns = ttk.Frame(pad); btns.pack(anchor=W, pady=(10, 0))
        ttk.Button(btns, text="Apply Position", style="Accent.TButton",
                   command=self._sb_apply).pack(side=LEFT, padx=(0, 6))
        ttk.Button(btns, text="Restore Stock (Bottom-Left)",
                   command=lambda: self._sb_apply(stock=True)).pack(side=LEFT)

        adv = ttk.LabelFrame(pad, text="Advanced — shared anchor slots 1-9", padding=10)
        adv.pack(anchor=W, pady=(14, 0))
        ttk.Label(adv, foreground="#999", font=("Segoe UI", 8), justify=LEFT, wraplength=540,
                  text="Every HUD/menu widget pins to one of these 9 shared slots; the scoreclock "
                       "uses slot 7. Editing a slot moves everything that uses it.").grid(
            row=0, column=0, columnspan=4, sticky=W, pady=(0, 6))
        self._sb_mode_vars = {}
        for m in range(1, sbanchor.N_MODES):
            ttk.Label(adv, text=f"Slot {m}").grid(row=m, column=0, sticky=W, padx=(0, 8))
            vx = StringVar(value=""); vy = StringVar(value="")
            ttk.Combobox(adv, textvariable=vx, state="readonly", width=8,
                         values=list(sbanchor.X_VALS)).grid(row=m, column=1, padx=2, pady=1)
            ttk.Combobox(adv, textvariable=vy, state="readonly", width=8,
                         values=list(sbanchor.Y_VALS)).grid(row=m, column=2, padx=2, pady=1)
            self._sb_mode_vars[m] = (vx, vy)
            note = "stock " + sbanchor.pos_name(*sbanchor.STOCK[m])
            if sbanchor.MODE_USERS.get(m):
                note += f" — {sbanchor.MODE_USERS[m]}"
            ttk.Label(adv, text=note, foreground="#999",
                      font=("Segoe UI", 8)).grid(row=m, column=3, sticky=W, padx=(10, 0))
        abtns = ttk.Frame(adv); abtns.grid(row=sbanchor.N_MODES, column=0, columnspan=4,
                                           sticky=W, pady=(8, 0))
        ttk.Button(abtns, text="Apply Slot Table",
                   command=self._sb_apply_table).pack(side=LEFT, padx=(0, 6))
        ttk.Button(abtns, text="Restore ALL Slots to Stock",
                   command=self._sb_restore_all).pack(side=LEFT)

        self._sb_reload()

    def _sb_reload(self):
        """Read the anchor table from the XEX (background) and sync the dialog controls, if open."""
        if not hasattr(self, "_sb_pos"):
            return
        xex = self._sb_xex()
        if not xex:
            self._sb_xexlbl.set("not found — set the Game path in Settings"); return
        self._sb_xexlbl.set(str(xex))
        def work():
            try:
                table = sbanchor.read_table(xex); err = None
            except Exception as e:
                table, err = None, str(e)
            def done():
                if err:
                    self._sb_xexlbl.set(f"{xex.name}: {err}")
                    self._log_q.put(f"[scoreclock] {xex}: {err}"); return
                x, y = table[sbanchor.SCOREBUG_MODE]
                self._sb_pos.set(f"{x},{y}")
                for m, (vx, vy) in self._sb_mode_vars.items():
                    tx, ty = table[m]
                    vx.set(sbanchor.X_NAMES[tx]); vy.set(sbanchor.Y_NAMES[ty])
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ── Gameplay tab ──────────────────────────────────────────────────────────

    def _build_gameplay_tab(self):
        """Gameplay tuning constants ("tuners") — bare floats in the XEX's .data that the
        gameplay engine reads directly. Registry + presets come from the Title Update #1
        binary diff (docs 16/17): the '2K Official Patch (v1.1)' preset writes 2K's own
        retuned values into our v1.0 xex."""
        t = self._tab_gameplay
        from launcher import gameplay_tuning as gpt
        self._gpt = gpt
        self._gpt_cur = {}            # {key: current float in xex}
        self._gpt_pending = {}        # {key: queued float}

        head = ttk.Frame(t, padding=(12, 10, 12, 4)); head.pack(fill=X)
        ttk.Label(head, text="Gameplay Tuning",
                  font=("Segoe UI", 13, "bold")).pack(side=LEFT)
        self._gpt_xexlbl = StringVar(value="")
        ttk.Label(head, textvariable=self._gpt_xexlbl, foreground="#999").pack(side=RIGHT)
        ttk.Label(t, foreground="#999", font=("Segoe UI", 8), justify=LEFT, wraplength=940,
                  text="Engine tuning constants recovered by diffing the official Title Update #1 "
                       "against the retail game. Queue values (or load a preset), then Apply — "
                       "writes default.xex; takes effect on the NEXT game launch. "
                       "“2K Official Patch (v1.1)” = the values 2K shipped in the title update: "
                       "faster acceleration, easier pinning, retuned shot velocity and rebounds."
                  ).pack(fill=X, padx=12)

        body = ttk.Frame(t); body.pack(fill=BOTH, expand=True, padx=12, pady=6)

        cols = ("tuner", "group", "current", "stock", "v11", "state", "pending")
        tv = ttk.Treeview(body, columns=cols, show="headings", height=18,
                          selectmode="extended")
        for c, w, a in (("tuner", 250, W), ("group", 130, W), ("current", 80, E),
                        ("stock", 80, E), ("v11", 90, E), ("state", 70, W), ("pending", 90, E)):
            tv.heading(c, text={"tuner": "Tuner", "group": "Group", "current": "Current",
                                "stock": "Stock (v1.0)", "v11": "2K Patch (v1.1)",
                                "state": "State", "pending": "Pending"}[c])
            tv.column(c, width=w, anchor=a)
        sb = ttk.Scrollbar(body, command=tv.yview); tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); tv.pack(side=LEFT, fill=BOTH, expand=True)
        tv.bind("<<TreeviewSelect>>", lambda e: self._gpt_show_note())
        tv.bind("<Double-1>", lambda e: self._gpt_edit_dialog())
        self._gpt_tv = tv

        # right-hand controls
        ctl = ttk.Frame(body, padding=(12, 0, 0, 0)); ctl.pack(side=LEFT, fill=Y)
        ed = ttk.LabelFrame(ctl, text="Set selected", padding=8); ed.pack(fill=X)
        row = ttk.Frame(ed); row.pack(fill=X)
        ttk.Label(row, text="Value").pack(side=LEFT)
        self._gpt_val = StringVar()
        ttk.Entry(row, textvariable=self._gpt_val, width=10).pack(side=LEFT, padx=4)
        ttk.Button(row, text="Queue", width=7, command=self._gpt_queue_value).pack(side=LEFT)
        ttk.Label(ed, text="Or double-click a row. Queued values are only written on Apply.",
                  foreground="#888", font=("Segoe UI", 7), wraplength=200,
                  justify=LEFT).pack(anchor=W, pady=(3, 0))

        pr = ttk.LabelFrame(ctl, text="Presets", padding=8); pr.pack(fill=X, pady=(8, 0))
        ttk.Button(pr, text="2K Official Patch (v1.1)",
                   command=lambda: self._gpt_queue_preset("v11")).pack(fill=X)
        ttk.Button(pr, text="Stock (v1.0 factory)",
                   command=lambda: self._gpt_queue_preset("stock")).pack(fill=X, pady=(4, 0))
        ttk.Label(pr, text="Presets queue every tuner; review the Pending column, then Apply.",
                  foreground="#888", font=("Segoe UI", 7), wraplength=200,
                  justify=LEFT).pack(anchor=W, pady=(3, 0))

        note = ttk.LabelFrame(ctl, text="About this tuner", padding=8); note.pack(fill=X, pady=(8, 0))
        self._gpt_note = StringVar(value="(select a tuner)")
        ttk.Label(note, textvariable=self._gpt_note, foreground="#aaa",
                  font=("Segoe UI", 8), wraplength=200, justify=LEFT).pack(anchor=W)

        act = ttk.Frame(t, padding=(12, 4, 12, 10)); act.pack(fill=X)
        ttk.Button(act, text="Apply to Game Files", style="Accent.TButton",
                   command=self._gpt_apply).pack(side=LEFT, padx=(0, 6))
        ttk.Button(act, text="Discard Pending", command=self._gpt_discard).pack(side=LEFT, padx=(0, 6))
        ttk.Button(act, text="Reload", command=self._gpt_load).pack(side=LEFT)
        self._gpt_status = StringVar(value="")
        ttk.Label(act, textvariable=self._gpt_status, foreground="#999").pack(side=RIGHT)

        self.after(900, self._gpt_load)

    @staticmethod
    def _gpt_fmt(v):
        return f"{v:.6g}" if v is not None else ""

    def _gpt_load(self):
        xex = self._sb_xex()
        if not xex:
            self._gpt_status.set("Set the game files folder in Settings.")
            return
        self._gpt_xexlbl.set(str(xex))
        def work():
            try:
                cur = self._gpt.read_all(str(xex))
                err = None
            except Exception as e:
                cur, err = {}, str(e)
            def done():
                if err:
                    self._gpt_status.set(err)
                    self._log_q.put(f"[gameplay] {xex}: {err}"); return
                self._gpt_cur = cur
                self._gpt_refresh()
                n = sum(1 for k, v in cur.items() if self._gpt.classify(k, v) != "stock")
                self._gpt_status.set(f"{len(cur)} tuners read — "
                                     + ("all stock" if n == 0 else f"{n} modified"))
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _gpt_refresh(self):
        tv = self._gpt_tv
        sel = set(tv.selection())
        tv.delete(*tv.get_children())
        for t in self._gpt.TUNERS:
            k = t["key"]
            cur = self._gpt_cur.get(k)
            state = self._gpt.classify(k, cur) if cur is not None else "?"
            pend = self._gpt_pending.get(k)
            v11 = None if t["v11"] is None else self._gpt._f(t["v11"])
            tv.insert("", END, iid=k, values=(
                t["label"], t["group"], self._gpt_fmt(cur),
                self._gpt_fmt(self._gpt._f(t["stock"])), self._gpt_fmt(v11),
                state, self._gpt_fmt(pend) if pend is not None else ""))
        for iid in sel:
            if tv.exists(iid):
                tv.selection_add(iid)

    def _gpt_show_note(self):
        sel = self._gpt_tv.selection()
        if not sel:
            self._gpt_note.set("(select a tuner)"); return
        t = self._gpt.BY_KEY[sel[0]]
        self._gpt_note.set(f"{t['note']}\n\nVA 0x{t['va']:X} — also live-pokeable via Cheat "
                           f"Engine while the game runs (float, big-endian).")

    def _gpt_queue_value(self):
        sel = self._gpt_tv.selection()
        if not sel:
            messagebox.showerror("Gameplay", "Select one or more tuners first."); return
        try:
            v = float(self._gpt_val.get())
        except ValueError:
            messagebox.showerror("Gameplay", "Value must be a number."); return
        for k in sel:
            self._gpt_pending[k] = v
        self._gpt_refresh()

    def _gpt_edit_dialog(self):
        sel = self._gpt_tv.selection()
        if not sel:
            return
        k = sel[0]; t = self._gpt.BY_KEY[k]
        cur = self._gpt_pending.get(k, self._gpt_cur.get(k))
        v = simpledialog.askstring("Gameplay", f"{t['label']}\n\n{t['note']}\n\nNew value:",
                                   initialvalue=self._gpt_fmt(cur), parent=self)
        if v is None:
            return
        try:
            self._gpt_pending[k] = float(v)
        except ValueError:
            messagebox.showerror("Gameplay", "Value must be a number."); return
        self._gpt_refresh()

    def _gpt_queue_preset(self, which):
        vals = self._gpt.preset_v11() if which == "v11" else self._gpt.preset_stock()
        # only queue actual changes vs current file state
        n = 0
        for k, v in vals.items():
            cur = self._gpt_cur.get(k)
            if cur is None or self._gpt_fmt(cur) != self._gpt_fmt(v):
                self._gpt_pending[k] = v; n += 1
            else:
                self._gpt_pending.pop(k, None)
        self._gpt_refresh()
        self._gpt_status.set(f"preset queued — {n} change(s) pending" if n else
                             "already matches that preset — nothing to apply")

    def _gpt_discard(self):
        self._gpt_pending.clear()
        self._gpt_refresh()
        self._gpt_status.set("pending changes discarded")

    def _gpt_apply(self):
        if not self._gpt_pending:
            self._gpt_status.set("nothing pending"); return
        xex = self._sb_xex()
        if not xex:
            messagebox.showerror("Gameplay", "Set the game files folder in Settings."); return
        edits = dict(self._gpt_pending)
        def work():
            try:
                n = self._gpt.write_values(str(xex), edits, log=self._log_q.put)
                err = None
            except Exception as e:
                n, err = 0, str(e)
            def done():
                if err:
                    messagebox.showerror("Gameplay", f"Apply failed:\n{err}"); return
                self._gpt_pending.clear()
                self._log_q.put(f"[gameplay] wrote {n} tuner(s) to {xex.name}")
                self._gpt_status.set(f"applied {n} change(s) — restart the game to take effect")
                self._gpt_load()
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # ── Settings tab ──────────────────────────────────────────────────────────

    def _build_settings_tab(self):
        outer = ttk.Frame(self._tab_settings, padding=28)
        outer.pack(fill=BOTH, expand=True)

        ttk.Label(outer, text="Settings",
                  font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, columnspan=3, sticky=W, pady=(0, 18))

        def path_row(r, label, key, is_dir=False):
            ttk.Label(outer, text=label).grid(row=r, column=0, sticky=W, pady=5)
            var = StringVar(value=self.cfg.get(key, ""))
            setattr(self, f"_sv_{key}", var)
            ttk.Entry(outer, textvariable=var, width=60).grid(
                row=r, column=1, padx=(8, 4), sticky=EW)
            def browse(v=var, d=is_dir):
                p = filedialog.askdirectory() if d else filedialog.askopenfilename(
                    filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
                if p: v.set(p)
            ttk.Button(outer, text="Browse…", command=browse).grid(
                row=r, column=2, padx=(0, 2))

        path_row(1, "NHL 2k10 game files folder:",  "root_path",  is_dir=True)
        path_row(2, "Xenia executable (.exe):",        "xenia_path", is_dir=False)

        # Game path — special row: accepts .xex, .iso, or a folder
        ttk.Label(outer, text="Game (.xex, .iso, or folder):").grid(
            row=3, column=0, sticky=W, pady=5)
        self._sv_game_path = StringVar(value=self.cfg.get("game_path", ""))
        ttk.Entry(outer, textvariable=self._sv_game_path, width=60).grid(
            row=3, column=1, padx=(8, 4), sticky=EW)
        def _browse_game():
            p = filedialog.askopenfilename(
                title="Select game file or folder",
                filetypes=[
                    ("Xbox 360 executable", "*.xex"),
                    ("ISO image", "*.iso *.xiso"),
                    ("All files", "*.*"),
                ])
            if not p:
                # Fallback: ask for directory (STFS / extracted folder)
                p = filedialog.askdirectory(title="Or select game folder")
            if p:
                self._sv_game_path.set(p)
        ttk.Button(outer, text="Browse…", command=_browse_game).grid(
            row=3, column=2, padx=(0, 2))
        path_row(4, "xma2encode.exe path:",          "xma2encode", is_dir=False)
        path_row(5, "ffmpeg.exe path:",              "ffmpeg",     is_dir=False)

        outer.columnconfigure(1, weight=1)
        ttk.Separator(outer).grid(row=8, column=0, columnspan=3, sticky=EW, pady=18)

        def on_save():
            self.cfg["root_path"]         = self._sv_root_path.get().strip()
            self.cfg["xenia_path"]        = self._sv_xenia_path.get().strip()
            self.cfg["game_path"]         = self._sv_game_path.get().strip()
            self.cfg["xma2encode"]        = self._sv_xma2encode.get().strip()
            self.cfg["ffmpeg"]            = self._sv_ffmpeg.get().strip()
            save_config(self.cfg)
            self._v_root.set(self.cfg["root_path"])
            self._reload_all()
            messagebox.showinfo("Settings", "Settings saved.")

        ttk.Button(outer, text="Save Settings", style="Accent.TButton",
                   command=on_save).grid(row=9, column=1, sticky=W)

        note = ("Game files folder — the folder containing the raw 0A, 0B, 1A, 1B archives.\n"
                "Xenia executable — path to xenia_canary.exe.\n"
                "Game ISO / folder — the NHL 2K10 ISO or extracted game folder.\n"
                "xma2encode.exe  — required for Extract and Patch Audio operations.\n"
                "ffmpeg.exe      — required for Patch Audio only.")
        ttk.Label(outer, text=note, foreground="#888888",
                  font=("Segoe UI", 8)).grid(
            row=10, column=0, columnspan=3, sticky=W, pady=(14, 0))

        ttk.Separator(outer).grid(row=11, column=0, columnspan=3, sticky=EW, pady=18)
        ttk.Label(outer, text="Share & Merge (collaboration)",
                  font=("Segoe UI", 11, "bold")).grid(row=12, column=0, columnspan=3, sticky=W)
        ttk.Label(
            outer, foreground="#888888", font=("Segoe UI", 8),
            text=("Export your work to share, or import someone else's and merge it into yours. "
                  "Conflicts (same item changed both ends) are previewed so you choose which to keep.\n"
                  "• Audio Names = just naming/category/sample-rate (small, git-friendly JSON).\n"
                  "• Mod Pack = everything: audio names + replacement WAVs + replacement textures "
                  "+ roster edits (team colours / arena names / team names). Audio & textures stage "
                  "into Modified (review, then Patch); roster edits apply straight onto your "
                  "Roster.ROS so you can share them without shipping your players/ratings.")
        ).grid(row=13, column=0, columnspan=3, sticky=W, pady=(2, 8))
        share = ttk.Frame(outer); share.grid(row=14, column=0, columnspan=3, sticky=W)
        ttk.Button(share, text="Export Audio Names…", command=self._export_names).pack(side=LEFT, padx=(0, 4))
        ttk.Button(share, text="Import Audio Names…", command=self._import_names).pack(side=LEFT, padx=4)
        ttk.Separator(share, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=10)
        ttk.Button(share, text="Export Mod Pack…", style="Accent.TButton",
                   command=self._export_modpack).pack(side=LEFT, padx=4)
        ttk.Button(share, text="Import Mod Pack…", style="Accent.TButton",
                   command=self._import_modpack).pack(side=LEFT, padx=4)

    # ── Share & Merge (mod packs / audio-name files) ──────────────────────────

    def _export_names(self):
        root = self._get_root()
        if not root: return
        p = filedialog.asksaveasfilename(
            title="Export Audio Names", defaultextension=mp.NAMES_EXT,
            initialfile="my_audio_names" + mp.NAMES_EXT,
            filetypes=[("Audio names", "*" + mp.NAMES_EXT), ("JSON", "*.json")])
        if not p: return
        try:
            res = mp.export_names(root, p)
        except Exception as e:
            messagebox.showerror("Export failed", str(e)); return
        self._log(f"Exported {res['audio_meta']} audio name(s) → {Path(p).name}")
        messagebox.showinfo("Exported", f"Exported {res['audio_meta']} audio name/category/rate "
                            f"edit(s).\n\nShare this file; others Import it to merge.")

    def _import_names(self):
        root = self._get_root()
        if not root: return
        p = filedialog.askopenfilename(
            title="Import Audio Names",
            filetypes=[("Audio names", "*" + mp.NAMES_EXT), ("JSON", "*.json"), ("All", "*.*")])
        if not p: return
        try:
            _manifest, items = mp.diff_names(p, root)
        except Exception as e:
            messagebox.showerror("Import failed", f"Could not read file:\n{e}"); return
        self._merge_with_selection(items, None, "Audio Names")

    _PICK_SECT = {"meta": "name", "audio": "audio", "tex": "texture"}

    def _pick_items_dialog(self, title, subtitle, items, show_status=False):
        """Unity-style checkbox picker with Type / Team / Category filters. Returns the SELECTED
        items (default: all checked), or None if cancelled. `items` = [{section,key,label,team,
        category, status?}]. Click the ✔ column (or press Space) to toggle a row; the filters narrow
        the view; Check All / Uncheck All apply to the CURRENTLY VISIBLE rows."""
        if not items:
            return []
        dlg = Toplevel(self); dlg.title(title)
        dlg.geometry("900x600"); dlg.transient(self); dlg.grab_set()
        checked = [True] * len(items)
        result = {"ok": False}

        top = ttk.Frame(dlg, padding=(12, 10)); top.pack(fill=X)
        ttk.Label(top, text=title, font=("Segoe UI", 11, "bold")).pack(anchor=W)
        ttk.Label(top, text=subtitle, foreground="#999").pack(anchor=W)

        # ── filter bar ──
        teams = ["All"] + sorted({it.get("team", "") for it in items if it.get("team")})
        cats  = ["All"] + sorted({it.get("category", "") for it in items if it.get("category")})
        v_type = StringVar(value="All"); v_team = StringVar(value="All"); v_cat = StringVar(value="All")
        fb = ttk.Frame(dlg, padding=(12, 0, 12, 6)); fb.pack(fill=X)
        ttk.Label(fb, text="Type:").pack(side=LEFT)
        ttk.Combobox(fb, textvariable=v_type, values=["All", "Audio", "Texture", "Roster"],
                     state="readonly", width=9).pack(side=LEFT, padx=(3, 12))
        ttk.Label(fb, text="Team:").pack(side=LEFT)
        ttk.Combobox(fb, textvariable=v_team, values=teams, state="readonly",
                     width=8).pack(side=LEFT, padx=(3, 12))
        ttk.Label(fb, text="Category:").pack(side=LEFT)
        ttk.Combobox(fb, textvariable=v_cat, values=cats, state="readonly",
                     width=18).pack(side=LEFT, padx=(3, 12))
        count_lbl = ttk.Label(fb, text="", foreground="#999"); count_lbl.pack(side=RIGHT)

        # ── tree grouped by ASSET (.iff) / audio bank — textures shown as t{id} children ──
        import re as _re
        from collections import OrderedDict
        try:
            _labelmap = {archtex.asset_iff(r["iff"]): (r.get("label") or r["iff"])
                         for r in archtex.load_catalog()}
        except Exception:
            _labelmap = {}

        def _group_of(i):
            it = items[i]
            if it["section"] == "roster":
                return ("roster", "Roster")                          # league-wide field groups
            if it["section"] == "tex":
                return ("tex", str(it["key"]).split("/")[0])          # the .iff folder
            return ("aud", it.get("category") or "Audio")
        def _group_label(g):
            kind, gid = g
            if kind == "roster":
                return "Roster (applied over your Roster.ROS)"
            if kind == "tex":
                return _labelmap.get(gid, gid)                        # friendly asset name
            return f"Audio — {gid}" if gid != "Audio" else "Audio"
        def _leaf_label(i):
            it = items[i]
            if it["section"] == "tex":
                stem = Path(str(it["key"])).stem                     # t10 / cover / (legacy) t10_256x64
                return _re.sub(r"_\d+x\d+$", "", stem)               # -> just the texture id
            return it["label"]
        groups = OrderedDict()
        for i in range(len(items)):
            groups.setdefault(_group_of(i), []).append(i)

        cols = ("chk", "team", "cat") + (("status",) if show_status else ())
        body = ttk.Frame(dlg); body.pack(fill=BOTH, expand=True, padx=12)
        tv = ttk.Treeview(body, columns=cols, show="tree headings", selectmode="browse")
        tv.heading("#0", text="Asset / texture"); tv.column("#0", width=360, stretch=True)
        tv.heading("chk", text="✔");        tv.column("chk", width=34, anchor=CENTER, stretch=False)
        tv.heading("team", text="Team");    tv.column("team", width=70, anchor=CENTER, stretch=False)
        tv.heading("cat", text="Category"); tv.column("cat", width=150, anchor=W, stretch=False)
        if show_status:
            tv.heading("status", text="Status"); tv.column("status", width=80, anchor=CENTER, stretch=False)
        sb = ttk.Scrollbar(body, command=tv.yview); tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); tv.pack(fill=BOTH, expand=True)
        tv.tag_configure("new", foreground="#9ccc65")
        tv.tag_configure("conflict", foreground="#ffb74d")
        tv.tag_configure("same", foreground="#888")
        BOX = {"all": "☑", "none": "☐", "some": "◪"}
        pmap = {}                                        # parent-row iid -> group key

        def matches(i):
            it = items[i]; t = v_type.get()
            if t == "Audio" and it["section"] not in ("meta", "audio"): return False
            if t == "Texture" and it["section"] != "tex": return False
            if t == "Roster" and it["section"] != "roster": return False
            if v_team.get() != "All" and it.get("team", "") != v_team.get(): return False
            if v_cat.get() != "All" and it.get("category", "") != v_cat.get(): return False
            return True
        def visible():
            return [i for i in range(len(items)) if matches(i)]
        def _pstate(g):
            cs = [checked[i] for i in groups[g] if matches(i)]
            return None if not cs else ("all" if all(cs) else "none" if not any(cs) else "some")
            
        def refresh(*_):
            # 1. Save which category IDs (pmap values) were currently expanded before wiping
            expanded_groups = {
                pmap[child] for child in tv.get_children() 
                if child in pmap and tv.item(child, "open")
            }

            tv.delete(*tv.get_children()); pmap.clear()
            for g, idxs in groups.items():
                vis = [i for i in idxs if matches(i)]
                if not vis:
                    continue
                st = _pstate(g)
                pid = "G%d" % len(pmap); pmap[pid] = g
                
                # 2. Keep it open if it was previously open (defaults to True on initial load)
                is_open = pid not in pmap or g in expanded_groups if expanded_groups or len(pmap) == 1 else True
                # Alternatively, use: is_open = (g in expanded_groups) if expanded_groups else True

                tv.insert("", END, iid=pid, open=(g in expanded_groups) if expanded_groups else True,
                          text=f"{_group_label(g)}   ({len(vis)})",
                          values=(BOX[st], "", "") + (("",) if show_status else ()))
                for i in vis:
                    it = items[i]
                    vals = (BOX["all" if checked[i] else "none"], it.get("team", ""),
                            it.get("category", "")) + ((it.get("status", ""),) if show_status else ())
                    tv.insert(pid, END, iid=str(i), text="    " + _leaf_label(i), values=vals,
                              tags=(it.get("status", ""),) if show_status else ())
            count_lbl.config(text=f"{sum(checked)} of {len(items)} checked")
            
        for v in (v_type, v_team, v_cat):
            v.trace_add("write", refresh)

        def toggle_leaf(i):
            checked[i] = not checked[i]; refresh()
        def toggle_group(g):
            vis = [i for i in groups[g] if matches(i)]
            target = not all(checked[i] for i in vis)    # all on -> turn all off, else turn all on
            for i in vis:
                checked[i] = target
            refresh()
        def _toggle_row(row):
            if row in pmap:
                toggle_group(pmap[row])
            elif row.isdigit():
                toggle_leaf(int(row))
        def on_click(ev):
            if tv.identify_region(ev.x, ev.y) == "cell" and tv.identify_column(ev.x) == "#1":
                row = tv.identify_row(ev.y)
                if row:
                    _toggle_row(row); return "break"
        def on_space(_ev):
            sel = tv.selection()
            if sel:
                _toggle_row(sel[0])
        tv.bind("<Button-1>", on_click)
        tv.bind("<space>", on_space)
        def set_all(val):                                # applies to VISIBLE leaves only
            for i in visible():
                checked[i] = val
            refresh()

        btn = ttk.Frame(dlg, padding=(12, 10)); btn.pack(fill=X, side=BOTTOM)
        ttk.Button(btn, text="Check All", command=lambda: set_all(True)).pack(side=LEFT)
        ttk.Button(btn, text="Uncheck All", command=lambda: set_all(False)).pack(side=LEFT, padx=4)
        ttk.Label(btn, text="(Check/Uncheck All affects the filtered view)",
                  foreground="#777").pack(side=LEFT, padx=8)
        def ok(): result["ok"] = True; dlg.destroy()
        ttk.Button(btn, text="OK", style="Accent.TButton", command=ok).pack(side=RIGHT)
        ttk.Button(btn, text="Cancel", command=dlg.destroy).pack(side=RIGHT, padx=4)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        refresh()
        self.wait_window(dlg)
        if not result["ok"]:
            return None
        return [items[i] for i in range(len(items)) if checked[i]]

    def _export_modpack(self):
        root = self._get_root()
        if not root: return
        try:
            items = mp.local_items(root)
        except Exception as e:
            messagebox.showerror("Export", f"Could not scan modified files:\n{e}"); return
        ros_path = self._current_roster_path()
        if ros_path:
            try:
                items += mp.local_roster_items(ros_path)     # Team Colours / Arena / Team Names
            except Exception as e:
                self._log(f"[modpack] roster scan skipped: {e}")
        if not items:
            messagebox.showinfo("Export Mod Pack",
                "No modified files to export.\n\nEdit some textures/audio (or set a Roster.ROS on the "
                "Teams tab for roster edits) first, then try again."); return
        sel = self._pick_items_dialog(
            "Export Mod Pack", "Choose the items to include (all checked by default):", items)
        if sel is None:
            return
        if not sel:
            messagebox.showinfo("Export Mod Pack", "Nothing selected — nothing to export."); return
        p = filedialog.asksaveasfilename(
            title="Export Mod Pack", defaultextension=mp.PACK_EXT,
            initialfile="my_modpack" + mp.PACK_EXT,
            filetypes=[("Mod Pack", "*" + mp.PACK_EXT), ("Zip", "*.zip")])
        if not p: return
        keys = {(it["section"], it["key"]) for it in sel}
        self._log(f"─── Export Mod Pack ({len(sel)} item(s)) ───")
        def work():
            try:
                res = mp.export_selected(root, p, keys, ros_path=ros_path)
                self._log_q.put(f"Mod Pack: {res['audio_meta']} names, {res['audio_wav']} "
                                f"audio, {res['textures']} texture(s), {res['roster']} roster "
                                f"group(s) → {Path(p).name}")
            except Exception as e:
                self._log_q.put(f"Export failed: {e}")
        self._run_in_thread(work, op_label="Building Mod Pack…")

    def _import_modpack(self):
        root = self._get_root()
        if not root: return
        p = filedialog.askopenfilename(
            title="Import Mod Pack",
            filetypes=[("Mod Pack", "*" + mp.PACK_EXT), ("Zip", "*.zip"), ("All", "*.*")])
        if not p: return
        self._log(f"─── Import Mod Pack: {Path(p).name} ───")
        ros_path = self._current_roster_path()
        def work():
            try:
                _manifest, items = mp.diff_pack(p, root, ros_path=ros_path)
                self._pending_import = (items, p)
            except Exception as e:
                self._log_q.put(f"Import failed: {e}"); self._pending_import = None
        def done():
            pend = getattr(self, "_pending_import", None); self._pending_import = None
            if pend:
                self._merge_with_selection(pend[0], pend[1], "Mod Pack")
        self._run_in_thread(work, op_label="Reading Mod Pack…", on_done=done)

    def _merge_with_selection(self, items, zip_path, what):
        """Show every incoming item with a checkbox (all checked by default), then apply the checked
        ones. A checked CONFLICT takes the incoming ('theirs') copy; 'same' items are no-ops."""
        if not items:
            messagebox.showinfo("Nothing to import", "This file has no changes for your setup."); return
        for it in items:
            it.setdefault("status", "new")
        try:
            mp.annotate(items, self._get_root())      # add team/category for the filter bar
        except Exception:
            pass
        sel = self._pick_items_dialog(
            f"Import {what}", "Choose the incoming files to import (all checked by default; "
            "checking a 'conflict' overwrites yours):", items, show_status=True)
        if sel is None:
            self._log("Import cancelled."); return
        if not sel:
            messagebox.showinfo("Import", "Nothing selected — nothing imported."); return
        root = self._get_root()
        if not root: return
        decisions = {f'{it["section"]}|{it["key"]}': "theirs" for it in sel if it["status"] == "conflict"}
        try:
            counts = mp.apply_items(root, sel, decisions, zip_path=zip_path,
                                    ros_path=self._current_roster_path(), log=self._log)
        except Exception as e:
            messagebox.showerror("Import failed", str(e)); return
        self._reload_audio()
        if getattr(self, "_bank_records", None): self._bank_populate()
        self._log(f"Imported {what}: +{counts['meta']} names, +{counts['audio']} audio, "
                  f"+{counts['tex']} textures, +{counts['roster']} roster group(s)")
        roster_note = ("\n\nRoster edits were written straight to your Roster.ROS (backups made) — "
                       "restart the game to see them." if counts["roster"] else "")
        messagebox.showinfo("Import complete",
            f"Names: {counts['meta']}   Audio: {counts['audio']}   Textures: {counts['tex']}   "
            f"Roster: {counts['roster']}\n\n"
            "Replacements are staged in your Extracted folder — review, then Apply All Mods."
            + roster_note)
        if counts["roster"] and Path(self._v_roster.get().strip() or "x").is_file():
            try: self._teams_load()          # refresh the Teams grid from the patched save
            except Exception: pass

    def _run_merge(self, items, zip_path, what):
        """Auto-apply NEW items, resolve CONFLICTs via a dialog, then apply. Shared by both
        the Audio-Names and Mod-Pack import paths."""
        new   = [it for it in items if it["status"] == "new"]
        same  = [it for it in items if it["status"] == "same"]
        confl = [it for it in items if it["status"] == "conflict"]
        if not items:
            messagebox.showinfo("Nothing to import", "This file has no changes for your setup.")
            return
        decisions: dict = {}
        if confl:
            decisions = self._resolve_conflicts_dialog(confl, zip_path)
            if decisions is None:
                self._log("Import cancelled."); return
        root = self._get_root()
        if not root: return
        try:
            counts = mp.apply_items(root, items, decisions, zip_path=zip_path,
                                    ros_path=self._current_roster_path(), log=self._log)
        except Exception as e:
            messagebox.showerror("Merge failed", str(e)); return
        kept_theirs = sum(1 for v in decisions.values() if v == "theirs")
        self._reload_audio()
        if getattr(self, "_bank_records", None): self._bank_populate()
        self._log(f"Merged {what}: +{counts['meta']} names, +{counts['audio']} audio, "
                  f"+{counts['tex']} textures  (kept theirs on {kept_theirs} conflict(s))")
        messagebox.showinfo("Merge complete",
            f"Imported {len(new)} new, kept {len(same)} identical, resolved {len(confl)} "
            f"conflict(s).\n\nNames: {counts['meta']}   Audio: {counts['audio']}   "
            f"Textures: {counts['tex']}\n\nReplacements are staged in your Extracted folder — "
            f"review, then Apply to IFF (or Apply All Mods).")

    def _resolve_conflicts_dialog(self, conflicts, zip_path):
        """Modal conflict resolver. Returns {('section|key'): 'mine'|'theirs'} or None if
        cancelled. Shows X of Y, a per-item Mine/Theirs choice, and a preview."""
        dlg = Toplevel(self); dlg.title("Resolve Conflicts")
        dlg.geometry("860x520"); dlg.transient(self); dlg.grab_set()
        choice: dict = {f'{it["section"]}|{it["key"]}': StringVar(value="mine") for it in conflicts}
        result = {"ok": False}
        top = ttk.Frame(dlg, padding=(10, 8)); top.pack(fill=X)
        hdr = ttk.Label(top, text=f"{len(conflicts)} conflict(s) to resolve",
                        font=("Segoe UI", 10, "bold")); hdr.pack(side=LEFT)
        def _set_all(val):
            for v in choice.values(): v.set(val)
            _refresh()
        ttk.Button(top, text="Keep ALL mine",
                   command=lambda: _set_all("mine")).pack(side=RIGHT, padx=2)
        ttk.Button(top, text="Keep ALL theirs",
                   command=lambda: _set_all("theirs")).pack(side=RIGHT, padx=2)

        body = ttk.Frame(dlg); body.pack(fill=BOTH, expand=True, padx=10)
        lst = ttk.Treeview(body, columns=("type", "item", "keep"), show="headings",
                           selectmode="browse", height=10)
        for c, t, w in (("type", "Type", 70), ("item", "Item", 360), ("keep", "Keeping", 90)):
            lst.heading(c, text=t); lst.column(c, width=w, anchor=W)
        lst.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(body, command=lst.yview); lst.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky="ns")
        prev = ttk.LabelFrame(body, text="Preview", padding=8)
        prev.grid(row=0, column=2, sticky="nsew", padx=(10, 0))
        body.rowconfigure(0, weight=1); body.columnconfigure(0, weight=2); body.columnconfigure(2, weight=3)

        SECT = {"meta": "name", "audio": "audio", "tex": "texture"}
        def _refresh():
            for i, it in enumerate(conflicts):
                k = f'{it["section"]}|{it["key"]}'
                lst.item(lst.get_children()[i],
                         values=(SECT.get(it["section"], it["section"]), it["label"],
                                 "THEIRS" if choice[k].get() == "theirs" else "mine"))
        for it in conflicts:
            lst.insert("", END, values=(SECT.get(it["section"], it["section"]), it["label"], "mine"))

        self._cf_tmp = []        # temp preview files to clean up
        def _show_preview(it):
            for w in prev.winfo_children(): w.destroy()
            k = f'{it["section"]}|{it["key"]}'
            sel = choice[k]
            rb = ttk.Frame(prev); rb.pack(anchor=W, pady=(0, 6))
            ttk.Radiobutton(rb, text="Keep mine", variable=sel, value="mine",
                            command=_refresh).pack(side=LEFT)
            ttk.Radiobutton(rb, text="Keep theirs", variable=sel, value="theirs",
                            command=_refresh).pack(side=LEFT, padx=8)
            if it["section"] == "meta":
                def fmt(d):
                    if not d: return "(none)"
                    return "  ".join(f"{kk}={vv}" for kk, vv in d.items())
                ttk.Label(prev, text="MINE:",  font=("Segoe UI", 8, "bold")).pack(anchor=W)
                ttk.Label(prev, text=fmt(it.get("local")), foreground="#4fc3f7",
                          wraplength=380, justify=LEFT).pack(anchor=W)
                ttk.Label(prev, text="THEIRS:", font=("Segoe UI", 8, "bold")).pack(anchor=W, pady=(6, 0))
                ttk.Label(prev, text=fmt(it.get("incoming")), foreground="#81c784",
                          wraplength=380, justify=LEFT).pack(anchor=W)
            elif it["section"] == "audio":
                ttk.Label(prev, text=it["label"]).pack(anchor=W)
                def play(which):
                    try:
                        if which == "mine" and it.get("local"):
                            wav = it["local"]
                        else:
                            tmp = Path(tempfile.mkdtemp(prefix="cf_")) / "theirs.wav"
                            mp.extract_member(it["zip"], it["arc"], tmp); self._cf_tmp.append(tmp)
                            wav = str(tmp)
                        winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
                    except Exception as e:
                        messagebox.showerror("Playback", str(e), parent=dlg)
                pf = ttk.Frame(prev); pf.pack(anchor=W, pady=6)
                ttk.Button(pf, text="▶ Play mine",
                           command=lambda: play("mine"),
                           state=NORMAL if it.get("local") else DISABLED).pack(side=LEFT, padx=2)
                ttk.Button(pf, text="▶ Play theirs", command=lambda: play("theirs")).pack(side=LEFT, padx=2)
            elif it["section"] == "tex":
                cv = ttk.Frame(prev); cv.pack(fill=BOTH, expand=True)
                def thumb(parent, title, getp, color):
                    ttk.Label(parent, text=title, font=("Segoe UI", 8, "bold"),
                              foreground=color).pack()
                    img = None
                    try:
                        if _PIL:
                            p2 = getp()
                            if p2:
                                im = Image.open(p2); im.thumbnail((170, 170)); img = ImageTk.PhotoImage(im)
                    except Exception:
                        img = None
                    if img:
                        lb = Label(parent, image=img); lb.image = img; lb.pack()
                    else:
                        ttk.Label(parent, text="(no preview)", foreground="#888").pack()
                def their_path():
                    tmp = Path(tempfile.mkdtemp(prefix="cf_")) / Path(it["key"]).name
                    mp.extract_member(it["zip"], it["arc"], tmp); self._cf_tmp.append(tmp)
                    return tmp
                left = ttk.Frame(cv); left.pack(side=LEFT, expand=True)
                right = ttk.Frame(cv); right.pack(side=LEFT, expand=True)
                thumb(left,  "MINE",   lambda: it.get("local"), "#4fc3f7")
                thumb(right, "THEIRS", their_path,              "#81c784")
        def _on_sel(_=None):
            s = lst.selection()
            if s: _show_preview(conflicts[lst.index(s[0])])
        lst.bind("<<TreeviewSelect>>", _on_sel)
        if conflicts:
            lst.selection_set(lst.get_children()[0])

        btm = ttk.Frame(dlg, padding=(10, 8)); btm.pack(fill=X)
        def _ok():  result["ok"] = True; dlg.destroy()
        def _cancel(): dlg.destroy()
        ttk.Button(btm, text="Apply", style="Accent.TButton", command=_ok).pack(side=RIGHT, padx=2)
        ttk.Button(btm, text="Cancel", command=_cancel).pack(side=RIGHT, padx=2)
        dlg.protocol("WM_DELETE_WINDOW", _cancel)
        self.wait_window(dlg)
        for t in self._cf_tmp:
            try: Path(t).unlink()
            except Exception: pass
        self._cf_tmp = []
        if not result["ok"]:
            return None
        return {k: v.get() for k, v in choice.items()}

    # ── Root / data reload ────────────────────────────────────────────────────

    def _get_root(self) -> Path | None:
        r = self._v_root.get().strip() or self.cfg.get("root_path", "")
        if not r:
            messagebox.showwarning("No Game Folder",
                "Set the NHL 2k10 game files folder in Settings first.")
            return None
        p = Path(r)
        if not p.is_dir():
            messagebox.showwarning("Invalid Path", f"Path does not exist:\n{p}")
            return None
        ex = p / "NHL2k10_Extracted_Files"
        ex.mkdir(parents=True, exist_ok=True)
        return ex

    def _current_roster_path(self) -> str:
        """The Roster.ROS a mod pack should read/write, or "" if none is available.
        Prefers the Teams-tab field, then the saved config, then auto-discovery."""
        p = ""
        try:
            p = self._v_roster.get().strip()
        except Exception:
            p = ""
        p = p or self.cfg.get("roster_path", "") or self._discover_roster()
        return p if p and Path(p).is_file() else ""

    def _get_game_root(self) -> Path | None:
        r = self.cfg.get("root_path", "")
        if not r: return None
        p = Path(r)
        return p if p.is_dir() else None

    def _get_xma2encode(self) -> str | None:
        p = self.cfg.get("xma2encode", "")
        if not p or not Path(p).exists():
            messagebox.showerror("Missing Tool",
                "xma2encode.exe not found. Configure it in Settings.")
            return None
        return p

    def _get_tools(self) -> tuple | None:
        xma = self._get_xma2encode()
        if not xma: return None
        ffm = self.cfg.get("ffmpeg", "")
        if not ffm or not Path(ffm).exists():
            messagebox.showerror("Missing Tool",
                "ffmpeg.exe not found. Configure it in Settings.")
            return None
        return xma, ffm
    def _op_busy(self) -> bool:
        if self._op_thread and self._op_thread.is_alive():
            messagebox.showwarning("Busy", "An operation is already running.")
            return True
        return False

    def _apply_game_paths(self):
        """Point archive_textures at the configured game-files folder.

        This is the ONLY folder the app needs: assets are read from it, and pristine bytes come
        from the <arc>.orig backups the launcher itself writes before first modifying an archive
        (there is no separate 'clean files' copy any more — it was a byte-identical 5GB duplicate).
        Also repoints the live-capture catalog, whose default resolves inside the PyInstaller
        bundle when frozen (so global.iff & other loader-repacked packs would list no sub-textures).
        """
        r = (self._v_root.get().strip() or self.cfg.get("root_path", "")).strip()
        if r and Path(r).is_dir():
            archtex.set_game_dir(r)
            archtex.LIVE_CATALOG = Path(r) / "live_capture" / "live_offsets.json"
            archtex.reload_live_catalog()
            try:
                from launcher import live_capture
                live_capture.set_out_root(r)          # captures land beside the game files
            except Exception:
                pass

    def _reload_all(self):
        self._apply_game_paths()
        self._reload_audio()
        self._iff_load_catalog()

    def _get_tools_quiet(self):
        """(xma2encode, ffmpeg) if both are configured and exist, else None — no error dialog."""
        xma = self.cfg.get("xma2encode", ""); ffm = self.cfg.get("ffmpeg", "")
        if xma and ffm and Path(xma).exists() and Path(ffm).exists():
            return xma, ffm
        return None

    def _apply_all_mods(self):
        """One-click: apply EVERY modified file — all texture edits (across all iffs) + all
        replacement audio (WAV re-import) — into the game archives in one pass."""
        if self._op_busy():
            return
        root = self._get_root()
        if not root:
            return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Game folder",
                "Set the game files folder (with 0A/0B/1A/1B) in Settings."); return
        # gather every asset that has an EDITED file (changed since extract) in Extracted/ (or a
        # legacy Modified//Original/ file) — untouched extractions are skipped so we never re-encode
        # pristine textures back into the game.
        tex_jobs = []
        prim_jobs = []                       # single-primary assets (logos, masks, cover art)
        # SPEED: only look at assets that actually have an edited file on disk. The old scan called
        # list_textures() — a full pack decompress (up to ~13 MB for a rink) — on EVERY catalog asset
        # just to hunt for edits, so the dialog took minutes to appear. folders_with_edits() walks the
        # Extracted/ tree once (hash-manifest compare) and returns only the touched folders.
        try:
            # folders_with_edits returns ABSOLUTE Paths; asset_iff() returns folder strings
            # relative to Textures/Extracted (or Modified/) — normalize before comparing, else
            # the filter never matches and Apply All reports "Nothing to apply".
            edited_folders = set()
            for _p in archtex.folders_with_edits(root):
                for _base in (root / "Textures" / "Extracted", root / "Textures" / "Modified"):
                    try:
                        edited_folders.add(_p.relative_to(_base).as_posix())
                    except ValueError:
                        pass
        except Exception as e:
            self._log(f"[apply-all] edit scan fell back to full scan ({e})"); edited_folders = None
        for iff in sorted({r["iff"] for r in self._iff_catalog}):
            if edited_folders is not None and not (
                    archtex.asset_iff(iff) in edited_folders
                    or archtex._legacy_asset_iff(iff) in edited_folders):
                continue                      # nothing edited for this asset -> skip the decompress
            try:
                recs = archtex.list_textures(iff)
            except Exception:
                recs = []
            edits = []
            for rec in (recs or []):
                p = archtex.find_any_edit(root, iff, rec)
                if p and rec["fmt"] in archtex.REPLACE_FORMATS and archtex.is_edited(root, p):
                    edits.append({**rec, "path": str(p)})
            if edits:
                tex_jobs.append((iff, edits))
            elif not recs:
                p = archtex.find_any_edit(root, iff, None)
                if p and archtex.is_edited(root, p):
                    prim_jobs.append((iff, str(p)))
        tools = self._get_tools_quiet()
        n_tex = sum(len(e) for _, e in tex_jobs) + len(prim_jobs)
        if not tex_jobs and not prim_jobs and not tools:
            messagebox.showinfo("Apply All Mods",
                "Nothing to apply.\n\nEdit some textures in the IFF Textures tab and/or add "
                "replacement audio, then try again."); return
        scope = self._apply_scope_dialog(n_tex, len(tex_jobs) + len(prim_jobs),
                                         tools is not None, game_dir)
        if scope is None:
            return
        do_tex, do_audio = scope
        if do_tex and not (tex_jobs or prim_jobs):
            do_tex = False
        if not do_tex and not (do_audio and tools):
            return
        if not do_tex:
            tex_jobs = []; prim_jobs = []
        if not do_audio:
            tools = None
        self._log("─── Apply ALL Mods → game files (PERMANENT) ───")
        total = len(tex_jobs) + len(prim_jobs) + (1 if tools else 0) + 1   # +1 = compact step
        def work():
            step = 0
            for iff, edits in tex_jobs:
                self._emit_progress(step, total, f"{iff}  ({len(edits)} texture(s))")
                try:
                    archtex.ensure_clean(iff, game_dir, self._log_q.put)   # reset -> apply fresh
                    status = archtex.replace_many(iff, edits, game_dir, self._log_q.put,
                                                  prefer_lossless=True)
                    self._log_q.put(f"  {iff}: {status}")
                    # keep a jersey's front-end team-select copy in sync (see _iff_mirror_jersey)
                    self._iff_mirror_jersey(iff, [(e["index"], e["path"]) for e in edits], game_dir)
                except Exception as e:
                    self._log_q.put(f"  ERROR {iff}: {e}")
                step += 1
            for iff, p in prim_jobs:                    # single-primary assets (logos, masks, …)
                self._emit_progress(step, total, f"{iff}")
                try:
                    archtex.ensure_clean(iff, game_dir, self._log_q.put)
                    status = archtex.replace(iff, p, game_dir, self._log_q.put)
                    self._log_q.put(f"  {iff}: {status}")
                    self._iff_mirror_jersey(iff, [(0, p)], game_dir)
                except Exception as e:
                    self._log_q.put(f"  ERROR {iff}: {e}")
                step += 1
            self._emit_progress(step, total, "compacting archives")
            try:                                             # reclaim orphans from the reset+apply
                self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
            except Exception as ce:
                self._log_q.put(f"  (compact skipped: {ce})")
            step += 1
            if tools:
                self._emit_progress(step, total, "re-importing audio")
                xma, ffm = tools
                try:
                    patched, skipped, nf = op_reimport(root, ffm, xma, False, self._log_q.put)
                    self._log_q.put(f"  audio: {patched} patched, {skipped} skipped, {nf} not catalogued")
                except Exception as e:
                    self._log_q.put(f"  audio ERROR: {e}")
                step += 1
            self._emit_progress(total, total, "done")
            self._log_q.put("─── Apply All done. Restart the game to see changes. ───")
        self._run_in_thread(work, op_label="Applying all mods…",
                            on_done=lambda: (self._reload_all(), self._prompt_restart_if_running()))

    def _apply_scope_dialog(self, n_tex, n_iffs, audio_ok, game_dir):
        """Modal 'what to apply' picker for Apply All: checkboxes for Textures / Audio.
        Returns (do_textures, do_audio) or None if cancelled."""
        dlg = Toplevel(self)
        dlg.title("Apply All Mods")
        dlg.transient(self); dlg.grab_set(); dlg.resizable(False, False)
        frm = ttk.Frame(dlg, padding=14); frm.pack(fill=BOTH, expand=True)
        ttk.Label(frm, text="Permanently apply to the game archives:",
                  font=("Segoe UI", 10, "bold")).pack(anchor=W)
        v_tex = BooleanVar(value=n_tex > 0)
        v_aud = BooleanVar(value=audio_ok)
        cb1 = ttk.Checkbutton(frm, variable=v_tex,
                              text=f"Textures — {n_tex} edit(s) across {n_iffs} asset(s)"
                                   if n_tex else "Textures — none found in Modified/")
        cb2 = ttk.Checkbutton(frm, variable=v_aud,
                              text="Audio — all replacement WAVs" if audio_ok
                                   else "Audio — unavailable (set ffmpeg + xma2encode in Settings)")
        cb1.pack(anchor=W, pady=(8, 0)); cb2.pack(anchor=W, pady=(2, 0))
        if not n_tex:
            cb1.state(["disabled"])
        if not audio_ok:
            cb2.state(["disabled"]); v_aud.set(False)
        ttk.Label(frm, text=f"Into: {game_dir}\n(one-time .orig/.bak backups are made)",
                  foreground="#888").pack(anchor=W, pady=(10, 0))
        out = {"r": None}
        bb = ttk.Frame(frm); bb.pack(anchor=E, pady=(12, 0))
        def ok():
            if not (v_tex.get() or v_aud.get()):
                messagebox.showinfo("Apply All Mods", "Pick at least one category (or Cancel).",
                                    parent=dlg); return
            out["r"] = (v_tex.get(), v_aud.get()); dlg.destroy()
        ttk.Button(bb, text="Apply", style="Accent.TButton", command=ok).pack(side=LEFT)
        ttk.Button(bb, text="Cancel", command=dlg.destroy).pack(side=LEFT, padx=(8, 0))
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        self.wait_window(dlg)
        return out["r"]

    # ── Launch + Apply Mods ───────────────────────────────────────────────────

    def _launch_and_apply(self):
        #1) Verify that our paths exist so we can launch the game
        xenia_path = self.cfg.get("xenia_path", "").strip()
        game_path  = self.cfg.get("game_path",  "").strip()
        xenia_root_path = os.path.dirname(xenia_path)

        if not xenia_root_path or not Path(xenia_root_path).exists():
            messagebox.showerror("Xenia not configured",
                "Set the Xenia executable path in Settings first.")
            self._nb.select(self._tab_settings); return
        if not game_path or not Path(game_path).exists():
            messagebox.showerror("Game not configured",
                "Set the game .xex / ISO / folder path in Settings first.")
            self._nb.select(self._tab_settings); return

        #2) Run the resolution check dialog
        # (self.settings stores launcher configs, where 'never_check_xenia_res' will be saved)
        settings_dict = getattr(self, "settings", None)
        if settings_dict is None:
            settings_dict = getattr(self, "cfg", {})

        if not check_and_update_xenia_resolution(xenia_root_path, settings_dict, parent=self):
            messagebox.showerror("Xenia not configured",
                "Set the Xenia executable path in Settings first.")
            return

        #3) Save launcher settings if "Never" was clicked
        if hasattr(self, "save_settings"):
            self.save_settings()

        #4) Proceed to launch the game
        self._log("─── Launch NHL 2k10 ───")
        self._log(f"Game: {Path(game_path).name}")
        try:
            subprocess.Popen([xenia_path, game_path])
            if self.cfg.get("goalie_masks"):
                self._log("[goalie] will auto-apply saved mask assignments once the roster loads…")
                self.after(60000, self._goalie_apply_saved_async)   # give the game time to boot
                self.after(60000, self._portrait_apply_saved_async)
        except Exception as e:
            messagebox.showerror("Launch failed", str(e))

    # ── "restart to see changes" prompt (only when the game is live in Xenia) ────
    def _xenia_game_pids(self):
        """PIDs of XENIA processes whose window shows NHL 2K10 is running (title-id 54540853),
        or [] if the game isn't running. Requires the owning process to be xenia*.exe so folder /
        terminal windows that merely contain 'NHL 2k10' in their title never match."""
        import ctypes, os
        from ctypes import wintypes as wt
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except Exception:
            return []
        def exe_of(pid):
            h = k32.OpenProcess(0x1000, False, pid)           # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            buf = ctypes.create_unicode_buffer(260); sz = wt.DWORD(260)
            try:
                k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz))
            finally:
                k32.CloseHandle(h)
            return os.path.basename(buf.value or "")
        pids = set()
        WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def cb(hwnd, _lparam):
            ln = user32.GetWindowTextLengthW(hwnd)
            if ln:
                buf = ctypes.create_unicode_buffer(ln + 1)
                user32.GetWindowTextW(hwnd, buf, ln + 1)
                t = (buf.value or "")
                if "54540853" in t or "NHL 2K10" in t.upper():      # game title-id / name …
                    pid = wt.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value and exe_of(pid.value).lower().startswith("xenia"):   # … in a Xenia window
                        pids.add(pid.value)
            return True
        try:
            user32.EnumWindows(WNDENUMPROC(cb), 0)
        except Exception:
            return []
        return list(pids)

    def _prompt_restart_if_running(self):
        """Called at the END of an apply: if NHL 2K10 is live in Xenia, remind that a restart is
        needed (with a one-click Relaunch). No-op when the game isn't running."""
        pids = self._xenia_game_pids()
        if not pids:
            return
        dlg = Toplevel(self); dlg.title("Restart Required")
        dlg.transient(self); dlg.resizable(False, False); dlg.grab_set()
        frm = ttk.Frame(dlg, padding=16); frm.pack(fill=BOTH, expand=True)
        ttk.Label(frm, text="Changes applied", font=("Segoe UI", 11, "bold")).pack(anchor=W)
        ttk.Label(frm, justify=LEFT, text=(
            "NHL 2K10 is currently running in Xenia.\n"
            "Restart the game for your changes to take effect.")).pack(anchor=W, pady=(6, 14))
        btns = ttk.Frame(frm); btns.pack(fill=X)
        ttk.Button(btns, text="Relaunch Game", style="Accent.TButton",
                   command=lambda: (dlg.destroy(), self._relaunch_game(pids))).pack(side=RIGHT)
        ttk.Button(btns, text="Not now", command=dlg.destroy).pack(side=RIGHT, padx=6)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.update_idletasks()
        try:                                                  # center over the main window
            x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
            y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
            dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        except Exception:
            pass

    def _relaunch_game(self, pids):
        """Terminate the running Xenia game process(es), then relaunch via the configured paths."""
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for pid in pids:
            h = k32.OpenProcess(0x0001, False, int(pid))     # PROCESS_TERMINATE
            if h:
                k32.TerminateProcess(h, 0); k32.CloseHandle(h)
        self._log("─── Relaunch: closed Xenia, restarting the game… ───")
        self.after(1200, self._launch_and_apply)             # reuses config checks + goalie re-apply

    def _browse_root(self):
        p = filedialog.askdirectory(
            title="Select NHL 2k10 game files folder (containing 0A, 0B, 1A, 1B)")
        if p:
            self._v_root.set(p)
            self.cfg["root_path"] = p
            save_config(self.cfg)
            if hasattr(self, "_sv_root_path"):
                self._sv_root_path.set(p)
            self._reload_all()

    # ── Audio data ────────────────────────────────────────────────────────────

    def _reload_audio(self):
        root = self._get_root()
        if not root: return
        self.audio_rows = load_all_audio(root)
        for (fid, oh), ch in self._pending_name_changes.items():
            for r in self.audio_rows:
                r_oh = r["off_hex"] or (f"0x{r['offset']:08X}" if r.get("offset") else "")
                if r["file_id"] == fid and r_oh == oh:
                    if "name" in ch:     r["name"] = ch["name"]
                    if "category" in ch: r["category"] = ch["category"]
                    break
        for (fid, oh), rc in self._pending_rate_changes.items():
            for r in self.audio_rows:
                r_oh = r["off_hex"] or (f"0x{r['offset']:08X}" if r.get("offset") else "")
                if r["file_id"] == fid and r_oh == oh:
                    r["sample_rate"] = rc["new_rate"]
                    break
        folders = sorted({r["folder"] for r in self.audio_rows if r["folder"]})
        labels  = ["All"] + [CATEGORY_LABELS.get(f, f) for f in folders]
        self._cat_cb["values"] = labels
        if self._v_cat.get() not in labels:
            self._v_cat.set("All")
        self._apply_audio_filter()
        self._log(f"Loaded {len(self.audio_rows)} audio tracks")
        self._v_status.set(f"{len(self.audio_rows)} tracks loaded")

    def _apply_audio_filter(self):
        cat_label = self._v_cat.get()
        team      = self._v_audio_team.get()
        search    = self._v_search.get().lower().strip()
        cat_key   = None
        if cat_label != "All":
            for k, v in CATEGORY_LABELS.items():
                if v == cat_label: cat_key = k; break
        self.filtered = [
            r for r in self.audio_rows
            if (not cat_key or r["folder"] == cat_key)
            and (team == "Any" or team.lower() in r["name"].lower()
                 or team.lower() in r.get("banks_hay", ""))
            and (not search or search in r["name"].lower()
                 or search in r.get("banks_hay", "")
                 or search in r.get("banks", "").lower())
        ]
        self._populate_audio_tree()

    def _populate_audio_tree(self):
        self._a_tree.delete(*self._a_tree.get_children())
        for row in self.filtered:
            dur   = f"{row['duration']:.1f}s" if row["duration"] else "—"
            ch    = "Mono" if row["channels"] == 1 else "Stereo"
            mod   = "✓" if row["has_mod"] else ""
            label = CATEGORY_LABELS.get(row["folder"], row["folder"])
            sr    = row.get("sample_rate", 0)
            oh    = row["off_hex"] or (f"0x{row['offset']:08X}" if row.get("offset") else "")
            rate_pending = (row["file_id"], oh) in self._pending_rate_changes
            rate  = (f"{sr // 1000} kHz" + ("*" if rate_pending else "")) if sr else "—"
            is_pending = (row["file_id"], oh) in self._pending_name_changes or rate_pending
            if is_pending:
                tags = ("pending",)
            elif row["has_mod"]:
                tags = ("modified",)
            elif not row["wav_path"]:
                tags = ("missing",)
            else:
                tags = ()
            self._a_tree.insert("", END,
                values=(row["name"], label, row.get("banks", ""), dur, rate,
                        row["source"], ch, mod),
                tags=tags)
        n_mod = sum(1 for r in self.filtered if r["has_mod"])
        self._v_status.set(
            f"Showing {len(self.filtered)} of {len(self.audio_rows)} tracks"
            + (f"  |  {n_mod} modified" if n_mod else ""))

    _a_sort_state: dict = {}

    def _sort_audio(self, col: str):
        rev = self._a_sort_state.get(col, False)
        keys = {
            "name":     lambda r: r["name"].lower(),
            "category": lambda r: r["folder"].lower(),
            "banks":    lambda r: (r.get("banks", "") == "", r.get("banks", "").lower()),
            "duration": lambda r: r["duration"],
            "rate":     lambda r: r.get("sample_rate", 0),
            "source":   lambda r: r["source"],
            "ch":       lambda r: r["channels"],
            "modified": lambda r: int(r["has_mod"]),
        }
        self.filtered.sort(key=keys[col], reverse=rev)
        self._a_sort_state[col] = not rev
        self._populate_audio_tree()

    def _selected_audio_row(self) -> dict | None:
        sel = self._a_tree.selection()
        if not sel: return None
        idx = self._a_tree.index(sel[0])
        return self.filtered[idx] if idx < len(self.filtered) else None

    def _selected_audio_rows(self) -> list:
        rows = []
        for iid in self._a_tree.selection():
            idx = self._a_tree.index(iid)
            if idx < len(self.filtered):
                rows.append(self.filtered[idx])
        return rows

    def _on_audio_select(self, _=None):
        row = self._selected_audio_row()
        if not row:
            self._btn_play.config(state=DISABLED)
            self._btn_replace.config(state=DISABLED)
            self._btn_showfile.config(state=DISABLED)
            self._lbl_sel.config(text="No file selected", foreground="#888888")
            return
        has_wav  = bool(row["wav_path"])
        has_file = has_wav or row["has_mod"]
        self._btn_play.config(state=NORMAL if has_wav else DISABLED)
        self._btn_replace.config(state=NORMAL)
        self._btn_showfile.config(state=NORMAL if has_file else DISABLED)
        sr  = row.get("sample_rate", 0)
        sr_ = f"  |  {sr // 1000} kHz" if sr else ""
        col = "#4fc3f7" if row["has_mod"] else "#cccccc"
        self._lbl_sel.config(
            text=f"{row['name']}  |  {row['source']}  |  "
                 f"{'Mono' if row['channels']==1 else 'Stereo'}{sr_}"
                 f"{'  [modified]' if row['has_mod'] else ''}",
            foreground=col)

    def _on_audio_double(self, event=None):
        if event:
            col = self._a_tree.identify_column(event.x)
            if col == "#1": self._inline_edit_name(event); return
            if col == "#2": self._inline_edit_category(event); return
        self._play()

    # ── Audio context menu ────────────────────────────────────────────────────

    def _show_audio_ctx(self, event):
        # Right-clicking a row that's NOT in the current selection selects just it;
        # right-clicking inside a multi-selection keeps the whole selection.
        iid = self._a_tree.identify_row(event.y)
        if iid and iid not in self._a_tree.selection():
            self._a_tree.selection_set(iid)
        self._on_audio_select()
        rows = self._selected_audio_rows()
        n    = len(rows)
        m = Menu(self._a_tree, tearoff=0)
        if n <= 1:
            row = rows[0] if rows else None
            m.add_command(label="▶  Play", command=self._play,
                          state=NORMAL if (row and row["wav_path"]) else DISABLED)
            m.add_command(label="Replace…", command=self._replace,
                          state=NORMAL if row else DISABLED)
            m.add_command(label="Patch This File", command=self._patch_single,
                          state=NORMAL if (row and row["has_mod"]) else DISABLED)
            m.add_separator()
            m.add_command(label="Show in Explorer",          command=self._show_in_explorer)
            m.add_command(label="Show Original in Explorer", command=self._show_original)
            m.add_separator()
            m.add_command(label="Edit Name…",     command=self._inline_edit_name_menu)
            m.add_command(label="Set Category…",   command=self._bulk_set_category)
            m.add_command(label="Set Sample Rate…", command=self._bulk_set_rate,
                          state=NORMAL if (row and row.get("offset") is not None) else DISABLED)
        else:
            m.add_command(label=f"{n} tracks selected", state=DISABLED)
            m.add_separator()
            m.add_command(label=f"Set Category for {n}…", command=self._bulk_set_category)
            any_rate = any(r.get("offset") is not None and r["wav_path"] for r in rows)
            m.add_command(label=f"Set Sample Rate for {n}…", command=self._bulk_set_rate,
                          state=NORMAL if any_rate else DISABLED)
        self._a_ctx = m          # keep a reference alive while the menu is posted
        try: m.tk_popup(event.x_root, event.y_root)
        finally: m.grab_release()

    # ── Audio inline edit ─────────────────────────────────────────────────────

    def _inline_edit_name(self, event=None):
        sel = self._a_tree.selection()
        if not sel: return
        iid = sel[0]; row = self._selected_audio_row()
        if not row: return
        bbox = self._a_tree.bbox(iid, column="#1")
        if not bbox: return
        x, y, w, h = bbox
        var = StringVar(value=row["name"])
        ent = ttk.Entry(self._a_tree, textvariable=var)
        ent.place(x=x, y=y, width=w, height=h)
        ent.select_range(0, END); ent.focus_set()
        def confirm(e=None):
            new = var.get().strip(); ent.destroy()
            if new and new != row["name"]:
                self._apply_name_change(row, new_name=new)
        def cancel(e=None): ent.destroy()
        ent.bind("<Return>", confirm); ent.bind("<Escape>", cancel)
        ent.bind("<FocusOut>", cancel)

    def _inline_edit_name_menu(self):
        sel = self._a_tree.selection()
        if sel:
            bbox = self._a_tree.bbox(sel[0], column="#1")
            if bbox:
                class _E: pass
                e = _E(); e.x = bbox[0] + 2
                self._inline_edit_name(e)

    def _inline_edit_category(self, event=None):
        sel = self._a_tree.selection()
        if not sel: return
        iid = sel[0]; row = self._selected_audio_row()
        if not row: return
        bbox = self._a_tree.bbox(iid, column="#2")
        if not bbox: return
        x, y, w, h = bbox
        cats = sorted(CATEGORY_LABELS.values())
        cur  = CATEGORY_LABELS.get(row["folder"], row["folder"])
        cb   = ttk.Combobox(self._a_tree, values=cats, state="readonly")
        cb.set(cur); cb.place(x=x, y=y, width=w, height=h); cb.focus_set()
        cb.after(30, lambda: cb.event_generate("<Down>") if cb.winfo_exists() else None)
        def confirm(e=None):
            label = cb.get()
            if cb.winfo_exists(): cb.destroy()
            new_folder = next((k for k, v in CATEGORY_LABELS.items() if v == label), None)
            if new_folder and new_folder != row["folder"]:
                self._apply_name_change(row, new_category=new_folder)
        def cancel(e=None):
            if cb.winfo_exists(): cb.destroy()
        cb.bind("<<ComboboxSelected>>", confirm); cb.bind("<Escape>", cancel)

    def _inline_edit_category_menu(self):
        sel = self._a_tree.selection()
        if sel:
            bbox = self._a_tree.bbox(sel[0], column="#2")
            if bbox:
                class _E: pass
                e = _E(); e.x = bbox[0] + 2
                self._inline_edit_category(e)

    def _apply_name_change(self, row: dict, new_name=None, new_category=None, refresh=True):
        root    = self._get_root()
        if not root: return
        fid     = row["file_id"]
        off_hex = row["off_hex"] or (f"0x{row['offset']:08X}" if row.get("offset") else None)
        if not off_hex:
            if refresh:
                messagebox.showwarning("Cannot Edit", "No offset info for this entry.")
            return
        key     = (fid, off_hex)
        pending = self._pending_name_changes.get(key, {"stem": row["stem"]})
        if new_name is not None:
            pending["name"] = new_name; pending.setdefault("category", row["category"])
        if new_category is not None:
            pending["category"] = new_category; pending.setdefault("name", row["name"])
        self._pending_name_changes[key] = pending
        if new_name is not None:     row["name"] = new_name
        if new_category is not None:
            row["category"] = new_category
            row["folder"]   = CATEGORY_FOLDER.get(new_category, "Unknown")
        if refresh:
            self._update_apply_btn()
            self._apply_audio_filter()

    def _flush_pending_names(self):
        if not self._pending_name_changes: return
        root = self._get_root()
        if not root: return
        by_fid: dict = {}
        for (fid, oh), ch in self._pending_name_changes.items():
            by_fid.setdefault(fid, {})[oh] = ch
        for fid, changes in by_fid.items():
            nm_path = names_path(root, fid)
            raw = json.loads(nm_path.read_text(encoding="utf-8")) if nm_path.exists() else {}
            for oh, ch in changes.items():
                entry = raw.get(oh, {}); entry.update(ch); raw[oh] = entry
            nm_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            self._log(f"[{fid}] Saved {len(changes)} name change(s)")
        self._pending_name_changes.clear()
        self._update_apply_btn()

    def _queue_rate_change(self, row: dict, new_rate: int, refresh=True):
        """Stage a sample-rate change instead of running it immediately.
        Applied (decoded) in one batch by Apply Changes."""
        fid     = row["file_id"]
        off_hex = row["off_hex"] or (f"0x{row['offset']:08X}" if row.get("offset") else None)
        if not off_hex or row.get("offset") is None:
            if refresh:
                messagebox.showwarning("Cannot Edit", "No offset info for this entry.")
            return
        key = (fid, off_hex)
        cur = row.get("sample_rate", SAMPLE_RATE) or SAMPLE_RATE
        if new_rate == cur:
            self._pending_rate_changes.pop(key, None)        # back to original = nothing to do
        else:
            self._pending_rate_changes[key] = {
                "offset":   row["offset"], "packets": row["max_pkts"],
                "channels": row["channels"], "wav_path": row["wav_path"],
                "new_rate": new_rate, "stem": row["stem"], "name": row["name"],
            }
        row["sample_rate"] = new_rate                        # reflect in the list right away
        if refresh:
            self._update_apply_btn()
            self._apply_audio_filter()

    def _apply_pending_changes(self):
        """One button for everything staged: name/category edits + sample-rate changes."""
        if self._op_busy(): return
        had_names    = bool(self._pending_name_changes)
        rate_changes = dict(self._pending_rate_changes)
        if not had_names and not rate_changes:
            return
        root = self._get_root()
        if not root: return
        xma = game_root = None
        if rate_changes:
            xma = self._get_xma2encode()
            if not xma: return                               # keep pending; tool missing
            game_root = self._get_game_root()
            if not game_root:
                messagebox.showwarning("No Root",
                    "Configure the game files folder first."); return
        # Commit: names to JSON now (fast), then batch the slow decodes in one thread.
        self._flush_pending_names()
        self._pending_rate_changes.clear()
        self._update_apply_btn()
        n = (1 if had_names else 0) + len(rate_changes)
        self._log(f"─── Apply Changes ({n} item group(s)) ───")
        self._run_in_thread(
            self._apply_changes_worker,
            root, game_root, had_names, rate_changes, xma, self._log_q.put,
            op_label="Applying changes…", on_done=self._reload_audio)

    def _apply_changes_worker(self, root, game_root, had_names, rate_changes, xma, log):
        if had_names:
            op_reload_names(root, log)
        if not rate_changes:
            return
        items = list(rate_changes.items())

        # A rename moves the WAV, so if names were just applied, re-resolve each rate
        # change's target file from the current catalog (by stem) before decoding.
        if had_names:
            cat_cache: dict = {}
            for (fid, _oh), rc in items:
                cat = cat_cache.get(fid)
                if cat is None:
                    cat = cat_cache[fid] = load_catalog(catalog_path(root, fid))
                e = cat.get(f"{rc['offset']:08X}_{rc['channels']}ch_{rc['packets']}p")
                if e and e.get("wav"):
                    rc["wav_path"] = str(root / e["wav"])

        def _decode(kv):
            (fid, _oh), rc = kv
            try:
                with open(game_root / fid, "rb") as f:
                    f.seek(rc["offset"]); raw = f.read(rc["packets"] * PACKET_SIZE)
            except Exception as e:
                return fid, rc, 0.0, f"read error: {e}"
            if len(raw) < rc["packets"] * PACKET_SIZE:
                return fid, rc, 0.0, "archive too short"
            wp = Path(rc["wav_path"]); wp.parent.mkdir(parents=True, exist_ok=True)
            try:
                dur = decode_xma2(raw, rc["channels"], wp, xma, sample_rate=rc["new_rate"])
            except Exception as e:
                return fid, rc, 0.0, str(e)
            return fid, rc, dur, None

        # Decode in parallel — each xma2encode is its own subprocess (releases the GIL)
        # and writes to a unique temp dir + WAV, so N tracks finish in ~1/N the wall time.
        workers = max(2, min(len(items), os.cpu_count() or 4))
        updates: dict = {}                       # fid -> [(stem, new_rate, dur)]
        ok = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fid, rc, dur, err in ex.map(_decode, items):
                if err or dur <= 0:
                    log(f"  FAILED {rc['name']}: {err or 'decode failed'}"); continue
                stem = f"{rc['offset']:08X}_{rc['channels']}ch_{rc['packets']}p"
                updates.setdefault(fid, []).append((stem, rc["new_rate"], dur))
                log(f"  {rc['name']} → {rc['new_rate']} Hz ({dur:.2f}s)"); ok += 1
        # Catalog writes serialized here (one load+save per archive) — avoids the
        # read-modify-write race that parallel op_set_sample_rate calls would have.
        for fid, ups in updates.items():
            cat_p = catalog_path(root, fid); cat = load_catalog(cat_p)
            for stem, nr, dur in ups:
                if stem in cat:
                    cat[stem]["sample_rate"] = nr
                    cat[stem]["duration"]    = round(dur, 3)
            save_catalog(cat_p, cat)
        # Persist the chosen rates into the names JSON too, so they travel as shareable
        # metadata (mod-pack / names export) alongside name + category edits.
        names_writes: dict = {}
        for (fid, oh), rc in rate_changes.items():
            names_writes.setdefault(fid, {})[oh] = rc["new_rate"]
        for fid, m in names_writes.items():
            np_ = names_path(root, fid)
            raw = json.loads(np_.read_text(encoding="utf-8")) if np_.exists() else {}
            for oh, rate in m.items():
                cur = raw.get(oh) if isinstance(raw.get(oh), dict) else {}
                cur = dict(cur or {}); cur["sample_rate"] = rate; raw[oh] = cur
            np_.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        log(f"─── Sample-rate: {ok}/{len(items)} applied ───")

    def _update_apply_btn(self):
        n = len(self._pending_name_changes) + len(self._pending_rate_changes)
        self._btn_apply_changes.config(
            text=f"Apply Changes ({n})" if n else "Apply Changes",
            state=NORMAL if n else DISABLED)

    # ── Audio playback ────────────────────────────────────────────────────────

    def _play(self):
        row = self._selected_audio_row()
        if not row: return
        wav = row["mod_path"] if row["has_mod"] else row["wav_path"]
        if not wav:
            messagebox.showinfo("Not Extracted",
                "Run Extract first to generate WAV files."); return
        try:
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._btn_stop.config(state=NORMAL); self._playing = True
        except Exception as e:
            messagebox.showerror("Playback Error", str(e))

    def _stop(self):
        winsound.PlaySound(None, winsound.SND_PURGE)
        self._btn_stop.config(state=DISABLED); self._playing = False

    def _replace(self):
        self._replace_row(self._selected_audio_row())

    def _replace_row(self, row):
        """Shared Replace pipeline — used by the Audio tab and the Audio Banks tab.
        Validates the new WAV fits the original slot, then stages it in Modified/Audio/."""
        if not row: return
        tools = self._get_tools(); root = self._get_root()
        if not tools or not root: return
        xma2encode, ffmpeg = tools
        new_wav = filedialog.askopenfilename(
            title=f"Select replacement for: {row['name']}",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")])
        if not new_wav: return
        new_wav = Path(new_wav)
        sr = row.get("sample_rate") or SAMPLE_RATE; max_pkts = row["max_pkts"]
        dur_orig = row["duration"]; dur_new = wav_duration(new_wav)
        self._log(f"Validating {new_wav.name} for {row['name']}…")
        try:
            raw, q_used = encode_wav_to_fit(
                new_wav, row["channels"], ffmpeg, xma2encode,
                sample_rate=sr, max_packets=max_pkts)
            n_new = len(raw) // PACKET_SIZE
        except Exception as e:
            messagebox.showerror("Encode Error", f"Could not encode WAV:\n{e}"); return
        excess    = n_new - max_pkts
        spare_sec = abs(excess) * (dur_new / max(n_new, 1))
        q_note    = f"  (quality {q_used})\n\n" if q_used < 60 else "\n\n"
        if excess > 0:
            msg = (f"File TOO LONG by {excess} pkts (~{spare_sec:.2f}s) at minimum quality.\n\n"
                   f"  Slot: {max_pkts}p ({dur_orig:.2f}s)\n  Yours: {n_new}p ({dur_new:.2f}s)\n\n"
                   "Continue and TRUNCATE?")
            if not messagebox.askyesno("File Too Long", msg, icon="warning"): return
        else:
            status = "fits perfectly" if excess == 0 else f"fits ({-excess} pkts spare)"
            msg = (f"Size check: {status}.{q_note}"
                   f"  Slot: {max_pkts}p ({dur_orig:.2f}s)\n  Yours: {n_new}p ({dur_new:.2f}s)\n\n"
                   "Copy to Modified folder?")
            if not messagebox.askyesno("Confirm Replace", msg): return
        dest_dir = modified_audio_dir(root) / row["folder"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new_wav, dest_dir / f"{row['name']}.wav")
        self._log(f"Saved to Modified/Audio/{row['folder']}/{row['name']}.wav")
        self._reload_audio()
        if getattr(self, "_bank_records", None):     # keep the Banks tab links fresh
            self._bank_populate()

    def _open_modified_folder(self):
        root = self._get_root()
        if not root: return
        mod = modified_audio_dir(root); mod.mkdir(parents=True, exist_ok=True)
        os.startfile(str(mod))

    def _show_in_explorer(self):
        row = self._selected_audio_row()
        if not row: return
        target = row["mod_path"] or row["wav_path"]
        if target and Path(target).exists():
            subprocess.Popen(f'explorer /select,"{target}"')
        else:
            root = self._get_root()
            if root:
                folder = audio_dir(root) / row["folder"]
                if folder.exists(): os.startfile(str(folder))

    def _show_original(self):
        row = self._selected_audio_row()
        if not row or not row["wav_path"]: return
        if Path(row["wav_path"]).exists():
            subprocess.Popen(f'explorer /select,"{row["wav_path"]}"')

    def _bulk_set_rate(self):
        """Stage a sample-rate change for every selected, extracted track (works for 1)."""
        rows = [r for r in self._selected_audio_rows()
                if r.get("offset") is not None and r["wav_path"]]
        if not rows:
            messagebox.showinfo("Not Extracted",
                "Select one or more extracted tracks to set the sample rate."); return
        n = len(rows)
        rates    = {r.get("sample_rate", SAMPLE_RATE) or SAMPLE_RATE for r in rows}
        cur_rate = next(iter(rates)) if len(rates) == 1 else SAMPLE_RATE
        title    = rows[0]["name"] if n == 1 else f"{n} tracks selected"
        dlg = Toplevel(self); dlg.title("Set Sample Rate")
        dlg.resizable(False, False); dlg.grab_set()
        f = ttk.Frame(dlg, padding=18); f.pack(fill=BOTH, expand=True)
        ttk.Label(f, text=title, font=("Segoe UI", 9, "bold")).pack(anchor=W)
        ttk.Label(f, text="Lower rate = lower pitch/slower.  Higher = higher/faster.",
                  foreground="#888888").pack(anchor=W, pady=(4, 8))
        sf = ttk.Frame(f); sf.pack(fill=X)
        ttk.Label(sf, text="Sample rate (Hz):").pack(side=LEFT)
        v  = StringVar(value=str(cur_rate))
        ttk.Entry(sf, textvariable=v, width=10).pack(side=LEFT, padx=6)
        cb = ttk.Combobox(sf, values=["22050","32000","44100","48000"],
                           width=7, state="readonly"); cb.pack(side=LEFT)
        cb.bind("<<ComboboxSelected>>", lambda _: v.set(cb.get()))
        def apply():
            try:
                new_rate = int(v.get())
                if not 8000 <= new_rate <= 192000: raise ValueError
            except ValueError:
                messagebox.showerror("Invalid", "Enter a rate between 8000–192000.", parent=dlg)
                return
            dlg.destroy()
            for r in rows:
                self._queue_rate_change(r, new_rate, refresh=False)
            self._update_apply_btn()
            self._apply_audio_filter()
        ttk.Button(f, text="Queue Change" + ("" if n == 1 else f"  (×{n})"),
                   style="Accent.TButton", command=apply).pack(pady=(10, 2))
        ttk.Label(f, text="Staged until you press Apply Changes (batched with name edits).",
                  foreground="#888888", font=("Segoe UI", 8)).pack()

    def _bulk_set_category(self):
        """Stage a category change for every selected track (works for 1)."""
        rows = self._selected_audio_rows()
        if not rows: return
        n = len(rows)
        cats  = sorted(CATEGORY_LABELS.values())
        cur   = CATEGORY_LABELS.get(rows[0]["folder"], rows[0]["folder"])
        title = rows[0]["name"] if n == 1 else f"{n} tracks selected"
        dlg = Toplevel(self); dlg.title("Set Category")
        dlg.resizable(False, False); dlg.grab_set()
        f = ttk.Frame(dlg, padding=18); f.pack(fill=BOTH, expand=True)
        ttk.Label(f, text=title, font=("Segoe UI", 9, "bold")).pack(anchor=W, pady=(0, 8))
        sf = ttk.Frame(f); sf.pack(fill=X)
        ttk.Label(sf, text="Category:").pack(side=LEFT)
        cb = ttk.Combobox(sf, values=cats, width=22, state="readonly"); cb.set(cur)
        cb.pack(side=LEFT, padx=6)
        def apply():
            label = cb.get()
            new_folder = next((k for k, val in CATEGORY_LABELS.items() if val == label), None)
            dlg.destroy()
            if not new_folder: return
            for r in rows:
                self._apply_name_change(r, new_category=new_folder, refresh=False)
            self._update_apply_btn()
            self._apply_audio_filter()
        ttk.Button(f, text="Queue Change" + ("" if n == 1 else f"  (×{n})"),
                   style="Accent.TButton", command=apply).pack(pady=(10, 2))
        ttk.Label(f, text="Staged until you press Apply Changes.",
                  foreground="#888888", font=("Segoe UI", 8)).pack()

    def _patch_single(self):
        row = self._selected_audio_row()
        if not row or not row["has_mod"]:
            messagebox.showinfo("No Modification",
                "Select a track that has a modified file."); return
        tools = self._get_tools(); root = self._get_root()
        if not tools or not root or self._op_busy(): return
        xma2encode, ffmpeg = tools
        arc = archive_path(root, row["source"])
        if not arc.exists():
            messagebox.showerror("Archive Not Found", f"Not found:\n{arc}"); return
        if not messagebox.askyesno("Patch Single Track",
                f"Write '{row['name']}' into {row['source']}?\n\n"
                "A .bak backup is created if one doesn't exist."): return
        self._log(f"─── Patch Single: {row['name']} ───")
        entry = {"source_file": row["source"], "offset": row["offset"],
                 "packets": row["max_pkts"], "channels": row["channels"],
                 "sample_rate": row.get("sample_rate"),
                 "friendly_name": row["name"], "stem": row["stem"]}
        def work(): op_patch_single(root, ffmpeg, xma2encode, Path(row["mod_path"]), entry, self._log_q.put)
        self._run_in_thread(work, on_done=self._reload_audio)
    def _open_extract_dlg(self):
        if self._op_busy(): return
        root = self._get_root(); xma = self._get_xma2encode()
        if not root or not xma: return
        win = Toplevel(self); win.title("Extract Audio")
        win.geometry("380x260"); win.resizable(False, False); win.grab_set()
        f = ttk.Frame(win, padding=18); f.pack(fill=BOTH, expand=True)
        ttk.Label(f, text="Extract Audio",
                  font=("Segoe UI", 11, "bold")).pack(pady=(0, 10))
        ttk.Label(f, text="Select archives to extract:").pack(anchor=W)
        checks = {}
        cf = ttk.Frame(f); cf.pack(fill=X, pady=6)
        for fid in FILE_IDS:
            arc = archive_path(root, fid); exists = arc.exists()
            mb  = arc.stat().st_size // 1024 // 1024 if exists else 0
            lbl = f"{fid}  ({mb} MB)" if exists else f"{fid}  (not found)"
            var = BooleanVar(value=exists)
            ttk.Checkbutton(cf, text=lbl, variable=var,
                            state=NORMAL if exists else DISABLED).pack(
                anchor=W, padx=16, pady=2)
            checks[fid] = (var, exists)
        ttk.Label(f, text="May take several minutes per archive.",
                  foreground="#888888").pack(pady=(6, 0))
        def start():
            sel = [fid for fid, (v, ex) in checks.items() if v.get() and ex]
            if not sel:
                messagebox.showwarning("Nothing selected",
                    "Select at least one archive.", parent=win); return
            win.destroy()
            self._log(f"─── Extract {sel} ───")
            self._run_in_thread(op_extract, root, sel, xma, self._log_q.put,
                                op_label="Extracting audio…",
                                on_done=self._reload_audio)
        ttk.Button(f, text="Extract", style="Accent.TButton", command=start).pack(pady=(12, 0))

    def _run_check(self):
        if self._op_busy(): return
        tools = self._get_tools(); root = self._get_root()
        if not tools or not root: return
        xma2encode, ffmpeg = tools
        self._log("─── Check All ───")
        mod = modified_audio_dir(root)
        wavs = sorted(mod.rglob("*.wav")) if mod.exists() else []
        if not wavs:
            self._log("No files in Modified/Audio/ — nothing to check."); return
        self._log(f"Found {len(wavs)} file(s) to check.")
        def work():
            all_cat: dict = {}; fi: dict = {}
            for fid in FILE_IDS:
                cat = load_catalog(catalog_path(root, fid))
                for stem, entry in cat.items():
                    all_cat[stem] = entry
                    fn = entry.get("friendly_name")
                    if fn: fi[fn] = stem
            ok = trunc = padded = fail = 0
            for wp in wavs:
                sk    = wp.stem
                entry = all_cat.get(sk) or all_cat.get(fi.get(sk, ""))
                if not entry:
                    self._log_q.put(f"  NOT FOUND: {wp.stem}"); fail += 1; continue
                max_pkts = entry["packets"]; ch = entry["channels"]
                sr = entry.get("sample_rate") or SAMPLE_RATE
                display = entry.get("friendly_name") or sk
                try:
                    raw, q = encode_wav_to_xma2(wp, ch, ffmpeg, xma2encode, sample_rate=sr)
                    n_new  = len(raw) // PACKET_SIZE
                except Exception as e:
                    self._log_q.put(f"  FAILED {display}: {e}"); fail += 1; continue
                excess = n_new - max_pkts
                if excess > 0:
                    self._log_q.put(f"  TRUNCATED {display}: slot={max_pkts}p yours={n_new}p"); trunc += 1
                elif excess < 0:
                    padded += 1
                else:
                    ok += 1
            self._log_q.put(
                f"─── Check done: {ok} OK  {padded} padded  {trunc} TRUNCATED  {fail} failed ───")
        self._run_in_thread(work, op_label="Checking replacements…")
        # No reload needed after check — it's read-only

    def _run_reimport(self):
        if self._op_busy(): return
        tools = self._get_tools(); root = self._get_root()
        if not tools or not root: return
        if not messagebox.askyesno("Patch Game",
                "This writes replacement audio into the game archives.\n\n"
                "A .bak backup is created the first time each archive is patched.\n\n"
                "Continue?", icon="warning"): return
        xma2encode, ffmpeg = tools
        self._log("─── Patch Game ───")
        def work():
            patched, skipped, nf = op_reimport(
                root, ffmpeg, xma2encode, False, self._log_q.put)
            self._log_q.put(
                f"─── Done: {patched} patched  {skipped} skipped  {nf} not in catalog ───")
        self._run_in_thread(work, op_label="Patching game files…",
                            on_done=self._reload_audio)

    def _run_reload_names(self):
        if self._op_busy(): return
        root = self._get_root()
        if not root: return
        self._log("─── Reload Names ───")
        self._run_in_thread(op_reload_names, root, self._log_q.put,
                            op_label="Applying name changes…",
                            on_done=self._reload_audio)

    # ── Threaded worker framework ─────────────────────────────────────────────

    def _run_in_thread(self, fn, *args, op_label="Working…", on_done=None):
        self._cancel_event.clear()
        self._op_done_callback = on_done   # None = no post-op reload
        self._progress.config(mode="indeterminate"); self._progress.start()
        self._show_loading_dlg(op_label)
        def worker():
            try:
                fn(*args)
            except Exception as e:
                self._log_q.put(f"ERROR: {e}")
            finally:
                self._log_q.put(None)
        self._op_thread = threading.Thread(target=worker, daemon=True)
        self._op_thread.start()

    def _poll_log(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                if msg is None:
                    self._progress.stop()
                    try: self._progress.config(mode="indeterminate")
                    except Exception: pass
                    if self._loading_dlg:
                        try: self._loading_dlg.destroy()
                        except Exception: pass
                        self._loading_dlg = None
                    self._ld_bar = self._ld_pct = self._ld_note = None
                    cb, self._op_done_callback = self._op_done_callback, None
                    if cb:
                        cb()
                elif isinstance(msg, str) and msg.startswith("__PROGRESS__"):
                    body = msg[len("__PROGRESS__"):]
                    frac_s, _, note = body.partition("|")
                    try:
                        frac = float(frac_s)
                    except ValueError:
                        frac = None
                    if frac is not None and frac < 0:      # sentinel = indeterminate
                        frac = None
                    self._set_progress(frac, note)
                else:
                    self._log(str(msg))
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _cancel_op(self):
        """Signal the current background operation to stop."""
        self._cancel_event.set()
        if self._loading_dlg:
            try: self._loading_dlg.destroy()
            except Exception: pass
            self._loading_dlg = None
        self._progress.stop()
        self._v_status.set("Cancelled")

    def _show_loading_dlg(self, message: str):
        if self._loading_dlg:
            try: self._loading_dlg.destroy()
            except Exception: pass
        C = self._COL
        dlg = Toplevel(self); dlg.title("")
        dlg.resizable(False, False); dlg.transient(self); dlg.configure(bg=C["bg1"])
        dlg.protocol("WM_DELETE_WINDOW", self._cancel_op)
        f = ttk.Frame(dlg, padding=(28, 18)); f.pack()
        ttk.Label(f, text=message, font=("Segoe UI", 10, "bold")).pack(anchor=W)
        row = ttk.Frame(f); row.pack(fill=X, pady=(10, 2))
        # Starts as an indeterminate spinner; flips to a real % bar the moment a task
        # reports progress (see _set_progress / _emit_progress).
        self._ld_bar = ttk.Progressbar(row, mode="indeterminate", length=260)
        self._ld_bar.pack(side=LEFT); self._ld_bar.start(12)
        self._ld_pct = ttk.Label(row, text="", width=5, anchor=E,
                                 foreground=C["red"], font=("Segoe UI", 10, "bold"))
        self._ld_pct.pack(side=LEFT, padx=(8, 0))
        self._ld_note = ttk.Label(f, text="", foreground=C["muted"], font=("Segoe UI", 8))
        self._ld_note.pack(anchor=W)
        ttk.Button(f, text="Cancel", command=self._cancel_op).pack(anchor=E, pady=(10, 0))
        dlg.update_idletasks()
        pw = self.winfo_width(); ph = self.winfo_height()
        px = self.winfo_x();    py = self.winfo_y()
        w  = dlg.winfo_reqwidth(); h = dlg.winfo_reqheight()
        dlg.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
        self._loading_dlg = dlg

    def _set_progress(self, frac, note=""):
        """Drive the progress UI. frac None = keep the indeterminate spinner going;
        a 0..1 float switches the bar(s) to a determinate % display."""
        pct = None if frac is None else max(0, min(100, int(round(frac * 100))))
        # inline bar in the Operation Log frame
        try:
            if pct is None:
                if str(self._progress["mode"]) != "indeterminate":
                    self._progress.config(mode="indeterminate"); self._progress.start(12)
            else:
                self._progress.stop(); self._progress.config(mode="determinate")
                self._progress["value"] = pct
        except Exception:
            pass
        # modal loading dialog
        if self._loading_dlg and self._ld_bar is not None:
            try:
                if pct is None:
                    if str(self._ld_bar["mode"]) != "indeterminate":
                        self._ld_bar.config(mode="indeterminate"); self._ld_bar.start(12)
                else:
                    self._ld_bar.stop(); self._ld_bar.config(mode="determinate")
                    self._ld_bar["value"] = pct
                    self._ld_pct.config(text=f"{pct}%")
                if note:
                    self._ld_note.config(text=note)
            except Exception:
                pass
        # status bar
        tail = (f" {pct}%" if pct is not None else "") + (f"  ·  {note}" if note else "")
        self._v_status.set("Working…" + tail)

    def _emit_progress(self, done, total, note=""):
        """Thread-safe: called from a worker to report done/total. total<=0 → indeterminate."""
        frac = (max(0.0, min(1.0, done / total)) if total else -1.0)
        self._log_q.put(f"__PROGRESS__{frac:.4f}|{note}")

    def _emit_progressf(self, frac, note=""):
        """Thread-safe fractional progress (0..1) for stage-based ops (e.g. a single Apply to IFF)."""
        self._log_q.put(f"__PROGRESS__{max(0.0, min(1.0, frac)):.4f}|{note}")

    # ── Log helpers ───────────────────────────────────────────────────────────

    # ── IFF Textures tab — PERMANENT, file-based extract / replace ─────────────
    # Extract from CLEAN (read-only) -> Original/<iff>/<stem>.dds; edit a copy in
    # Modified/<iff>/; Apply re-encodes + splices into the GAME archives (in-place
    # or relocate). No live/temporary path — every change is a real file edit.
    def _build_iff_tab(self):
        t = self._tab_iff
        bar = ttk.Frame(t, padding=(4, 4, 4, 2)); bar.pack(fill=X)
        ttk.Label(bar, text="Team:").pack(side=LEFT)
        self._v_iff_team = StringVar(value="All")
        self._iff_team_cb = ttk.Combobox(bar, textvariable=self._v_iff_team, state="readonly", width=8)
        self._iff_team_cb.pack(side=LEFT, padx=(4, 8))
        self._iff_team_cb.bind("<<ComboboxSelected>>", lambda _: self._iff_apply_filter())
        ttk.Label(bar, text="Category:").pack(side=LEFT)
        self._v_iff_cat = StringVar(value="All")
        self._iff_cat_cb = ttk.Combobox(bar, textvariable=self._v_iff_cat, state="readonly", width=18)
        self._iff_cat_cb.pack(side=LEFT, padx=(4, 8))
        self._iff_cat_cb.bind("<<ComboboxSelected>>", lambda _: self._iff_apply_filter())
        ttk.Label(bar, text="Search:").pack(side=LEFT)
        self._v_iff_search = StringVar()
        self._v_iff_search.trace_add("write", lambda *_: self._iff_apply_filter())
        ttk.Entry(bar, textvariable=self._v_iff_search, width=18).pack(side=LEFT, padx=(4, 8))
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        ttk.Button(bar, text="Extract Selected", command=self._iff_extract_asset).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Extract All Listed", command=self._iff_extract_shown).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Apply to IFF", style="Accent.TButton",
                   command=self._iff_apply_selected).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Extract Original Files", command=self._iff_revert_extract).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Revert IFF to Original", command=self._iff_revert).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Open Extracted Files", command=lambda: self._iff_open("Extracted")).pack(side=LEFT, padx=2)
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        ttk.Button(bar, text="Jersey Normal Stitcher…", command=self._open_normal_stitcher).pack(side=LEFT, padx=2)
        # Quality is AUTOMATIC now (no toggles): replacements store as lossless 8888 when the pack can
        # grow, a larger-than-native source auto-upscales the slot (hi-res), and the archives
        # auto-compact after any relocating apply. Capture-from-Game logic is retained
        # (self._iff_capture_from_game / self._iff_compact) but their buttons are hidden — every
        # repacked pack is already catalogued.
        self._v_iff_lossless = BooleanVar(value=True)

        ttk.Label(t, foreground="#ffd479", font=("Segoe UI", 8, "bold"),
                  text="Workflow:  Extract Selected  →  edit the PNG/DDS in the Extracted folder  →  Apply to IFF. "
                       "Ctrl/Shift-click to select several assets and Extract or Apply them all at once. "
                       "Right-click a texture on the right to work on just that one. PNG is easiest — author at any "
                       "size (same aspect) and it's fitted for you.").pack(fill=X, padx=6, pady=(2, 0))
        ttk.Label(t, foreground="#999", font=("Segoe UI", 8),
                  text="Apply re-encodes only the files you changed and is permanent (a one-time backup is kept); "
                       "quality is automatic (lossless where possible, larger art auto-upscales).  "
                       "'Extract Original Files' re-pulls a clean copy · 'Revert IFF to Original' undoes an apply.").pack(fill=X, padx=6)

        pane = ttk.PanedWindow(t, orient=HORIZONTAL); pane.pack(fill=BOTH, expand=True, padx=4, pady=(0, 4))
        left = ttk.Frame(pane); pane.add(left, weight=3)
        cols = ("label", "iff", "team", "cat", "size", "status")
        # extended = multi-select (Ctrl/Shift-click) so you can Extract or Apply many assets at once.
        tv = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for c, w, txt, anc in (("label", 200, "Asset", W), ("iff", 190, "File", W),
                               ("team", 55, "Team", CENTER), ("cat", 120, "Category", W),
                               ("size", 70, "Size", CENTER), ("status", 80, "Status", CENTER)):
            tv.heading(c, text=txt); tv.column(c, width=w, anchor=anc)
        sb = ttk.Scrollbar(left, command=tv.yview); tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y); tv.pack(fill=BOTH, expand=True)
        tv.tag_configure("modified", foreground="#4fc3f7")
        tv.tag_configure("extracted", foreground="#9ccc65")
        tv.bind("<<TreeviewSelect>>", self._on_iff_select)
        tv.bind("<Button-3>", self._iff_show_ctx)
        self._iff_tv = tv
        self._build_iff_ctx()

        right = ttk.Frame(pane); pane.add(right, weight=2)
        self._iff_canvas = Canvas(right, bg="#1a1a2e", width=300, height=240, highlightthickness=0)
        self._iff_canvas.pack(fill=BOTH, expand=True)
        sub = ttk.Frame(right); sub.pack(fill=X, padx=4)
        ttk.Label(sub, text="Textures in this asset (multi-texture .iff)",
                  foreground="#999", font=("Segoe UI", 8)).pack(anchor=W)
        stv = ttk.Treeview(sub, columns=("n", "size", "fmt"), show="headings",
                           height=6, selectmode="browse")
        for c, w, txt in (("n", 110, "Texture"), ("size", 100, "Size"), ("fmt", 80, "Format")):
            stv.heading(c, text=txt); stv.column(c, width=w, anchor=CENTER)
        ssb = ttk.Scrollbar(sub, command=stv.yview); stv.configure(yscrollcommand=ssb.set)
        ssb.pack(side=RIGHT, fill=Y); stv.pack(fill=X)
        stv.bind("<<TreeviewSelect>>", self._on_iff_sub_select)
        stv.bind("<Button-3>", self._iff_sub_show_ctx)
        self._iff_subtv = stv
        self._build_iff_subctx()
        self._iff_info = ttk.Label(right, text="Select an asset to preview", foreground="#888888",
                                   font=("Consolas", 8), justify=LEFT)
        self._iff_info.pack(fill=X, padx=6, pady=4)
        self._iff_catalog = []
        self._iff_subtex = []
        self._iff_load_catalog()

    def _iff_load_catalog(self):
        try:
            hidden = set(getattr(archtex, "HIDDEN_ASSETS", set()))
            # One jersey = one row. Each jersey exists twice (in-game uniform + front-end
            # team-select sheet, whose `decals` is a byte-identical copy of the uniform's
            # `stamps`), so hide the twins and let Apply fan the edit out to them instead.
            try:
                from launcher import jerseys
                hidden |= set(jerseys.hidden_members())
                self._log(f"[jersey] {jerseys.stats()} — twins hidden; one edit applies to both")
            except Exception as je:
                self._log(f"[jersey] map unavailable ({je}); showing every asset separately")
            all_rows = archtex.load_catalog()
            # zamboni_<code>.iff is the untextured zamboni MODEL (no editable textures); the textured
            # one the user actually paints is zamboni_team_<code>.iff. Hide the model, keep the team.
            import re as _re
            hidden |= {r["iff"] for r in all_rows
                       if r.get("iff") and _re.match(r"zamboni_[a-z]+\.iff$", r["iff"])}
            rows = [r for r in all_rows
                    if r.get("category") != "arena_audio" and r.get("iff") not in hidden]
        except Exception as e:
            self._log(f"[iff] catalog load failed: {e}"); rows = []
        self._iff_catalog = rows
        teams = ["All"] + sorted(set(r["team"] for r in rows))
        cats = ["All"] + sorted(set(r["category"] for r in rows))
        self._iff_team_cb["values"] = teams
        self._iff_cat_cb["values"] = cats
        self._iff_apply_filter()
        if hasattr(self, "_ga_mask_cb"):          # goalie tab: refresh built-in masks + free counts
            self._goalie_refresh_masklist()

    def _iff_apply_filter(self):
        tv = getattr(self, "_iff_tv", None)
        if tv is None:
            return
        prev = self._iff_selected()          # preserve the selected asset across the rebuild so the
        tv.delete(*tv.get_children())        # sub-texture list stays live after an Apply / refresh
        team = self._v_iff_team.get(); cat = self._v_iff_cat.get()
        q = self._v_iff_search.get().strip().lower()
        root = self._root_ex_quiet()
        # Precompute the edit-file layout in ONE directory walk (was per-row list_textures ->
        # ~700s at boot; then per-row globbing -> ~3s; this makes it instant): folder-relative
        # posix path -> [edit files]. Keyed by the grouped Extracted/ folder + legacy per-iff folder.
        edit_by_folder = {}
        if root:
            for base in ("Extracted", "Modified", "Original"):
                bd = Path(root) / "Textures" / base
                if bd.is_dir():
                    for f in bd.rglob("*"):
                        if f.suffix.lower() in (".dds", ".png"):
                            key = f.parent.relative_to(bd).as_posix()
                            edit_by_folder.setdefault(key, []).append(f)
        for r in self._iff_catalog:
            if team != "All" and r["team"] != team:
                continue
            if cat != "All" and r["category"] != cat:
                continue
            if q and q not in r["iff"].lower() and q not in r["category"].lower() and q not in r["team"].lower():
                continue   # match filename OR category OR team (so "ice" finds rink_*=ice_regular)
            iff = r["iff"]; status = ""; tag = ()
            # FAST status from the prebuilt index (no VRAM parsing): look up this asset's grouped +
            # legacy folders; for the shared Logos/ folder keep only this asset's own file.
            if edit_by_folder:
                folder = archtex.asset_iff(iff)
                files = []
                for key in {folder, archtex._legacy_asset_iff(iff)}:
                    files += edit_by_folder.get(key, [])
                if folder == "Logos":                          # shared folder -> this asset's file only
                    want = archtex.texture_filename(iff, None).lower()
                    files = [f for f in files if f.name.lower() in (want, want[:-4] + ".png")]
                if files:
                    if any(archtex.is_edited(root, f) for f in files):
                        status = "edited"; tag = ("modified",)
                    else:
                        status = "extracted"; tag = ("extracted",)
            try:
                kb = f"{int(r['size'], 16) // 1024} KB"
            except Exception:
                kb = ""
            tv.insert("", END, values=(r.get("label") or iff, iff, r["team"], r["category"], kb, status), tags=tag)
        if prev:                             # restore selection -> re-fires _on_iff_select, which
            for iid in tv.get_children():    # reloads the sub-texture list (fixes it going dead
                if tv.set(iid, "iff") == prev:   # after applying a single texture)
                    tv.selection_set(iid); tv.see(iid); break

    def _iff_selected(self):
        sel = self._iff_tv.selection()
        if not sel:
            return None
        focus = self._iff_tv.focus()                      # preview/act on the row with keyboard focus
        return self._iff_tv.set(focus if focus in sel else sel[0], "iff")

    def _iff_selected_many(self):
        """Every selected asset's .iff (multi-select), preserving row order."""
        return [self._iff_tv.set(i, "iff") for i in self._iff_tv.selection()]

    def _on_iff_select(self, _=None):
        """Asset selected -> enumerate its textures, fill the sub-list, preview the first."""
        iff = self._iff_selected()
        if not iff:
            return
        self._iff_canvas.delete("all")
        self._iff_subtv.delete(*self._iff_subtv.get_children())
        self._iff_subtex = []
        self._iff_info.config(text=f"reading {iff}…")
        import threading
        def work():
            try:
                recs = archtex.list_textures(iff)
            except Exception:
                recs = []
            self.after(0, lambda: self._iff_populate_sub(iff, recs))
        threading.Thread(target=work, daemon=True).start()

    def _iff_populate_sub(self, iff, recs):
        if self._iff_selected() != iff:
            return
        self._iff_subtex = recs
        stv = self._iff_subtv; stv.delete(*stv.get_children())
        if recs:                                            # multi-texture / scene asset
            for r in recs:
                stv.insert("", END, iid=str(r["index"]),
                           values=(r.get("label", r["index"]), f"{r['w']}×{r['h']}", r["fmt"]))
            stv.selection_set(str(recs[0]["index"]))        # triggers preview
        else:                                               # single / primary asset
            stv.insert("", END, iid="primary", values=("•", "primary", "…"))
            stv.selection_set("primary")
            import threading                                  # fill the real format/size async
            threading.Thread(target=lambda: self.after(
                0, lambda fi=archtex.primary_fetch(iff): self._iff_set_primary_fmt(iff, fi)),
                daemon=True).start()

    def _iff_set_primary_fmt(self, iff, fi):
        if self._iff_selected() != iff or not self._iff_subtv.exists("primary"):
            return
        fmt, w, h = fi if fi else (None, None, None)
        self._iff_subtv.item("primary", values=("•", f"{w}×{h}" if fmt else "primary", fmt or ""))

    def _iff_sub_rec(self):
        """The selected sub-texture record, or None for the single/primary texture."""
        sel = self._iff_subtv.selection()
        if not sel or sel[0] == "primary":
            return None
        return next((r for r in self._iff_subtex if str(r["index"]) == sel[0]), None)

    def _root_ex_quiet(self):
        """…/NHL2k10_Extracted_Files if the game-files root is set & valid, else None.
        No warning dialog, no directory creation (safe during list refresh / preview)."""
        r = (self._v_root.get().strip() or self.cfg.get("root_path", "")).strip()
        if not r or not Path(r).is_dir():
            return None
        return Path(r) / "NHL2k10_Extracted_Files"

    def _iff_dds_path(self, which, iff, rec):
        """Extracted/ DDS path for an asset+texture (folder = grouped layout), or None if no
        game-files root is set. Does NOT create directories. (`which` kept for call compatibility.)"""
        ex = self._root_ex_quiet()
        if ex is None:
            return None
        return archtex.extracted_root(ex) / archtex.asset_iff(iff) / archtex.texture_filename(iff, rec)

    def _on_iff_sub_select(self, _=None):
        iff = self._iff_selected()
        if not iff:
            return
        rec = self._iff_sub_rec()
        ex = self._root_ex_quiet()                         # show the user's edit if present
        mod = archtex.find_any_edit(ex, iff, rec) if ex is not None else None
        self._iff_canvas.delete("all")
        self._iff_info.config(text=f"decoding {iff}…")
        import threading
        def work():
            img = None; src = "CLEAN"
            if mod and mod.exists():
                try:
                    img = Image.open(mod).convert("RGBA"); src = "MODIFIED"
                except Exception:
                    img = None
            if img is None:
                try:
                    img = archtex.decode_record(iff, rec) if rec else archtex.decode_preview(iff)
                except Exception:
                    img = None
            self.after(0, lambda: self._iff_show_preview(iff, img, rec, src))
        threading.Thread(target=work, daemon=True).start()

    def _iff_show_preview(self, iff, img, rec=None, src="CLEAN"):
        if self._iff_selected() != iff:
            return
        cv = self._iff_canvas; cv.delete("all")
        if img is None:
            self._iff_info.config(text=f"{iff}\n(no decodable texture — packed/scene asset)"); return
        bg = Image.new("RGBA", img.size, (26, 26, 46, 255))
        comp = Image.alpha_composite(bg, img).convert("RGB")
        cw = max(cv.winfo_width(), 290); ch = max(cv.winfo_height(), 200)
        comp.thumbnail((cw - 12, ch - 12))
        self._iff_photo = ImageTk.PhotoImage(comp)
        cv.create_image(cw // 2, ch // 2, image=self._iff_photo)
        tag = "your edit — MODIFIED" if src == "MODIFIED" else "CLEAN"
        head = f"{iff}  ·  texture #{rec['index']}" if rec else iff
        fmt = f"  {rec['fmt']}" if rec else ""
        self._iff_info.config(text=f"{head}\n{img.width}×{img.height}{fmt}  ({tag})")

    def _open_normal_stitcher(self):
        """Open the standalone Jersey Normal Stitcher — regenerates the sewn-stripe relief in a
        uniform_base base_normal so it follows a re-striped base colour. Passes the configured
        game folder so 'Load stock from game' / 'Apply to game' work with no extra setup."""
        try:
            from launcher import normal_stitcher
            gd = None
            try:
                gd = self._get_game_root()
            except Exception:
                pass
            # Extract root (…/NHL2k10_Extracted_Files) so the tool can auto-load YOUR edited base
            # colour for the selected team+kit. Compute quietly (no warning dialogs).
            er = None
            try:
                r = self._v_root.get().strip() or self.cfg.get("root_path", "")
                if r:
                    ex = Path(r) / "NHL2k10_Extracted_Files"
                    if ex.is_dir():
                        er = str(ex)
            except Exception:
                pass
            normal_stitcher.open_stitcher(self, game_dir=gd, extract_root=er)
        except Exception as e:
            messagebox.showerror("Jersey Normal Stitcher", f"Could not open the tool:\n{e}")

    def _build_iff_ctx(self):
        """Main asset list — whole-asset actions."""
        m = Menu(self, tearoff=0)
        m.add_command(label="Extract (all textures)", command=self._iff_extract_asset)
        m.add_command(label="Apply to IFF", command=self._iff_apply_selected)
        m.add_separator()
        m.add_command(label="Extract Original Files (re-extract)", command=self._iff_revert_extract)
        m.add_command(label="Revert IFF to Original", command=self._iff_revert)
        m.add_command(label="Reveal in Extracted/", command=lambda: self._iff_reveal("Extracted"))
        self._iff_ctx = m

    def _iff_show_ctx(self, event):
        row = self._iff_tv.identify_row(event.y)
        if row:
            # keep an existing multi-selection if you right-click inside it; otherwise select this row
            if row not in self._iff_tv.selection():
                self._iff_tv.selection_set(row)
            self._iff_tv.focus(row)
            self._iff_ctx.tk_popup(event.x_root, event.y_root)

    def _build_iff_subctx(self):
        """Sub-texture list — per-texture actions (follow the <iff>/ folder structure)."""
        m = Menu(self, tearoff=0)
        m.add_command(label="Extract This Texture", command=self._iff_extract_texture)
        m.add_command(label="Extract Original (This Texture)", command=self._iff_revert_extract_one)
        self._iff_subctx = m

    def _iff_sub_show_ctx(self, event):
        row = self._iff_subtv.identify_row(event.y)
        if row:
            self._iff_subtv.selection_set(row)
            self._iff_subctx.tk_popup(event.x_root, event.y_root)

    def _iff_open(self, which: str):
        root = self._get_root()
        if not root:
            return
        iff = self._iff_selected() or ""
        folder = archtex.asset_iff(iff)
        base = archtex.extracted_root(root) / folder
        base.mkdir(parents=True, exist_ok=True)
        import os
        os.startfile(str(base))

    def _iff_extract_texture(self):
        """Extract ONLY the selected sub-texture (right-click on the texture list) -> Original/<iff>/."""
        if self._op_busy():
            return
        root = self._get_root(); iff = self._iff_selected()
        if not root or not iff:
            return
        rec = self._iff_sub_rec()
        folder = archtex.asset_iff(iff); name = archtex.texture_filename(iff, rec)
        out = archtex.extracted_root(root) / folder / name
        label = f"tex #{rec['index']}" if rec else "primary"
        self._log(f"─── Extract {iff} ({label}, from CLEAN) ───")
        def work():
            try:
                w, h, fmt = (archtex.extract_record(iff, rec, out) if rec
                             else archtex.extract_dds(iff, out))
                archtex.mark_extracted(root, out)
                self._log_q.put(f"  {iff}: {w}×{h} {fmt} → Extracted/{folder}/{name}")
            except Exception as e:
                self._log_q.put(f"  ERROR {iff}: {e}")
        self._run_in_thread(work, op_label="Extracting…", on_done=self._iff_apply_filter)

    def _iff_capture_from_game(self):
        """Capture the loader-repacked packs (global.iff, franchise.iff, …) that are loaded in
        the RUNNING game and refresh the live-offset catalog, so their textures become listable
        + extractable here. Those packs don't tabulate sub-texture offsets in the file, so the
        offsets are recovered live (see live_capture.py). Run on whatever screen is up."""
        if self._op_busy():
            return
        self._log("─── Capture textures from running game ───")
        def work():
            try:
                from launcher import live_capture as lc, xenia_mem as xm
                pid = xm.find_pid()
                if not pid:
                    self._log_q.put("  Xenia not running — launch the game first."); return
                h = xm.open_process(pid)
                phys = xm.find_phys_base(h) or xm.PHYS_BASE
                self._log_q.put(f"  attached pid={pid} — scanning loaded packs…")
                new = []
                for iff in lc.REPACKED:
                    e = lc.capture(iff, h, phys, save_png=False)
                    if e:
                        uniq = len({x['file_offset'] for x in e})
                        self._log_q.put(f"  {iff}: {len(e)} textures ({uniq} unique offsets)")
                        new += e
                xm.close_handle(h)
                if new:
                    rows = lc._merge_catalog(new)
                    archtex.reload_live_catalog()
                    self._log_q.put(f"  catalog now {len(rows)} entries — select a pack to see its textures.")
                else:
                    self._log_q.put("  none of those packs is loaded — open a menu/screen that uses them, then retry.")
            except Exception as e:
                self._log_q.put(f"  ERROR: {e}")
        self._run_in_thread(work, op_label="Capturing from game…", on_done=self._iff_apply_filter)

    def _iff_extract_asset(self):
        """Extract the selected asset(s) -> Extracted/<folder>/ (every sub-texture for a multi-texture
        .iff, or the single file for a normal asset). Multi-select extracts each in turn with a
        progress bar. The main 'Extract Selected' action."""
        if self._op_busy():
            return
        root = self._get_root(); iffs = self._iff_selected_many()
        if not root or not iffs:
            return
        multi = len(iffs) > 1
        if multi:
            self._log(f"─── Extract {len(iffs)} selected assets → Extracted/ ───")
        else:
            self._log(f"─── Extract {iffs[0]} → Extracted/{archtex.asset_iff(iffs[0])}/ ───")
        cancel = self._cancel_event; cancel.clear()
        def work():
            nfiles = nfail = 0
            for k, iff in enumerate(iffs):
                if cancel.is_set():
                    self._log_q.put("  cancelled by user."); break
                if multi:
                    self._emit_progress(k, len(iffs), f"{iff}  ({k + 1}/{len(iffs)})")
                folder = archtex.asset_iff(iff)
                out_dir = archtex.extracted_root(root) / folder
                try:
                    if archtex.list_textures(iff):              # multi-texture -> all sub-textures
                        written = archtex.extract_all_textures(iff, out_dir)
                        for r, pth in written:
                            archtex.mark_extracted(root, pth)
                            if not multi:
                                self._log_q.put(f"  tex #{r['index']}: {r['w']}×{r['h']} {r['fmt']} → {pth.name}")
                        nfiles += len(written)
                        self._log_q.put(f"  {iff}: {len(written)} textures → Extracted/{folder}/")
                    else:                                       # single / primary -> one file
                        out = out_dir / archtex.texture_filename(iff, None)
                        w, h, fmt = archtex.extract_dds(iff, out)
                        archtex.mark_extracted(root, out)
                        nfiles += 1
                        self._log_q.put(f"  {iff}: {w}×{h} {fmt} → Extracted/{folder}/{out.name}")
                except Exception as e:
                    nfail += 1
                    self._log_q.put(f"  ERROR {iff}: {e}")
            if multi:
                self._emit_progress(len(iffs), len(iffs), "done")
                self._log_q.put(f"─── Done — {nfiles} file(s) from {len(iffs)} assets"
                                + (f", {nfail} failed" if nfail else "") + ". ───")
        self._run_in_thread(work, op_label="Extracting…", on_done=self._iff_apply_filter)

    def _iff_extract_shown(self):
        """Bulk-extract every texture of every asset currently listed (respects the filter)."""
        if self._op_busy():
            return
        root = self._get_root()
        if not root:
            return
        iffs = [self._iff_tv.set(i, "iff") for i in self._iff_tv.get_children()]
        if not iffs:
            return
        if not messagebox.askyesno("Extract all shown",
                f"Extract every texture of all {len(iffs)} listed assets into the Extracted folder?\n\n"
                "Multi-texture assets (rink ≈ 50 textures each) produce many files, so this "
                "can take a while. Filter by team/category first to narrow it down. Cancel-able."):
            return
        self._log(f"─── Extract ALL SHOWN — {len(iffs)} assets → Extracted/ ───")
        cancel = self._cancel_event; cancel.clear()
        def work():
            nfiles = nfail = 0
            for k, iff in enumerate(iffs):
                if cancel.is_set():
                    self._log_q.put("  cancelled by user."); break
                self._emit_progress(k, len(iffs), f"{iff}  ({k + 1}/{len(iffs)})")
                folder = archtex.asset_iff(iff)
                out_dir = archtex.extracted_root(root) / folder
                try:
                    if archtex.list_textures(iff):                 # multi-texture / scene
                        written = archtex.extract_all_textures(iff, out_dir)
                        for _r, _pth in written:
                            archtex.mark_extracted(root, _pth)
                        nfiles += len(written)
                        self._log_q.put(f"  {iff}: {len(written)} textures")
                    else:                                          # single / primary
                        _o = out_dir / archtex.texture_filename(iff, None)
                        w, h, fmt = archtex.extract_dds(iff, _o)
                        archtex.mark_extracted(root, _o)
                        nfiles += 1
                        self._log_q.put(f"  {iff}: {w}×{h} {fmt}")
                except Exception as e:
                    nfail += 1
                    self._log_q.put(f"  ERROR {iff}: {e}")
            self._emit_progress(len(iffs), len(iffs), "done")
            self._log_q.put(f"─── Done — {nfiles} files extracted"
                            + (f", {nfail} failed" if nfail else "") + ". ───")
        self._run_in_thread(work, op_label="Extracting all shown…", on_done=self._iff_apply_filter)

    def _iff_revert_extract(self):
        """Re-extract the selected asset from the CLEAN game files back into Extracted/ (undo your
        edits for it) and refresh its extract hash so it counts as unedited again."""
        if self._op_busy():
            return
        root = self._get_root(); iff = self._iff_selected()
        if not root or not iff:
            return
        if not messagebox.askyesno("Extract Original Files",
                f"Re-extract {iff} from the clean game files into Extracted/, discarding your edits "
                f"to it?\n\n(This only touches the Extracted/ file — it does not change the game "
                f"archives; use 'Revert IFF to Original' for that.)"):
            return
        self._log(f"─── Revert {iff} → clean into Extracted/ ───")
        def work():
            try:
                recs = archtex.list_textures(iff)
                n = archtex.revert_extract(root, iff, recs, log=self._log_q.put)
                self._log_q.put(f"  {iff}: {n} file(s) restored from clean")
            except Exception as e:
                self._log_q.put(f"  ERROR reverting {iff}: {e}")
        self._run_in_thread(work, op_label="Reverting extract…", on_done=self._iff_apply_filter)

    def _iff_revert_extract_one(self):
        """Re-extract ONLY the selected sub-texture from clean into Extracted/ (undo its edit)."""
        if self._op_busy():
            return
        root = self._get_root(); iff = self._iff_selected()
        if not root or not iff:
            return
        rec = self._iff_sub_rec()
        folder = archtex.asset_iff(iff); name = archtex.texture_filename(iff, rec)
        out = archtex.extracted_root(root) / folder / name
        self._log(f"─── Revert {iff} ({name}) → clean ───")
        def work():
            try:
                if rec:
                    archtex.extract_record(iff, rec, out)
                else:
                    archtex.extract_dds(iff, out)
                archtex.mark_extracted(root, out)
                self._log_q.put(f"  {iff}: {name} restored from clean")
            except Exception as e:
                self._log_q.put(f"  ERROR: {e}")
        self._run_in_thread(work, op_label="Reverting…", on_done=self._iff_apply_filter)

    def _iff_apply(self):
        if self._op_busy():
            return
        root = self._get_root(); iff = self._iff_selected()
        if not root or not iff:
            return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Game folder", "Set the game files folder (with 0A/0B/1A/1B) in Settings."); return
        rec = self._iff_sub_rec()
        if rec and rec["fmt"] not in archtex.REPLACE_FORMATS:
            messagebox.showerror("Unsupported format",
                f"Texture #{rec['index']} is {rec['fmt']} — replace doesn't support that "
                "format yet."); return
        folder = archtex.asset_iff(iff); name = archtex.texture_filename(iff, rec)
        edited = archtex.find_any_edit(root, iff, rec)
        if not edited:
            messagebox.showinfo("Nothing to apply",
                f"No extracted/edited {name} for {iff}. Extract the asset, edit the DDS/PNG in "
                f"Extracted/{folder}/, then Apply."); return
        # Auto Hi-Res: for scene/UI logos (titlepage_*), if the imported source is LARGER than the
        # native slot, store it at that higher resolution — the fetch constant is recomputed for any
        # size (capped at 2048/dim). A native-or-smaller source stays native. (Multi-texture records
        # already store lossless 8888 via smart_replace_record.)
        hires_scale = 0
        if not rec and iff in archtex.SCENE_ASSETS:
            ow, oh = archtex.SCENE_ASSETS[iff][2], archtex.SCENE_ASSETS[iff][3]
            if ow and oh:
                try:
                    sw, sh = Image.open(edited).size
                except Exception:
                    sw, sh = ow, oh
                hires_scale = max(1, min(8, 2048 // ow, 2048 // oh, min(sw // ow, sh // oh)))
        label = f"texture #{rec['index']}" if rec else "primary texture"
        if hires_scale >= 2:
            ow, oh = archtex.SCENE_ASSETS[iff][2], archtex.SCENE_ASSETS[iff][3]
            label += f"  [Hi-Res {ow*hires_scale}×{oh*hires_scale}]"
        if not messagebox.askyesno("Apply permanently",
                f"Permanently splice your edited {iff} ({label}) into the game archives at:\n"
                f"{game_dir}\n\nA one-time .orig backup is made. Continue?"):
            return
        self._log(f"─── Apply {iff} ({label}) → game files (PERMANENT) ───")
        def work():
            try:
                # Always apply from a FRESH clean base — reset first so edits never stack on a previous
                # apply (stacking corrupts textures: a magenta test came out WHITE + format drifted
                # DXT->8888). overlay_static.iff shares its file with the Scoreclock tab (layout in
                # blob0), so SNAPSHOT that layout, reset, apply textures fresh (native format), then
                # RESTORE the layout.
                is_overlay = iff.lower() == "overlay_static.iff"
                snap = shadow = teal = None
                if is_overlay:
                    try:
                        snap = self._sblay.capture_snapshot(game_dir)
                        shadow = self._sblay.scorebug_logo_hidden(game_dir)
                        teal = self._sblay.teal_bar_hidden(game_dir)
                    except Exception as ce:
                        self._log_q.put(f"  (layout snapshot skipped: {ce})")
                self._emit_progressf(0.2, f"{iff}: resetting to clean")
                archtex.ensure_clean(iff, game_dir, self._log_q.put)
                pl = False if is_overlay else self._v_iff_lossless.get()
                self._emit_progressf(0.4, f"{iff}: encoding {label}…")
                if hires_scale >= 2:
                    status = archtex.replace_scene_hires(iff, edited, game_dir,
                                                         hires_scale, self._log_q.put)
                elif rec:
                    # Splice the FULL current mod-set of this iff in one pass (from clean) so applying
                    # one texture never drops the others you'd previously applied.
                    _recs, edits = self._iff_all_edits(iff, root)
                    if edits:
                        status = archtex.replace_many(iff, edits, game_dir, self._log_q.put,
                                                      prefer_lossless=pl)
                    else:
                        status = archtex.smart_replace_record(
                            iff, rec, edited, game_dir, self._log_q.put, prefer_lossless=pl)
                else:
                    status = archtex.replace(iff, edited, game_dir, self._log_q.put, prefer_lossless=pl)
                self._log_q.put(f"  {status}")
                if is_overlay and snap:
                    try:
                        led = self._sblay.snapshot_edits(snap, game_dir)
                        if led:
                            self._sblay.apply_edits(led, game_dir, self._log_q.put)
                        if shadow:
                            self._sblay.set_scorebug_logo_hidden(True, game_dir, self._log_q.put)
                        if teal:
                            self._sblay.set_teal_bar_hidden(True, game_dir, self._log_q.put)
                        self._log_q.put(f"  restored scoreclock layout ({len(led)} element edit(s))")
                    except Exception as re:
                        self._log_q.put(f"  (WARNING: layout restore failed — {re})")
                self._emit_progressf(0.75, f"{iff}: syncing front-end copy")
                self._iff_mirror_jersey(iff, [(rec["index"] if rec else 0, edited)], game_dir)
                self._emit_progressf(0.9, "compacting archives")
                try:                                            # reclaim orphans from the reset+apply
                    self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
                except Exception as ce:
                    self._log_q.put(f"  (compact skipped: {ce})")
                self._emit_progressf(1.0, f"{iff}: done")
                self._log_q.put("─── Done. Restart the game to see it. ───")
            except Exception as e:
                self._log_q.put(f"  ERROR {iff}: {e}")
        self._run_in_thread(work, op_label="Applying to game…",
                            on_done=lambda: (self._iff_apply_filter(), self._prompt_restart_if_running()))

    def _iff_mirror_jersey(self, iff, applied, game_dir):
        """Mirror an edit onto this jersey's other copy — the front-end team-select sheet.

        A jersey lives in the archives TWICE (in-game uniform + front-end sheet, whose `decals` is
        a byte-identical copy of the uniform's `stamps`), so writing one copy alone leaves the game
        and the team-select screen disagreeing — that's the two-pass problem this removes.

        `applied` = [(texture_index, edited_path)] just written to `iff`. Only textures 0/1
        (stamps<->decals, normal<->decals_normal) have a counterpart; helmet/letters/crowd exist on
        the uniform only. Uses replace_many so a twin is re-encoded ONCE even when both mirror.
        Failures are logged, never fatal — the primary asset is already written.

        MUST be called from every apply path. It originally hung off the single-texture apply only,
        so 'Apply to IFF' (the button you actually use for a 6-texture uniform) silently skipped the
        mirror and the front-end never updated.
        """
        try:
            from launcher import jerseys
        except Exception:
            return
        tw = jerseys.twins(iff)
        if not tw:
            return
        mirror = [(i, p) for i, p in applied if i in jerseys.MIRRORED_INDICES]
        if not mirror:
            self._log_q.put(f"  (texture(s) {sorted({i for i, _ in applied})} have no front-end "
                            f"counterpart — nothing to mirror)")
            return
        self._log_q.put(f"─── Mirroring texture(s) {sorted(i for i, _ in mirror)} → "
                        f"{', '.join(tw)} ───")
        for twin in tw:
            try:
                trecs = archtex.list_textures(twin)
                edits = [{**trecs[i], "path": str(p)} for i, p in mirror if i < len(trecs)]
                if not edits:
                    self._log_q.put(f"  SKIP {twin}: no matching texture"); continue
                archtex.ensure_clean(twin, game_dir, self._log_q.put)
                st = archtex.replace_many(twin, edits, game_dir, self._log_q.put,
                                          prefer_lossless=self._v_iff_lossless.get())
                self._log_q.put(f"  {twin}: {st}")
            except Exception as e:
                self._log_q.put(f"  ERROR mirroring to {twin}: {e}")

    def _iff_all_edits(self, iff, root):
        """(recs, edits) — every Modified texture of `iff` as a replace_many edit list."""
        recs = archtex.list_textures(iff)
        if not recs:
            return None, []
        edits = []
        for rec in recs:
            p = archtex.find_any_edit(root, iff, rec)
            if p and rec["fmt"] in archtex.REPLACE_FORMATS:
                edits.append({**rec, "path": str(p)})
        return recs, edits

    def _iff_revert(self):
        """Revert the selected iff in the GAME archives back to its pristine original (repoint TOC
        to the untouched 0A/0B copy), then reclaim the freed space."""
        if self._op_busy():
            return
        iff = self._iff_selected()
        game_dir = self._get_game_root()
        if not iff or not game_dir:
            messagebox.showerror("Revert", "Select an asset and set the game folder first."); return
        if not messagebox.askyesno("Revert IFF to Original",
                f"Revert {iff} in the game archives back to its ORIGINAL (undo your applied "
                f"changes for this asset)?\n\nYour Extracted/ source files are not touched."):
            return
        self._log(f"─── Revert {iff} → original ───")
        def work():
            try:
                # ensure_clean repoints this asset back to its pristine CLEAN copy (undoes every applied
                # edit). NOTE: archive_textures has no restore_from_clean — that name lived in
                # overlay_editor and used a stale CLEAN_DIR attribute (hence the AttributeError);
                # ensure_clean is the correct, current revert path.
                if archtex.ensure_clean(iff, game_dir, self._log_q.put):
                    self._log_q.put(f"  {iff} reverted to its original textures"
                                    + (" + scoreclock layout" if iff.lower() == "overlay_static.iff" else ""))
                else:
                    self._log_q.put(f"  {iff} was already original — nothing to undo")
                self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
                self._log_q.put("─── Reverted. Restart the game to see it. ───")
            except Exception as e:
                self._log_q.put(f"  ERROR reverting {iff}: {e}")
        self._run_in_thread(work, op_label="Reverting…", on_done=self._iff_apply_filter)

    def _iff_compact(self):
        """Manually compact archive 1B — drop orphaned/dead space from past relocations."""
        if self._op_busy():
            return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Compact", "Set the game folder first."); return
        self._log("─── Compact archives (reclaim dead space) ───")
        def work():
            try:
                self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
                self._log_q.put("─── Done. ───")
            except Exception as e:
                self._log_q.put(f"  ERROR compacting: {e}")
        self._run_in_thread(work, op_label="Compacting…")

    def _iff_apply_selected(self):
        """Route 'Apply to IFF': one asset -> the single-asset apply (handles hires/overlay); many
        selected assets -> a batched apply that repacks each in turn with a per-asset progress bar."""
        iffs = self._iff_selected_many()
        if len(iffs) > 1:
            self._iff_apply_batch(iffs)
        else:
            self._iff_apply_all()

    def _iff_apply_all_work(self, iff, edits, game_dir, compact=True, progress=None):
        """Apply every edited texture of ONE multi-texture asset from a clean base: preserve the
        scoreclock layout for overlay_static.iff, mirror jersey twins to the front-end copy, and
        (optionally) compact. Shared by the single 'Apply to IFF' and the multi-select batch so both
        take the IDENTICAL path. `progress` (optional) = fn(frac 0..1, note) for a stage-level bar."""
        def prog(f, note):
            if progress:
                progress(f, note)
        # overlay_static.iff is SHARED with the Scoreclock tab (its per-element layout lives in blob0
        # of the SAME file). Texture replacement is only reliable from a CLEAN base — stacking a new
        # edit on a prior apply corrupts it — so ALWAYS reset to clean, but PRESERVE the scoreclock
        # layout across it: snapshot, ensure_clean, apply fresh, then re-apply the layout.
        is_overlay = iff.lower() == "overlay_static.iff"
        snap = shadow = teal = None
        if is_overlay:
            try:
                snap = self._sblay.capture_snapshot(game_dir)
                shadow = self._sblay.scorebug_logo_hidden(game_dir)
                teal = self._sblay.teal_bar_hidden(game_dir)
                self._log_q.put(f"  snapshotted scoreclock layout ({len(snap)} elements) to restore after")
            except Exception as ce:
                self._log_q.put(f"  (layout snapshot skipped: {ce})")
        prog(0.15, f"{iff}: resetting to clean")
        archtex.ensure_clean(iff, game_dir, self._log_q.put)      # ALWAYS a clean base
        prog(0.35, f"{iff}: encoding {len(edits)} texture(s)…")
        status = archtex.replace_many(iff, edits, game_dir, self._log_q.put,
                                      prefer_lossless=(False if is_overlay else self._v_iff_lossless.get()))
        self._log_q.put(f"  {status}")
        if is_overlay and snap:
            try:
                led = self._sblay.snapshot_edits(snap, game_dir)
                if led:
                    self._sblay.apply_edits(led, game_dir, self._log_q.put)
                if shadow:
                    self._sblay.set_scorebug_logo_hidden(True, game_dir, self._log_q.put)
                if teal:
                    self._sblay.set_teal_bar_hidden(True, game_dir, self._log_q.put)
                self._log_q.put(f"  restored scoreclock layout ({len(led)} element edit(s)"
                                f"{', 2K hidden' if shadow else ''}{', teal hidden' if teal else ''})")
            except Exception as re:
                self._log_q.put(f"  (WARNING: layout restore failed — {re})")
        prog(0.75, f"{iff}: syncing front-end copy")
        self._iff_mirror_jersey(iff, [(e["index"], e["path"]) for e in edits], game_dir)
        if compact:
            prog(0.9, "compacting archives")
            try:                                            # reclaim orphans from reset+apply
                self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
            except Exception as ce:
                self._log_q.put(f"  (compact skipped: {ce})")
        prog(1.0, f"{iff}: done")
        return status

    def _iff_apply_batch(self, iffs):
        """Apply the edited textures of MANY selected assets in one run (repack multiple at once),
        with a per-asset % bar. Each asset resets-to-clean then splices; compact once at the end."""
        if self._op_busy():
            return
        root = self._get_root()
        if not root:
            return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Game folder",
                "Set the game files folder (with 0A/0B/1A/1B) in Settings."); return
        multi_jobs = []; prim_jobs = []; n_tex = 0; skipped = 0
        for iff in iffs:
            try:
                recs = archtex.list_textures(iff)
            except Exception:
                recs = []
            if recs:                                        # multi-texture pack -> gather its edits
                edits = []
                for rec in recs:
                    p = archtex.find_any_edit(root, iff, rec)
                    if not p:
                        continue
                    if rec["fmt"] not in archtex.REPLACE_FORMATS:
                        skipped += 1; continue
                    edits.append({**rec, "path": str(p)})
                if edits:
                    multi_jobs.append((iff, edits)); n_tex += len(edits)
            else:                                           # single / primary asset (logo, mask, …)
                p = archtex.find_any_edit(root, iff, None)
                if p:
                    prim_jobs.append((iff, str(p))); n_tex += 1
        njobs = len(multi_jobs) + len(prim_jobs)
        if not njobs:
            messagebox.showinfo("Nothing to apply",
                f"None of the {len(iffs)} selected assets have extracted/edited files.\n\n"
                "Use “Extract Selected” first, then edit the DDS/PNG and Apply to IFF."); return
        msg = f"Apply {n_tex} texture(s) across {njobs} selected asset(s) into the game archives?"
        if skipped:
            msg += f"\n\n({skipped} skipped — unsupported format.)"
        msg += "\n\nA one-time .orig backup is made. Continue?"
        if not messagebox.askyesno("Apply to IFF (multiple)", msg):
            return
        self._log(f"─── Apply {njobs} selected assets ({n_tex} texture(s)) → game files (PERMANENT) ───")
        total = njobs + 1                                   # +1 = the single compact step at the end
        cancel = self._cancel_event; cancel.clear()
        def work():
            step = 0
            for iff, edits in multi_jobs:
                if cancel.is_set():
                    self._log_q.put("  cancelled by user."); break
                self._emit_progress(step, total, f"{iff}  ({len(edits)} texture(s))")
                try:
                    self._iff_apply_all_work(iff, edits, game_dir, compact=False)
                except Exception as e:
                    self._log_q.put(f"  ERROR {iff}: {e}")
                step += 1
            for iff, p in prim_jobs:
                if cancel.is_set():
                    self._log_q.put("  cancelled by user."); break
                self._emit_progress(step, total, f"{iff}")
                try:
                    archtex.ensure_clean(iff, game_dir, self._log_q.put)
                    status = archtex.replace(iff, p, game_dir, self._log_q.put,
                                             prefer_lossless=self._v_iff_lossless.get())
                    self._log_q.put(f"  {iff}: {status}")
                    self._iff_mirror_jersey(iff, [(0, p)], game_dir)
                except Exception as e:
                    self._log_q.put(f"  ERROR {iff}: {e}")
                step += 1
            self._emit_progress(step, total, "compacting archives")
            try:
                self._log_q.put(f"  {archtex.compact_1b(game_dir, self._log_q.put)}")
            except Exception as ce:
                self._log_q.put(f"  (compact skipped: {ce})")
            self._emit_progress(total, total, "done")
            self._log_q.put("─── Apply done. Restart the game to see changes. ───")
        self._run_in_thread(work, op_label=f"Applying {njobs} assets…",
                            on_done=lambda: (self._iff_apply_filter(), self._prompt_restart_if_running()))

    def _iff_apply_all(self):
        """Apply to IFF — splice EVERY edited texture of the selected asset into the game in ONE
        re-encode (one decompress + one re-compress; much faster than applying each individually).
        A single-texture asset falls through to the primary-apply path. (To apply just one texture
        of a multi-texture pack, right-click it in the texture list on the right.)"""
        if self._op_busy():
            return
        root = self._get_root(); iff = self._iff_selected()
        if not root or not iff:
            return
        game_dir = self._get_game_root()
        if not game_dir:
            messagebox.showerror("Game folder",
                "Set the game files folder (with 0A/0B/1A/1B) in Settings."); return
        recs = archtex.list_textures(iff)
        if not recs:
            self._iff_apply(); return   # single-texture asset — apply its one (primary) texture
        folder = archtex.asset_iff(iff)
        # ALWAYS apply whatever DDS/PNG files are sitting in the Extracted/ folder — the folder is the
        # source of truth. We deliberately do NOT gate on is_edited() (SHA-1 vs an extract-time hash):
        # that silently skipped files after a folder delete + re-extract, so "Apply to IFF" reported
        # "no files changed" even though the folder had real edits. Files identical to the archive just
        # re-encode to the same bytes (harmless); genuinely-changed ones get pushed in.
        edits = []; skipped = 0
        for rec in recs:
            p = archtex.find_any_edit(root, iff, rec)
            if not p:
                continue                              # this slot isn't extracted -> nothing to push
            if rec["fmt"] not in archtex.REPLACE_FORMATS:
                skipped += 1; continue
            edits.append({**rec, "path": str(p)})
        if not edits:
            messagebox.showinfo("Nothing to apply",
                f"No extracted texture files found in Extracted/{folder}/.\n\nUse “Extract Selected” "
                f"first, then edit the DDS/PNG in that folder and Apply to IFF."); return
        msg = f"Apply {len(edits)} texture(s) from Extracted/{folder}/ into the game archives in ONE pass?"
        if skipped:
            msg += f"\n\n({skipped} skipped — unsupported format.)"
        msg += "\n\nA one-time .orig backup is made. Continue?"
        if not messagebox.askyesno("Apply to IFF", msg):
            return
        self._log(f"─── Apply ALL ({len(edits)}) {iff} → game files (PERMANENT) ───")
        def work():
            try:
                self._iff_apply_all_work(iff, edits, game_dir, compact=True,
                                         progress=self._emit_progressf)
                self._log_q.put("─── Done. Restart the game to see it. ───")
            except Exception as e:
                self._log_q.put(f"  ERROR {iff}: {e}")
        self._run_in_thread(work, op_label=f"Applying {len(edits)} textures…",
                            on_done=lambda: (self._iff_apply_filter(), self._prompt_restart_if_running()))

    def _iff_reveal(self, which: str):
        root = self._get_root(); iff = self._iff_selected()
        if not root or not iff:
            return
        base = archtex.extracted_root(root) / archtex.asset_iff(iff)
        base.mkdir(parents=True, exist_ok=True)
        import os
        os.startfile(str(base))

    # ── Teams tab (Roster.ROS display-name editor) ────────────────────────────
    # Team + Name = the full team name ("New Jersey" + "Devils"); City/State are where the team
    # physically plays (Newark, New Jersey) and are NOT the same thing. Only what the save actually
    # stores is shown — no synthesised "display name" (that's what produced "Glendale Coyotes").
    _ROSTER_COLS = ("code", "city", "state", "team", "name", "arena", "primary", "secondary")
    _ROSTER_EDITABLE = {"city", "state", "team", "name", "arena"}
    _ROSTER_COLOR_COLS = {"primary", "secondary"}   # double-click -> colour picker
    _ROSTER_SLOT = {"city": "city", "state": "state", "team": "team",
                    "name": "nickname", "arena": "arena"}          # column -> Team attribute

    def _build_teams_tab(self):
        t = self._tab_teams
        bar = ttk.Frame(t, padding=(4, 4, 4, 2)); bar.pack(fill=X)
        ttk.Label(bar, text="Roster.ROS:").pack(side=LEFT)
        # No baked-in default: it only ever pointed at one machine. Found under the Xenia folder as
        # content\<profile>\54540853\00000001\Roster.ROS\Roster.ROS — auto-discovered when the Xenia
        # path is configured, else the user browses to it once and it's remembered.
        default_ros = self.cfg.get("roster_path") or self._discover_roster() or ""
        self._v_roster = StringVar(value=default_ros)
        ttk.Entry(bar, textvariable=self._v_roster, width=58).pack(side=LEFT, padx=(4, 2))
        ttk.Button(bar, text="Browse…", command=self._teams_browse).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Load",    command=self._teams_load).pack(side=LEFT, padx=2)
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        ttk.Button(bar, text="Save to Roster.ROS", style="Accent.TButton",
                   command=self._teams_save).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Export CSV…", command=self._teams_export).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Import CSV…", command=self._teams_import).pack(side=LEFT, padx=2)
        ttk.Separator(bar, orient=VERTICAL).pack(side=LEFT, fill=Y, padx=6)
        ttk.Button(bar, text="Team Record Fields…",
                   command=self._teams_fields_editor).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Advanced Editor (all tables & fields)…",
                   command=self._teams_advanced_editor).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Live Roster Editor (running game)…",
                   command=self._teams_live_editor).pack(side=LEFT, padx=2)
        ttk.Button(bar, text="Team Rosters (running game)…",
                   command=self._teams_roster_editor).pack(side=LEFT, padx=2)

        ttk.Label(t, foreground="#999", font=("Segoe UI", 8), justify=LEFT,
                  text="Team + Name is the full team name (\"New Jersey\" + \"Devils\"); City/State is where "
                       "it plays (Newark, New Jersey) — not the same thing.  \"—\" means the save stores "
                       "nothing for that field (Dallas has no city; only Carolina/Phoenix/Tampa Bay store a "
                       "Team prefix — the game composes the rest itself).\n"
                       "Double-click City / State / Team / Name / Arena to rename — a name must fit its "
                       "existing slot; longer is rejected (growing needs the string pool repointed).  "
                       "Double-click Primary / Secondary for a colour picker — those save straight to the "
                       "Roster.ROS (a .colorbak is made) and show after a game restart.").pack(fill=X, padx=6)

        tv = ttk.Treeview(t, columns=self._ROSTER_COLS, show="headings", height=20)
        for c, w in (("code", 52), ("city", 118), ("state", 128), ("team", 100), ("name", 118),
                     ("arena", 190), ("primary", 88), ("secondary", 88)):
            tv.heading(c, text={"name": "Name", "team": "Team"}.get(c, c.title()))
            tv.column(c, width=w, anchor=W)
        sb = ttk.Scrollbar(t, command=tv.yview); tv.configure(yscrollcommand=sb.set)
        sb.pack(side=RIGHT, fill=Y)
        tv.pack(fill=BOTH, expand=True, padx=6, pady=4)
        tv.bind("<Double-1>", self._teams_edit_cell)
        self._teams_tv = tv

    def _teams_live_editor(self):
        """Open the live roster editor — edits the RUNNING game's player records with labeled fields."""
        try:
            from launcher import ros_live_editor
            ros_live_editor.open_live_editor(self)
        except Exception as e:
            messagebox.showerror("Live Roster Editor", f"Could not open the editor:\n{e}")

    def _teams_roster_editor(self):
        """Open the team-roster editor — move players between teams in the RUNNING game."""
        try:
            from launcher import ros_live_editor
            ros_live_editor.open_team_editor(self)
        except Exception as e:
            messagebox.showerror("Team Rosters", f"Could not open the editor:\n{e}")

    def _teams_fields_editor(self):
        """Open the team-record field grid — every editable field of the 412-byte team record,
        labelled from team_fields.json (rename unidentified_N there once you've identified one)."""
        path = self._v_roster.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showerror("Team Fields", "Set a valid Roster.ROS path first (Browse…)."); return
        try:
            from launcher import team_fields_gui
            team_fields_gui.open_editor(self, path)
        except Exception as e:
            messagebox.showerror("Team Fields", f"Could not open the editor:\n{e}")

    def _teams_advanced_editor(self):
        """Open the full ROS table/field editor (every chunk as a grid) on the current roster."""
        path = self._v_roster.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showerror("ROS Editor", "Set a valid Roster.ROS path first (Browse…)."); return
        try:
            from launcher import ros_editor_gui
            ros_editor_gui.open_editor(self, path)
        except Exception as e:
            messagebox.showerror("ROS Editor", f"Could not open the editor:\n{e}")

    def _discover_roster(self) -> str:
        """Find the game's Roster.ROS under the configured Xenia folder, or "" if not found.

        Layout is <xenia>/content/<profile>/54540853/00000001/Roster.ROS/Roster.ROS — the profile id
        differs per install (canary uses e.g. B13EBABEBABEBABE, older/stable builds have no profile
        level at all), so both shapes are globbed rather than assumed. 54540853 is NHL 2K10's title id.
        """
        xp = (self.cfg.get("xenia_path") or "").strip()
        if not xp:
            return ""
        base = Path(xp)
        base = base.parent if base.is_file() or base.suffix.lower() == ".exe" else base
        for pat in ("content/*/54540853/00000001/Roster.ROS/Roster.ROS",
                    "content/54540853/00000001/Roster.ROS/Roster.ROS"):
            try:
                hits = sorted(p for p in base.glob(pat) if p.is_file())
            except Exception:
                continue
            if hits:
                return str(hits[0])
        return ""

    def _teams_browse(self):
        p = filedialog.askopenfilename(title="Select Roster.ROS",
                                       filetypes=[("Roster save", "*.ROS"), ("All files", "*.*")])
        if p:
            self._v_roster.set(p); self._teams_load()

    def _teams_load(self):
        p = self._v_roster.get().strip()
        if not p or not Path(p).is_file():
            messagebox.showerror("Roster", "Set a valid Roster.ROS path first."); return
        try:
            ed = rost.RosterEditor(p)
        except Exception as e:
            messagebox.showerror("Roster", f"Could not parse:\n{e}"); return
        try:
            colors = tcol.load(p)
        except Exception as e:
            colors = {}; self._log(f"[teams] team colours unavailable: {e}")
        tv = self._teams_tv; tv.delete(*tv.get_children())
        for tm in ed.teams:
            tc = colors.get(tm.code.upper(), {})
            pri, sec = tc.get("primary"), tc.get("secondary")
            txt = lambda s: s.text if s else "—"          # "—" = not stored in the save at all
            tv.insert("", END, values=(tm.code, txt(tm.city), txt(tm.state), txt(tm.team),
                                       txt(tm.nickname), txt(tm.arena),
                                       ("#%02X%02X%02X" % pri) if pri else "—",
                                       ("#%02X%02X%02X" % sec) if sec else "—"))
        self.cfg["roster_path"] = p; save_config(self.cfg)
        self._log(f"[teams] loaded {len(ed.teams)} teams from {Path(p).name}"
                  + (f" (+colours for {len(colors)})" if colors else " (no colour map)"))

    def _teams_edit_cell(self, event):
        tv = self._teams_tv
        if tv.identify("region", event.x, event.y) != "cell":
            return
        col = tv.identify_column(event.x); row = tv.identify_row(event.y)
        if not row:
            return
        cname = self._ROSTER_COLS[int(col[1:]) - 1]
        if cname in self._ROSTER_COLOR_COLS:
            self._teams_pick_color(row, cname); return
        if cname not in self._ROSTER_EDITABLE:
            return
        if tv.set(row, cname) == "—":
            # "—" means the save has no string for this field (e.g. Dallas ships with no city,
            # the Rangers with neither city nor state, and only Carolina/Phoenix/Tampa Bay store a
            # Team prefix). There is no slot to write into, and adding one would need the string
            # pool relocated + repointed — see the module docstring.
            messagebox.showinfo("Teams",
                f"This roster stores no '{cname}' for {tv.set(row,'code')}, so there's nothing to "
                "edit. Adding a string the game never had isn't supported (it needs the string pool "
                "relocated and its reference repointed).")
            return
        x, y, w, h = tv.bbox(row, col)
        e = Entry(tv); e.place(x=x, y=y, width=w, height=h)
        e.insert(0, tv.set(row, cname)); e.focus_set(); e.select_range(0, END)

        def commit(_=None):
            tv.set(row, cname, e.get())
            e.destroy()
        e.bind("<Return>", commit); e.bind("<FocusOut>", commit)
        e.bind("<Escape>", lambda _: e.destroy())

    def _teams_pick_color(self, row, cname):
        """Set a team's Primary/Secondary — paste a hex or use the picker. Writes to the Roster.ROS."""
        from launcher import colorpick
        tv = self._teams_tv
        code = tv.set(row, "code")
        cur = tv.set(row, cname)
        init = cur if cur.startswith("#") else "#808080"
        hx = colorpick.ask_color(self, init, f"{code} — {cname} colour")
        if not hx:
            return
        rgb3 = colorpick.to_rgb(hx)
        path = self._v_roster.get().strip()
        if not path or not Path(path).is_file():
            messagebox.showerror("Team Colour", "Set a valid Roster.ROS path first (Browse…)."); return
        try:
            kw = {"primary": rgb3} if cname == "primary" else {"secondary": rgb3}
            rec = tcol.set_color(path, code, log=self._log, **kw)
        except KeyError:
            messagebox.showerror("Team Colour",
                f"'{code}' isn't in this roster's team list, so it has no colour record."); return
        except Exception as e:
            messagebox.showerror("Team Colour", f"Could not write the colour:\n{e}"); return
        tv.set(row, cname, hx.upper())
        self._log(f"[teams] {code} {cname} → {hx.upper()} (record {rec}). Restart the game to see it.")
        self._prompt_restart_if_running()

    def _teams_save(self):
        p = self._v_roster.get().strip()
        if not p or not Path(p).is_file():
            messagebox.showerror("Roster", "Set a valid Roster.ROS path first."); return
        try:
            ed = rost.RosterEditor(p)
        except Exception as e:
            messagebox.showerror("Roster", f"Could not parse:\n{e}"); return
        # gather ALL edits first, then apply in one batch — a longer name shifts the pool
        # tail, so per-field calls would write later strings at stale offsets.
        by = {tm.code: tm for tm in ed.teams}
        edits, n = [], 0
        for row in self._teams_tv.get_children():
            tm = by.get(self._teams_tv.set(row, "code"))
            if not tm:
                continue
            for field in ("city", "state", "team", "arena"):
                slot = getattr(tm, self._ROSTER_SLOT[field])
                nv = self._teams_tv.set(row, field).strip()
                if slot and nv and nv != "—" and nv != slot.text:
                    edits.append((slot, nv)); n += 1
            nv = self._teams_tv.set(row, "name").strip()
            if tm.nickname and nv and nv != "—" and nv != tm.nickname.text:
                edits.append((tm.nickname, nv)); n += 1
                if tm.nick_lower:                          # keep the internal lowercase key in sync
                    edits.append((tm.nick_lower, nv.lower().replace(" ", "")))
        if n == 0:
            messagebox.showinfo("Teams", "No changes to apply."); return
        try:
            ed.apply_edits(edits)          # raises ValueError if the grow exceeds the free budget
            ed.save()                      # writes <ROS>.bak then patches
        except ValueError as e:
            messagebox.showerror("Names too long", str(e)); return
        except Exception as e:
            messagebox.showerror("Roster", f"Write failed:\n{e}"); return
        self._log(f"[teams] applied {n} change(s) to {Path(p).name} (backup: {Path(p).name}.bak)")
        messagebox.showinfo("Saved", f"Applied {n} change(s).\nBackup: {Path(p).name}.bak")
        self._teams_load()                 # refresh the tree from the patched file

    def _teams_export(self):
        p = self._v_roster.get().strip()
        if not p or not Path(p).is_file():
            messagebox.showerror("Roster", "Set a valid Roster.ROS path first."); return
        out = filedialog.asksaveasfilename(title="Export team CSV", defaultextension=".csv",
                                           initialfile="team_info_editable.csv",
                                           filetypes=[("CSV", "*.csv")])
        if not out:
            return
        try:
            rost.RosterEditor(p).export_csv(out)
            self._log(f"[teams] exported CSV -> {out}")
            messagebox.showinfo("Exported", f"Wrote {out}")
        except Exception as e:
            messagebox.showerror("Export", str(e))

    def _teams_import(self):
        p = self._v_roster.get().strip()
        if not p or not Path(p).is_file():
            messagebox.showerror("Roster", "Set a valid Roster.ROS path first."); return
        csvp = filedialog.askopenfilename(title="Import team CSV", filetypes=[("CSV", "*.csv")])
        if not csvp:
            return
        try:
            ed = rost.RosterEditor(p)
            ch = ed.apply_csv(csvp)
        except ValueError as e:
            messagebox.showerror("Name too long", str(e)); return
        except Exception as e:
            messagebox.showerror("Import", str(e)); return
        if ch:
            ed.save()
            self._log(f"[teams] CSV applied {len(ch)} change(s) (backup written)")
            messagebox.showinfo("Imported", f"Applied {len(ch)} change(s).\nBackup: {Path(p).name}.bak")
            self._teams_load()
        else:
            messagebox.showinfo("Teams", "No changes in CSV.")

    def _log(self, text: str):
        # __dict__ lookup, NOT getattr: this class extends tkinter.Tk, whose __getattr__ forwards
        # unknown names to self.tk — so a plain getattr here depends on that delegation raising
        # cleanly. Reading __dict__ can't be surprised by it.
        box = self.__dict__.get("_log_box")
        if box is None:
            # _build_ui() builds every TAB before it builds the log box, so anything logged during
            # a tab's construction (e.g. the IFF catalog load) used to raise AttributeError and
            # kill the app at startup. Worse, the catalog loader's own `except` also logs — so the
            # crash replaced whatever the real error was. Buffer here; _build_ui flushes below.
            self.__dict__.setdefault("_log_early", []).append(text)
            return
        box.config(state=NORMAL)
        box.insert(END, text + "\n")
        box.see(END)
        box.config(state=DISABLED)

    def _flush_early_log(self):
        """Emit anything logged before the log box existed (see _log)."""
        pending = self.__dict__.pop("_log_early", [])
        for m in pending:
            self._log(m)

    def _clear_log(self):
        self._log_box.config(state=NORMAL)
        self._log_box.delete("1.0", END)
        self._log_box.config(state=DISABLED)

    # ── Close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        n_names = len(self._pending_name_changes)
        n_rates = len(self._pending_rate_changes)
        if n_names or n_rates:
            bits = []
            if n_names: bits.append(f"{n_names} name/category change(s)")
            if n_rates: bits.append(f"{n_rates} sample-rate change(s)")
            ans = messagebox.askyesnocancel(
                "Unsaved Changes",
                f"You have staged {', '.join(bits)}.\n\n"
                "Save name/category edits before closing?\n"
                "(Staged sample-rate changes are only applied by Apply Changes — "
                "they'll be discarded.)", icon="warning")
            if ans is None: return
            if ans: self._flush_pending_names()
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
