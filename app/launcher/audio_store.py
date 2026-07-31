"""On-disk store for extracted audio — the single source of truth for what is extracted,
where it lives, and whether the user has changed it.

Layout (mirrors Textures/, which moved to this shape first):

    NHL2k10_Extracted_Files/
        Audio/
            Extracted/
                <Category>/<name>.wav          <- edit these IN PLACE
                .extract_manifest.json
            Audio_Names.json

There is no Original/ + Modified/ split any more. You extract into `Extracted/`, edit the WAV
where it sits, and Patch Game writes back whatever differs from the pristine extract. The manifest
records the pristine SHA-1 so "differs" is a fact rather than a guess, exactly like
`archive_textures.is_edited()`.

**The manifest replaces the four `<fid>_Audio_Catalog.json` files.** Textures can get away with a
bare relpath -> sha1 manifest because an edited .dds can be re-encoded from the file alone. Audio
cannot: writing a WAV back needs the archive it came from, the byte offset, the packet slot size,
the channel count and the sample rate, none of which survive in the .wav. So the audio manifest is
a superset — it is the dirty-bit file *and* the stream index — and one file covers all four
containers instead of four that silently collided on `stem` (0A and 1A both have a stream at
offset 0, so `all_cat[stem]` in the old reimport path could resolve to the wrong archive).

Keys are `"<fid>:0x%08X"` (`akey`), the format `modpack.py` already used on the wire.

Splitting per wave bank (.bin) was considered and rejected: bank membership is derivable at any
time from `wave_banks.bank_for(fid, offset)`, so a stored copy is redundant state that can only go
stale, and 23 files are harder to hand-edit than one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
from pathlib import Path

SCHEMA = 3          # 3: sample_rate comes from the wave bank, not from re-reading our own WAV

FILE_IDS = ["0A", "0B", "1A", "1B"]

_MANIFEST_NAME = ".extract_manifest.json"
_NAMES_NAME    = "Audio_Names.json"
_LEGACY_DIR    = "_legacy_json"


# ── keys ──────────────────────────────────────────────────────────────────────

def akey(fid: str, off: int) -> str:
    return f"{fid}:0x{off:08X}"


def parse_akey(k: str) -> tuple:
    fid, h = k.split(":")
    return fid, int(h, 16)


def stem_of(entry: dict) -> str:
    """The offset-derived filename used when a stream has no friendly name."""
    return "%08X_%dch_%dp" % (entry["offset"], entry.get("channels", 1), entry.get("packets", 0))


# ── paths ─────────────────────────────────────────────────────────────────────

def audio_root(root: Path) -> Path:
    return Path(root) / "Audio"


def extracted_root(root: Path) -> Path:
    return audio_root(root) / "Extracted"


def manifest_file(root: Path) -> Path:
    return extracted_root(root) / _MANIFEST_NAME


def names_file(root: Path) -> Path:
    return audio_root(root) / _NAMES_NAME


def wav_path(root: Path, entry: dict) -> Path:
    """Absolute path of an entry's WAV. `wav` is stored relative to Extracted/ so the whole
    tree can be moved or shared without rewriting the manifest."""
    rel = entry.get("wav")
    if rel:
        return extracted_root(root) / rel
    folder = entry.get("folder") or "Unknown"
    return extracted_root(root) / folder / f"{entry.get('name') or stem_of(entry)}.wav"


def rel_wav(root: Path, p: Path) -> str:
    try:
        return Path(p).relative_to(extracted_root(root)).as_posix()
    except ValueError:
        return Path(p).name


# ── hashing / edit detection ──────────────────────────────────────────────────

def sha1_file(p) -> str:
    h = hashlib.sha1()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def is_edited(root: Path, entry: dict, wav: Path | None = None) -> bool:
    """True when the WAV on disk differs from the bytes we extracted.

    A missing `sha1` counts as edited, the same call `archive_textures.is_edited()` makes: an
    entry that predates the hash (or a WAV the user dropped in by hand) must still be applied,
    or it would silently stop working.
    """
    wav = wav or wav_path(root, entry)
    if not wav.exists():
        return False
    base = entry.get("sha1")
    if not base:
        return True
    return sha1_file(wav) != base


# ── manifest ──────────────────────────────────────────────────────────────────

def load_manifest(root: Path) -> dict:
    """akey -> entry. Returns {} when nothing has been extracted yet."""
    p = manifest_file(root)
    if not p.exists():
        return {}
    try:
        doc = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if isinstance(doc, dict) and isinstance(doc.get("entries"), dict):
        return doc["entries"]
    return {}


def manifest_schema(root: Path) -> int:
    """`_schema` of the manifest on disk, or 0 when there isn't one."""
    p = manifest_file(root)
    if not p.exists():
        return SCHEMA                       # nothing extracted yet -- nothing to bring forward
    try:
        return int(json.loads(p.read_text(encoding="utf-8-sig")).get("_schema") or 0)
    except Exception:
        return 0


