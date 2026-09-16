"""goal_songs.py — the per-team goal song, addressed the way the ENGINE addresses it.

There is no goal-song lookup table anywhere in the game data, and that is not because it is
hidden: the mapping is POSITIONAL. `pamusic.bin` carries no speech-DB key block, so the engine
falls back to its positional base, and the goal song a club scores to is

    pamusic.bin cue = 43 + <the club's AUDIO id>

where the audio id is the u16 at **+0x10C** of the club's roster record (its twin at +0x10E holds
the same value).  It is NOT `+0xC8`: every stock record carries the id in all three fields and
they never disagree, which is exactly why the wrong one looked right for a while.  A relocated
club keeps its donor's audio id — Utah reads 22 (Phoenix), Winnipeg 1 (Atlanta) — and the two
expansion clubs own the ids the launcher added, 30 and 31, so they land on cues 73 and 74.

Because the id is read from the LIVE roster, this module never hardcodes a team list.  Move a
club, relocate one, add one — the row follows whatever `+0x10C` now says.

Anchor: Vancouver is audio id 28, cue 71, and cue 71 is the one goal song the user replaced by
hand long before this module existed.  Every read here resolves to it.

## Addressing, and why it is not an offset

Never key a cue by the physical offset it had at extraction time.  23 wave banks have moved since
this install's audio store was built, and acting on those stale offsets once wrote XMA packets
over 48 art entries.  `live_cues` holds the durable identity — (bank name, cue index) — resolves
the bank by TOC NAME at write time, and bounds-checks a write against the cue's own block.  This
module only ever asks it for a slot and hands it bytes.

## Stock bytes come from `<vol>.orig`, never from the tree

`NHL2k10_Extracted/` is write-through: it holds what is INSTALLED, so reading "the original" from
it returns the user's own replacement.  The pristine copy is the `.orig` archive backup, read at
the CLEAN TOC's base for the bank.

Related: [[project_goal_songs]], [[project_live_cue_addressing]], [[project_stock_audio_only_from_orig]].
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

try:
    from . import archive_textures as _AT
    from . import live_cues as _LC
    from . import team_order as _TO
    from . import volume_store as _VS
except ImportError:                                  # flat import — launcher/ is on sys.path
    import archive_textures as _AT
    import live_cues as _LC
    import team_order as _TO
    import volume_store as _VS

BANK       = "pamusic.bin"
CUE_BASE   = 43                  # cue = CUE_BASE + audio id
PACKET     = 2048
RATE       = 44100               # pamusic is 44.1 kHz, not the 48 kHz the store defaults to
CHANNELS   = 2
LEN_EPS    = 3.0                 # seconds of length drift that a re-encode alone can explain
FP_BLOCK   = 0.5                 # seconds per envelope block — a fixed TIME base, see _envelope
FP_DIST    = 0.002               # distance above which the content is NOT the stock take

STORE_DIR  = "Audio/GoalSongs"   # under NHL2k10_Extracted_Files
LEDGER     = "goal_songs.json"

_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW


class GoalSongError(RuntimeError):
    pass


# ── teams ───────────────────────────────────────────────────────────────────────

def teams(roster_path) -> list[dict]:
    """Every club in the roster's league-0 block, with the cue its goal song lives in.

    Read from the live roster on purpose: the audio id is the only thing that decides which cue
    a club scores to, and a relocation or an expansion install rewrites it.
    """
    p = Path(roster_path)
    d = p.read_bytes()
    base, _n = _TO.find_table(d)
    try:
        from . import roster_editor as _RE
    except ImportError:
        import roster_editor as _RE
    ed = _RE.RosterEditor(str(p))

    def _txt(v):
        return getattr(v, "text", v) or ""

    rows = []
    for i, t in enumerate(ed.teams):
        off = base + i * _TO.STRIDE
        aud = struct.unpack_from(">H", d, off + 0x10C)[0]
        city = _txt(getattr(t, "city", ""))
        nick = _txt(getattr(t, "nickname", ""))
        rows.append({
            "index": i,
            "code":  getattr(t, "code", "") or "",
            "city":  city,
            "nick":  nick,
            "team":  (f"{city} {nick}".strip() or getattr(t, "code", "") or f"Team {i}"),
            "audio_id": aud,
            "cue":   CUE_BASE + aud,
        })
    return rows


# ── the bank ────────────────────────────────────────────────────────────────────

def slot(cue: int) -> dict:
    """The cue's live record: bank-relative offset, the block it owns, its stock duration."""
    return _LC.slot(BANK, cue)


