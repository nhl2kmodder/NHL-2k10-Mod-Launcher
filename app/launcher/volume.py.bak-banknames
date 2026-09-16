"""volume.py — the NHL 2K10 archive volume as ONE contiguous sector-addressed byte space.

The shipped archives (0A, 0B, 1A, 1B, plus any we add) are not four independent files: they
are consecutive slices of a single address space. Every TOC entry stores a SECTOR index into
that space, so an entry can — and two of them do — straddle an archive boundary. Anything that
treats "0B" as a standalone container will corrupt those two.

Header, at the top of the FIRST archive (all big-endian):
    0x00 magic 0xAA00B3BF
    0x04 align                     (0x800)
    0x08 archive count
    0x10 entry count
    0x18 archive rows, 16 B each   [size in sectors][0][name UTF-16BE, NUL-padded to 8 B]
    then entry rows, 16 B each     [flags][size][crc32(NAME.IFF uppercased)][sector]
The entry table is followed by zero padding out to the first entry's sector.

EXTRACT writes one file per entry plus a manifest; REPACK rebuilds the whole volume from that
folder, assigning sectors fresh and growing / adding archives as needed. Because layout is an
OUTPUT of repack rather than an input, an entry that grows costs nothing — which is what makes
the per-file size ceiling a non-issue (see SPILL_CEILING).

Repacking an unmodified tree reproduces the clean archives BYTE FOR BYTE; verify_roundtrip()
proves it, and first-launch extract runs it before it trusts the tree.
"""
from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path

MAGIC = 0xAA00B3BF
ALIGN = 0x800
# Per-file ceiling. The 360 filesystem tops out at 4 GiB and the game's reader has been seen to
# misbehave well before that, so archives are capped conservatively and a NEW one is added
# rather than growing an existing one past this. Matches archive_textures.SPILL_CEILING.
SPILL_CEILING = 2_000_000_000
MANIFEST = "volume.json"


def _be(b, o):
    return struct.unpack_from(">I", b, o)[0]


def crc_of(name: str) -> int:
    """The TOC key: crc32 of the UPPERCASED asset name. Not a hash of the bytes."""
    return zlib.crc32(name.upper().encode("ascii")) & 0xFFFFFFFF


# -- header -------------------------------------------------------------------
def _first_archive(game_dir: Path) -> Path:
    """The archive holding the header. 0A shipped, but probe for the magic rather than assume —
    archive NAMES come from the header itself everywhere else, so nothing else hardcodes them."""
    game_dir = Path(game_dir)
    for cand in ("0A", "0A.orig"):
        p = game_dir / cand
        if p.exists():
            with open(p, "rb") as f:
                if _be(f.read(4), 0) == MAGIC:
                    return p
    for p in sorted(game_dir.iterdir()):
        if p.is_file() and p.stat().st_size > 0x10000:
            with open(p, "rb") as f:
                if _be(f.read(4), 0) == MAGIC:
                    return p
    raise RuntimeError("no archive carrying the volume header in %s" % game_dir)


def read_header(game_dir, clean=False):
    """-> (align, [{name,size,lo,hi}], [{idx,flags,size,crc,sector,off}], volume_size)."""
    game_dir = Path(game_dir)
    p = _first_archive(game_dir)
    if clean and (game_dir / (p.name + ".orig")).exists():
        p = game_dir / (p.name + ".orig")
    with open(p, "rb") as f:
        d = f.read(0x20000)
        align, narc, count = _be(d, 0x04), _be(d, 0x08), _be(d, 0x10)
        need = 0x18 + narc * 16 + count * 16
        if need > len(d):
            f.seek(0)
            d = f.read(need)
    ebase = 0x18 + narc * 16
    arcs, cum = [], 0
    for i in range(narc):
        o = 0x18 + i * 16
        nm = d[o + 8:o + 16].decode("utf-16-be").split("\0")[0]
        sz = _be(d, o) * align
        arcs.append({"name": nm, "size": sz, "lo": cum, "hi": cum + sz})
        cum += sz
    ents = []
    for i in range(count):
        fl, sz, crc, sec = struct.unpack_from(">4I", d, ebase + i * 16)
        ents.append({"idx": i, "flags": fl, "size": sz, "crc": crc,
                     "sector": sec, "off": sec * align})
    return align, arcs, ents, cum


