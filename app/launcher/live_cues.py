"""live_cues.py — address an audio slot the way the GAME does, not the way we extracted it.

Every other audio key in this launcher is a PHYSICAL address recorded at extraction time — an
"akey" like "1A:0x5A128800". That key is a lie the moment a wave bank is relocated, and on
2026-09-02 acting on stale akeys wrote XMA packets over 48 art entries (loading.iff, rinks, logos,
ices, uniforms). 23 banks have moved since the store was built, so 28,754 of the manifest's 81,277
slots point at something that is no longer theirs.

The durable identity is (bank name, cue index):

  * the BANK NAME is a TOC entry name, and volume_store.write_in_entry resolves a name to a
    physical address at write time — so a relocation costs the caller nothing;
  * the CUE INDEX is the game's own wave-bank directory, the thing the engine itself indexes with.
    It cannot miss a cue and needs no heuristic, because it IS the directory.

Two consequences worth stating, because both are bugs this module retires:

  1. Capacity comes from the cue table, NEVER from the manifest's `packets`. That field was
     produced by the stream scanner as the gap to the next stream start, so on ~27% of palines
     slots it over-declares — the extractor decoded past the cue's real end, and a writer trusting
     it would run past the slot. The block size here is rel[i+1] - rel[i], the room the cue owns.
  2. An unresolved cue is REFUSED, not guessed. write_cue raises rather than writing to a
     best-effort address. Guessing is what produced the 48 clobbered art entries.

The table is identified exactly, not by pattern: a candidate is accepted only when its sentinel
record's offset equals the bank's size in the LIVE 0A TOC (cue_table_locate.scan).

Related: [[project_patch_audio_stale_bank_offsets]], [[project_relocated_bank_rescan_fails]],
[[project_audio_cue_record_layout]], [[project_cue_table_live_location]].
"""
import json
import os
import sys
from pathlib import Path

import volume_store

_PIPE = Path(__file__).resolve().parents[2] / "AI Voice Pipeline"
if str(_PIPE) not in sys.path:
    sys.path.insert(0, str(_PIPE))

PACKET = 2048

# ── where the index lives ─────────────────────────────────────────────────────
#
# NOT in launcher/data/. That directory is shipped INSIDE the onefile exe and unpacked to
# sys._MEIPASS, a temp folder deleted when the app exits — an index written there would be lost
# every launch, and worse, a snapshot built on the developer's install would ship to users whose
# banks sit at completely different offsets. Stale offsets are not a cosmetic problem here: acting
# on them once wrote XMA packets over 48 art entries.
#
# So the index is USER STATE, keyed to the install it describes, next to the config in %APPDATA%.
_STATE_NAME = "live_cue_index.json"
_LEGACY = Path(__file__).resolve().parent / "data" / _STATE_NAME


def _state_dir() -> Path:
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    d = Path(appdata) / "NHL2K10 Mod Launcher" if appdata else _LEGACY.parent
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = _LEGACY.parent
    return d


def index_path() -> Path:
    return _state_dir() / _STATE_NAME


# Kept as a module attribute because callers and tools refer to it; it is now the writable path.
INDEX = index_path()

_cache = None
_game_dir = None            # the install the index describes — set by bind()


def bind(game_dir):
    """Point the module at an install so the index can build itself on demand.

    Call this wherever the game folder is chosen. Without it a missing index can only raise,
    because rebuilding needs to know which install to read.
    """
    global _game_dir
    _game_dir = Path(game_dir) if game_dir else None
    return _game_dir


def _live_sizes(game_dir):
    """{bank: size} from the LIVE TOC — the fingerprint the index is checked against."""
    import archive_textures as AT
    import wave_banks as WB
    out = {}
    for n in WB.all_banks():
        loc = AT.resolve(n, Path(game_dir))
        if loc:
            out[n] = loc[2]
    return out


