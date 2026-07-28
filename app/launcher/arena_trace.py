"""
NHL 2K10 — Arena / Rink live texture TRACE tool  (dev app)
==========================================================
WHY THIS EXISTS
---------------
The per-team rink/arena assets (rink_<team>.iff, arena_presentation_<team>.iff, led_<team>.iff,
ice_*.iff, and the frontend/UI packs) are NOT flat single-package texture files. Their DRAM
resource tree has a formal record array (count@0x20) covering the FIRST sub-package — those
textures decode perfectly. But the file ALSO concatenates one or more EXTRA sub-packages after
it (jumbotron faces, crowd/seats, stairs, LED ad boards, banners, pucks, …). Each extra
sub-package's per-record VRAM offset (@+0x6C) is RELATIVE TO ITS OWN GROUP BASE, and those group
bases are assigned by the loader at runtime — they are NOT recoverable from the file alone. The
offline extractor (archive_textures._extra_fetch_records) treats those offsets as absolute, so the
tail textures land on the wrong bytes and decode as scattered / swizzled noise.

This tool recovers them the only reliable way: from the RUNNING game.
  1. attach to xenia.exe (direct ReadProcessMemory — no Cheat Engine needed),
  2. find the loaded DRAM records blob of each target iff in guest physical RAM (by signature),
  3. read each record's loader-RESOLVED +0x6C VRAM pointer (absolute guest address),
  4. read that texture's bytes from VRAM and CONTENT-MATCH them against the file's texture blob
     -> the texture's TRUE byte offset inside the .iff (byte-identical == certain),
  5. classify each match as FORMAL (already in the tree, i.e. the good 0..N) or RECOVERED
     (a tail texture the offline extractor mislocated),
  6. write a live catalog (feeds archive_textures) + a verification PNG decoded FROM THE FILE at
     the matched offset (proof the offset is right) + a human report.

Because step 4 is a byte-identical content match against the file, a RECOVERED entry is proven
correct without needing the game a second time — its file_offset becomes a normal
extract / replace target.

USAGE  (load the game INTO the arena you want first — the textures must be resident)
    python arena_trace.py                 # one-shot: capture every resident rink/arena/ui target
    python arena_trace.py rink_det.iff    # one specific asset
    python arena_trace.py --cats rink,led # only these categories (see CATEGORIES)
    python arena_trace.py --scan           # DRY: one read-only sweep, list every target asset that
                                           #   is resident RIGHT NOW (incl. disc_* scenes); writes
                                           #   nothing — use it to pick what's worth capturing
    python arena_trace.py --scenes         # map the resident disc_* raw-VRAM scenes: sweep RAM for
                                           #   texture fetch constants, content-match into the scene
                                           #   files' raw tails -> catalog entries (dims + offsets)
    python arena_trace.py --watch          # GUIDED: capture the arena you're IN, then it tells you
                                           #   to load the next one; loops until you Ctrl-C
    python arena_trace.py --watch --all    # same, but ALSO sweep global/loading/frontend/menus as
                                           #   you pass through them (full coverage in one session)
    python arena_trace.py --gui            # minimal attach/capture window
    python arena_trace.py --diag rink_det.iff   # explain WHY an asset captured nothing

CROSS-TEAM NOTE: rink texture blobs are ~95% byte-identical across teams, so a NON-loaded rink can
"capture" shared textures by borrowing the loaded arena's records. That data is still correct (the
bytes really are in that file), but it's confusing and misses each team's UNIQUE textures. --watch
therefore only captures the arena GENUINELY loaded (its own DRAM header resident), so you get each
team's real textures by actually visiting that team.

Output (all under <game>/live_capture/, and mirrored to launcher/data/live_offsets.json to ship):
    live_offsets.json         the catalog archive_textures reads (reload in the launcher)
    arena_trace_report.json   per-asset formal / resident / recovered breakdown
    <asset>/<off>_<WxH>_<fmt>.png   verification thumbnails
"""
import sys, os, struct, json, time
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import xenia_mem as xm
import archive_textures as at
import live_capture as lc
T = at.T

try:
    from resources import data_path as _data_path
except Exception:
    def _data_path(name):                       # fallback: alongside launcher/data
        return APP / "data" / name

# archive_textures was refactored CLEAN_DIR -> GAME_DIR (passed explicitly); live_capture.py still
# references the removed at.CLEAN_DIR, so lc.capture() would crash. We point both at the project
# root (where 0A/0B live) and reimplement the tiny capture core here against the current API,
# reusing only lc's pure helpers (_scan/_parse_rec/_strong_sig/_read_region/_merge_catalog).
GAME = str(at._PROJ)
at.GAME_DIR = at._PROJ
try:
    at.CLEAN_DIR = at._PROJ                      # keep any stale reference alive, just in case
except Exception:
    pass

# Categories in team_iff_catalog.csv that are arena / rink / presentation / UI scenes.
CATEGORIES = ["rink", "arena_presentation", "led", "ice", "ui", "overlay", "titlepage"]
# Extra non-team packs worth tracing (frontend / launch-screen logos / scoreclock overlay).
EXTRA_TARGETS = ["frontend.iff", "titlepage.iff", "overlay_static.iff"]
# --all also sweeps the loader-repacked packs (menus/franchise/create) + the loading screen. These
# don't tabulate sub-texture offsets in the file at all, so live capture is the ONLY way to map them.
GENERAL_TARGETS = list(dict.fromkeys(
    lc.REPACKED + ["Loading.iff", "frontend.iff", "titlepage.iff", "overlay_static.iff"]))

# Per-asset locate signatures, disk-cached (HEADER_CACHE) and SHIPPED (install-independent — retail
# files are byte-identical). Two parts, because many led_*/arena_presentation_*/some rink_* share an
# identical 16-byte DRAM header (same decompressed size) yet DIFFER deeper in the tree:
#   coarse = fdram[:16]  -> cheap key to find CANDIDATE resident blobs in one RAM sweep.
#   voff/vwin           -> a DISCRIMINATING window (fdram[voff:voff+64]) chosen so it's unique among
#                          the files that share this coarse key, to confirm WHICH team is really loaded.
# Without this, a watch pass walked all 512MB once PER target (170x, minutes, no output). Now: ONE
# regex sweep for coarse keys + a couple of 64-byte confirm-reads -> seconds/pass.
HEADER_CACHE = _data_path("iff_headers.json")
_CACHE_VER = 4                                                # bump -> old caches auto-rebuild
_VLEN = 64                                                    # max discriminator-window length
_ALEN = 32                                                   # anchor-window length