def _live_source(game_dir):
    """(path, offset) to read this bank's live bytes from.

    The extracted tree is preferred because it is one file per entry and write-through, so it is
    always what the archives hold.  Without a tree, fall back to the live TOC's own address.
    """
    game_dir = Path(game_dir)
    tree = _VS.tree_dir(game_dir) / BANK
    if tree.exists():
        return tree, 0
    loc = _AT.resolve(BANK, game_dir)
    if not loc:
        raise GoalSongError(f"{BANK} is not in the live TOC of {game_dir}")
    vol, base, size, _idx, _sec = loc
    arc = game_dir / vol
    if not arc.exists() or arc.stat().st_size < base + size:
        raise GoalSongError(
            f"{BANK} straddles the end of {vol} — build the extracted container tree "
            "(Settings) so the bank can be read as one file.")
    return arc, base


def read_live(game_dir, cue: int) -> bytes:
    """The bytes the game will actually play for this cue."""
    s = slot(cue)
    path, base = _live_source(game_dir)
    with open(path, "rb") as f:
        f.seek(base + s["rel"])
        return f.read(s["size"])


def read_stock(game_dir, cue: int) -> bytes | None:
    """The pristine bytes from the `.orig` archive backup, or None if there is no backup."""
    game_dir = Path(game_dir)
    loc = _AT.resolve(BANK, game_dir, clean=True)
    if not loc:
        return None
    vol, base, size, _idx, _sec = loc
    orig = game_dir / f"{vol}.orig"
    if not orig.exists():
        return None
    s = slot(cue)
    if orig.stat().st_size < base + s["rel"] + s["size"]:
        return None
    with open(orig, "rb") as f:
        f.seek(base + s["rel"])
        return f.read(s["size"])


# ── decoding ────────────────────────────────────────────────────────────────────

def _riff_xma2(raw: bytes, channels: int = CHANNELS, rate: int = RATE) -> bytes:
    n_packets = len(raw) // PACKET
    n_samples = n_packets * 2 * 512
    ch_mask   = 4 if channels == 1 else 3
    le16 = lambda v: struct.pack("<H", v & 0xFFFF)
    le32 = lambda v: struct.pack("<I", v & 0xFFFFFFFF)
    avg  = max(1, (len(raw) * rate) // max(1, n_samples))
    fmt  = le16(0x0166) + le16(channels) + le32(rate) + le32(avg)
    fmt += le16(PACKET) + le16(16) + le16(34)
    fmt += le16(1) + le32(ch_mask) + le32(n_samples)
    fmt += le32(PACKET) + le32(0) + le32(n_samples)
    fmt += le32(0) * 2 + bytes([0, 4]) + le16(n_packets)
    hdr  = b"RIFF" + le32(4 + 8 + len(fmt) + 8 + len(raw)) + b"WAVE"
    hdr += b"fmt " + le32(len(fmt)) + fmt
    hdr += b"data" + le32(len(raw))
    return hdr + raw


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, creationflags=_NO_WINDOW)