def build_header(align, arcs, ents) -> bytes:
    """Header bytes for a volume with these archives and entries, zero-padded out to the first
    entry's sector. arcs = [{name,size}]; ents = [{flags,size,crc,sector}] in TOC order."""
    ebase = 0x18 + len(arcs) * 16
    buf = bytearray(ebase + len(ents) * 16)
    struct.pack_into(">I", buf, 0x00, MAGIC)
    struct.pack_into(">I", buf, 0x04, align)
    struct.pack_into(">I", buf, 0x08, len(arcs))
    struct.pack_into(">I", buf, 0x10, len(ents))
    for i, a in enumerate(arcs):
        o = 0x18 + i * 16
        if a["size"] % align:
            raise ValueError("archive %s size %d is not sector-aligned" % (a["name"], a["size"]))
        struct.pack_into(">I", buf, o, a["size"] // align)
        nm = a["name"].encode("utf-16-be")
        if len(nm) > 8:
            raise ValueError("archive name %r too long for the 8-byte field" % a["name"])
        buf[o + 8:o + 8 + len(nm)] = nm
    for i, e in enumerate(ents):
        # The manifest keeps `crc` as a hex STRING because a human reads that folder; the
        # header needs the number.
        crc = e["crc"]
        struct.pack_into(">4I", buf, ebase + i * 16, e["flags"], e["size"],
                         int(crc, 16) if isinstance(crc, str) else crc, e["sector"])
    first = min((e["sector"] for e in ents), default=0) * align
    if first < len(buf):
        raise ValueError("entry table ends at 0x%X but the first entry starts at 0x%X — "
                         "the table would overwrite it" % (len(buf), first))
    return bytes(buf) + b"\x00" * (first - len(buf))


def header_sectors(align, narc, count) -> int:
    """Sectors the header + archive table + entry table occupy."""
    return -(-(0x18 + narc * 16 + count * 16) // align)


# -- contiguous I/O across the archive files -----------------------------------
class VolumeReader:
    """Random access to the volume by absolute offset, spanning archive boundaries."""

    def __init__(self, game_dir, arcs, clean=False):
        self.dir, self.arcs, self.clean = Path(game_dir), arcs, clean
        self._fh = {}

    def _f(self, name):
        if name not in self._fh:
            p = self.dir / name
            if self.clean and (self.dir / (name + ".orig")).exists():
                p = self.dir / (name + ".orig")
            self._fh[name] = open(p, "rb")
        return self._fh[name]

    def read(self, off, n) -> bytes:
        out = bytearray()
        while n > 0:
            a = next((a for a in self.arcs if a["lo"] <= off < a["hi"]), None)
            if a is None:
                raise IOError("volume offset 0x%X is outside the volume" % off)
            take = min(n, a["hi"] - off)
            f = self._f(a["name"])
            f.seek(off - a["lo"])
            b = f.read(take)
            if len(b) != take:
                # The final archive can be short of its declared size when its tail is all
                # padding; the missing bytes ARE the zeros they stand for.
                b = b + b"\x00" * (take - len(b))
            out += b
            off += take
            n -= take
        return bytes(out)

    def close(self):
        for f in self._fh.values():
            f.close()
        self._fh.clear()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class VolumeWriter:
    """Sequential writer over the archive files; splits automatically at boundaries."""

    def __init__(self, game_dir, arcs):
        self.dir, self.arcs = Path(game_dir), arcs
        self.pos, self.ai = 0, 0
        self._f = None
        self._open(0)

    def _open(self, i):
        if self._f:
            self._f.close()
        self.ai = i
        self._f = open(self.dir / self.arcs[i]["name"], "wb")

    def write(self, buf):
        mv = memoryview(buf)
        while len(mv):
            a = self.arcs[self.ai]
            room = a["hi"] - self.pos
            if room <= 0:
                self._open(self.ai + 1)
                continue
            take = min(len(mv), room)
            self._f.write(mv[:take])
            self.pos += take
            mv = mv[take:]

    def pad_to(self, off):
        if off < self.pos:
            raise ValueError("cannot pad backwards: at 0x%X, asked for 0x%X" % (self.pos, off))
        n = off - self.pos
        while n > 0:
            take = min(n, 1 << 22)
            self.write(b"\x00" * take)
            n -= take

    def close(self):
        # Every archive must reach its declared size or the next one's offsets shift.
        self.pad_to(self.arcs[-1]["hi"])
        if self._f:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


# -- names ---------------------------------------------------------------------
def _name_index():
    """crc32 -> friendly asset name, from the bundled catalogues. Purely cosmetic: the manifest
    stores the filename it actually used, so repack never has to reproduce this mapping."""
    import csv
    try:
        from . import resources as R
    except ImportError:
        import resources as R
    m = {}

    def add(crc, nm):
        if crc is not None and nm:
            m.setdefault(crc, nm)

    for fn, cols in (("named_assets.csv", ("name",)),
                     ("team_iff_catalog.csv", ("iff",)),
                     ("discovered_assets.csv", ("iff",))):
        try:
            p = R.data_path(fn)
        except Exception:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    nm = next((r[c].strip() for c in cols if r.get(c)), "")
                    if not nm:
                        continue
                    raw = (r.get("crc32") or "").strip()
                    if raw:
                        try:
                            add(int(raw, 16), nm)
                        except ValueError:
                            pass
                    if not nm.lower().startswith("disc_"):
                        add(crc_of(nm), nm)
        except (OSError, csv.Error):
            continue

    # The catalogues list the clubs that existed in 2010. Every team asset name is FORMULAIC,
    # though, so expansion (SEA/VGK), relocation (ATL->WPG, PHO->UTA) and added mask slots all
    # mint names no CSV has ever heard of. Those entries used to extract as unknown_<crc>.bin --
    # right bytes, useless filename. Generating the families costs a few thousand crc32s and
    # needs no new catalogue to keep in sync. `add` is setdefault, so a real catalogue name
    # always wins over a generated one.
    codes = {mm.group(1) for mm in
             (re.match(r"^logo_([a-z0-9]{2,4})\.iff$", n.lower()) for n in m.values()) if mm}
    codes |= set(_EXTRA_TEAM_CODES)
    for code in sorted(codes):
        for pat in _TEAM_ASSET_PATTERNS:
            add(crc_of(pat % code), pat % code)
        for pat in _UNIFORM_SLOT_PATTERNS:
            for slot in _UNIFORM_SLOTS:
                add(crc_of(pat % (code, slot)), pat % (code, slot))
    for g in range(1, 13):                      # goalie mask slots: 6-bit pattern id, so 0..63
        for pat_id in range(64):
            nm = "helmet_g%02d_pattern_%02d.iff" % (g, pat_id)
            add(crc_of(nm), nm)
    for nm in _KNOWN_BIN_NAMES:
        add(crc_of(nm), nm)
    return m


# Team-asset name families, in the spelling extra_teams.FAMILY uses. Kept here rather than
# imported so this module stays dependency-free -- it has to work during a first-launch extract,
# before anything else is loaded.
_TEAM_ASSET_PATTERNS = ("logo_%s.iff", "rink_%s.iff", "led_%s.iff", "arena_%s.iff",
                        "arena_presentation_%s.iff", "zamboni_%s.iff", "zamboni_team_%s.iff",
                        "ice_%s_playoffs.iff", "ice_%s_finals.iff")
_UNIFORM_SLOT_PATTERNS = ("uniform_%s_%s.iff", "uniform_base_%s_%s.iff",
                          "uniform_frontend_%s_%s.iff")
_UNIFORM_SLOTS = ("home", "away", "alt", "third", "winter")
# Clubs the shipped catalogues cannot know about: the two expansion teams and the relocations.
_EXTRA_TEAM_CODES = ("sea", "vgk", "wpg", "uta", "atl", "phx", "ari")
# Audio banks the catalogues miss; these relocate on modding and would read as 200 MB of
# unknown_<crc>.bin in the tree.
_KNOWN_BIN_NAMES = ("lines.bin", "lines_ts.bin", "lines_ps.bin", "palines.bin", "paplyrs.bin",
                    "pamusic.bin", "teams.bin", "players.bin", "crowd.bin", "chants.bin",
                    "chatter.bin", "sfx.bin", "music.bin", "loadingaudio.bin",
                    "loadingaudio_teams.bin")


def _safe(nm: str) -> str:
    keep = "-_.()[] "
    return "".join(c if (c.isalnum() or c in keep) else "_" for c in nm).strip() or "entry"


# -- extract -------------------------------------------------------------------
def extract(game_dir, out_dir, clean=False, log=print, progress=None, strict=True):
    """Unpack every TOC entry into out_dir, one file each, plus the manifest.

    Layout is FLAT: which archive an entry currently sits in is a fact about the packing, not
    about the entry, and repack reassigns it freely. Returns the manifest dict."""
    import json
    game_dir, out_dir = Path(game_dir), Path(out_dir)
    align, arcs, ents, vol = read_header(game_dir, clean=clean)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = _name_index()

    ordered = sorted(ents, key=lambda e: e["sector"])
    used, records, gaps = set(), {}, []
    cursor = header_sectors(align, len(arcs), len(ents)) * align

    with VolumeReader(game_dir, arcs, clean=clean) as vr:
        # Any non-zero byte BETWEEN entries would be data the manifest does not describe, and
        # repack would silently drop it. Check rather than assume.
        for n, e in enumerate(ordered):
            if e["off"] > cursor:
                chunk = vr.read(cursor, e["off"] - cursor)
                if any(chunk):
                    gaps.append((cursor, e["off"] - cursor))
            cursor = max(cursor, e["off"] + e["size"])

            nm = names.get(e["crc"])
            fn = _safe(nm) if nm else ("unknown_%08X.bin" % e["crc"])
            if fn.lower() in used:
                stem, dot, ext = fn.rpartition(".")
                fn = ("%s_%08X.%s" % (stem, e["crc"], ext)) if dot else ("%s_%08X" % (fn, e["crc"]))
            used.add(fn.lower())

            with open(out_dir / fn, "wb") as fo:
                off, left = e["off"], e["size"]
                while left > 0:
                    take = min(left, 1 << 24)
                    fo.write(vr.read(off, take))
                    off += take
                    left -= take
            records[e["idx"]] = {"idx": e["idx"], "crc": "%08X" % e["crc"], "flags": e["flags"],
                                 "size": e["size"], "sector": e["sector"], "file": fn,
                                 "name": nm or ""}
            if progress and (n + 1) % 100 == 0:
                progress(n + 1, len(ordered))

    if gaps:
        # On a PRISTINE volume every inter-entry byte is zero — that is what proved the container
        # model, so a stray byte there means we have misread the layout and must not proceed.
        # On a volume the launcher has already modified it is expected: relocating a resource
        # leaves its old footprint behind, and nothing reads those bytes. Dropping them is right.
        msg = ("%d inter-entry gaps hold non-zero bytes (first at 0x%X, %d bytes)"
               % (len(gaps), gaps[0][0], gaps[0][1]))
        if strict:
            raise RuntimeError(msg + " — repack would drop them; refusing to trust this tree")
        log("  note: " + msg + " — stale bytes from earlier relocations; not carried over")

    man = {"align": align, "volume_size": vol,
           "archives": [{"name": a["name"], "size": a["size"]} for a in arcs],
           "entries": [records[i] for i in sorted(records)]}
    with open(out_dir / MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    log("extracted %d entries to %s" % (len(records), out_dir))
    return man


def load_manifest(out_dir):
    import json
    p = Path(out_dir) / MANIFEST
    if not p.exists():
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def is_extracted(out_dir) -> bool:
    """True when out_dir holds a manifest AND every file it names is present."""
    man = load_manifest(out_dir)
    if not man:
        return False
    out_dir = Path(out_dir)
    return all((out_dir / e["file"]).exists() for e in man["entries"])


# -- change detection ----------------------------------------------------------
def _stamp(p: Path):
    st = p.stat()
    return [st.st_size, st.st_mtime_ns]


def stamp_tree(out_dir, man=None):
    """Record size+mtime for every entry file so repack knows what to rewrite without
    re-hashing 6 GB — the same edit detection the audio editor uses."""
    import json
    out_dir = Path(out_dir)
    man = man or load_manifest(out_dir)
    for e in man["entries"]:
        e["stamp"] = _stamp(out_dir / e["file"])
    with open(out_dir / MANIFEST, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    return man


def rename_unknowns(out_dir, log=print):
    """Re-derive names for entries that extracted as unknown_<crc>.bin, renaming them in place.

    The TOC stores only crc32(NAME), never the name, so a file can only be named by reversing the
    hash through a dictionary of candidates. That dictionary grows -- a new expansion club, a
    relocation, an added mask slot -- and re-extracting 6 GB to pick up a better one would be
    absurd. This renames the file and updates the manifest instead. No bytes move, so the stamps
    stay valid and nothing about repack changes. Returns how many were named.
    """
    out_dir = Path(out_dir)
    man = load_manifest(out_dir)
    if not man:
        return 0
    names = _name_index()
    used = {e["file"].lower() for e in man["entries"]}
    renamed = 0
    for e in man["entries"]:
        if e.get("name"):
            continue
        nm = names.get(int(e["crc"], 16))
        if not nm:
            continue
        fn = _safe(nm)
        if fn.lower() in used:                  # same disambiguation rule extract() uses
            stem, dot, ext = fn.rpartition(".")
            fn = ("%s_%s.%s" % (stem, e["crc"], ext)) if dot else ("%s_%s" % (fn, e["crc"]))
            if fn.lower() in used:
                continue
        src, dst = out_dir / e["file"], out_dir / fn
        if not src.exists() or dst.exists():
            continue
        src.rename(dst)
        used.discard(e["file"].lower())
        used.add(fn.lower())
        e["file"], e["name"] = fn, nm
        renamed += 1
    if renamed:
        stamp_tree(out_dir, man)                # rewrites the manifest with the new filenames
        log("  named %d previously unidentified entr%s"
            % (renamed, "y" if renamed == 1 else "ies"))
    return renamed


def changed_entries(out_dir, man=None):
    """Entries whose file differs from what the archives hold -> [(entry, new_size)]."""
    out_dir = Path(out_dir)
    man = man or load_manifest(out_dir)
    out = []
    for e in man["entries"]:
        p = out_dir / e["file"]
        if not p.exists():
            raise RuntimeError("entry file missing: %s" % p)
        st = _stamp(p)
        if e.get("stamp") != st or e["size"] != st[0]:
            out.append((e, st[0]))
    return out


# -- repack --------------------------------------------------------------------
def _next_archive_name(existing, game_dir=None):
    """Successor to the shipped 0A/0B/1A/1B naming: 1C, 1D, ... then 2A onward.

    Digit 0 is skipped and any name already present as a FILE is skipped, matching
    archive_textures._next_arc_name — the two must agree or a volume grown by one path and then
    by the other would mint two different fifth archives. The name field is 8 bytes of UTF-16BE
    with a NUL terminator, so 3 characters is the hard ceiling.
    """
    used = {n.upper() for n in existing}
    for hi in "123456789":
        for lo in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            nm = hi + lo
            if nm not in used and not (game_dir and (Path(game_dir) / nm).exists()):
                return nm
    raise RuntimeError("no free archive name")


def repack(out_dir, game_dir, log=print, progress=None):
    """Push the extracted tree back into the game archives, minimally.

    An entry whose size is unchanged is written in place. An entry that GREW is appended at the
    tail of the volume and its TOC sector repointed — the same move the in-place texture writer
    has always made, and the reason a grown asset never forces a 6 GB rewrite. The last archive
    is extended to absorb the append; if that would push it past SPILL_CEILING a new archive is
    added instead, which is what lifts the per-file ceiling.
    """
    out_dir, game_dir = Path(out_dir), Path(game_dir)
    man = load_manifest(out_dir)
    if not man:
        raise RuntimeError("no %s in %s — extract first" % (MANIFEST, out_dir))
    align = man["align"]
    changed = changed_entries(out_dir, man)
    if not changed:
        log("repack: nothing changed")
        return "nothing to repack"

    arcs, cum = [], 0
    for a in man["archives"]:
        arcs.append({"name": a["name"], "size": a["size"], "lo": cum, "hi": cum + a["size"]})
        cum += a["size"]
    ents = {e["idx"]: e for e in man["entries"]}
    end = max(e["sector"] * align + e["size"] for e in man["entries"])
    append_at = -(-end // align) * align          # sector-aligned tail of the live data

    inplace, moved, added_arcs = 0, 0, []
    # An entry that moves or shrinks leaves its old bytes behind. Nothing in the game would
    # read them, but they are bytes no manifest describes — which breaks the "inter-entry gaps
    # are zero" invariant extract relies on, so a later re-extract would refuse the tree.
    # Remember each old footprint and blank it below.
    stale = [(e["sector"] * align, -(-e["size"] // align) * align) for e, _ in changed]
    for e, newsize in changed:
        if newsize > e["size"]:
            e["sector"] = append_at // align
            append_at += -(-newsize // align) * align
            moved += 1
        else:
            inplace += 1
        e["size"] = newsize

    # Grow / add archives until the volume covers append_at.
    while append_at > arcs[-1]["hi"]:
        last = arcs[-1]
        room = SPILL_CEILING - last["size"]
        want = append_at - last["hi"]
        if room >= want:
            last["size"] += want
            last["hi"] += want
            break
        if room > 0:
            last["size"] += room
            last["hi"] += room
        nm = _next_archive_name([a["name"] for a in arcs], game_dir)
        arcs.append({"name": nm, "size": 0, "lo": arcs[-1]["hi"], "hi": arcs[-1]["hi"]})
        added_arcs.append(nm)
    if arcs[-1]["size"] == 0:                     # a just-added archive still needs its size
        grow = append_at - arcs[-1]["lo"]
        arcs[-1]["size"] = -(-grow // align) * align
        arcs[-1]["hi"] = arcs[-1]["lo"] + arcs[-1]["size"]

    man["archives"] = [{"name": a["name"], "size": a["size"]} for a in arcs]
    man["volume_size"] = arcs[-1]["hi"]

    # Every archive file must exist before we seek into it. Deliberately NOT padded out to its
    # declared size: after a compaction an archive is legitimately shorter than the capacity the
    # header advertises (1B is, by ~440 MB), nothing reads that tail, and re-adding it as zeros
    # would silently undo the compaction. Seeking past the end and writing extends the file by
    # exactly as much as the write needs.
    for a in arcs:
        p = game_dir / a["name"]
        if not p.exists():
            with open(p, "wb"):
                pass

    def _write(off, data):
        left, pos = len(data), 0
        while left > 0:
            a = next(a for a in arcs if a["lo"] <= off < a["hi"])
            take = min(left, a["hi"] - off)
            with open(game_dir / a["name"], "r+b") as f:
                f.seek(off - a["lo"])
                f.write(data[pos:pos + take])
            off += take
            pos += take
            left -= take

    for off, span in stale:
        while span > 0:
            take = min(span, 1 << 22)
            _write(off, bytes(take))
            off += take
            span -= take
    for n, (e, _sz) in enumerate(changed):
        blob = (out_dir / e["file"]).read_bytes()
        _write(e["sector"] * align, blob + b"\x00" * ((-len(blob)) % align))
        if progress:
            progress(n + 1, len(changed))

    _write(0, build_header(align, arcs, [ents[i] for i in sorted(ents)]))
    stamp_tree(out_dir, man)

    msg = "repacked %d entries (%d in place, %d appended)" % (len(changed), inplace, moved)
    if added_arcs:
        msg += "; added archive(s) " + ", ".join(added_arcs)
    log(msg)
    return msg


def verify_roundtrip(out_dir, game_dir, clean=False, log=print) -> bool:
    """Rebuild the whole volume from the tree in memory and hash it against the real archives —
    the proof that extract lost nothing. First-launch extract runs this before trusting the tree."""
    import hashlib
    out_dir, game_dir = Path(out_dir), Path(game_dir)
    man = load_manifest(out_dir)
    align = man["align"]
    arcs, cum = [], 0
    for a in man["archives"]:
        arcs.append({"name": a["name"], "size": a["size"], "lo": cum, "hi": cum + a["size"]})
        cum += a["size"]

    real = {}
    for a in arcs:
        p = game_dir / (a["name"] + ".orig")
        if not (clean and p.exists()):
            p = game_dir / a["name"]
        h = hashlib.sha1()
        with open(p, "rb") as f:
            for c in iter(lambda: f.read(1 << 22), b""):
                h.update(c)
        real[a["name"]] = h.hexdigest()

    rebuilt = {a["name"]: hashlib.sha1() for a in arcs}
    pos, ai = 0, 0

    def emit(buf):
        nonlocal pos, ai
        mv = memoryview(buf)
        while len(mv):
            a = arcs[ai]
            room = a["hi"] - pos
            if room <= 0:
                ai += 1
                continue
            take = min(len(mv), room)
            rebuilt[a["name"]].update(mv[:take])
            pos += take
            mv = mv[take:]

    emit(build_header(align, arcs, sorted(man["entries"], key=lambda e: e["idx"])))
    for e in sorted(man["entries"], key=lambda e: e["sector"]):
        off = e["sector"] * align
        if off < pos:
            log("  OVERLAP at entry %d (%s)" % (e["idx"], e["file"]))
            return False
        emit(b"\x00" * (off - pos))
        with open(out_dir / e["file"], "rb") as f:
            for c in iter(lambda: f.read(1 << 22), b""):
                emit(c)
        emit(b"\x00" * ((-e["size"]) % align))
    emit(b"\x00" * (arcs[-1]["hi"] - pos))

    ok = True
    for a in arcs:
        same = rebuilt[a["name"]].hexdigest() == real[a["name"]]
        ok = ok and same
        log("  %-4s %s" % (a["name"], "MATCH" if same else "MISMATCH"))
    log("round trip: " + ("BYTE-IDENTICAL" if ok else "FAILED"))
    return ok