# Record-relative byte ranges that stay BYTE-IDENTICAL between the file and the LOADED copy during
# gameplay (measured live, rink_van + led_van, 101 records). At load the game relocates pointer /
# handle / VRAM-address fields scattered through every 0xE0 record (0x08-0x0F, 0x1C-0x1F, 0x50-0x5D,
# 0x6C-0x77, 0x88-0x8B, 0x98-0x9B, 0xA2-0xAA) AND rewrites the WHOLE pre-record resource-tree header,
# including fdram[:16]. The old anchor lived in that header, so it was NOT resident during play ->
# every arena silently failed to locate ("doesn't capture the arena I'm in"). These three ranges are
# always stable, so anchors + discriminators are taken only from here.
_REC_STABLE_ZONES = ((0x20, 0x50), (0xAB, 0xE0), (0x78, 0x88))


def _rec_positions(fdram):
    """File offsets of the 0xE0 texture records in a blob (file order); [] if it has no record array
    (loader-repacked packs). Mirrors the walk in _capture (4-byte probe, 0xE0 stride once locked)."""
    pos = []; b = 0; n = len(fdram)
    while b + 0xE0 <= n:
        if lc._parse_rec(fdram, b) is not None:
            pos.append(b); b += 0xE0
        else:
            b += 4
    return pos


def _stable_windows(fdram, win, min_distinct):
    """Yield (abs_offset, bytes) windows (len<=win, >=16) that sit ENTIRELY inside a runtime-stable
    record zone and carry >=min_distinct distinct bytes (so they're rare in 512MB). File order =
    priority, so callers just take the first acceptable window."""
    n = len(fdram)
    for p in _rec_positions(fdram):
        for lo, hi in _REC_STABLE_ZONES:
            end = min(p + hi, n)
            k = p + lo
            while k + min(win, 16) <= end:
                w = fdram[k:k + min(win, end - k)]
                if len(set(w)) >= min_distinct:
                    yield k, w
                k += 2


def _prefix_anchor(fdram):
    """Fallback needle from the blob prefix — for loader-repacked packs (global/loading/frontend),
    whose RAM copy is NOT the file record array but DOES preserve the prefix."""
    for off in range(0, max(1, len(fdram) - _ALEN), 8):
        w = fdram[off:off + _ALEN]
        if len(set(w)) >= 20:
            return off, w
    return 0, fdram[:_ALEN]


def _unique_anchor(fdram, corpus, prefer_record=True):
    """(aoff, anchor32) where anchor32 is a runtime-stable window that occurs EXACTLY ONCE in `corpus`
    (the concatenation of every target's fdram). Because arenas are ~95% byte-identical, an anchor
    taken from a SHARED texture record is resident whenever ANY team loads -> the old build located
    all 30 rinks at once. A corpus-unique window comes from this team's OWN textures (ice logo, LED
    ads), so locating it means THIS team is loaded. None if the file has no unique stable window
    (a byte-identical twin, e.g. arena_presentation_fla == nyi) -> caller co-attributes the twins."""
    if prefer_record:
        for off, w in _stable_windows(fdram, _ALEN, 18):
            if len(w) == _ALEN and corpus.count(w) == 1:      # 1 == only here -> team-unique
                return off, w
        return None
    for off in range(0, max(1, len(fdram) - _ALEN), 8):       # repacked: unique prefix window
        w = fdram[off:off + _ALEN]
        if len(set(w)) >= 20 and corpus.count(w) == 1:
            return off, w
    return None


def _header_index(targets, progress=True):
    """{iff: (aoff, anchor32, voff, vwin)} for `targets`, disk-cached. Each anchor is a runtime-stable,
    CORPUS-UNIQUE needle, so finding it in guest RAM identifies exactly the loaded team (no separate
    discriminator needed — voff/vwin stay 0/empty and are kept only for byte-identical twin groups)."""
    from collections import defaultdict
    import hashlib
    try:
        cache = json.loads(Path(HEADER_CACHE).read_text())
    except Exception:
        cache = {}
    fmt_ok = all(isinstance(cache.get(t), dict) and cache[t].get("v") == _CACHE_VER
                 for t in targets if t in cache)
    missing = [t for t in targets if t not in cache]
    if bool(missing) or not fmt_ok:                           # rebuild on any miss or stale format
        if progress:
            print(f"indexing asset headers (one-time, then cached)…")
        fdrams = {}
        scenes = set(_scene_targets())                        # ff3bef94 raw-VRAM arena scenes
        for i, iff in enumerate(targets):
            try:
                loc, data, size = at._read_asset(iff, GAME)
                if loc is None:
                    continue
                if iff in scenes:
                    # The scene's DRAM tree is rewritten at load (like rink record headers), so it
                    # is NOT a reliable needle. Its raw tiled-DXT tail IS loaded into guest RAM
                    # verbatim -> anchor on a mid-tail slice instead (runtime-stable by nature).
                    start = size // 2
                    fdrams[iff] = bytes(data[start:start + 0x80000])
                    continue
                blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
                if len(blobs) >= 2:
                    fdrams[iff] = min(blobs, key=len)          # same blob _capture scans for
                elif len(blobs) == 1:
                    fdrams[iff] = blobs[0]                     # single-blob pack: still a needle
            except Exception:
                pass
            if progress and (i + 1) % 40 == 0:
                print(f"  …{i + 1}/{len(targets)}")
        repacked = set(lc.REPACKED)
        # corpus = every fdram concatenated (0xFF*_ALEN separators so no window straddles a boundary).
        sep = b"\xff" * _ALEN
        corpus = sep.join(fdrams.values())
        cache = {}
        twins = defaultdict(list)                             # md5 -> members lacking a unique anchor
        for iff, fd in fdrams.items():
            ua = _unique_anchor(fd, corpus, prefer_record=iff not in repacked and iff not in scenes)
            if ua:                                            # unique -> anchor alone identifies it
                cache[iff] = {"v": _CACHE_VER, "aoff": ua[0], "anchor": ua[1].hex(),
                              "voff": 0, "vwin": ""}
            else:
                twins[hashlib.md5(fd).digest()].append(iff)
        # byte-identical twins share one anchor (any stable window) and co-attribute: their textures
        # ARE the same file, so a hit on either is correct for all. Prefer a window that appears only
        # among the twins (count == group size) to avoid borrowing a texture shared with other teams.
        for _hsh, members in twins.items():
            fd = fdrams[members[0]]
            pr = members[0] not in repacked and members[0] not in scenes
            cands = [(o, w) for o, w in _stable_windows(fd, _ALEN, 18) if len(w) == _ALEN] if pr \
                else [(o, fd[o:o + _ALEN]) for o in range(0, max(1, len(fd) - _ALEN), 8)
                      if len(set(fd[o:o + _ALEN])) >= 20]
            # prefer a window seen ONLY in the twins (count == group size); else first entropic window.
            chosen = next(((o, w) for o, w in cands if corpus.count(w) == len(members)),
                          cands[0] if cands else _prefix_anchor(fd))
            for iff in members:
                cache[iff] = {"v": _CACHE_VER, "aoff": chosen[0], "anchor": chosen[1].hex(),
                              "voff": 0, "vwin": ""}
        try:
            Path(HEADER_CACHE).parent.mkdir(parents=True, exist_ok=True)
            Path(HEADER_CACHE).write_text(json.dumps(cache))
        except Exception:
            pass
    out = {}
    for iff in targets:
        e = cache.get(iff)
        if isinstance(e, dict) and "anchor" in e:
            out[iff] = (e["aoff"], bytes.fromhex(e["anchor"]), e["voff"], bytes.fromhex(e["vwin"]))
    return out