def decode(raw: bytes, out_wav: Path, xma2encode: str) -> float:
    """Decode a cue's packets to a PCM WAV.  Returns its length in seconds (0.0 on failure)."""
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gs_dec_") as td:
        xma = Path(td) / "c.xma"
        xma.write_bytes(_riff_xma2(raw))
        out_wav.unlink(missing_ok=True)
        _run([xma2encode, str(xma), "/DecodeToPCM", str(out_wav)])
    return wav_seconds(out_wav)


def wav_seconds(p) -> float:
    try:
        with contextlib.closing(wave.open(str(p), "rb")) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def _envelope(p) -> "list | None":
    """A coarse loudness envelope on a fixed TIME base — the part of a take a re-encode can't move.

    Blocks are FP_BLOCK seconds each, never a fixed count, because two copies of one song rarely
    decode to the same length: XMA pads to whole packets, and this bank has been through an
    encode pass that shifted most cues by a second or two. A fixed block COUNT stretches the two
    envelopes against each other, so a song trimmed by 20 s scores the same as a different song
    entirely — which is exactly what a percentage-based envelope did here. On a fixed time base
    the same song scores 0.00000 and a different one 0.006 upward: a 60x gap to put a line in.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        with contextlib.closing(wave.open(str(p), "rb")) as w:
            if w.getsampwidth() != 2:
                return None
            ch, sr = w.getnchannels(), w.getframerate()
            a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    except Exception:
        return None
    if ch > 1:
        a = a[: (a.size // ch) * ch].reshape(-1, ch).mean(axis=1)
    n = int(sr * FP_BLOCK)
    a = a[: (a.size // n) * n]
    if a.size < n:
        return None
    return np.abs(a).reshape(-1, n).mean(axis=1).tolist()


def _envelope_distance(a, b) -> float:
    """Cosine distance over the span the two takes SHARE.

    Comparing only the overlap is the point: a goal song cut short is still the same song for as
    long as it lasts, and the tail one of them doesn't have says nothing about the other.
    """
    if not a or not b:
        return 1.0
    m = min(len(a), len(b))
    if m < 4:
        return 1.0
    a, b = a[:m], b[:m]
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if not na or not nb:
        return 1.0
    return max(0.0, 1.0 - sum(x * y for x, y in zip(a, b)) / (na * nb))


# ── the ledger ──────────────────────────────────────────────────────────────────

def store_dir(files_root) -> Path:
    d = Path(files_root) / STORE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_dir(files_root) -> Path:
    d = store_dir(files_root) / "Replaced"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backups(files_root, cue: int) -> list[Path]:
    """Every saved copy of what used to be in this cue, newest last."""
    d = backup_dir(files_root)
    return sorted(d.glob(f"cue_{cue:03d}_*.xma"))


def _backup(game_dir, files_root, cue: int, log=None) -> Path | None:
    """Save the cue's CURRENT bytes before anything overwrites them.

    A goal song the user installed by hand exists in exactly one place — the live bank. It is not
    in `<vol>.orig` (that holds the disc's song) and, if an earlier re-encode broke the stream
    scanner on it, it is not in the extracted store either: 9 of this install's 10 replaced cues
    have no copy anywhere on disk. So both writing a new song and restoring the disc's would
    destroy it silently. Keep a copy first; the slot is at most a couple of MB.
    """
    try:
        cur = read_live(game_dir, cue)
    except Exception as e:                       # never let a backup failure block the write
        if log:
            log(f"  could not read cue {cue} to back it up: {e}")
        return None
    d = backup_dir(files_root)
    tag = hashlib.sha1(cur).hexdigest()[:8]
    for old in d.glob(f"cue_{cue:03d}_*_{tag}.xma"):
        return old                               # these exact bytes are already saved
    p = d / f"cue_{cue:03d}_{_now().replace(':', '').replace(' ', '_')}_{tag}.xma"
    p.write_bytes(cur)
    if log:
        log(f"  saved the previous cue {cue} audio to {p.name}")
    return p


def load_ledger(files_root) -> dict:
    p = store_dir(files_root) / LEDGER
    if not p.exists():
        return {"_comment": "What the launcher installed into each pamusic goal-song cue, plus a "
                            "cached measurement of what is live. Keyed by cue index; `sha1` is of "
                            "the live bank bytes, so a stale row is detected rather than trusted.",
                "cues": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        d.setdefault("cues", {})
        return d
    except Exception:
        return {"cues": {}}


def forget_measurements(led: dict) -> dict:
    """Drop every cached verdict but keep what the launcher INSTALLED.

    A Rescan is for re-measuring, not for forgetting which song the user put in: the source
    filename and the install date are records of an action, not a measurement, and no amount of
    re-decoding can recover them once they are gone.
    """
    for rec in led.get("cues", {}).values():
        for k in ("sha1", "seconds", "modified"):
            rec.pop(k, None)
    return led


def save_ledger(files_root, led: dict) -> None:
    (store_dir(files_root) / LEDGER).write_text(json.dumps(led, indent=1), encoding="utf-8")


# ── what is in the slot right now ───────────────────────────────────────────────

def inspect(game_dir, files_root, cue: int, xma2encode: str, led: "dict | None" = None) -> dict:
    """Measure one cue: how long the live take is, and whether it is still the stock one.

    Content decides, not length. Three tests, cheapest first, and the decode is cached against
    the live bytes' sha1 so it is paid once per change:

      1. the live bytes are byte-identical to `.orig` -> stock, no decode at all;
      2. decode both and compare envelopes over the span they share -> the real answer;
      3. no `.orig` to compare against -> fall back to the length the cue table declares.

    Length alone is not the test, and that is not a refinement: this install's cues sit anywhere
    from 0.15 s to 2.5 s off their declared length while decoding to identical audio, because the
    bank has been re-encoded. Judging by length called seven untouched clubs modified.
    """
    s = slot(cue)
    raw = read_live(game_dir, cue)
    sha = hashlib.sha1(raw).hexdigest()
    led = load_ledger(files_root) if led is None else led
    rec = dict(led["cues"].get(str(cue)) or {})

    out = {"cue": cue, "sha1": sha, "slot_bytes": s["size"],
           "slot_packets": s["size"] // PACKET, "stock_seconds": s["dur"],
           "source": rec.get("source", ""), "installed": rec.get("installed", "")}

    if rec.get("sha1") == sha and "seconds" in rec and "modified" in rec:
        out["seconds"]  = rec["seconds"]
        out["modified"] = rec["modified"]
        return out

    stock = read_stock(game_dir, cue)
    if stock is not None and stock == raw:
        out["seconds"], out["modified"] = s["dur"], False
        out["source"] = out["installed"] = ""
        _remember(led, cue, out)
        return out

    cache = store_dir(files_root)
    live_wav = cache / f"cue_{cue:03d}_live.wav"
    out["seconds"] = decode(raw, live_wav, xma2encode)

    if stock is None:
        # No pristine copy to compare against. The declared length is the only evidence left, and
        # claiming "modified" without it would be a guess.
        out["modified"] = bool(rec.get("source")) or abs(out["seconds"] - s["dur"]) > LEN_EPS
        _remember(led, cue, out)
        return out

    stock_wav = cache / f"cue_{cue:03d}_stock.wav"
    decode(stock, stock_wav, xma2encode)
    a, b = _envelope(live_wav), _envelope(stock_wav)
    if a is None or b is None:
        # No envelope (numpy absent, or a decode that produced nothing readable). Fall back to
        # length rather than to the distance function's 1.0, which would call every club modified.
        out["modified"] = abs(out["seconds"] - s["dur"]) > LEN_EPS
        _remember(led, cue, out)
        return out
    d = _envelope_distance(a, b)
    out["envelope_distance"] = round(d, 5)
    out["modified"] = d > FP_DIST or abs(out["seconds"] - s["dur"]) > LEN_EPS
    _remember(led, cue, out)
    return out


def _remember(led, cue, out):
    rec = led["cues"].setdefault(str(cue), {})
    rec.update({"sha1": out["sha1"], "seconds": out["seconds"], "modified": out["modified"]})
    if not out["modified"]:
        rec.pop("source", None)
        rec.pop("installed", None)
        out["source"] = out["installed"] = ""


def preview_wav(game_dir, files_root, cue: int, xma2encode: str, stock: bool = False) -> Path:
    """A playable PCM WAV of what is in the slot (or of the stock take), decoded on demand."""
    cache = store_dir(files_root)
    out = cache / f"cue_{cue:03d}_{'stock' if stock else 'live'}.wav"
    raw = read_stock(game_dir, cue) if stock else read_live(game_dir, cue)
    if raw is None:
        raise GoalSongError("no .orig backup of this archive — the stock take is not available")
    if out.exists():
        # Only re-decode when the bytes behind it changed.
        led = load_ledger(files_root)
        rec = led["cues"].get(str(cue)) or {}
        if not stock and rec.get("sha1") == hashlib.sha1(raw).hexdigest():
            return out
        if stock:
            return out
    decode(raw, out, xma2encode)
    if not out.exists():
        raise GoalSongError("xma2encode could not decode this cue")
    return out


# ── replacing ───────────────────────────────────────────────────────────────────

def _pcm(src, dst, ffmpeg, start=0.0, length=None, fade=0.0):
    cmd = [ffmpeg, "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    if fade and length and length > fade:
        cmd += ["-af", f"afade=t=out:st={length - fade:.3f}:d={fade:.3f}"]
    cmd += ["-acodec", "pcm_s16le", "-ar", str(RATE), "-ac", str(CHANNELS), str(dst)]
    r = _run(cmd)
    if r.returncode != 0 or not Path(dst).exists():
        err = (r.stderr or r.stdout or b"").decode(errors="replace").strip().splitlines()
        raise GoalSongError("ffmpeg could not read that file:\n" + "\n".join(err[-4:]))


def _encode(pcm_wav, xma2encode, quality=60) -> bytes:
    with tempfile.TemporaryDirectory(prefix="gs_enc_") as td:
        out = Path(td) / "o.xma"
        base = [xma2encode, str(pcm_wav), "/TargetFile", str(out),
                "/Quality", str(quality), "/Speaker", "F,R"]
        for cmd in (base + ["/UseLoopPoints"], base,
                    [xma2encode, str(pcm_wav), "/TargetFile", str(out), "/Quality", str(quality)]):
            out.unlink(missing_ok=True)
            r = _run(cmd)
            if r.returncode == 0 and out.exists():
                return _xma_data(out.read_bytes())
        err = (r.stderr or r.stdout or b"").decode(errors="replace").strip()
        raise GoalSongError(f"xma2encode failed (rc={r.returncode}): {err or 'no output'}")


def _xma_data(riff: bytes) -> bytes:
    pos = 12
    while pos < len(riff) - 8:
        cid = riff[pos:pos + 4]
        csz = struct.unpack_from("<I", riff, pos + 4)[0]
        if cid == b"data":
            raw = riff[pos + 8: pos + 8 + csz]
            return raw[: (len(raw) // PACKET) * PACKET]
        pos += 8 + csz + (csz & 1)
    raise GoalSongError("no data chunk in the encoded XMA2")


def source_seconds(src, ffmpeg) -> float:
    """How long the user's file is, as ffmpeg sees it — any format ffmpeg can open."""
    r = _run([ffmpeg, "-i", str(src), "-f", "null", "-"])
    txt = (r.stderr or b"").decode(errors="replace")
    best = 0.0
    for tag in ("time=", "Duration: "):
        i = txt.rfind(tag)
        while i >= 0:
            t = txt[i + len(tag): i + len(tag) + 11].strip().rstrip(",")
            try:
                h, m, s = t.split(":")
                best = max(best, int(h) * 3600 + int(m) * 60 + float(s))
            except Exception:
                pass
            i = txt.rfind(tag, 0, i)
    return best


def fit(src, cue: int, ffmpeg, xma2encode, start=0.0, length=None, fade=1.5,
        log=None) -> tuple[bytes, float, bool]:
    """Encode `src` down to something that fits the cue's block.

    Returns (packets, seconds actually used, trimmed?).  Length is traded before quality: a goal
    song cut short at full bitrate sounds like a goal song, one squeezed to quality 10 does not.
    """
    s = slot(cue)
    budget = s["size"]
    total = source_seconds(src, ffmpeg)
    want = length if length else max(0.0, total - start)
    trimmed = False
    with tempfile.TemporaryDirectory(prefix="gs_fit_") as td:
        pcm = Path(td) / "p.wav"
        for _attempt in range(4):
            _pcm(src, pcm, ffmpeg, start=start, length=want or None, fade=fade)
            raw = _encode(pcm, xma2encode, 60)
            if len(raw) <= budget:
                return raw, wav_seconds(pcm), trimmed
            used = wav_seconds(pcm) or want or 1.0
            want = used * (budget / len(raw)) * 0.97
            trimmed = True
            if log:
                log(f"    too long by {(len(raw) - budget) // PACKET} packets — "
                    f"retrying at {want:.1f}s")
        # Length alone did not get there (a very dense master). Spend quality now.
        for q in (50, 40, 30, 20, 10):
            _pcm(src, pcm, ffmpeg, start=start, length=want or None, fade=fade)
            raw = _encode(pcm, xma2encode, q)
            if len(raw) <= budget:
                if log:
                    log(f"    fitted at quality {q}")
                return raw, wav_seconds(pcm), trimmed
    raise GoalSongError(
        f"could not fit this song into the slot: it holds {budget // PACKET} packets "
        f"(~{s['dur']:.0f}s) and the shortest encode was {len(raw) // PACKET}.")


def install(game_dir, files_root, cue: int, raw: bytes, source_name: str = "",
            seconds: float = 0.0, log=None) -> None:
    """Write the packets into the cue — bounds-checked, tree and archives, in one call.

    The block is zero-filled behind the new stream so no packet of the previous song is left
    where the engine could reach it.
    """
    s = slot(cue)
    if len(raw) > s["size"]:
        raise GoalSongError(f"{len(raw)} B will not fit cue {cue}'s {s['size']} B block")
    _backup(game_dir, files_root, cue, log=log)
    _LC.write_cue(Path(game_dir), BANK, cue, raw.ljust(s["size"], b"\0"), log=log)
    _VS.save(Path(game_dir))

    led = load_ledger(files_root)
    led["cues"][str(cue)] = {
        "sha1": hashlib.sha1(raw.ljust(s["size"], b"\0")).hexdigest(),
        "seconds": round(seconds, 3), "modified": True,
        "source": source_name, "installed": _now(),
    }
    save_ledger(files_root, led)
    for stale in (store_dir(files_root) / f"cue_{cue:03d}_live.wav",):
        stale.unlink(missing_ok=True)


def restore(game_dir, files_root, cue: int, log=None) -> None:
    """Put the disc's own goal song back."""
    stock = read_stock(game_dir, cue)
    if stock is None:
        raise GoalSongError(
            "there is no .orig backup of this archive, so the stock goal song cannot be "
            "recovered from this install.")
    _backup(game_dir, files_root, cue, log=log)
    _LC.write_cue(Path(game_dir), BANK, cue, stock, log=log)
    _VS.save(Path(game_dir))
    led = load_ledger(files_root)
    led["cues"].pop(str(cue), None)
    save_ledger(files_root, led)
    (store_dir(files_root) / f"cue_{cue:03d}_live.wav").unlink(missing_ok=True)


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
