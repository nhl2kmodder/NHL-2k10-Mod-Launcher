"""speech_lines.py — which LINE ID and which TAKE a given speech stream is.

Data (launcher/data/, see resources.data_path):
  speech_line_tables.json   the speech DB's key/prefix arrays, decoded off disk

Every speech request the game makes is `(lineId, speaker, variation)`. The engine resolves it
through a serialized database section (IFF type 0xBB05A9C1) that ships inside global.iff and
gamedata.iff, one block per wave bank. A block is two parallel arrays:

    lineIds[N]    strictly ascending   -- the key array SpeechDB_FindKeyBinarySearch bisects
    cueStart[N]   non-decreasing       -- index of that line's FIRST cue in the bank's cue table

so a line's takes are the contiguous run `cueStart[i] .. cueStart[i+1]-1`, and the engine picks
one with `rand() % (cueStart[i+1] - cueStart[i])`. That is the whole of "variation": there is no
per-take record anywhere, just a slot in a run.

Two key widths exist, chosen by the section's `flags & 0x02`, and BOTH ship:
  u16 banks (paplyrs, players, teams, lines_ps, lines_ts, horns, env_amb) store the bare lineId.
  u32 banks (chatter, streamedchatter) store `lineId * K + variation`, K per bank -- so for those
  a "line" in the table is really one (line, variation) pair and the run under it is that pair's
  takes. `key_div` carries K; it is 1 for the u16 banks, which makes the split a no-op there.

This maps STREAM -> LINE. The reverse (which situation triggers a line) is code, not data; see
docs/32_speech_trigger_system.md.

Coverage is 9 of the 19 banks / 57,244 of 80,779 cues, including everything the roster and PA
work needs. The rest use a multi-group block whose header is not decoded yet -- their anchors are
recorded under "undecoded" in the data file. `line_for` returns None for those, and callers must
treat that as "not known", never as "no line".
"""
from __future__ import annotations
import bisect
import json

try:
    from . import resources, wave_banks
except ImportError:                                     # flat import when run un-packaged
    import resources
    import wave_banks

_doc: dict | None = None
_by_bank: dict = {}                                     # bank -> {"offsets": [...], "cue": [...]}


def _load() -> dict:
    """Lazy-load and index the tables. One flat cue->line list per bank, built once."""
    global _doc
    if _doc is not None:
        return _doc
    try:
        _doc = json.loads(resources.data_path("speech_line_tables.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        _doc = {"banks": {}}
        return _doc
    for bank, ent in (_doc.get("banks") or {}).items():
        n = int(ent.get("cue_count") or 0)
        # cue index -> (line_id, take, takes); groups of one bank never overlap
        cue = [None] * n
        for g in ent.get("groups") or []:
            div = int(g.get("key_div") or 1)
            for key, start, takes in g.get("lines") or []:
                line, var = (key // div, key % div) if div > 1 else (key, None)
                for t in range(takes):
                    if 0 <= start + t < n:
                        cue[start + t] = (line, var, t, takes)
        _by_bank[bank] = {"offsets": ent.get("cue_offsets") or [], "cue": cue}
    return _doc


def banks() -> list:
    """Bank names that have a decoded line table."""
    _load()
    return sorted(_by_bank)


def cue_index(fid: str, offset) -> tuple:
    """(bank, cue_index) for a raw stream, or ("", -1).

    `offset` is the physical offset inside `fid`, the same value the audio manifest stores.
    Cue offsets are bank-relative, so the bank's base has to come off first -- which
    wave_banks already knows how to do, including the 1A/1B logical-space fold.
    """
    bank = wave_banks.bank_for(fid, offset)
    ent = _load() and _by_bank.get(bank)
    if not ent:
        return "", -1
    base = _bank_base(fid, bank)
    if base is None:
        return bank, -1
    rel = _logical(fid, offset) - base
    offs = ent["offsets"]
    i = bisect.bisect_right(offs, rel) - 1
    # exact hit only: a stream that does not start on a cue boundary is not that cue
    return (bank, i) if i >= 0 and offs[i] == rel else (bank, -1)


def line_for(fid: str, offset):
    """{"bank","cue","line","take","takes","variation"} for a stream, or None if not decoded."""
    bank, i = cue_index(fid, offset)
    if i < 0:
        return None
    rec = _by_bank[bank]["cue"][i] if i < len(_by_bank[bank]["cue"]) else None
    if rec is None:
        return None
    line, var, take, takes = rec
    return {"bank": bank, "cue": i, "line": line, "take": take + 1,
            "takes": takes, "variation": var}


def label(fid: str, offset) -> str:
    """Short human form for a table cell: "9081 · 2/4" (u16) or "10000.9999 · 1/2" (u32)."""
    r = line_for(fid, offset)
    if not r:
        return ""
    lid = str(r["line"]) if r["variation"] is None else "%d.%d" % (r["line"], r["variation"])
    return "%s · %d/%d" % (lid, r["take"], r["takes"])


def lines_of(bank: str) -> list:
    """[(line_id, variation_or_None, first_cue, takes), ...] for one bank, in key order."""
    ent = (_load().get("banks") or {}).get(bank) or {}
    out = []
    for g in ent.get("groups") or []:
        div = int(g.get("key_div") or 1)
        for key, start, takes in g.get("lines") or []:
            out.append(((key // div, key % div) if div > 1 else (key, None)) + (start, takes))
    return sorted(out)


def cues_of(bank: str, line_id: int, variation=None) -> list:
    """Bank-relative byte offsets of every take of one line, in take order."""
    ent = (_load().get("banks") or {}).get(bank) or {}
    offs = _by_bank.get(bank, {}).get("offsets") or []
    for g in ent.get("groups") or []:
        div = int(g.get("key_div") or 1)
        want = line_id * div + (variation or 0) if div > 1 else line_id
        for key, start, takes in g.get("lines") or []:
            if key == want:
                return [offs[c] for c in range(start, start + takes) if c < len(offs)]
    return []


# ── bank geometry ─────────────────────────────────────────────────────────────
# wave_banks owns the layout; these two reach into it rather than duplicating the JSON, so a
# regenerated audio_wave_banks.json stays the single source of truth for where a bank starts.
def _logical(fid: str, offset) -> int:
    lay = wave_banks._load().get((fid or "").upper())
    if not lay:
        return int(offset)
    vol, _spans, first = lay
    return int(offset) if (fid or "").upper() == first else int(offset) + vol


def _bank_base(fid: str, bank: str):
    lay = wave_banks._load().get((fid or "").upper())
    if not lay:
        return None
    for name, lo, _hi in lay[1]:
        if name == bank:
            return lo
    return None


def physical(bank: str, rel):
    """Inverse of `cue_index`: a bank-relative cue offset -> ("1A"/"1B", physical offset).

    Cue offsets are relative to the bank's base in the pair's LOGICAL space (first file, then
    second continuing at `vol`). Which of the two files a cue actually lands in therefore
    depends on the bank's base, not on the bank name -- paplyrs is based at 0x88C43000, past
    1A's 0x6B800000, so every paplyrs cue is physically in 1B.
    """
    for fid, (vol, spans, first) in wave_banks._load().items():
        base = next((lo for n, lo, _hi in spans if n == bank), None)
        if base is None:
            continue
        second = next((f for f, l in wave_banks._load().items()
                       if l[2] == first and f != first), first)
        lg = base + int(rel)
        return (first, lg) if lg < vol else (second, lg - vol)
    return "", 0
