"""Re-transcribe the WAVs the user has replaced, on request.

The Line column comes from an offline sweep that took ~13 hours of GPU time, so it cannot be
rebuilt on every patch. But a replaced WAV says something different from what the sweep heard,
and a stale line is worse than none: after the Atlanta->Winnipeg rewrites, searching "Atlanta"
must stop finding takes that now say Winnipeg. So the refresh is a button, and it is cheap:

  1. **Prefilter** on the size+mtime "modified" flag the audio list already tracks. No reads.
  2. **Hash** just those. A patch that rewrites identical bytes -- re-applying a mod pack, a
     re-export at the same settings -- costs one hash and stops there.
  3. **Decode** only files whose sha1 differs from the one the line was transcribed from.

Only streams the sweep already covered are eligible. An entry with no stored hash is a bank the
sweep deliberately skipped, and pointing ASR at a non-speech bank makes it invent words
(standing rule) -- so "no baseline" means skip, not transcribe.

Results land in the %APPDATA% overlay via ``transcripts.update()``; the shipped baseline in
``launcher/data/`` is never written.

CPU by design. The workload is a handful of two-second takes, the launcher's interpreter has no
torch, and CUDA here would need Applio's DLLs on PATH -- for the size of job a user actually
patches, ~1 s each is not worth any of that.
"""
from __future__ import annotations

from pathlib import Path

try:
    from . import audio_store as astore
    from . import transcripts
except ImportError:                                 # running from launcher/ directly
    import audio_store as astore
    import transcripts

MODEL = "medium.en"        # small.en gets team names wrong: 83.8% vs 86.2% on the sweep's set


def available() -> str:
    """'' when a refresh can run, else why it can't -- shown instead of failing at click time."""
    try:
        import faster_whisper                       # noqa: F401
    except Exception as e:
        return f"faster-whisper is not installed for this Python ({e.__class__.__name__})."
    return ""


def plan(root: Path, entries: dict, keys, progress=None) -> list[dict]:
    """Which of `keys` actually need re-transcribing. Hashes; does not decode.

    `keys` is the caller's cheap prefilter -- the akeys the audio list flags as modified.
    Returns [{key, name, wav, sha1}] for the ones whose audio no longer matches their line."""
    out = []
    keys = list(keys)
    for n, key in enumerate(keys, 1):
        entry = entries.get(key)
        if not entry or not transcripts.sha_for(key):
            continue                                # never swept => not ours to guess at
        wav = astore.wav_path(root, entry)
        if not wav.exists():
            continue
        sha = astore.sha1_file(wav)
        if sha and not transcripts.matches(key, sha):
            out.append({"key": key, "name": entry.get("name") or key, "wav": wav, "sha1": sha})
        if progress:
            progress(n, len(keys), len(out))
    return out


def run(items: list[dict], progress=None, cancel=None) -> dict:
    """Transcribe `plan()`'s output and merge it into the user overlay.

    Saves as it goes, every SAVE_EVERY takes, so a cancel or a crash keeps the work already
    done -- the same reason the offline sweep checkpoints."""
    from faster_whisper.audio import decode_audio
    from faster_whisper import WhisperModel

    SAVE_EVERY = 25
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=8)

    done, pending = {}, {}
    for n, it in enumerate(items, 1):
        if cancel and cancel():
            break
        try:
            audio = decode_audio(str(it["wav"]), sampling_rate=16000)
            # vad_filter=False: VAD clips leading consonants off short takes, and most of the
            # store is short takes.
            segs, _ = model.transcribe(audio, vad_filter=False, beam_size=5, language="en")
            line = " ".join(" ".join(s.text.split()) for s in segs).strip()
        except Exception:
            continue                                # one unreadable WAV must not end the run
        done[it["key"]] = pending[it["key"]] = (line, it["sha1"])
        if progress:
            progress(n, len(items), it["name"], line)
        if len(pending) >= SAVE_EVERY:
            transcripts.update(pending)
            pending.clear()
    if pending:
        transcripts.update(pending)
    return done
