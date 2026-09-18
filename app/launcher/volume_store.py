"""volume_store.py — the launcher's view of the extracted container tree.

Layout under the configured game-files folder (ROOT = config "root_path"):

    ROOT/0A, 0B, 1A, 1B, ...        the game's own archives — what the console loads
    ROOT/NHL2k10_Extracted/         THIS tree: one file per TOC entry, plus volume.json
    ROOT/NHL2k10_Extracted_Files/   the user-facing extract folder (textures, audio) — unrelated

The tree is extracted once, on the first launch that has a valid game folder, and from then on
it is the source of truth for entry bytes: it is the pristine baseline, the per-entry revert,
and the unit a release ships (changed entries only, not 6 GB of archives).

WRITE-THROUGH is the whole design. Every entry write updates the tree file AND lands in the
archives in the same call, so the archives are never stale. That is what lets the twenty-odd
modules that read assets through `archive_textures.resolve()` carry on untouched — there is no
"apply" step they could be caught on the wrong side of, and no second source of truth to drift.
`repack()` exists for the other direction: rebuilding the archives wholesale from a tree the
user (or an installer) has changed underneath us.

The archives can still be restructured by the legacy relocate / spill / compact paths in
archive_textures. Those move entries without going through here, so `resync()` re-reads the live
header afterwards; it is a header-only read, so it costs nothing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

try:
    from . import volume
except ImportError:
    import volume

TREE_NAME = "NHL2k10_Extracted"

_CACHE = {}          # game_dir(str) -> manifest dict, so a write does not re-read volume.json


def tree_dir(game_dir) -> Path:
    return Path(game_dir) / TREE_NAME


_READY = {}                      # game_dir(str) -> (expires_at, result)
_READY_TTL = 30.0                # seconds; forget() clears it outright


def is_ready(game_dir) -> bool:
    """True when the tree exists and every file it names is present.

    It was NOT cheap enough to gate on: the honest answer stats all 2,451 entry files (~0.23 s),
    and every archive write asks up to four times, so a single logo replace spent ~0.9 s here.
    Nothing inside the launcher deletes tree files, so the answer only changes when the game
    folder changes -- which goes through forget() -- or when the user edits the tree from
    outside. A short TTL covers the latter without paying the sweep on every write.
    """
    key = str(Path(game_dir))
    now = time.monotonic()
    hit = _READY.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        r = volume.is_extracted(tree_dir(game_dir))
    except Exception:
        r = False
    _READY[key] = (now + _READY_TTL, r)
    return r


def manifest(game_dir, reload=False):
    key = str(Path(game_dir))
    if reload or key not in _CACHE:
        _CACHE[key] = volume.load_manifest(tree_dir(game_dir))
    return _CACHE[key]


def forget(game_dir=None):
    """Drop the cached manifest — call when the game folder changes."""
    if game_dir is None:
        _CACHE.clear(); _READY.clear()
    else:
        _CACHE.pop(str(Path(game_dir)), None)
        _READY.pop(str(Path(game_dir)), None)


# -- first-launch extraction ---------------------------------------------------
def ensure_extracted(game_dir, log=print, progress=None, verify=True) -> bool:
    """Extract the tree if it is not already there. Returns True if the tree is usable.

    The tree mirrors the LIVE archives, not a pristine copy — `<arc>.orig` already is the
    pristine copy, and its job is different. On a fresh install the two are the same bytes. On a
    folder the launcher has already modified, mirroring live is the only correct choice: a
    pristine tree would silently claim stock bytes for assets that have been replaced in place.

    So the strict checks only apply where they mean something. A volume with no `.orig` beside
    any archive has never been written, so every inter-entry gap must be zero and the tree must
    rebuild it byte-for-byte; if either fails we have misread the layout and back off. A volume
    that has been modified carries stale bytes from earlier relocations, which nothing reads and
    repack correctly drops, so neither check would tell us anything.
    """
    game_dir = Path(game_dir)
    out = tree_dir(game_dir)
    if is_ready(game_dir):
        # A tree extracted by an older build named entries from a smaller dictionary, so assets
        # the modding minted (expansion clubs, relocations, added mask slots) are sitting there
        # as unknown_<crc>.bin. Re-deriving costs a manifest read and a few thousand crc32s, and
        # only touches anything when it can actually name something -- so it self-heals rather
        # than making the user re-extract 6 GB.
        try:
            volume.rename_unknowns(out, log=log)
        except Exception:
            pass          # cosmetic; never block startup over a filename
        forget(game_dir)
        try:
            heal_drift(game_dir, log)      # NOT cosmetic: stale bytes here would survive a repack
        except Exception as e:
            log("  WARNING: could not re-sync relocated entries: %s" % e)
        return True
    pristine = not any(p.with_suffix(".orig").exists() or
                       p.parent.joinpath(p.name + ".orig").exists()
                       for p in _archive_paths(game_dir))
    log("Extracting the game container to %s — this happens once." % out.name)
    volume.extract(game_dir, out, clean=False, log=log, progress=progress, strict=pristine)
    volume.stamp_tree(out)
    forget(game_dir)
    if verify and pristine and not volume.verify_roundtrip(out, game_dir, clean=False, log=log):
        log("WARNING: the extracted tree does not rebuild the archives byte-for-byte. "
            "Leaving the archives alone; the tree will not be used.")
        return False
    return True


def _archive_paths(game_dir):
    """The archive files the header names, as paths in the game folder."""
    try:
        _al, arcs, _e, _v = volume.read_header(game_dir)
    except Exception:
        return []
    return [Path(game_dir) / a["name"] for a in arcs]


def resync(game_dir, log=print):
    """Re-read the live header into the manifest, keeping each entry's filename.

    Needed after anything that restructured the archives outside this module (relocate, spill to
    a new archive, compaction). Header-only, so it is cheap. Entries the live TOC has that the
    manifest does not — assets added by the expansion-team work — are extracted now.
    """
    game_dir = Path(game_dir)
    out = tree_dir(game_dir)
    man = manifest(game_dir)
    if not man:
        return None
    align, arcs, ents, _vol = volume.read_header(game_dir)
    by_crc = {int(e["crc"], 16): e for e in man["entries"]}
    man["align"] = align
    man["archives"] = [{"name": a["name"], "size": a["size"]} for a in arcs]
    man["volume_size"] = arcs[-1]["hi"] if arcs else 0
    new = []
    with volume.VolumeReader(game_dir, arcs) as vr:
        for e in ents:
            rec = by_crc.get(e["crc"])
            if rec is None:                       # an asset added since we extracted
                fn = "added_%08X.bin" % e["crc"]
                (out / fn).write_bytes(vr.read(e["off"], e["size"]))
                rec = {"idx": e["idx"], "crc": "%08X" % e["crc"], "flags": e["flags"],
                       "size": e["size"], "sector": e["sector"], "file": fn, "name": ""}
                new.append(fn)
            rec["idx"], rec["flags"] = e["idx"], e["flags"]
            rec["sector"], rec["size"] = e["sector"], e["size"]
            by_crc[e["crc"]] = rec
    man["entries"] = [by_crc[e["crc"]] for e in ents]
    volume.stamp_tree(out, man)
    if new:
        log("  tree picked up %d new entries" % len(new))
    return man


# -- write-through -------------------------------------------------------------
def _bounds(man):
    arcs, cum = [], 0
    for a in man["archives"]:
        arcs.append({"name": a["name"], "size": a["size"], "lo": cum, "hi": cum + a["size"]})
        cum += a["size"]
    return arcs


def volume_offset(game_dir, arc: str, local_off: int) -> int | None:
    """(archive, offset within it) -> absolute volume offset."""
    man = manifest(game_dir)
    if not man:
        return None
    for a in _bounds(man):
        if a["name"] == arc:
            return a["lo"] + local_off
    return None


def entry_at(game_dir, vol_off: int):
    """The manifest entry that owns this absolute volume offset, or None."""
    man = manifest(game_dir)
    if not man:
        return None
    align = man["align"]
    for e in man["entries"]:
        start = e["sector"] * align
        if start <= vol_off < start + e["size"]:
            return e
    return None


def note_write(game_dir, arc: str, local_off: int, data: bytes, log=None) -> bool:
    """Mirror a write that has just gone into the archives into the tree file it belongs to.

    Returns True when the tree was updated. False means the write landed somewhere the tree does
    not describe — the header, or an entry added since the last resync — and the caller should
    `resync()`; it is NOT an error.
    """
    if not is_ready(game_dir):
        return False
    voff = volume_offset(game_dir, arc, local_off)
    if voff is None:
        return False
    e = entry_at(game_dir, voff)
    if e is None:
        return False
    align = manifest(game_dir)["align"]
    inner = voff - e["sector"] * align
    if inner + len(data) > e["size"]:
        # The write runs past this entry — a relocation or a grown resource. The archives are
        # already authoritative for it, so pull the entry back out of them rather than guess.
        return False
    p = tree_dir(game_dir) / e["file"]
    # Game files copied off a disc keep the read-only attribute and every write here is
    # "r+b". Cleared at each write site, so no path can forget (see fs_util).
    import fs_util
    fs_util.ensure_writable(p)
    with open(p, "r+b") as f:
        f.seek(inner)
        f.write(data)
    e["stamp"] = volume._stamp(p)
    e["size"] = p.stat().st_size
    return True


def entry_named(game_dir, name: str):
    """The manifest entry with this TOC name, or None. Name is the identity — never the index."""
    man = manifest(game_dir)
    if not man:
        return None
    return next((e for e in man["entries"] if e["name"] == name), None)


def write_in_entry(game_dir, name: str, inner: int, data: bytes, log=None) -> bool:
    """Write `data` at `inner` bytes INSIDE the TOC entry called `name` — tree and archives.

    This is the write primitive every in-place edit should use, and the reason is the whole point of
    the extracted tree: a caller says WHICH ASSET and where inside it, and this works out the
    physical address. `note_write` is the other way round — the caller has already written at a raw
    archive address and this module only mirrors it — so a caller holding a stale address silently
    corrupts whatever now lives there. That is how a Patch Game run put XMA packets through
    loading.iff and a dozen logos ([[project_patch_audio_stale_bank_offsets]]).

    Here that cannot happen. The bounds are the ENTRY's, so an offset that has gone stale raises
    instead of landing on a neighbour, and a relocation costs the caller nothing: the entry moved,
    the offset inside it did not.

    An entry may straddle an archive boundary (paplyrs.bin starts in 1A and ends in 1B), so the
    archive-side write is split across whatever files the span covers.
    """
    man = manifest(game_dir)
    e = entry_named(game_dir, name)
    if man is None or e is None:
        raise KeyError(f"no TOC entry named {name!r}")
    if inner < 0 or inner + len(data) > e["size"]:
        raise ValueError(f"write of {len(data)} at +0x{inner:X} runs outside {name} "
                         f"(size {e['size']}) — the offset is stale, refusing")
    p = tree_dir(game_dir) / e["file"]
    import fs_util
    fs_util.ensure_writable(p)
    with open(p, "r+b") as f:
        f.seek(inner)
        f.write(data)
    voff = e["sector"] * man["align"] + inner
    root = Path(game_dir)
    for a in _bounds(man):
        lo = max(voff, a["lo"])
        hi = min(voff + len(data), a["hi"])
        if lo >= hi:
            continue
        import fs_util
        fs_util.ensure_writable(root / a["name"])
        with open(root / a["name"], "r+b") as f:
            f.seek(lo - a["lo"])
            f.write(data[lo - voff:hi - voff])
        if log:
            log(f"    {name} +0x{inner:X}  {hi - lo} B -> {a['name']} @ 0x{lo - a['lo']:08X}")
    e["stamp"] = volume._stamp(p)
    return True


def pull_entry(game_dir, crc: int, log=None) -> bool:
    """Refresh one tree file from the live archives (after a relocation or an over-long write)."""
    if not is_ready(game_dir):
        return False
    man = manifest(game_dir)
    align, arcs, ents, _v = volume.read_header(game_dir)
    live = next((e for e in ents if e["crc"] == crc), None)
    rec = next((e for e in man["entries"] if int(e["crc"], 16) == crc), None)
    if live is None or rec is None:
        return False
    p = tree_dir(game_dir) / rec["file"]
    with volume.VolumeReader(game_dir, arcs) as vr:
        p.write_bytes(vr.read(live["off"], live["size"]))
    rec["sector"], rec["size"], rec["flags"] = live["sector"], live["size"], live["flags"]
    rec["stamp"] = volume._stamp(p)
    return True


def save(game_dir):
    """Persist the cached manifest."""
    man = manifest(game_dir)
    if man:
        with open(tree_dir(game_dir) / volume.MANIFEST, "w", encoding="utf-8") as f:
            json.dump(man, f, indent=1)


# -- the other direction -------------------------------------------------------
def repack(game_dir, log=print, progress=None) -> str:
    """Rebuild the archives from the tree — for a tree changed outside the launcher."""
    game_dir = Path(game_dir)
    # Last line of defence: repack writes tree -> archives, so any entry the tree is stale on
    # would be reverted here. Pull those first; the archives are authoritative for entry bytes.
    try:
        heal_drift(game_dir, log)
    except Exception as e:
        log("  WARNING: could not re-sync relocated entries before repack: %s" % e)
    out = volume.repack(tree_dir(game_dir), game_dir, log=log, progress=progress)
    forget(game_dir)
    return out


def changed(game_dir):
    """Entries whose tree file no longer matches what the archives hold."""
    if not is_ready(game_dir):
        return []
    return volume.changed_entries(tree_dir(game_dir), manifest(game_dir))


def drifted(game_dir):
    """Entries the live archives have MOVED or RESIZED since the manifest last looked.

    `changed()` answers the opposite question: it compares each tree file against its own recorded
    stamp, so it catches a file the user edited but is blind to the archives moving out from under
    us. A relocate/spill that grows an entry -- what the audio banks do routinely -- leaves the
    tree holding pre-relocation bytes under a perfectly valid stamp. Nothing would notice until a
    repack wrote those stale bytes back over the archives and silently reverted the very edit that
    caused the relocation. (Observed on gamedata.iff and palines.bin, 2026-08-25.)

    One header read, no file I/O, so it is cheap enough to gate startup and repack on.
    """
    if not is_ready(game_dir):
        return []
    man = manifest(game_dir)
    if not man:
        return []
    try:
        _align, _arcs, ents, _v = volume.read_header(Path(game_dir))
    except Exception:
        return []
    by = {int(e["crc"], 16): e for e in man["entries"]}
    return [e["crc"] for e in ents
            if e["crc"] in by
            and (by[e["crc"]]["size"] != e["size"] or by[e["crc"]]["sector"] != e["sector"])]


def heal_drift(game_dir, log=None) -> int:
    """Re-pull every drifted entry from the archives, which are authoritative for entry bytes."""
    bad = drifted(game_dir)
    if not bad:
        return 0
    n = sum(1 for crc in bad if pull_entry(game_dir, crc))
    if n:
        save(game_dir)
        if log:
            log("  tree re-synced %d relocated entr%s from the archives"
                % (n, "y" if n == 1 else "ies"))
    return n


def sync_after_op(game_dir, names=(), log=None):
    """Bring the tree back in step with the archives after a replace/relocate/spill.

    Called once at the end of every public `replace_*` in archive_textures. It costs a header
    read plus one file copy per entry that actually changed, so it is cheap enough to run
    unconditionally: an in-place splice pulls exactly one entry, and a relocation that shifted
    the tail pulls those too, without either caller having to know which happened.
    """
    if not is_ready(game_dir):
        return 0
    game_dir = Path(game_dir)
    man = manifest(game_dir)
    align, arcs, ents, _v = volume.read_header(game_dir)
    by_crc = {int(e["crc"], 16): e for e in man["entries"]}
    want = {volume.crc_of(n) for n in names if n}
    # An entry needs pulling when the header moved or resized it, or when it is one the caller
    # just rewrote (same sector, same size, different bytes — the in-place case).
    todo = [e for e in ents
            if e["crc"] in want
            or by_crc.get(e["crc"]) is None
            or by_crc[e["crc"]]["sector"] != e["sector"]
            or by_crc[e["crc"]]["size"] != e["size"]]
    if not todo:
        _refresh_shape(man, align, arcs)
        return 0
    out = tree_dir(game_dir)
    with volume.VolumeReader(game_dir, arcs) as vr:
        for e in todo:
            rec = by_crc.get(e["crc"])
            if rec is None:
                rec = {"idx": e["idx"], "crc": "%08X" % e["crc"], "file": "added_%08X.bin" % e["crc"],
                       "name": ""}
                by_crc[e["crc"]] = rec
            fp = out / rec["file"]
            with open(fp, "wb") as fo:
                off, left = e["off"], e["size"]
                while left > 0:
                    take = min(left, 1 << 24)
                    fo.write(vr.read(off, take))
                    off += take
                    left -= take
            rec["idx"], rec["flags"] = e["idx"], e["flags"]
            rec["sector"], rec["size"] = e["sector"], e["size"]
            rec["stamp"] = volume._stamp(fp)
    man["entries"] = [by_crc[e["crc"]] for e in ents]
    _refresh_shape(man, align, arcs)
    save(game_dir)
    if log:
        log("  tree synced: %d entr%s" % (len(todo), "y" if len(todo) == 1 else "ies"))
    return len(todo)


def _refresh_shape(man, align, arcs):
    man["align"] = align
    man["archives"] = [{"name": a["name"], "size": a["size"]} for a in arcs]
    man["volume_size"] = arcs[-1]["hi"] if arcs else 0