def is_stale(idx, game_dir) -> bool:
    """True when a bank has moved or resized since the index was built.

    Every offset in the index is relative to a bank whose SIZE is recorded alongside it, and the
    cue table is only accepted when its sentinel equals that size. So a size that no longer
    matches the live TOC means the table this index describes is not the table on disk any more.
    Cheap enough (~0.02 s) to check on every load.
    """
    try:
        live = _live_sizes(game_dir)
    except Exception:
        return False                      # cannot tell; do not thrash the rebuild
    for bank, b in (idx.get("banks") or {}).items():
        if bank in live and live[bank] != b.get("size"):
            return True
    return False


def ensure(game_dir=None, log=None):
    """Make sure a current index exists for this install, building it if it does not.

    This is the frictionless path: no user should ever be told to run a command to make the app
    work. Returns the index.
    """
    global _cache
    game_dir = Path(game_dir) if game_dir else _game_dir
    p = index_path()
    if _cache is None and p.exists():
        try:
            _cache = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _cache = None
    if _cache is None and not getattr(sys, "frozen", False) and _LEGACY.exists() and _LEGACY != p:
        # A dev tree that built the index before it became user state — adopt it rather than
        # spending 8 s rebuilding what is already correct. Source trees only: an index that
        # arrived inside a build would describe the machine that BUILT it, not this one, and
        # is_stale() only catches that if a bank size happens to differ.
        try:
            _cache = json.loads(_LEGACY.read_text(encoding="utf-8"))
            p.write_text(json.dumps(_cache), encoding="utf-8")
        except Exception:
            _cache = None
    if _cache is not None and game_dir and is_stale(_cache, game_dir):
        if log:
            log("a wave bank has moved since the cue index was built — rebuilding it")
        _cache = None
    if _cache is None:
        if not game_dir:
            raise FileNotFoundError(
                "the live cue index has not been built and no game folder is set — "
                "choose your game files folder in Settings.")
        rebuild(game_dir, log=log or (lambda *_a: None))
    return _cache


def _load_index():
    global _cache
    if _cache is None:
        return ensure()
    return _cache


def forget():
    """Drop the cached index (after a rebuild, or after a bank was rewritten)."""
    global _cache
    _cache = None


def banks():
    return sorted(_load_index()["banks"])


def table(bank):
    """Every live cue in `bank` as {"cue", "rel", "size", "dur"}, ascending by `rel`.

    `size` is the block the cue owns — the distance to the next cue start, or to the end of the
    bank for the last one. It is the write ceiling.
    """
    idx = _load_index()
    if bank not in idx["banks"]:
        raise KeyError(f"no live cue table for {bank!r}; known: {', '.join(sorted(idx['banks']))}")
    b = idx["banks"][bank]
    offs, durs = b["offsets"], b["durations"]
    return [{"cue": i, "rel": offs[i], "size": offs[i + 1] - offs[i], "dur": durs[i]}
            for i in range(len(durs))]


def slot(bank, cue):
    """One cue's record. Raises rather than returning a plausible neighbour."""
    idx = _load_index()
    if bank not in idx["banks"]:
        raise KeyError(f"no live cue table for {bank!r}")
    b = idx["banks"][bank]
    n = len(b["durations"])
    if not 0 <= cue < n:
        raise IndexError(f"{bank} has {n} live cues; {cue} is out of range")
    offs = b["offsets"]
    return {"bank": bank, "cue": cue, "rel": offs[cue],
            "size": offs[cue + 1] - offs[cue], "dur": b["durations"][cue],
            "rate": b["rate"]}


def rate(bank):
    return _load_index()["banks"][bank]["rate"]


def cue_at(bank, rel):
    """The cue index whose start is exactly `rel`, or None.

    Exact membership only. A near miss is a miss: an offset one packet off is not "close enough",
    it is a different cue's audio.
    """
    b = _load_index()["banks"].get(bank)
    if not b:
        return None
    return b["by_offset"].get(str(rel))