def save_manifest(root: Path, entries: dict) -> None:
    p = manifest_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "_schema": SCHEMA,
        "_comment": "Extracted audio index. Key = <archive>:<byte offset>. "
                    "'wav' is relative to this folder; 'sha1' is the pristine extract, so a "
                    "differing file is a user edit and gets written back by Patch Game.",
        "entries": entries,
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, p)          # atomic — an interrupted 80k-entry write can't truncate the index


# ── names ─────────────────────────────────────────────────────────────────────

def load_names(root: Path) -> tuple:
    """(entries, default_category). Underscore keys are metadata, not streams."""
    p = names_file(root)
    if not p.exists():
        return {}, None
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}, None
    entries = {k: v for k, v in raw.items()
               if not k.startswith("_") and isinstance(v, dict)}
    return entries, raw.get("_default_category")


def load_names_raw(root: Path) -> dict:
    p = names_file(root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def save_names_raw(root: Path, raw: dict) -> None:
    p = names_file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw.setdefault("_schema", SCHEMA)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def update_names(root: Path, changes: dict) -> None:
    """Merge {akey: {field: value}} into the names file, preserving other fields."""
    raw = load_names_raw(root)
    for k, ch in changes.items():
        cur = raw.get(k)
        cur = dict(cur) if isinstance(cur, dict) else {}
        cur.update(ch)
        raw[k] = cur
    save_names_raw(root, raw)


# ── sample-rate repair (schema 2 -> 3) ────────────────────────────────────────

def _patch_wav_rate(wav: Path, rate: int) -> float:
    """Restamp a PCM WAV's `fmt ` chunk to `rate`. Returns the new duration, 0.0 on failure.

    The samples themselves are untouched and do not need to be -- an XMA2 stream decodes to a
    fixed number of samples whatever rate you claim it is, so a mis-rated extract holds the
    right audio and only lies about how fast to play it. Rewriting 8 bytes of header is the
    whole fix; re-decoding 80,000 streams would produce byte-identical PCM.
    """
    try:
        with open(wav, "r+b") as f:
            if f.read(4) != b"RIFF":
                return 0.0
            f.seek(8)
            if f.read(4) != b"WAVE":
                return 0.0
            ch = bits = data_size = 0
            while True:
                cid = f.read(4)
                if len(cid) < 4:
                    break
                csz = struct.unpack("<I", f.read(4))[0]
                nxt = f.tell() + csz + (csz & 1)
                if cid == b"fmt " and csz >= 16:
                    body = f.tell()
                    f.seek(body + 2)
                    ch = struct.unpack("<H", f.read(2))[0]
                    f.seek(body + 14)
                    bits = struct.unpack("<H", f.read(2))[0]
                    f.seek(body + 4)
                    f.write(struct.pack("<I", rate))                     # sampleRate
                    f.write(struct.pack("<I", rate * ch * (bits // 8)))  # byteRate
                elif cid == b"data":
                    data_size = csz
                    break
                f.seek(nxt)
        if ch and bits and data_size:
            return data_size / (rate * ch * (bits // 8))
    except OSError:
        pass
    return 0.0


def repair_sample_rates(root: Path, log=None) -> dict:
    """Bring an older extract's sample rates in line with the wave banks.

    Everything extracted before this was decoded at a hardcoded 48000 and then had that same
    48000 read back off the file as if it had been measured, so the six 44.1 kHz banks
    (pamusic, palines, paplyrs, chants, chatter, streamedchatter) came out 8.8% sharp.

    A pristine extract gets its header restamped in place. A WAV the user has edited is left
    exactly as it is -- only the manifest's rate is corrected, which is the rate Patch Game
    resamples their edit to on the way back into the game.
    """
    try:
        from . import wave_banks as wb
    except ImportError:
        import wave_banks as wb

    root = Path(root)
    man = load_manifest(root)
    if not man:
        return {"checked": 0, "fixed": 0, "edited": 0, "missing": 0}

    n = fixed = edited = missing = 0
    for key, e in man.items():
        try:
            fid, off = parse_akey(key)
        except Exception:
            continue
        want = wb.rate_for(fid, off)
        if int(e.get("sample_rate") or 0) == want:
            continue
        n += 1
        e["sample_rate"] = want
        wav = wav_path(root, e)
        if not wav.exists():
            missing += 1
            continue
        if e.get("sha1") and sha1_file(wav) != e["sha1"]:
            edited += 1            # the user's own audio -- their file, their bytes
            continue
        dur = _patch_wav_rate(wav, want)
        if dur > 0:
            e["duration"] = round(dur, 3)
            e["sha1"] = sha1_file(wav)
            e["size"] = wav.stat().st_size
            fixed += 1
    # Written unconditionally: the write is what stamps the new `_schema`, and without it a
    # store that needed no fixing would be re-walked on every single reload.
    save_manifest(root, man)
    if n:
        if log:
            log(f"Sample-rate repair: {fixed} WAV(s) restamped, {edited} user-edited left alone, "
                f"{missing} not extracted ({n} manifest entries corrected)")
    elif log:
        log("Sample rates already match the wave banks")
    return {"checked": n, "fixed": fixed, "edited": edited, "missing": missing}


# ── one-time migration from the old layout ────────────────────────────────────

def needs_migration(root: Path) -> bool:
    root = Path(root)
    if manifest_file(root).exists():
        return False
    if any((root / f"{fid}_Audio_Catalog.json").exists() for fid in FILE_IDS):
        return True
    if any((root / f"{fid}_Audio_Names.json").exists() for fid in FILE_IDS):
        return True
    # extracted WAVs sitting directly under Audio/<Category>/ (no Extracted/ level)
    ad = audio_root(root)
    if ad.exists():
        for child in ad.iterdir():
            if child.is_dir() and child.name not in ("Extracted", _LEGACY_DIR):
                return True
    return False


def _load_json(p) -> dict:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def migrate(root: Path, log=None) -> dict:
    """Convert the old four-catalog / Audio + Modified/Audio layout in place.

    Nothing is deleted. The superseded JSONs are moved to Audio/_legacy_json/ and the old WAV
    tree is *moved* (same volume, so it is a rename per file, not a copy) under Extracted/.
    A modified WAV wins over the pristine one for the same stream, which is precisely what the
    new edit-in-place model means, and it is recorded as edited because its bytes won't match
    the recorded sha1.
    """
    root = Path(root)
    log = log or (lambda _m: None)
    stats = {"entries": 0, "moved": 0, "from_modified": 0, "names": 0, "legacy": 0}

    ex = extracted_root(root)
    ex.mkdir(parents=True, exist_ok=True)

    # 1. names: four files -> one, keys "0x…" -> "<fid>:0x…"
    raw_names: dict = {}
    default_cat = None
    for fid in FILE_IDS:
        old = root / f"{fid}_Audio_Names.json"
        if not old.exists():
            continue
        doc = _load_json(old)
        default_cat = doc.get("_default_category") or default_cat
        for k, v in doc.items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            try:
                off = int(k, 16)
            except ValueError:
                continue                      # the two known typo'd keys ("0x", "0x4480C0800")
            if v:
                raw_names[akey(fid, off)] = dict(v)
                stats["names"] += 1
    if default_cat:
        raw_names["_default_category"] = default_cat

    # 2. catalogs -> manifest
    entries: dict = {}
    old_audio = audio_root(root)
    old_mod   = root / "Modified" / "Audio"
    for fid in FILE_IDS:
        for stem, e in _load_json(root / f"{fid}_Audio_Catalog.json").items():
            off = e.get("offset")
            if off is None:
                continue
            name   = e.get("friendly_name") or stem
            cat_   = e.get("category") or "Unknown"
            folder = Path(e.get("wav", "")).parent.name or "Unknown"
            entries[akey(fid, off)] = {
                "fid": fid, "offset": off,
                "packets": e.get("packets", 0), "channels": e.get("channels", 1),
                "sample_rate": e.get("sample_rate") or 0,
                "duration": e.get("duration", 0.0),
                "name": name, "category": cat_,
                "wav": f"{folder}/{name}.wav",
                "sha1": "",            # unknown until re-extract; "" => treated as edited
            }
            stats["entries"] += 1

    # 3. move the WAV tree under Extracted/
    if old_audio.exists():
        for child in sorted(old_audio.iterdir()):
            if not child.is_dir() or child.name in ("Extracted", _LEGACY_DIR):
                continue
            dest = ex / child.name
            if dest.exists():
                for p in child.rglob("*.wav"):
                    t = dest / p.relative_to(child)
                    t.parent.mkdir(parents=True, exist_ok=True)
                    if not t.exists():
                        os.replace(p, t); stats["moved"] += 1
            else:
                shutil.move(str(child), str(dest))
                stats["moved"] += sum(1 for _ in dest.rglob("*.wav"))

    # 4. fold Modified/Audio on top — a user edit replaces the pristine file in place
    if old_mod.exists():
        for p in old_mod.rglob("*.wav"):
            t = ex / p.relative_to(old_mod)
            t.parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, t)
            stats["from_modified"] += 1

    # 5. reconcile: point each entry at a WAV that actually exists
    for k, e in entries.items():
        p = ex / e["wav"]
        if p.exists():
            continue
        alt = ex / Path(e["wav"]).parent / f"{stem_of(e)}.wav"
        if alt.exists():
            e["wav"] = rel_wav(root, alt)

    save_manifest(root, entries)
    save_names_raw(root, raw_names)

    # 6. park the superseded files rather than deleting them
    legacy = audio_root(root) / _LEGACY_DIR
    for fid in FILE_IDS:
        for suffix in ("_Audio_Catalog.json", "_Audio_Names.json"):
            old = root / f"{fid}{suffix}"
            if old.exists():
                legacy.mkdir(parents=True, exist_ok=True)
                os.replace(old, legacy / old.name)
                stats["legacy"] += 1

    log("Audio layout migrated: %d stream(s) indexed, %d WAV(s) moved into Audio/Extracted/, "
        "%d former Modified/ edit(s) folded in, %d name(s) merged, %d old JSON(s) parked in "
        "Audio/_legacy_json/" % (stats["entries"], stats["moved"], stats["from_modified"],
                                 stats["names"], stats["legacy"]))
    return stats