def _locate_headers(handle, phys, sigs):
    """ONE walk of guest RAM -> {iff: host_addr of its resident records blob}. A 16MB boolean LUT keyed
    on each anchor's first 3 bytes prefilters every RAM region to the few thousand candidate positions
    (a single C-level gather over all rolling 3-byte windows); the full 32-byte anchor + a 64-byte
    discriminating confirm-read then pick the EXACT loaded team. ~6s vs a ~50s Python-regex sweep."""
    import numpy as np
    anchor_to = {}            # anchor bytes -> [(iff, aoff, voff, vwin)]
    for iff, (aoff, anchor, vo, vw) in sigs.items():
        anchor_to.setdefault(anchor, []).append((iff, aoff, vo, vw))
    if not anchor_to:
        return {}
    by_key = {}               # first-3-bytes int -> [(anchor, cands)]
    for anchor, cands in anchor_to.items():
        by_key.setdefault(int.from_bytes(anchor[:3], "little"), []).append((anchor, cands))
    lut = np.zeros(1 << 24, dtype=bool)
    lut[np.fromiter(by_key.keys(), dtype=np.uint32, count=len(by_key))] = True
    found = {}
    for base, sz in xm.enum_committed_regions(handle, phys, xm.PHYS_SIZE):
        c = xm.read_bytes(handle, base, sz)                   # whole region: no cross-chunk split
        if not c or len(c) < _ALEN:
            continue
        a = np.frombuffer(c, dtype=np.uint8).astype(np.uint32)
        w24 = a[:-2] | (a[1:-1] << 8) | (a[2:] << 16)         # rolling 3-byte key at every offset
        for pos in np.nonzero(lut[w24])[0]:
            pos = int(pos)
            for anchor, cands in by_key.get(int(w24[pos]), ()):
                if c[pos:pos + len(anchor)] != anchor:        # confirm the full anchor (any length; a
                    continue                                  # slice past region end is short -> no match
                anchor_addr = base + pos
                read_at = {}                                  # cache confirm-reads per (voff,len)
                for iff, aoff, vo, vw in cands:
                    if iff in found:
                        continue
                    rec_base = anchor_addr - aoff             # blob start (anchor sits aoff into it)
                    if not vw:                                # no discriminator -> anchor alone confirms
                        found[iff] = rec_base
                        continue
                    key = (vo, len(vw))                       # discriminator window may be < _VLEN
                    if key not in read_at:
                        read_at[key] = xm.read_bytes(handle, rec_base + vo, len(vw))
                    if read_at[key] == vw:                    # confirm exact team / content class
                        found[iff] = rec_base                 # attribute ALL members of the class
    return found


# ── target discovery ─────────────────────────────────────────────────────────
def _team_catalog_rows():
    p = _data_path("team_iff_catalog.csv")
    rows = []
    try:
        for ln in Path(p).read_text().splitlines()[1:]:
            f = ln.split(",")
            if len(f) >= 7:
                rows.append(dict(team=f[0], category=f[1], iff=f[2],
                                 archive=f[3], offset=f[4], size=f[5], resolved=f[6]))
    except Exception as e:
        print(f"[warn] could not read team_iff_catalog.csv: {e}")
    return rows


def _scene_targets():
    """disc_* arena-interior scenes (category=scene_arena in discovered_assets.csv). They're not in
    team_iff_catalog, so the normal target list never includes them — the dry scan adds them."""
    out = []
    try:
        for ln in Path(at.DISCOVERED_CSV).read_text().splitlines()[1:]:
            f = ln.split(",")
            if len(f) >= 3 and f[1] == "scene_arena" and f[2] not in out:
                out.append(f[2])
    except Exception as e:
        print(f"[warn] could not read discovered_assets.csv: {e}")
    return out


def build_targets(cats=None, only_iff=None, all_mode=False):
    """List of iff names to trace. `cats` filters categories; `only_iff` forces a single asset;
    `all_mode` unions every arena category PLUS the general packs (global/loading/frontend/…)."""
    if only_iff:
        return [only_iff if only_iff.endswith(".iff") else only_iff + ".iff"]
    if all_mode:
        cats = set(CATEGORIES)
    cats = set(cats or CATEGORIES)
    out = []
    for r in _team_catalog_rows():
        if r["category"] in cats and r["resolved"] == "1" and r["iff"] not in out:
            out.append(r["iff"])
    if not cats.isdisjoint({"frontend", "titlepage", "ui", "overlay"}):
        for t in EXTRA_TARGETS:
            if t not in out:
                out.append(t)
    if all_mode:
        for t in GENERAL_TARGETS:
            if t not in out:
                out.append(t)
    return out


# ── formal-record classification (which offsets the file already knows) ──────
def formal_info(iff):
    """(formal_count, {formal vram_off}, total_recs_from_tree) for an asset, or (0,set(),0).
    'formal' = records inside the count@0x20 array = the sub-package that decodes correctly today."""
    try:
        loc, data, size = at._read_asset(iff, GAME)
        if loc is None:
            return 0, set(), 0
        blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
        if not blobs:
            return 0, set(), 0
        dram = blobs[0]
        count = struct.unpack_from(">I", dram, 0x20)[0] if len(dram) >= 0x28 else 0
        vram, recs = at._texture_tree(data, size)
        formal_offs = set()
        if 0 < count <= 8192:
            for r in recs:
                if r.get("index", 0) < count and r.get("packing") != "scatter":
                    formal_offs.add(r["vram_off"])
        return count, formal_offs, len(recs)
    except Exception:
        return 0, set(), 0


