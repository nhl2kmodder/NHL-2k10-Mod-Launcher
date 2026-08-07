"""pa_names.py — the PA announcer's recorded-name catalogue, keyed by the id a roster row stores.

A player record in Roster.ROS chunk 0x1E159C31 carries two u16 announcer ids:

    +0x2C  audio_last    the SURNAME the PA announcer says
    +0x2E  audio_first   the GIVEN name

Both are `lineId`s into `paplyrs.bin`'s speech database, so each one resolves through
`speech_lines` to a run of takes -- alternate readings of the same name, which the engine picks
between with `rand() % takes`. 0xFFFF means "no recording": that half of the name is simply not
spoken. See docs/32_speech_trigger_system.md.

The two ids live in disjoint id bands, which is what makes a picker possible at all -- offering
surnames for +0x2C and given names for +0x2E rather than one undifferentiated list of 3,622:

    0 .. 3091      surnames,    alphabetical
    5000 .. 6074   given names, alphabetical
    8000 .. 8100   goals-scored lines   (not names -- excluded from the picker)
    9000 .. 9100   "a <team> player"    (ditto)

Display names come from the ASR-derived seed catalogue where it has a name for one of the takes;
roughly a third of the lines are covered. For the rest the id is all we have, so the picker leans
on AUDITIONING instead -- `wavs()` hands back the extracted takes to play. Note the seed labels
every one of these `PA_Name_Last_*`, including the 5000 band, which is demonstrably given names
(Bobby, Boris, Brad); trust the BAND, not the seed's prefix.
"""
from __future__ import annotations
import json
import re

try:
    from . import audio_store as astore, resources, speech_lines
except ImportError:                                     # flat import when run un-packaged
    import audio_store as astore
    import resources
    import speech_lines

BANK = "paplyrs.bin"
SILENT = 0xFFFF                                         # "no recording" -- announcer stays quiet

# (lo, hi) inclusive. Derived by clustering the decoded line ids, cross-checked against the
# line bands in doc 32 §"Line-id bands".
BANDS = {"last":  (0, 3091),
         "first": (5000, 6074)}

_seed: dict | None = None
_cache: dict = {}


def _seed_names() -> dict:
    """{("1A"/"1B", offset): display name} from the ASR seed catalogue."""
    global _seed
    if _seed is not None:
        return _seed
    _seed = {}
    try:
        doc = json.loads(resources.data_path("speech_seed_names.json")
                         .read_text(encoding="utf-8"))
    except Exception:
        return _seed
    for fid, entries in doc.items():
        if fid.startswith("_") or not isinstance(entries, dict):
            continue
        for off, rec in entries.items():
            try:
                _seed[(fid.upper(), int(off, 16))] = (rec or {}).get("name") or ""
            except Exception:
                pass
    return _seed


def _pretty(raw: str) -> str:
    """`PA_Name_Last_Jaspers_Take1_Var2` -> `Jaspers`. Returns "" if nothing name-like is left.

    Suffixes come off before the prefix, because the prefix test is what decides whether the
    remainder is a real name -- `PA_Name_Unknown_208E5800` is a placeholder the ASR pass emitted
    for a take it could not transcribe, and must read as "no name", not as a name.
    """
    if not raw:
        return ""
    s = re.sub(r"(?:_(?:Take\d+|Var\d+))+$", "", raw.strip())
    if re.match(r"^PA_Name_Unknown", s, re.I):
        return ""
    s = re.sub(r"^PA_Name_(?:Last|First)?_?", "", s)
    return "" if not s or s.upper().startswith("PA_") else s.replace("_", " ").strip()


def band_of(kind: str) -> tuple:
    return BANDS.get(kind, (0, 0xFFFE))


def catalogue(kind: str) -> list:
    """[{"id", "name", "takes", "cues": [(fid, offset), ...]}, ...] for one band, id order.

    `name` is "" when no take of that line has a seed name -- the caller should show the bare id
    and let the user audition it, never hide the row.
    """
    if kind in _cache:
        return _cache[kind]
    lo, hi = band_of(kind)
    seed, out = _seed_names(), []
    for line, _var, _start, takes in speech_lines.lines_of(BANK):
        if not (lo <= line <= hi):
            continue
        cues = [speech_lines.physical(BANK, rel)
                for rel in speech_lines.cues_of(BANK, line)]
        name = ""
        for fid, off in cues:                            # first take that has a name wins
            name = _pretty(seed.get((fid.upper(), off), ""))
            if name:
                break
        out.append({"id": line, "name": name, "takes": takes, "cues": cues})
    _cache[kind] = out
    return out


def kind_for_field(field: str) -> str:
    """Which band a roster field draws from; "" for fields that are not announcer ids."""
    return {"audio_last": "last", "audio_first": "first"}.get(field, "")


def describe(kind: str, line_id) -> str:
    """One-line label for a stored id: `1204 · Jaspers (4 takes)`, or a plain-language sentinel."""
    try:
        line_id = int(line_id)
    except Exception:
        return str(line_id)
    if line_id == SILENT:
        return "%d · (not spoken)" % SILENT
    for e in catalogue(kind):
        if e["id"] == line_id:
            return "%d · %s (%d take%s)" % (line_id, e["name"] or "unnamed",
                                            e["takes"], "" if e["takes"] == 1 else "s")
    lo, hi = band_of(kind)
    if not (lo <= line_id <= hi):
        return "%d · outside the %s-name band (%d-%d)" % (line_id, kind, lo, hi)
    return "%d · no recording" % line_id


def wavs(root, line_id, kind="last") -> list:
    """Extracted WAV paths for every take of a line, so the picker can audition it.

    Only takes that have actually been extracted come back; an empty list means "run Extract",
    not "no such line".
    """
    man = astore.load_manifest(root)
    out = []
    for e in catalogue(kind):
        if e["id"] != int(line_id):
            continue
        for fid, off in e["cues"]:
            entry = man.get(astore.akey(fid, off))
            if not entry:
                continue
            p = astore.wav_path(root, entry)
            if p and p.exists():
                out.append(p)
    return out