def write_cue(game_dir, bank, cue, data, log=None):
    """Write `data` into (bank, cue) — tree and archives, bounds-checked twice.

    First against the cue's own block, so a long take cannot spill into the next cue; then against
    the bank's TOC entry inside volume_store.write_in_entry, so a stale bank base cannot land on a
    neighbouring asset. The caller never sees a physical address.
    """
    s = slot(bank, cue)
    if len(data) > s["size"]:
        raise ValueError(
            f"{bank} cue {cue} owns {s['size']} B ({s['size'] // PACKET} packets); "
            f"the take is {len(data)} B ({len(data) // PACKET} packets) — refusing to spill "
            f"into cue {cue + 1}")
    if len(data) % PACKET:
        raise ValueError(f"take is {len(data)} B, not a whole number of {PACKET}-B packets")
    if volume_store.entry_named(game_dir, bank) is None:
        raise KeyError(f"no TOC entry named {bank!r} — the bank is untracked, so a write to it "
                       "cannot be bounds-checked. Refusing.")
    volume_store.write_in_entry(game_dir, bank, s["rel"], data, log=log)
    if log:
        log(f"    {bank} cue {cue} @ +0x{s['rel']:08X}  "
            f"{len(data) // PACKET}/{s['size'] // PACKET} packets")
    return True


# -- building the index --------------------------------------------------------

def rebuild(game_dir, log=print):
    """Re-read every bank's live cue table and write data/live_cue_index.json.

    Run this after ANY operation that grows, shrinks or relocates a wave bank — a palines rebuild,
    a lines_ts relocation, an expansion install. It is cheap (three container decompressions) and
    it is the only thing standing between a caller and a stale address.
    """
    import wave_banks as WB
    import bank_parser as BP
    import archive_textures as AT
    import cue_table_locate as CT

    game_dir = Path(game_dir)
    names = list(WB.all_banks())
    sizes = {}
    for n in names:
        loc = AT.resolve(n, game_dir)
        if loc:
            sizes[n] = loc[2]
    by_size = {}
    for n, s in sizes.items():
        by_size.setdefault(s, []).append(n)

    out = {"_comment": "Live wave-bank cue tables — the game's own audio directory. Identity is "
                       "(bank, cue index); offsets are BANK-RELATIVE and are resolved to a "
                       "physical address at write time by volume_store.write_in_entry. Rebuild "
                       "after anything that moves or resizes a bank.",
           "_schema": 1, "banks": {}}
    for cont in CT.CONTAINERS:
        try:
            dec = BP.decompress_bank(cont, clean_dir=game_dir)[0]
        except Exception as exc:
            log(f"  {cont}: cannot decompress ({exc}) — skipped")
            continue
        for h in CT.scan(dec, sizes) or []:
            owners = by_size.get(h["sentinel"], [])
            if len(owners) != 1:
                log(f"  {cont} @0x{h['hdr']:X}: sentinel 0x{h['sentinel']:X} matches "
                    f"{len(owners)} banks {owners} — ambiguous, skipped")
                continue
            bank = owners[0]
            recs = CT.records(dec, h["hdr"], h["count"])
            offs = [o for o, _ in recs]
            durs = [d for _, d in recs[:-1]]
            if offs != sorted(offs) or offs[-1] != sizes[bank]:
                log(f"  {bank}: table @0x{h['hdr']:X} not ascending / sentinel mismatch — skipped")
                continue
            prev = out["banks"].get(bank)
            if prev and len(prev["durations"]) >= len(durs):
                continue                       # a duplicate table; keep the fuller one
            out["banks"][bank] = {
                "container": cont, "hdr": h["hdr"], "rate": h["rate"],
                "size": sizes[bank], "offsets": offs, "durations": durs,
                "by_offset": {str(o): i for i, o in enumerate(offs[:-1])},
            }
            log(f"  {bank:<24} {len(durs):>6} cues  {h['rate']} Hz  {cont} @0x{h['hdr']:X}")
    p = index_path()
    p.write_text(json.dumps(out), encoding="utf-8")
    missing = [n for n in names if n not in out["banks"]]
    log(f"wrote {p.name}: {len(out['banks'])} banks, "
        f"{sum(len(b['durations']) for b in out['banks'].values())} cues")
    if missing:
        log(f"  no live table found for: {', '.join(sorted(missing))}")
    global _cache
    _cache = out
    return out


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rebuild":
        rebuild(Path(__file__).resolve().parents[2])
    else:
        for b in banks():
            print(f"{b:<24} {len(table(b)):>6} cues  {rate(b)} Hz")