# ── capture core (current-API reimplementation of live_capture.capture) ──────
def _capture(iff, handle, phys, save_png=True, require_header16=False, rec_base=None, how="header16",
             dup_count=True):
    """Capture the currently-loaded textures of `iff` -> list of catalog entries, each with the
    TRUE file offset recovered by content-matching resident VRAM bytes against the file blob.
    Mirrors live_capture.capture() but uses the current archive_textures API (GAME dir).

    Match methods (rink texture blobs are ~95% byte-identical across teams, so a NON-loaded rink
    still "captures" shared textures by borrowing the LOADED arena's resident records via strong-sig):
      header16  = the file's OWN DRAM header is resident  -> this asset is GENUINELY loaded.
      strong-sig= borrowed another resident tree; only SHARED textures match (correct, but confusing).
    `require_header16=True` returns [] unless the asset is genuinely loaded — use it in watch mode so
    only the arena you're actually in is captured (no cross-team shared-texture noise)."""
    loc, data, size = at._read_asset(iff, GAME)
    if loc is None:
        return []
    blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
    if len(blobs) < 2:
        return []
    fdram = min(blobs, key=len); ftex = max(blobs, key=len)
    if rec_base is None:                                  # not pre-located -> scan for it now
        rec_base = lc._scan(handle, phys, fdram[:16])
        how = "header16"
        if rec_base is None:                              # weak header -> locate by deep signature
            if require_header16:
                return []                                 # not genuinely loaded — skip (watch mode)
            so, sw = lc._strong_sig(fdram)
            if sw:
                m = lc._scan(handle, phys, sw)
                if m is not None:
                    rec_base = m - so; how = "strong-sig"
    if rec_base is None:
        return []
    mem = lc._read_region(handle, rec_base, min(len(fdram), 0x1800000))

    recs = []; b = 0
    while b + 0xE0 <= len(mem):
        r = lc._parse_rec(mem, b)
        if r is not None and 0xA0000000 <= r["ptr"] < 0xC0000000:
            recs.append(r); b += 0xE0
        else:
            b += 4
    if not recs:
        return []

    out_dir = lc.OUT_ROOT / iff[:-4]
    if save_png:
        out_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    for r in recs:
        vram = xm.read_bytes(handle, phys + (r["ptr"] & 0x1FFFFFFF), r["mip0"])
        if not vram:
            continue
        foff = ftex.find(vram)                            # byte-identical -> certain file offset
        if foff < 0:
            continue
        # dup = how many times these exact bytes appear in the file (a texture shared across slots).
        # ftex.count is a full 64MB memmem per texture -> ~24s over global.iff's 472 records, so watch
        # passes skip it (dup=1); offsets stay correct, only the shared-slot count is deferred to a
        # final dup_count=True pass.
        dup = (ftex.count(vram) if r["mip0"] <= 0x40000 else 1) if dup_count else 1
        entries.append(dict(iff=iff, file_offset=foff, w=r["w"], h=r["h"], fmt=r["fmt"],
                            bpu=r["bpu"], block=r["block"], tiled=r["tiled"], mip0=r["mip0"],
                            dup=dup, how=how))
        if save_png:
            try:                                          # decode FROM THE FILE at the matched offset (proof)
                img = T.decode(at._dxt_endian(ftex[foff:foff + r["mip0"]], r["fmt"]),
                               r["w"], r["h"], r["fmt"], r["bpu"], r["block"], r["tiled"], 0).convert("RGBA")
                at._to_straight(img, r["fmt"]).save(out_dir / f"{foff:08x}_{r['w']}x{r['h']}_{r['fmt']}.png")
            except Exception:
                pass
    return entries


# ── one asset ────────────────────────────────────────────────────────────────
def trace_iff(iff, handle, phys, save_png=True, require_header16=False, rec_base=None, dup_count=True):
    """Capture resident textures of `iff`; classify each as formal vs recovered.
    Returns a report dict (and merges its entries into the live catalog via the caller).
    `rec_base` (from _locate_headers) skips the per-target RAM scan when already known."""
    count, formal_offs, n_tree = formal_info(iff)
    entries = _capture(iff, handle, phys, save_png=save_png,     # content-matched -> true file_offset
                       require_header16=require_header16, rec_base=rec_base, dup_count=dup_count)
    resident = len(entries)
    loaded = bool(entries) and entries[0].get("how") == "header16"
    recovered = [e for e in entries if e["file_offset"] not in formal_offs]
    known = resident - len(recovered)
    return dict(iff=iff, formal_count=count, tree_records=n_tree, loaded=loaded,
                resident=resident, known=known, recovered=len(recovered),
                recovered_list=sorted(
                    ({"file_offset": e["file_offset"], "w": e["w"], "h": e["h"],
                      "fmt": e["fmt"], "mip0": e["mip0"], "dup": e.get("dup", 1)}
                     for e in recovered), key=lambda x: x["file_offset"]),
                entries=entries)


# ── ff3bef94 scene mapping (fetch-constant sweep) ────────────────────────────
# The disc_* arena scenes have NO 0xE0 record array — their raw tiled-DXT tail is loaded into guest
# RAM verbatim and the GAME builds the texture descriptors (Xenos fetch constants) at runtime. So we
# recover dims the only place they exist: sweep guest RAM for plausible fetch constants (same field
# layout _parse_rec matches inside records: (d0&3)==2, code=d1&0x3F, w/h in d2), then content-match
# each constant's pixel bytes into the resident scenes' RAW FILE bytes. A byte-identical match gives
# a proven (file_offset, w, h, fmt, tiled) -> a normal live-catalog entry with raw=1 (offsets are
# WHOLE-FILE offsets, not texture-blob offsets — archive_textures decodes raw entries accordingly).

