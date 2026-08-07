"""What each speech stream actually says, from the offline ASR sweep.

Filenames are transcripts truncated to ~25 characters, so the catalogue is searchable by the
first four words of a line and no further. ``Color_AhRandyILoveTeams`` is really *"Ah, Randy, I
love teams that play like this. This team hardly ever gets beaten in the defensive numbers."* --
everything after "teams" was unfindable. This module makes the whole line searchable without
renaming a single file, which matters because the filename is an identity: ``audio_store``
builds the wav path from it, so a rename moves files on disk and breaks every reference to them.

Speech only (~47k of ~81k streams). Non-speech banks have no entry and show blank, which is
correct -- ASR on a non-speech bank invents words (standing rule), so silence beats a guess.

**Two layers.** The shipped baseline in ``launcher/data/`` is the offline sweep and is read-only.
On top of it sits a user overlay in ``%APPDATA%\\NHL2K10 Mod Launcher\\`` holding lines the
"Sweep for changes" button re-transcribed after the user replaced a WAV. Replaced audio says
something different, and the old line would be worse than none -- a search for "Atlanta" must
stop finding a take that now says Winnipeg. Overlay wins by KEY PRESENCE, not by having text:
an entry with a hash and an empty line means "we listened to the new audio and it said nothing",
which must still shadow the baseline.

Each layer stores, alongside the line, the 16-char sha1 prefix of the WAV it was transcribed
from. That is what lets the refresh be cheap: hash the handful of files the launcher already
flags as modified and re-decode only the ones whose audio genuinely differs.

Rebuild the baseline with ``AI Voice Pipeline\\transcript_export.py --write``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

_TEXT: dict[str, str] | None = None
_SHA: dict[str, str] = {}
_UTEXT: dict[str, str] = {}
_USHA: dict[str, str] = {}

HASH_CHARS = 16
_USER_NAME = "audio_transcripts_user.json"


def _split(doc) -> tuple[dict, dict]:
    """(text, sha1) out of either schema. _schema 1 was a bare {akey: line} map with no
    hashes; it still loads, it just can't tell the refresh what it listened to."""
    if isinstance(doc, dict) and doc.get("_schema"):
        return doc.get("text") or {}, doc.get("sha1") or {}
    return (doc if isinstance(doc, dict) else {}), {}


def user_file() -> Path:
    """The overlay the refresh writes. User state, so %APPDATA% -- never launcher/data/."""
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(appdata) / "NHL2K10 Mod Launcher" / _USER_NAME


def _load() -> dict[str, str]:
    """Load once. A missing data file degrades to "no transcripts" rather than raising --
    the Line column just stays empty and every other column still works."""
    global _TEXT, _SHA, _UTEXT, _USHA
    if _TEXT is None:
        try:
            try:
                from . import resources
            except ImportError:
                import resources                    # running from launcher/ directly
            p = resources.data_path("audio_transcripts.json")
            _TEXT, _SHA = _split(json.loads(p.read_text(encoding="utf-8"))) if p.exists() \
                else ({}, {})
        except Exception:
            _TEXT, _SHA = {}, {}
        try:
            up = user_file()
            _UTEXT, _USHA = _split(json.loads(up.read_text(encoding="utf-8"))) if up.exists() \
                else ({}, {})
        except Exception:
            _UTEXT, _USHA = {}, {}
    return _TEXT


def text_for(akey: str) -> str:
    """The spoken line for a manifest key ('1B:0x0933A000'), or '' when not swept."""
    base = _load()
    if akey in _USHA:
        return _UTEXT.get(akey, "")
    return base.get(akey, "")


def sha_for(akey: str) -> str:
    """16-char sha1 prefix of the WAV this key's line was transcribed from, or ''."""
    _load()
    return _USHA.get(akey) or _SHA.get(akey, "")


def matches(akey: str, wav_sha1: str) -> bool:
    """Is the stored line still describing this audio? An unknown key answers False so the
    refresh offers to transcribe it rather than silently skipping it."""
    have = sha_for(akey)
    return bool(have) and wav_sha1[:HASH_CHARS] == have


def update(rows: dict[str, tuple[str, str]]) -> Path:
    """Merge {akey: (line, full_sha1)} into the user overlay and persist it.

    Applied to the in-memory maps first so the Line column is right the moment the sweep
    finishes, without a restart or a catalogue reload."""
    _load()
    for akey, (line, sha) in rows.items():
        _UTEXT[akey] = line
        _USHA[akey] = (sha or "")[:HASH_CHARS]
    p = user_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"_schema": 2, "text": _UTEXT, "sha1": _USHA},
                              ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(p)                                  # never leave a half-written overlay
    return p


def count() -> int:
    return len(_load())


def user_count() -> int:
    _load()
    return len(_USHA)
