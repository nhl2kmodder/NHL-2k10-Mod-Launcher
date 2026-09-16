"""releasepack.py — ship a WHOLE mod as a delta against the clean game.

A .n2kpack (modpack.py) is an ADDITIVE merge: a handful of textures, WAVs and captured field
values that layer onto whatever the recipient already has. That model stops working once the mod
is structural — relocated wave banks, a fifth archive, added TOC entries, a rebuilt global.iff, a
patched XEX. There is no "merge" for those: the release IS a volume layout.

So a .n2kreleasepack is a different kind of artefact. It describes ONE EXACT VOLUME — every TOC
entry, its sector, the archive geometry — and for each entry says where its bytes come from:

    src = "clean"   the recipient's own pristine copy already holds them (most entries)
    src = "delta"   pristine bytes + a block delta shipped in the pack
    src = "raw"     the entry does not exist in the clean game at all (expansion-club art, a
                    relocated bank that outgrew its slot), so the bytes ride along in full

Installing therefore RECONSTRUCTS the author's volume byte-for-byte out of the recipient's own
game files. Two things follow from that, and both are the point:

  * The pack stays small — nothing unchanged is shipped.
  * It CANNOT be installed without a legitimate, unmodified copy of the game: the deltas have
    nothing to apply to, and every base is sha1-checked before it is used. That is the ownership
    gate — a folder holding 0A/0B/1A/1B/default.xex/nxeart whose volume layout and XEX hash match
    the ones the release was built against.

Identity, as everywhere else in this launcher, is the TOC key crc32(NAME) — never a filename,
never an index. `idx`/`sector` come from the shipped manifest because the release owns the
layout; `crc` is what pairs an entry with its clean counterpart.

The install is a full volume WRITE, not a patch: header, then every entry at its shipped sector,
then each archive padded to its declared size (volume.VolumeWriter). That is deliberate —
project_volume_full_rebuild is the whole reason this module goes nowhere near volume.repack,
which is a minimal diff and can never reset a layout.

Also carried, because a release is more than the volume:
  * default.xex   — as a delta against the clean XEX (same ownership gate, same sha1 check)
  * Roster.ROS    — in full; it is the author's own save data, not game code

NOT carried: the launcher's staging tree (NHL2k10_Extracted_Files). Those files are INPUTS to
edits already baked into the volume this pack describes. The recipient's staging tree is theirs,
and re-applying it on top of the release is offered as a separate, optional step afterwards.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import struct
import time
import zipfile
from pathlib import Path

try:
    from . import volume as V
except ImportError:                                   # running as a script / from tools/
    import volume as V

EXT     = ".n2kreleasepack"
FORMAT  = "nhl2k10-releasepack"
VERSION = 1

MANIFEST_ARC = "release.json"
ENTRY_DIR    = "entries"
XEX_ARC      = "xex/default.xex.delta"
ROSTER_ARC   = "roster/Roster.ROS"

# What a folder must hold to count as a legitimate, unmodified copy of the game. The four
# archives carry the volume; default.xex is the executable; nxeart is the disc's art blob, the
# one file no repack of the volume could ever produce, so it is cheap evidence the folder came
# off a real disc rather than out of somebody else's release.
CLEAN_FILES = ("0A", "0B", "1A", "1B", "default.xex", "nxeart")

CHUNK = 1 << 22


def _sha1_file(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(CHUNK), b""):
            h.update(c)
    return h.hexdigest()


def _human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return "%d B" % n if u == "B" else "%.1f %s" % (n, u)
        n /= 1024.0


# -- block delta ---------------------------------------------------------------
# Deliberately the dumbest thing that works: fixed-size blocks, ship the ones that differ. A
# rolling-hash delta would win on an INSERT (everything after it shifts), but the edits this pack
# carries are overwhelmingly in place — a texture re-encoded into its slot, a record rewritten, a
# table repointed — where fixed blocks are already near-optimal and, unlike a resync-based
# format, cannot go quadratic on a 200 MB bank.

DELTA_MAGIC = b"N2KD"
DELTA_VER   = 1
BLOCK       = 4096
_DHDR       = "<BIQQ"


def make_delta(src: bytes, dst: bytes, block: int = BLOCK) -> bytes:
    recs = []
    n, off = len(dst), 0
    while off < n:
        end = min(off + block, n)
        if src[off:end] != dst[off:end]:
            if recs and recs[-1][0] + len(recs[-1][1]) == off:      # coalesce adjacent runs
                recs[-1] = (recs[-1][0], recs[-1][1] + dst[off:end])
            else:
                recs.append((off, dst[off:end]))
        off = end
    head = bytearray(DELTA_MAGIC)
    head += struct.pack(_DHDR, DELTA_VER, block, len(src), len(dst))
    head += hashlib.sha1(src).digest()
    head += hashlib.sha1(dst).digest()
    head += struct.pack("<I", len(recs))
    for o, b in recs:
        head += struct.pack("<QI", o, len(b))
    return bytes(head) + b"".join(b for _o, b in recs)


def apply_delta(src: bytes, delta: bytes) -> bytes:
    if delta[:4] != DELTA_MAGIC:
        raise ValueError("not a delta")
    ver, _block, ssz, dsz = struct.unpack_from(_DHDR, delta, 4)
    if ver != DELTA_VER:
        raise ValueError("delta format v%d is newer than this launcher understands" % ver)
    o = 4 + struct.calcsize(_DHDR)
    s_sha, d_sha = delta[o:o + 20], delta[o + 20:o + 40]
    o += 40
    if len(src) != ssz or hashlib.sha1(src).digest() != s_sha:
        raise ValueError("the base bytes are not what this delta was built against")
    (nrec,) = struct.unpack_from("<I", delta, o)
    o += 4
    recs = []
    for _ in range(nrec):
        ro, rl = struct.unpack_from("<QI", delta, o)
        o += struct.calcsize("<QI")
        recs.append((ro, rl))
    out = bytearray(src[:dsz])
    if len(out) < dsz:
        out += bytes(dsz - len(out))
    for ro, rl in recs:
        out[ro:ro + rl] = delta[o:o + rl]
        o += rl
    if hashlib.sha1(out).digest() != d_sha:
        raise ValueError("delta applied but the result does not hash as expected")
    return bytes(out)


# -- the ownership gate --------------------------------------------------------
def verify_clean(d):
    """(ok, missing, detail) — does this folder hold an unmodified copy of the game?

    Presence of the six files is the ownership check the user sees. The header read on top of it
    is the sanity check: a folder with six files of the right names but a volume we cannot parse
    is not something to install against, and saying so here beats failing 6 GB in.
    """
    d = Path(d)
    missing = [n for n in CLEAN_FILES if not (d / n).exists()]
    if missing:
        return False, missing, "missing: " + ", ".join(missing)
    for n in ("0A", "0B", "1A", "1B"):
        if (d / (n + ".orig")).exists():
            return False, [], ("%s.orig sits beside %s — the launcher has already modded this "
                               "folder, so it is not a clean copy." % (n, n))
    try:
        _align, arcs, ents, vol = V.read_header(d)
    except Exception as e:
        return False, [], "the volume header could not be read (%s)" % e
    return True, [], "%d entries, %s, archives %s" % (
        len(ents), _human(vol), ", ".join(a["name"] for a in arcs))


def is_releasepack(path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read(MANIFEST_ARC).decode("utf-8")).get("format") == FORMAT
    except Exception:
        return False


def read_manifest(path) -> dict:
    with zipfile.ZipFile(path) as z:
        return json.loads(z.read(MANIFEST_ARC).decode("utf-8"))


def describe(man) -> str:
    """Human summary for the confirm dialog."""
    ents = man.get("entries", [])
    # "changed" vs "added" is about the ENTRY, not about how its bytes travel: an entry
    # rewritten end to end ships raw because a delta of it would be bigger, and calling that
    # "added" would misreport the release to the person installing it.
    def _is_new(e):
        # Packs built before the "new" flag existed have to be read the old way, where an entry
        # that shipped raw was assumed to be one the clean game does not have.
        return e.get("new", e.get("src") == "raw")
    ch = sum(1 for e in ents if e.get("payload") and not _is_new(e))
    nw = sum(1 for e in ents if e.get("payload") and _is_new(e))
    lines = ["%s" % (man.get("name") or "Untitled release"),
             "by %s" % (man.get("author") or "unknown"),
             "built %s" % (man.get("created") or "?"), "",
             "%d game files changed, %d added (of %d in the game)" % (ch, nw, len(ents))]
    if man.get("xex"):
        lines.append("default.xex is patched")
    if man.get("roster"):
        lines.append("includes a roster save (%s)"
                     % (man["roster"].get("source_name") or "Roster.ROS"))
    if man.get("notes"):
        lines += ["", man["notes"]]
    return "\n".join(lines)



def overlaps(align, ents, names=None):
    """[(a, b, bytes)] — entries whose declared extent runs into the next entry's sector.

    A volume is one address space and the install is a single forward pass over it, so two entries
    claiming the same bytes is not something a release can express — one of them is already
    corrupt on the author's own disk, whichever was written second. Catch it while it is still a
    build-time error with two filenames in it, rather than a pad-backwards crash 6 GB into
    somebody else's install.
    """
    out, reach = [], None
    for e in sorted(ents, key=lambda x: (x["sector"], x["size"])):
        start = e["sector"] * align
        if reach is not None and start < reach["sector"] * align + reach["size"]:
            out.append((reach, e, reach["sector"] * align + reach["size"] - start))
        if reach is None or start + e["size"] > reach["sector"] * align + reach["size"]:
            reach = e
    return out


def _overlap_report(align, ents, names=None):
    names = names or {}
    def nm(c):
        return names.get(c) or "unknown_%08x.bin" % c
    ov = overlaps(align, ents)
    if not ov:
        return ""
    lines = ["%d entries overlap in the volume:" % len(ov)]
    for a, b, n in ov[:8]:
        lines.append("  %s (sector %d, %s) runs %s into %s (sector %d)"
                     % (nm(a["crc"]), a["sector"], _human(a["size"]), _human(n),
                        nm(b["crc"]), b["sector"]))
    if len(ov) > 8:
        lines.append("  ... and %d more" % (len(ov) - 8))
    return "\n".join(lines)


# -- build ---------------------------------------------------------------------
def _load_cache(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


def build(clean_dir, mod_dir, out_path, *, clean_tree=None, roster_path=None,
          name="", author="", notes="", cache_path=None, log=print, progress=None):
    """Diff the modded volume + XEX against the clean ones and write the pack.

    Modded entry bytes are read from the LIVE ARCHIVES, never from the launcher's extracted tree:
    the tree can legitimately hold pre-relocation bytes under a perfectly valid stamp
    (project_tree_drift_vs_changed), and a release built from those would ship a silent revert.
    The archives are authoritative and, because the tree is write-through, always current.

    Clean bytes come from `clean_tree` when it is extracted (one file per entry, so their sha1s
    cache across builds) and from the clean archives otherwise. They are the same bytes.
    """
    clean_dir, mod_dir, out_path = Path(clean_dir), Path(mod_dir), Path(out_path)
    ok, _missing, detail = verify_clean(clean_dir)
    if not ok:
        raise RuntimeError("clean game folder rejected: %s" % detail)
    log("clean : %s" % detail)

    c_align, c_arcs, c_ents, c_vol = V.read_header(clean_dir)
    m_align, m_arcs, m_ents, m_vol = V.read_header(mod_dir)
    if c_align != m_align:
        raise RuntimeError("sector size differs between the two volumes")
    log("modded: %d entries, %s, archives %s"
        % (len(m_ents), _human(m_vol), ", ".join(a["name"] for a in m_arcs)))

    bad = _overlap_report(m_align, m_ents, V._name_index())
    if bad:
        raise RuntimeError(
            bad + "\n\nThe LATER entry won when it was written, so the earlier one is already "
            "truncated in your own archives. Relocate one of them into free space and rebuild; "
            "shipping this layout would ship the corruption.")

    clean_by_crc = {e["crc"]: e for e in c_ents}
    names = V._name_index()

    tree_files = {}
    if clean_tree:
        tman = V.load_manifest(clean_tree)
        if tman:
            tree_files = {int(e["crc"], 16): e for e in tman["entries"]}

    cache_path = Path(cache_path) if cache_path else None
    cache = _load_cache(cache_path) if cache_path else {}
    dirty = False

    def _tree_path(ce):
        te = tree_files.get(ce["crc"])
        if not te:
            return None
        p = Path(clean_tree) / te["file"]
        return p if p.exists() and p.stat().st_size == ce["size"] else None

    def clean_bytes(ce, vr):
        p = _tree_path(ce)
        return p.read_bytes() if p else vr.read(ce["off"], ce["size"])

    def clean_sha(ce, vr):
        nonlocal dirty
        key = "%08X" % ce["crc"]
        hit = cache.get(key)
        if hit and hit[0] == ce["size"]:
            return hit[1]
        p = _tree_path(ce)
        s = _sha1_file(p) if p else hashlib.sha1(vr.read(ce["off"], ce["size"])).hexdigest()
        cache[key] = [ce["size"], s]
        dirty = True
        return s

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".part")

    entries, n_same, n_delta, n_raw, payload = [], 0, 0, 0, 0
    ordered = sorted(m_ents, key=lambda e: e["sector"])

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z, \
            V.VolumeReader(mod_dir, m_arcs) as mvr, V.VolumeReader(clean_dir, c_arcs) as cvr:
        for n, e in enumerate(ordered):
            crc8 = "%08X" % e["crc"]
            nm = names.get(e["crc"], "")
            rec = {"idx": e["idx"], "crc": crc8, "flags": e["flags"], "size": e["size"],
                   "sector": e["sector"], "name": nm,
                   "file": V._safe(nm) if nm else ("unknown_%s.bin" % crc8)}
            ce = clean_by_crc.get(e["crc"])
            blob_mod = None
            if ce is not None and ce["size"] == e["size"]:
                # Same size is the common case. Hash both rather than compare 6 GB of bytes
                # twice, and reuse the cached clean side across builds.
                blob_mod = mvr.read(e["off"], e["size"])
                msha = hashlib.sha1(blob_mod).hexdigest()
                rec["sha1"] = msha
                if msha == clean_sha(ce, cvr):
                    rec["src"] = "clean"
                    entries.append(rec)
                    n_same += 1
                    if progress and (n + 1) % 50 == 0:
                        progress(n + 1, len(ordered))
                    continue
            if blob_mod is None:
                blob_mod = mvr.read(e["off"], e["size"])
                rec["sha1"] = hashlib.sha1(blob_mod).hexdigest()

            if ce is not None:
                base = clean_bytes(ce, cvr)
                data = make_delta(base, blob_mod)
                # A delta only earns its place while it is smaller than the bytes themselves. An
                # entry rewritten end to end (a relocated wave bank, a rebuilt global.iff) deltas
                # to slightly MORE than its own size, and shipping that would be silly — and
                # would make the install do a pointless base read and hash for no saving.
                if len(data) >= e["size"]:
                    data = blob_mod
                    arc = "%s/%s.raw" % (ENTRY_DIR, crc8)
                    rec.update(src="raw", payload=arc, new=False)
                    n_delta += 1
                    log("  ~ %-42s %10s  raw (delta was no smaller)"
                        % (rec["file"], _human(e["size"])))
                else:
                    arc = "%s/%s.delta" % (ENTRY_DIR, crc8)
                    rec.update(src="delta", payload=arc, new=False,
                               base_size=ce["size"],
                               base_sha1=hashlib.sha1(base).hexdigest())
                    n_delta += 1
                    log("  ~ %-42s %10s  delta %s"
                        % (rec["file"], _human(e["size"]), _human(len(data))))
            else:
                data = blob_mod
                arc = "%s/%s.raw" % (ENTRY_DIR, crc8)
                rec.update(src="raw", payload=arc, new=True)
                n_raw += 1
                log("  + %-42s %10s  (new entry)" % (rec["file"], _human(e["size"])))
            z.writestr(arc, data)
            payload += len(data)
            entries.append(rec)
            if progress and (n + 1) % 50 == 0:
                progress(n + 1, len(ordered))
        if progress:
            progress(len(ordered), len(ordered))

        # -- the XEX -----------------------------------------------------------
        xex = None
        c_xex, m_xex = clean_dir / "default.xex", mod_dir / "default.xex"
        if m_xex.exists():
            cb, mb = c_xex.read_bytes(), m_xex.read_bytes()
            if cb != mb:
                d = make_delta(cb, mb)
                z.writestr(XEX_ARC, d)
                payload += len(d)
                xex = {"payload": XEX_ARC, "size": len(mb),
                       "sha1": hashlib.sha1(mb).hexdigest(),
                       "base_size": len(cb), "base_sha1": hashlib.sha1(cb).hexdigest()}
                log("  ~ default.xex %s  delta %s" % (_human(len(mb)), _human(len(d))))
            else:
                log("  = default.xex unchanged")

        # -- the roster --------------------------------------------------------
        roster = None
        if roster_path and Path(roster_path).is_file():
            rb = Path(roster_path).read_bytes()
            z.writestr(ROSTER_ARC, rb)
            payload += len(rb)
            roster = {"payload": ROSTER_ARC, "size": len(rb),
                      "sha1": hashlib.sha1(rb).hexdigest(),
                      "source_name": Path(roster_path).name}
            log("  + Roster.ROS %s  (%s)" % (_human(len(rb)), roster_path))
        elif roster_path:
            log("  ! no roster at %s — the pack will carry none" % roster_path)

        man = {
            "format": FORMAT, "version": VERSION,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "name": name, "author": author, "notes": notes,
            "clean": {
                "align": c_align, "volume_size": c_vol, "entry_count": len(c_ents),
                "archives": [{"name": a["name"], "size": a["size"]} for a in c_arcs],
                "xex": {"size": c_xex.stat().st_size, "sha1": _sha1_file(c_xex)},
            },
            "volume": {
                "align": m_align, "volume_size": m_vol,
                "archives": [{"name": a["name"], "size": a["size"]} for a in m_arcs],
            },
            "entries": entries, "xex": xex, "roster": roster,
        }
        z.writestr(MANIFEST_ARC, json.dumps(man, indent=1))

    if dirty and cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
        except Exception:
            pass

    if out_path.exists():
        out_path.unlink()
    tmp.rename(out_path)
    log("")
    log("%d entries: %d unchanged, %d changed, %d new" % (len(entries), n_same, n_delta, n_raw))
    log("payload %s  ->  pack %s" % (_human(payload), _human(out_path.stat().st_size)))
    return man


# -- install -------------------------------------------------------------------
def preflight(pack, clean_dir, install_dir):
    """(ok, message) — everything worth refusing on BEFORE a byte is written.

    Checked here rather than mid-write because the install rewrites the whole volume: a failure
    two thirds of the way through leaves a folder that is neither the release nor what was there.
    """
    clean_dir, install_dir = Path(clean_dir), Path(install_dir)
    try:
        man = read_manifest(pack)
    except Exception as e:
        return False, "that file is not a release pack (%s)." % e
    if man.get("format") != FORMAT:
        return False, "that file is not a release pack."
    if man.get("version", 1) > VERSION:
        return False, ("this pack was built by a newer launcher (format v%d) — update the "
                       "launcher first." % man["version"])
    ok, _m, detail = verify_clean(clean_dir)
    if not ok:
        return False, "clean game files rejected — %s" % detail
    try:
        if clean_dir.resolve() == install_dir.resolve():
            return False, ("the install folder is the same as your clean copy. Point the install "
                           "at your game folder and leave the clean files untouched.")
    except Exception:
        pass

    bad = _overlap_report(man["volume"]["align"], man["entries"])
    if bad:
        return False, ("this pack describes a volume whose entries overlap, so it cannot be "
                       "installed:\n\n" + bad)

    _align, arcs, ents, _vol = V.read_header(clean_dir)
    want = man["clean"]
    if len(ents) != want["entry_count"] or \
            [(a["name"], a["size"]) for a in arcs] != \
            [(a["name"], a["size"]) for a in want["archives"]]:
        return False, ("those clean files are not the build of the game this release was made "
                       "from — the volume layout differs.")
    if _sha1_file(clean_dir / "default.xex") != want["xex"]["sha1"]:
        return False, ("the clean default.xex does not match the one this release was built "
                       "against — a different region, or an already-patched executable.")

    need = man["volume"]["volume_size"] + sum(e["size"] for e in man["entries"])
    try:
        probe = install_dir if install_dir.exists() else install_dir.parent
        free = shutil.disk_usage(probe).free
        if free < need + (1 << 30):
            return False, ("not enough room on that drive: the install needs about %s (the game "
                           "plus its extracted copy) and %s is free." % (_human(need), _human(free)))
    except Exception:
        pass
    return True, describe(man)


def install(pack, clean_dir, install_dir, log=print, progress=None, write_tree=True):
    """Reconstruct the release's volume into install_dir out of the recipient's clean files.

    One sequential pass — header, every entry at its shipped sector, then padding to each
    archive's declared size — so the result is the author's layout exactly, not a patched version
    of whatever was there before. Every clean base is sha1-checked before it is used, so wrong or
    tampered source files fail loudly instead of producing a subtly broken game.
    """
    clean_dir, install_dir = Path(clean_dir), Path(install_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    man = read_manifest(pack)
    align = man["volume"]["align"]
    arcs, cum = [], 0
    for a in man["volume"]["archives"]:
        arcs.append({"name": a["name"], "size": a["size"], "lo": cum, "hi": cum + a["size"]})
        cum += a["size"]

    bad = _overlap_report(align, man["entries"])
    if bad:
        # preflight already refuses this; repeated here because install() is callable on its own
        # and a pad-backwards crash would leave a half-written volume behind.
        raise RuntimeError("this pack describes an overlapping volume:\n" + bad)

    _ca, c_arcs, c_ents, _cv = V.read_header(clean_dir)
    clean_by_crc = {"%08X" % e["crc"]: e for e in c_ents}

    ents = sorted(man["entries"], key=lambda e: e["sector"])
    tree = install_dir / V.TREE_NAME if hasattr(V, "TREE_NAME") else \
        install_dir / "NHL2k10_Extracted"
    if write_tree:
        tree.mkdir(parents=True, exist_ok=True)

    log("Writing %s across %s…" % (_human(cum), ", ".join(a["name"] for a in arcs)))
    with zipfile.ZipFile(pack) as z, V.VolumeReader(clean_dir, c_arcs) as cvr, \
            V.VolumeWriter(install_dir, arcs) as vw:
        vw.write(V.build_header(align, [{"name": a["name"], "size": a["size"]} for a in arcs],
                                sorted(man["entries"], key=lambda e: e["idx"])))
        for n, e in enumerate(ents):
            src = e.get("src", "clean")
            if src == "raw":
                blob = z.read(e["payload"])
            else:
                ce = clean_by_crc.get(e["crc"])
                if ce is None:
                    raise RuntimeError("your game files have no entry %s, which this release "
                                       "needs (%s)" % (e["crc"], e.get("file")))
                base = cvr.read(ce["off"], ce["size"])
                if src == "delta":
                    if hashlib.sha1(base).hexdigest() != e["base_sha1"]:
                        raise RuntimeError(
                            "%s in your game files is not the pristine version this release "
                            "patches — install against untouched game files."
                            % (e.get("file") or e["crc"]))
                    blob = apply_delta(base, z.read(e["payload"]))
                else:
                    blob = base
            if len(blob) != e["size"]:
                raise RuntimeError("%s came out %d bytes, expected %d"
                                   % (e.get("file"), len(blob), e["size"]))
            if e.get("sha1") and hashlib.sha1(blob).hexdigest() != e["sha1"]:
                raise RuntimeError("%s did not rebuild correctly (hash mismatch)" % e.get("file"))
            vw.pad_to(e["sector"] * align)
            vw.write(blob)
            vw.pad_to(vw.pos + ((-len(blob)) % align))
            if write_tree:
                (tree / e["file"]).write_bytes(blob)
            if progress and (n + 1) % 25 == 0:
                progress(n + 1, len(ents))
        if progress:
            progress(len(ents), len(ents))

        # -- the XEX -----------------------------------------------------------
        if man.get("xex"):
            out = apply_delta((clean_dir / "default.xex").read_bytes(),
                              z.read(man["xex"]["payload"]))
            (install_dir / "default.xex").write_bytes(out)
            log("default.xex patched (%s)" % _human(len(out)))
        elif not (install_dir / "default.xex").exists():
            shutil.copy2(clean_dir / "default.xex", install_dir / "default.xex")

        # -- the roster --------------------------------------------------------
        ros_out = None
        if man.get("roster"):
            rd = install_dir / "Release Roster"
            rd.mkdir(exist_ok=True)
            ros_out = rd / "Roster.ROS"
            ros_out.write_bytes(z.read(man["roster"]["payload"]))
            log("roster written to %s" % ros_out)

    # nxeart and the title-update folder are not part of the volume; the game folder still needs
    # them, and they are the recipient's own files either way.
    for extra in ("nxeart", "$SystemUpdate"):
        s, d = clean_dir / extra, install_dir / extra
        if s.exists() and not d.exists():
            (shutil.copytree if s.is_dir() else shutil.copy2)(s, d)
            log("copied %s" % extra)

    if write_tree:
        tman = {"align": align, "volume_size": cum,
                "archives": [{"name": a["name"], "size": a["size"]} for a in arcs],
                "entries": [{"idx": e["idx"], "crc": e["crc"], "flags": e["flags"],
                             "size": e["size"], "sector": e["sector"], "file": e["file"],
                             "name": e.get("name", "")}
                            for e in sorted(man["entries"], key=lambda x: x["idx"])]}
        # An older tree in the same folder can hold entry files this release does not have (an
        # asset the author's volume dropped, or a name the dictionary since improved on). They
        # would sit there unreferenced, and the first thing anyone would do with them is trust
        # them, so clear them out rather than leave a second, wrong copy of the game around.
        keep = {e["file"] for e in tman["entries"]} | {V.MANIFEST}
        for stale in tree.iterdir():
            if stale.is_file() and stale.name not in keep:
                try:
                    stale.unlink()
                except OSError:
                    pass
        (tree / V.MANIFEST).write_text(json.dumps(tman, indent=1), encoding="utf-8")
        V.stamp_tree(tree, tman)
        log("extracted tree written to %s" % tree)

    log("Install complete.")
    return {"roster": str(ros_out) if ros_out else "", "manifest": man,
            "tree": str(tree) if write_tree else ""}