def _scene_mip0(w, h, bpu, blk, tiled, pitch):
    """Byte size of mip level 0 as stored (tiled rows/pitch round up to 32 units)."""
    if blk:
        row = max(pitch * 8, (w + 3) // 4); rows = (h + 3) // 4
    else:
        row = max(pitch * 32, w); rows = h
    if tiled:
        row = (row + 31) & ~31; rows = (rows + 31) & ~31
    return row * rows * bpu


def _fetch_sweep(handle, phys):
    """One walk of guest RAM -> {guest_base: (w, h, code, tiled, pitch)} candidate texture fetch
    constants. Vectorized; the pitch/width consistency check kills most false positives, and the
    content-match verification later kills the rest."""
    import numpy as np
    codes = np.zeros(64, bool)
    for c in at._FETCH_FMT:
        codes[c] = True
    out = {}
    for base, sz in xm.enum_committed_regions(handle, phys, xm.PHYS_SIZE):
        c = xm.read_bytes(handle, base, sz)
        if not c or len(c) < 16:
            continue
        a = np.frombuffer(c, dtype=">u4", count=len(c) // 4)
        if len(a) < 4:
            continue
        f0, f1, f2 = a[:-2], a[1:-1], a[2:]
        w = (f2 & 0x1FFF) + 1; hh = ((f2 >> 13) & 0x1FFF) + 1
        pitch = (f0 >> 22) & 0x1FF
        pu = pitch * 32                                   # texels/row the pitch field implies
        m = (((f0 & 3) == 2) & codes[f1 & 0x3F]
             & (w >= 16) & (w <= 2048) & (hh >= 16) & (hh <= 2048)
             & (f1 >= 1 << 12) & ((f1 >> 12) < 0x20000)   # base page inside 512MB guest RAM
             & ((pitch == 0) | ((pu >= w) & (pu <= 2 * w + 255))))
        for pos in np.nonzero(m)[0]:
            f0i, f1i, f2i = int(a[pos]), int(a[pos + 1]), int(a[pos + 2])
            gbase = (f1i >> 12) << 12
            if gbase not in out:
                out[gbase] = ((f2i & 0x1FFF) + 1, ((f2i >> 13) & 0x1FFF) + 1,
                              f1i & 0x3F, (f0i >> 31) & 1, (f0i >> 22) & 0x1FF)
    return out


def scene_capture(handle, phys, save_png=True):
    """Map the resident disc_* scenes' textures -> live-catalog entries (raw whole-file offsets).
    Returns (loaded_scene_list, entries)."""
    scenes = _scene_targets()
    sigs = {i: s for i, s in _header_index(scenes, progress=False).items() if i in scenes}
    found = _locate_headers(handle, phys, sigs)
    if not found:
        return [], []
    files = {}
    for iff in sorted(found):
        loc, data, size = at._read_asset(iff, GAME)
        if loc is not None:
            files[iff] = bytes(data[:size])
    print(f"resident scenes: {', '.join(files)}")
    print("sweeping guest RAM for texture fetch constants…")
    fetches = _fetch_sweep(handle, phys)
    print(f"  {len(fetches)} candidate fetch constants; content-matching into "
          f"{len(files)} scene file(s)…")
    entries = []; seen = set()
    for gbase in sorted(fetches):
        w, h, code, tiled, pitch = fetches[gbase]
        nm, bpu, blk = T.FMT[code]
        mip0 = _scene_mip0(w, h, bpu, blk, tiled, pitch)
        probe = xm.read_bytes(handle, phys + gbase, min(mip0, 0x400))
        if not probe or len(probe) < 0x140:
            continue
        needle = probe[0x100:0x140]                       # skip leading bytes (often padding)
        if len(set(needle)) < 8:                          # too flat to be a trustworthy needle
            continue
        for iff, fb in files.items():
            pos = fb.find(needle)
            while pos >= 0:
                foff = pos - 0x100
                if foff >= 0 and fb[foff:foff + len(probe)] == probe:
                    big = xm.read_bytes(handle, phys + gbase, min(mip0, 0x10000))
                    if big and fb[foff:foff + len(big)] == big and (iff, foff) not in seen:
                        seen.add((iff, foff))
                        entries.append(dict(iff=iff, file_offset=foff, w=w, h=h, fmt=nm,
                                            bpu=bpu, block=blk, tiled=int(tiled), mip0=mip0,
                                            dup=1, how="fetch-scan", raw=1))
                        if save_png:
                            try:
                                out_dir = lc.OUT_ROOT / iff[:-4]
                                out_dir.mkdir(parents=True, exist_ok=True)
                                img = T.decode(at._dxt_endian(fb[foff:foff + mip0], nm),
                                               w, h, nm, bpu, blk, tiled, 0).convert("RGBA")
                                at._to_straight(img, nm).save(
                                    out_dir / f"{foff:08x}_{w}x{h}_{nm}.png")
                            except Exception:
                                pass
                    break                                 # verified (or refuted) at first real hit
                pos = fb.find(needle, pos + 1)
    return sorted(files), entries


# ── diagnostics: why did an asset capture nothing? ───────────────────────────
def diag_iff(iff, handle, phys):
    """Dump why capture() returned nothing for `iff` — is the records blob resident, and are the
    +0x6C fields patched to VRAM pointers (0xA0000000..0xC0000000)?  Guides the fallback."""
    loc, data, size = at._read_asset(iff, GAME)
    if loc is None:
        print(f"  {iff}: not in archive"); return
    blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
    if len(blobs) < 2:
        print(f"  {iff}: <2 blobs (single-texture / not a multi-pack)"); return
    fdram = min(blobs, key=len); ftex = max(blobs, key=len)
    base = lc._scan(handle, phys, fdram[:16])
    how = "header16"
    if base is None:
        so, sw = lc._strong_sig(fdram)
        m = lc._scan(handle, phys, sw)
        base = None if m is None else m - so
        how = f"strong-sig@0x{so:X}"
    if base is None:
        print(f"  {iff}: records blob NOT resident in guest RAM (asset not loaded on this screen)")
        return
    mem = lc._read_region(handle, base, min(len(fdram), 0x1800000))
    ptrs = ok = 0; b = 0; samples = []
    while b + 0xE0 <= len(mem):
        r = lc._parse_rec(mem, b)
        if r is not None:
            ptrs += 1
            inv = 0xA0000000 <= r["ptr"] < 0xC0000000
            if inv:
                ok += 1
            if len(samples) < 8:
                samples.append((r["w"], r["h"], r["fmt"], r["ptr"], inv))
            b += 0xE0
        else:
            b += 4
    print(f"  {iff}: records blob resident (via {how}, host=0x{base:X}); "
          f"{ptrs} records parsed, {ok} have a live VRAM pointer (+0x6C in 0xA0000000..0xC0000000)")
    for w, h, fmt, ptr, inv in samples:
        print(f"      {w}x{h:<4} {fmt:>7}  +0x6C=0x{ptr:08X}  {'VRAM-ptr' if inv else 'not-a-ptr'}")
    if ok == 0:
        print("      -> loader does NOT patch +0x6C here; needs the anchor-scan fallback "
              "(report this and I will add it).")


# ── run over a target list ───────────────────────────────────────────────────
def run(targets, handle, phys, save_png=True, require_header16=False, verbose=True):
    reports = []; all_entries = []
    for iff in targets:
        t = time.time()
        rep = trace_iff(iff, handle, phys, save_png=save_png, require_header16=require_header16)
        dt = time.time() - t
        if rep["resident"]:
            all_entries += rep["entries"]
            if verbose:
                tag = "LOADED " if rep["loaded"] else "shared "
                print(f"  {tag}{iff:32} formal={rep['formal_count']:<3} resident={rep['resident']:<3} "
                      f"RECOVERED={rep['recovered']:<3}  {dt:.1f}s")
        elif verbose:
            print(f"  {iff:32} not currently loaded")
        rep.pop("entries", None)
        reports.append(rep)
    return reports, all_entries


# ── FAST pass: one RAM sweep locates what's loaded, then extract only those ───
def fast_pass(targets, handle, phys, header_by_iff, save_png=False, on_hit=None,
              dup_count=True, done=None):
    """One guest-RAM sweep finds every loaded asset, then extracts textures for only those.
    `on_hit(iff, report)` fires as each loaded asset finishes -> live feedback.
    `done` (dict {iff: rec_base}) marks assets already fully captured at that rec_base: they are
    reported but their (expensive) extraction is skipped while they stay put -> subsequent watch
    passes cost ~just the 6s sweep, so transient screens (loading.iff) get caught between them.
    Returns (reports, entries, loaded) where loaded = {iff: rec_base} this pass."""
    loaded = _locate_headers(handle, phys, header_by_iff)       # {iff: rec_base}  — the slow part, ONCE
    reports = []; entries = []
    for iff, rec_base in loaded.items():
        if done is not None and done.get(iff) == rec_base:      # unchanged & already captured -> skip
            rep = dict(iff=iff, loaded=True, resident=0, recovered=0, skipped=True)
            reports.append(rep)
            if on_hit:
                on_hit(iff, rep)
            continue
        rep = trace_iff(iff, handle, phys, save_png=save_png, rec_base=rec_base, dup_count=dup_count)
        if rep["resident"]:
            entries += rep["entries"]
        rep.pop("entries", None)
        reports.append(rep)
        if on_hit:
            on_hit(iff, rep)
    return reports, entries, loaded


# ── ONE-SHOT: capture whatever is on screen NOW (no anchor cache, no team guessing) ──
def _now_targets(target):
    """Resolve a --now argument to the iff list to try.
       None        -> every arena/general candidate (whole screen).
       'van'       -> every *_van.iff in the catalog (a team abbreviation).
       'rink_det'  -> that one iff (with or without .iff)."""
    allc = build_targets(all_mode=True)
    if not target:
        return allc
    t = target.strip().lower()
    if t.endswith(".iff") or "_" in t or "." in t:               # an explicit iff name
        return [target if target.endswith(".iff") else target + ".iff"]
    hits = [iff for iff in allc if iff.lower().endswith(f"_{t}.iff")]   # team abbreviation
    if hits:
        return hits
    return [f"{fam}_{t}.iff" for fam in                          # abbrev not in catalog: try the families
            ("rink", "arena_presentation", "led", "arena", "ice")]


def capture_now(handle, phys, target=None, save_png=True, verbose=True, on_hit=None):
    """Capture whatever is resident on screen RIGHT NOW and content-match it to the file(s) you name.

    No anchor cache, no 'which team is loaded' inference: each candidate file's own strong-sig (a deep,
    runtime-stable signature) is looked up in ONE guest-RAM snapshot; a hit means that blob is loaded,
    and every texture is proven by a byte-identical VRAM->file match. Because arena files are byte-similar,
    several teams' strong-sigs can resolve to the SAME resident blob, so results are deduped by rec_base
    and the extra members are flagged 'same blob as …'. SCOPE with `target` (the arena you're actually in)
    and attribution is exact — you naming the screen is the discriminator I used to try to compute.

    Returns (reports, entries, loaded{iff:rec_base})."""
    targets = _now_targets(target)
    sigs = {}
    for iff in targets:
        try:
            loc, data, size = at._read_asset(iff, GAME)
            if loc is None:
                continue
            blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
            if len(blobs) < 2:                                   # single-tex / repacked: prefix needle
                if blobs:
                    ao, aw = _prefix_anchor(blobs[0])
                    sigs[iff] = (ao, aw, 0, b"")
                continue
            fd = min(blobs, key=len)
            so, sw = lc._strong_sig(fd)                          # deep stable window (resident when loaded)
            if sw:
                sigs[iff] = (so, sw, 0, b"")
        except Exception:
            pass
    loaded = _locate_headers(handle, phys, sigs)                 # {iff: rec_base} for the resident ones
    reports = []; entries = []; first_at = {}
    for iff in sorted(loaded, key=lambda k: (loaded[k], k)):     # group same-blob members together
        rec_base = loaded[iff]
        rep = trace_iff(iff, handle, phys, save_png=save_png, rec_base=rec_base, dup_count=False)
        owner = first_at.setdefault(rec_base, iff)               # first file to claim this physical blob
        rep["shared_with"] = None if owner == iff else owner
        if rep["resident"]:
            entries += rep["entries"]
        rep.pop("entries", None)
        reports.append(rep)
        if verbose:
            note = "" if rep["shared_with"] is None else f"   (same blob as {rep['shared_with']})"
            tag = "LOADED" if rep.get("loaded") else "shared"
            print(f"  [{tag}] {iff:32} resident={rep['resident']:>3} recovered={rep['recovered']:>3}{note}")
        if on_hit:
            on_hit(iff, rep)
    return reports, entries, loaded


def _write_report(reports):
    lc.OUT_ROOT.mkdir(parents=True, exist_ok=True)
    p = lc.OUT_ROOT / "arena_trace_report.json"
    p.write_text(json.dumps(reports, indent=1))
    return p


def _publish_bundled():
    """Copy the just-merged local catalog into launcher/data/ so it ships with the launcher
    (offsets are install-independent -> valid for everyone who clones the repo). One step:
    capture -> data/live_offsets.json updated -> commit it."""
    try:
        dst = at.LIVE_CATALOG_BUNDLED
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(Path(lc.CATALOG).read_text())
        return dst
    except Exception as e:
        print(f"  (could not update bundled catalog: {e})")
        return None


def main(argv):
    args = argv[1:]
    watch = "--watch" in args;  args = [a for a in args if a != "--watch"]
    scan = "--scan" in args;    args = [a for a in args if a != "--scan"]
    scenes_mode = "--scenes" in args;  args = [a for a in args if a != "--scenes"]
    gui = "--gui" in args;      args = [a for a in args if a != "--gui"]
    diag = "--diag" in args;    args = [a for a in args if a != "--diag"]
    now = "--now" in args;      args = [a for a in args if a != "--now"]
    all_mode = "--all" in args;  args = [a for a in args if a != "--all"]
    cats = None
    if "--cats" in args:
        i = args.index("--cats"); cats = args[i + 1].split(","); del args[i:i + 2]
    only = args[0] if args else None

    if gui:
        return _gui(cats)

    lc.set_out_root(str(at._PROJ))               # captures + catalog live beside the game files
    pid = xm.find_pid()
    if not pid:
        print("Xenia is not running. Launch the game (into the arena you want) and retry.")
        return
    h = xm.open_process(pid)
    phys = xm.find_phys_base(h) or xm.PHYS_BASE
    print(f"attached pid={pid}  phys_base=0x{phys:X}  reads from GAME={GAME}")

    def _commit(entries, reports):
        rows = lc._merge_catalog(entries); _write_report(reports); _publish_bundled()
        return rows

    if scenes_mode:
        loaded, entries = scene_capture(h, phys, save_png=True)
        if not loaded:
            print("No disc_* scene is resident — get to the screen that loads one "
                  "(check with --scan) and re-run.")
        elif entries:
            per = {}
            for e in entries:
                per.setdefault(e["iff"], []).append(e)
            rows = _commit(entries, [dict(iff=i, formal_count=0, tree_records=0, loaded=True,
                                          resident=len(v), known=0, recovered=len(v),
                                          recovered_list=sorted(
                                              ({"file_offset": e["file_offset"], "w": e["w"],
                                                "h": e["h"], "fmt": e["fmt"], "mip0": e["mip0"],
                                                "dup": 1} for e in v),
                                              key=lambda x: x["file_offset"]),
                                          entries=v) for i, v in per.items()])
            print(f"\nmapped {len(entries)} scene texture(s):")
            for i, v in sorted(per.items()):
                dims = ", ".join(f"{e['w']}x{e['h']} {e['fmt']}" for e in
                                 sorted(v, key=lambda e: e["file_offset"])[:6])
                print(f"  {i:22} {len(v):>3} textures  ({dims}{', …' if len(v) > 6 else ''})")
            print(f"\ncatalog: {len(rows)} entries -> {lc.CATALOG}")
            print(f"bundled for shipping -> {at.LIVE_CATALOG_BUNDLED}   (commit this file)")
            print(f"verification PNGs -> {lc.OUT_ROOT}\\<scene>\\")
            print("In the launcher: Reload, then the scene shows its textures in the IFF tab.")
        else:
            print(f"\nScenes resident ({', '.join(loaded)}) but no fetch constant matched their "
                  "bytes — the scene may not be on screen this instant (its textures must be "
                  "bound for drawing). Re-run while the scene is visibly rendering.")
        xm.close_handle(h); return

    if scan:
        # DRY sweep: locate every target's resident signature in ONE RAM walk and report it.
        # Reads guest RAM only — no extraction, no catalog/report/PNG writes.
        targets = build_targets(cats, all_mode=True)
        for t in _scene_targets():
            if t not in targets:
                targets.append(t)
        print(f"DRY SCAN — read-only, nothing is captured or written.  targets: {len(targets)}")
        sigs = _header_index(targets)
        nosig = [t for t in targets if t not in sigs]
        t0 = time.time()
        found = _locate_headers(h, phys, sigs)
        dt = time.time() - t0
        if found:
            print(f"\n{len(found)} asset(s) resident right now ({dt:.1f}s sweep):")
            for iff in sorted(found):
                print(f"  {iff:34} @ host 0x{found[iff]:X}")
            print("\nCapture any of these with:  python arena_trace.py <name>.iff   (or --watch)")
        else:
            print(f"\nNothing resident ({dt:.1f}s sweep) — get into an arena/menu and re-run.")
        if nosig:
            print(f"\n(no locate signature for {len(nosig)} target(s) — can't be scanned: "
                  f"{', '.join(nosig[:8])}{' …' if len(nosig) > 8 else ''})")
        print("NOTE: disc_* scenes are located by raw texture-tail bytes (loaded verbatim), so a "
              "hit means that scene's data really is in RAM right now.")
        xm.close_handle(h); return

    if now:
        # One-shot: capture what's on screen RIGHT NOW. `only` scopes it (team abbrev or iff); the
        # arena you're actually in is the ground truth, so no header cache / team inference is needed.
        scope = only or "(whole screen)"
        print(f"capturing what's loaded now — scope: {scope}\n")
        known0 = {(e["iff"], e["file_offset"]) for e in at._live_catalog()}
        reports, entries, loaded = capture_now(h, phys, target=only, save_png=True)
        if entries:
            rows = _commit(entries, reports)
            new = [e for e in entries if (e["iff"], e["file_offset"]) not in known0]
            rec = sum(r["recovered"] for r in reports)
            blobs = len({loaded[i] for i in loaded})
            print(f"\n{len(loaded)} file(s) resident in {blobs} loaded blob(s); "
                  f"recovered {rec} tail textures ({len(new)} new).")
            print(f"catalog: {len(rows)} entries -> {lc.CATALOG}")
            print(f"bundled for shipping -> {at.LIVE_CATALOG_BUNDLED}   (commit this file)")
            print("In the launcher: Reload, then the IFF tab shows the recovered textures correctly.")
        else:
            hint = f"'{only}' " if only else ""
            print(f"\nNothing for {hint}is resident right now. Make sure that arena/screen is ON SCREEN, "
                  f"then re-run.  (Whole-screen scan: drop the name and just use --now.)")
        xm.close_handle(h); return

    targets = build_targets(cats, only, all_mode=all_mode)
    print(f"targets ({len(targets)}): {', '.join(targets[:12])}{' …' if len(targets) > 12 else ''}\n")

    if diag:
        for iff in targets:
            diag_iff(iff, h, phys)
        xm.close_handle(h); return

    # Index every target's records-header once (cached) so each pass scans RAM ONCE, not per-target.
    header_by_iff = _header_index(targets)
    print(f"header index ready for {len(header_by_iff)}/{len(targets)} targets.\n")

    known = {(e["iff"], e["file_offset"]) for e in at._live_catalog()}

    if watch:
        # Guided, loaded-only watch. Each pass: ONE RAM sweep finds what's loaded (its own header
        # resident), then extracts only those — so you never pick up another team's shared textures,
        # and a pass is seconds not minutes. Prints each loaded asset + running NEW count as it goes.
        deadline = time.time() + 3 * 3600
        print("WATCH — walk through arenas/menus; each screen is captured automatically.")
        print(f"starting from {len(known)} textures already in the catalog.  Ctrl-C to stop.\n")
        pass_no = 0
        done = {}                                            # {iff: rec_base} already fully captured
        try:
            while time.time() < deadline:
                pass_no += 1

                def _hit(iff, rep):
                    if rep.get("skipped"):
                        return                               # already captured — stay quiet, keep it fast
                    tag = "LOADED" if rep.get("loaded") else "shared"
                    print(f"  [{tag}] {iff:30} {rep['resident']:>3} textures")

                t0 = time.time()
                # dup_count=False: skip the slow per-texture full-file scan during discovery (global.iff
                # alone is ~24s of it); a final one-shot run refines dup. done{}: don't re-extract a
                # screen we already captured -> passes after the first cost ~just the sweep.
                reports, entries, loaded_map = fast_pass(
                    targets, h, phys, header_by_iff, on_hit=_hit, dup_count=False, done=done)
                loaded = [r["iff"] for r in reports if r.get("loaded")]
                new = [e for e in entries if (e["iff"], e["file_offset"]) not in known]
                for e in entries:
                    known.add((e["iff"], e["file_offset"]))
                # mark every asset we actually extracted this pass as done at its current rec_base
                extracted = {r["iff"] for r in reports if not r.get("skipped")}
                for iff in extracted:
                    if iff in loaded_map:
                        done[iff] = loaded_map[iff]
                dt = time.time() - t0
                skipped = [r["iff"] for r in reports if r.get("skipped")]
                tail = f", {len(skipped)} already done" if skipped else ""
                print(f"── pass {pass_no}  ({dt:.1f}s, {len(loaded)} loaded{tail}) " + "─" * 12)
                if new:
                    _commit(entries, reports)
                    print(f"  +{len(new)} NEW textures  |  catalog total: {len(known)}  "
                          f"|  saved -> launcher/data/live_offsets.json")
                    print("  → load the NEXT arena/menu when ready (this one keeps capturing too).")
                elif loaded:
                    print("  ✔ nothing new — these screens are captured. "
                          "LOAD A DIFFERENT ARENA / MENU (capture resumes automatically).")
                else:
                    print("  …nothing loaded yet — get into an arena or a menu screen.")
                time.sleep(2)
        except KeyboardInterrupt:
            print(f"\nstopped. catalog holds {len(known)} unique textures -> {lc.CATALOG}")
            print("In the launcher hit Reload to see them; commit launcher/data/live_offsets.json + rebuild to ship.")
    else:
        def _hit(iff, rep):
            tag = "LOADED" if rep.get("loaded") else "shared"
            print(f"  [{tag}] {iff:30} {rep['resident']:>3} textures  (recovered {rep['recovered']})")
        reports, entries, _ = fast_pass(targets, h, phys, header_by_iff, save_png=True, on_hit=_hit)
        new = [e for e in entries if (e["iff"], e["file_offset"]) not in known]
        if entries:
            rows = _commit(entries, reports)
            rec_total = sum(r["recovered"] for r in reports)
            print(f"\ncatalog now holds {len(rows)} entries ({len(new)} new this run) -> {lc.CATALOG}")
            print(f"recovered this pass: {rec_total} tail textures")
            print(f"bundled for shipping -> {at.LIVE_CATALOG_BUNDLED}   (commit this file)")
            print("In the launcher: Reload, then the IFF tab shows the recovered textures correctly.")
        else:
            print("\nNothing loaded on this screen — get INTO the arena/menu you want, then re-run "
                  "(or use --watch and navigate).")
    xm.close_handle(h)


# ── minimal dev GUI ──────────────────────────────────────────────────────────
def _gui(cats):
    import tkinter as tk
    from tkinter import scrolledtext
    lc.set_out_root(str(at._PROJ))
    root = tk.Tk(); root.title("NHL 2K10 — Arena/Rink Trace"); root.geometry("760x520")
    state = {"h": None, "phys": None}
    top = tk.Frame(root); top.pack(fill="x", padx=8, pady=6)
    status = tk.Label(top, text="not attached", anchor="w"); status.pack(side="left")
    log = scrolledtext.ScrolledText(root, height=26, font=("Consolas", 9)); log.pack(fill="both", expand=True, padx=8, pady=6)

    def out(s):
        log.insert("end", s + "\n"); log.see("end"); root.update_idletasks()

    def attach():
        pid = xm.find_pid()
        if not pid:
            status.config(text="Xenia not running"); out("Xenia not running — launch the game and retry."); return
        h = xm.open_process(pid); phys = xm.find_phys_base(h) or xm.PHYS_BASE
        state["h"] = h; state["phys"] = phys
        status.config(text=f"attached pid={pid}  phys=0x{phys:X}")
        out(f"attached pid={pid}  phys_base=0x{phys:X}")

    def capture():
        if not state["h"]:
            out("attach first."); return
        targets = build_targets(cats, all_mode=True)
        if "hdr" not in state:
            out("indexing asset headers (one-time)…")
            state["hdr"] = _header_index(targets)
        out(f"\ncapturing the loaded arena/screen…")
        reports, entries, _ = fast_pass(targets, state["h"], state["phys"], state["hdr"],
                                        on_hit=lambda i, r: out(
                                            f"  [{'LOADED' if r.get('loaded') else 'shared'}] {i}: {r['resident']} textures"))
        loaded = [r["iff"] for r in reports if r.get("loaded")]
        if entries:
            rows = lc._merge_catalog(entries); _write_report(reports)
            bundled = _publish_bundled()
            rec = sum(r["recovered"] for r in reports)
            out(f"catalog: {len(rows)} entries; recovered {rec} tail textures. "
                f"Reload in launcher to see them.")
            if bundled:
                out(f"bundled -> {bundled.name} (commit to ship it)")
        else:
            out("nothing resident — load into the arena/screen first.")

    def openout():
        try:
            os.startfile(lc.OUT_ROOT)
        except Exception as e:
            out(str(e))

    for txt, cmd in [("Attach", attach), ("Capture now", capture), ("Open output", openout)]:
        tk.Button(top, text=txt, command=cmd).pack(side="right", padx=3)
    out(__doc__.strip().splitlines()[3])
    out("1) load the game INTO an arena  2) Attach  3) Capture now")
    root.mainloop()


if __name__ == "__main__":
    main(sys.argv)
