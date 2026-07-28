"""
scoreclock_live.py — LIVE position/scale tuning of the in-game scoreclock (Xenia), no relaunch.

Proven 2026-07-28 (see project_live_texture_patch memory): the scoreclock's per-element WORLD
MATRICES are computed at scorebug activation into an array inside the loaded overlay_static blob0
(base + 0x219B70, 0x40-byte stride, records 0..12 = the scoreclock joints). Each record:
    +0x00 row0 basis   +0x10 row1 = TRANSLATION (x,y,z,w=1)   +0x20 row2 basis   +0x30 row3 basis
Element SCALE is carried by the dominant (largest-magnitude) component of basis rows 0/2/3
(axis-permuted; stock ~1.10 / 1.28 / 1.38). POSITION is the +0x10 x,y.

During ACTIVE play a per-frame skeleton eval rewrites these matrices back to authored values ~60/s,
so a one-shot poke reverts. We WIN THE RACE by spin-writing the desired bytes from a background
thread (thousands of writes/s) — the value then holds on screen. During a whistle/stoppage the eval
is idle and a single write already holds. On a scene REBUILD (stoppage/menu) the array can move, so
the writer VALIDATES its anchor every batch and halts if the record stops looking like a matrix
(prevents hammering stale memory and corrupting the game) — the UI then asks for a Re-attach.

This is a LIVE tool only (memory, lost on relaunch). "Bake to file" translates the current holds
into scorebug_layout offline edits so a chosen layout also survives a relaunch.

Record<->element identity is NOT reliable from coordinates (world-matrix space != authored space),
so the UI provides Blink-to-identify + rename; the map persists to scoreclock_live_labels.json.
"""
import json
import struct
import threading
import time
import zlib
from pathlib import Path

import xenia_mem as xm
import overlay_editor as oe
try:
    from launcher import archive_textures as _AT
except ImportError:  # standalone
    import archive_textures as _AT
try:
    from launcher import scorebug_layout as _SBL
except ImportError:
    import scorebug_layout as _SBL

IFF = "overlay_static.iff"
MTX_OFF_HINT = 0x219B70          # matrix array offset within blob0 (this build) — validated, not trusted
STRIDE = 0x40
N_RECORDS = 13                   # records 0..12 = the CORE scoreclock joints (used to VALIDATE the array)
SCAN_RECORDS = 96                # capture this many records — real elements live past 12 too
                                 # (e.g. 38/39/40 = away-panel abbrev/logo, 44 = shots, ...)
_SIG_NAME = "gameclock_semi"     # UTF-16BE string that survives load, anchors the blob0 base


def _sane_translation(x, y, z, w):
    return (abs(w - 1.0) < 1e-3 and -2.0 < z < 40.0
            and abs(x) < 700.0 and abs(y) < 700.0 and (x or y))


class LiveScoreclock:
    """Attach to the running game, locate the world-matrix array, hold live edits."""

    def __init__(self, gdir):
        self.gdir = gdir
        self.h = None
        self.base = 0                # host addr of blob0 base
        self.mtx = 0                 # host addr of record[0] (the TRUE first record)
        self.mtx_off = 0             # self.mtx - self.base (array start offset in blob0)
        self.stock = {}              # idx -> original 0x40 bytes (for restore + scale base)
        self._holds = {}             # idx -> list[(rel_off, packed_bytes)]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._halted = ""            # non-empty when the writer self-halted (anchor lost)
        self._writer = None
        self._attach()

    # ── attach / locate ──────────────────────────────────────────────────────
    def _attach(self):
        pid = xm.find_pid()
        if not pid:
            raise RuntimeError("Xenia is not running.")
        self.h = xm.open_process(pid)
        if not self.h:
            raise RuntimeError(f"Could not open the Xenia process (pid {pid}).")
        # sig file-offset from the CURRENT overlay_static (adapts to the user's own edits)
        dram, _ = oe.load_dram(IFF, self.gdir)
        dram = bytes(dram)
        sig = _SIG_NAME.encode("utf-16-be")
        sig_off = dram.find(sig)
        if sig_off < 0:
            raise RuntimeError("scoreclock signature not found in overlay_static — is it the right file?")
        phys = xm.find_phys_base(self.h) or xm.PHYS_BASE
        hits = self._scan(sig, phys)
        if not hits:
            raise RuntimeError("Scoreclock scene not resident. Get INTO a game (scoreclock on screen), "
                               "then Re-attach.")
        # pick the hit whose implied blob0 base yields a valid matrix array
        for a in hits:
            base = a - sig_off
            m = self._find_matrix(base)
            if m:
                self.base = base
                self.mtx = m
                self.mtx_off = m - base
                break
        if not self.mtx:
            raise RuntimeError("Found the scene but not the live matrix array — try Re-attach once "
                               "in steady gameplay.")
        self._capture_stock()
        self._start_writer()

    def _scan(self, pattern, phys, cap=8):
        hits, plen = [], len(pattern)
        for base, sz in xm.enum_committed_regions(self.h, phys, xm.PHYS_SIZE):
            off = 0
            while off < sz:
                n = min(0x100000, sz - off)
                chunk = xm.read_bytes(self.h, base + off, n)
                if chunk:
                    j = chunk.find(pattern)
                    while j >= 0:
                        hits.append(base + off + j)
                        if len(hits) >= cap:
                            return hits
                        j = chunk.find(pattern, j + 1)
                off += n - plen if n > plen else n
        return hits

    def _valid_matrix(self, addr):
        """Count records 0..12 with a sane translation at this candidate matrix address."""
        blob = xm.read_bytes(self.h, addr, STRIDE * N_RECORDS)
        if not blob or len(blob) < STRIDE * N_RECORDS:
            return 0
        good = 0
        for i in range(N_RECORDS):
            x, y, z, w = struct.unpack_from(">4f", blob, i * STRIDE + 0x10)
            if _sane_translation(x, y, z, w):
                good += 1
        return good

    def _find_matrix(self, base):
        """Locate the matrix array for a blob0 base: try the known offset, else scan a window.
        The hint (0x219B70) lands two records INTO the array — record[0] is really the away
        abbreviation at 0x219AF0 — so walk back to the true first record before returning."""
        cand = base + MTX_OFF_HINT
        if self._valid_matrix(cand) >= 6:
            return self._walk_back(cand)
        best, best_score = 0, 5
        for off in range(0x200000, 0x260000, 0x10):
            score = self._valid_matrix(base + off)
            if score > best_score:
                best, best_score = base + off, score
        return self._walk_back(best) if best else best

    def _walk_back(self, addr):
        """Step back in 0x40 strides while the PRECEDING record is still a sane matrix, so the
        returned address is the array's true record[0] (the leading abbreviations, which the old
        hint skipped). Stops at the first null/plumbing gap before the array."""
        while addr:
            prev = addr - STRIDE
            b = xm.read_bytes(self.h, prev + 0x10, 16)
            if not b or len(b) < 16:
                break
            x, y, z, w = struct.unpack(">4f", b)
            if _sane_translation(x, y, z, w):
                addr = prev
            else:
                break
        return addr

    def _capture_stock(self):
        blob = xm.read_bytes(self.h, self.mtx, STRIDE * SCAN_RECORDS) or b""
        self.stock = {i: blob[i * STRIDE:(i + 1) * STRIDE]
                      for i in range(SCAN_RECORDS) if len(blob) >= (i + 1) * STRIDE}

    # ── record queries ───────────────────────────────────────────────────────
    def real_records(self):
        """Indices whose stock record is a sane, non-null element (skip plumbing/null slots)."""
        out = []
        for i, rec in self.stock.items():
            x, y, z, w = struct.unpack_from(">4f", rec, 0x10)
            if _sane_translation(x, y, z, w):
                out.append(i)
        return out

    def pos(self, idx):
        """Live (x, y) translation of a record, or None."""
        b = xm.read_bytes(self.h, self.mtx + idx * STRIDE + 0x10, 8)
        if not b:
            return None
        return struct.unpack(">2f", b)

    def stock_pos(self, idx):
        x, y = struct.unpack_from(">2f", self.stock[idx], 0x10)
        return (x, y)

    def _scale_comps(self, idx):
        """(float_index, stock_value) of the dominant scale component in each basis row 0/2/3."""
        f = struct.unpack(">16f", self.stock[idx])
        comps = []
        for row in (0, 2, 3):
            base = row * 4
            k = max(range(4), key=lambda i: abs(f[base + i]))
            comps.append((base + k, f[base + k]))
        return comps

    # ── holds (the live edits the writer maintains) ──────────────────────────
    def set_hold(self, idx, dx, dy, scx, scy=None):
        """Hold record `idx` at stock+(dx,dy) translation. Element scale is written to the
        PREVIOUS record (idx-1): the scoreclock's joint chain makes record N's basis drive the
        glyph rendered by record N+1, so to resize the SELECTED glyph we scale its parent joint
        one record back (evidence-backed: scaling record N was observed to resize glyph N+1).
        scx/scy scale the X (basis row 0) and Y (basis row 2) axes independently; the first
        record in the array (idx 0) has no parent joint and cannot be scaled live."""
        if idx not in self.stock:
            return
        if scy is None:
            scy = scx
        sx, sy = struct.unpack_from(">2f", self.stock[idx], 0x10)
        writes = [(0x10, struct.pack(">2f", sx + dx, sy + dy))]
        tgt = idx - 1                               # parent joint that actually scales this glyph
        if (abs(scx - 1.0) > 1e-4 or abs(scy - 1.0) > 1e-4) and tgt in self.stock:
            comps = self._scale_comps(tgt)          # [(row0 fi,val), (row2 fi,val), (row3 fi,val)]
            for (fi, val), s in ((comps[0], scx), (comps[1], scy)):   # row0->X, row2->Y
                if abs(s - 1.0) > 1e-4:
                    writes.append((-STRIDE + fi * 4, struct.pack(">f", val * s)))
        with self._lock:
            self._holds[idx] = writes
            self._halted = ""

    def clear_hold(self, idx):
        with self._lock:
            self._holds.pop(idx, None)
        for r in (idx, idx - 1):                    # idx-1 may carry this element's scale write
            if r in self.stock:                     # snap back to stock immediately; the writer
                xm.write_bytes(self.h, self.mtx + r * STRIDE, self.stock[r])   # re-asserts its own hold

    def restore_all(self):
        with self._lock:
            self._holds.clear()
        for i, rec in self.stock.items():
            xm.write_bytes(self.h, self.mtx + i * STRIDE, rec)

    def blink(self, idx, amp=45.0, cycles=6, period=0.11):
        """Jiggle a record left/right a few times so the user can spot which glyph it is. Runs on a
        throwaway thread; respects any active hold afterwards by restoring it."""
        if idx not in self.stock:
            return
        sx, sy = struct.unpack_from(">2f", self.stock[idx], 0x10)
        with self._lock:
            held = self._holds.get(idx)

        def run():
            base = self.mtx + idx * STRIDE + 0x10
            for c in range(cycles):
                dx = amp if c % 2 == 0 else -amp
                end = time.time() + period
                pk = struct.pack(">2f", sx + dx, sy)
                while time.time() < end:
                    xm.write_bytes(self.h, base, pk)
            # leave it as the active hold (or stock) dictated
            if held is None:
                xm.write_bytes(self.h, self.mtx + idx * STRIDE, self.stock[idx])
        threading.Thread(target=run, daemon=True).start()

    # ── background writer ────────────────────────────────────────────────────
    def _start_writer(self):
        self._stop.clear()
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._writer.start()

    def _anchor_bad(self, idx):
        """True if record `idx` no longer looks like our matrix slot (z/w off stock). A SINGLE bad
        read is not trusted — the game rewrites the record every frame, so a read can catch it
        mid-update (torn). The caller debounces across consecutive checks; a real scene rebuild
        stays bad indefinitely while a tear clears next read."""
        cur = xm.read_bytes(self.h, self.mtx + idx * STRIDE + 0x18, 8)
        if not cur or len(cur) < 8:
            return False                         # transient read failure — don't count it
        z, w = struct.unpack(">2f", cur)
        sz = struct.unpack_from(">f", self.stock[idx], 0x18)[0]
        return abs(w - 1.0) > 0.05 or abs(z - sz) > 6.0

    def _run(self):
        # Write cadence is a tight loop (must out-race the ~60/s per-frame eval). Validation is
        # THROTTLED (a few checks/s) and DEBOUNCED (N consecutive bad) so a one-frame torn read
        # can't false-trip the safety halt — only a persistent rebuild does.
        VALIDATE_EVERY = 0.15
        BAD_LIMIT = 4
        next_check = 0.0
        bad_streak = 0
        while not self._stop.is_set():
            with self._lock:
                holds = list(self._holds.items())
            if not holds:
                bad_streak = 0
                time.sleep(0.05)
                continue
            now = time.time()
            if now >= next_check:
                next_check = now + VALIDATE_EVERY
                if any(self._anchor_bad(idx) for idx, _ in holds):
                    bad_streak += 1
                    if bad_streak >= BAD_LIMIT:
                        with self._lock:
                            self._holds.clear()
                        self._halted = ("The scoreclock scene moved (rebuild/menu). Live holds stopped "
                                        "for safety — click Re-attach to resume.")
                        continue
                else:
                    bad_streak = 0
            for idx, writes in holds:
                for rel, pk in writes:
                    xm.write_bytes(self.h, self.mtx + idx * STRIDE + rel, pk)

    @property
    def halted(self):
        return self._halted

    def close(self):
        self._stop.set()
        try:
            self.restore_all()
        except Exception:
            pass
        if self.h:
            xm.close_handle(self.h)
            self.h = None

    # ── persistent labels ────────────────────────────────────────────────────
    def _labels_path(self):
        return Path(self.gdir) / "scoreclock_live_labels.json"

    def _index_shift(self, data):
        """Records saved against a different array-start must be re-keyed to the current start.
        `data` is the parsed labels/hidden JSON. Returns the shift to ADD to saved indices:
        (saved_start_off - current_start_off) / STRIDE. Legacy files (no _start_off) were saved
        against the old hint (0x219B70), which is 2 records after the true start."""
        saved_off = data.get("_start_off") if isinstance(data, dict) else None
        if not isinstance(saved_off, int):
            saved_off = MTX_OFF_HINT            # legacy: keyed off the old (too-late) hint
        return (saved_off - self.mtx_off) // STRIDE

    def load_labels(self):
        p = self._labels_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text())
        except Exception:
            return {}
        raw = data.get("labels", data) if isinstance(data, dict) else {}
        shift = self._index_shift(data)
        out = {}
        for k, v in raw.items():
            if k == "_start_off" or not str(k).lstrip("-").isdigit():
                continue
            out[int(k) + shift] = v
        return out

    def save_labels(self, labels):
        try:
            self._labels_path().write_text(json.dumps(
                {"_start_off": self.mtx_off,
                 "labels": {str(k): v for k, v in labels.items()}}, indent=1))
        except Exception:
            pass

    def _hidden_path(self):
        return Path(self.gdir) / "scoreclock_live_hidden.json"

    def load_hidden(self):
        p = self._hidden_path()
        if not p.exists():
            return set()
        try:
            data = json.loads(p.read_text())
        except Exception:
            return set()
        if isinstance(data, dict):
            shift = self._index_shift(data)
            return set(int(i) + shift for i in data.get("hidden", []))
        shift = self._index_shift({})           # legacy flat list → old-hint keyed
        return set(int(i) + shift for i in data)

    def save_hidden(self, hidden):
        try:
            self._hidden_path().write_text(json.dumps(
                {"_start_off": self.mtx_off, "hidden": sorted(int(i) for i in hidden)}))
        except Exception:
            pass

    # ── bake current holds into the offline file ─────────────────────────────
    def bake(self, labels, log=print):
        """Translate current live position holds into scorebug_layout offline edits, keyed by the
        element NAME the user identified for each record (label must match a scorebug_layout element).
        Position deltas transfer 1:1 (same +X right / +Y up scene convention). Scale is live-only
        (the offline glyph-scale lever is a different mechanism) and is skipped here with a note."""
        name_by_label = {v: k for k, v in _SBL.LABELS.items()}
        edits, skipped = {}, []
        with self._lock:
            holds = dict(self._holds)
        for idx, writes in holds.items():
            lbl = labels.get(idx)
            nm = name_by_label.get(lbl) or (lbl if lbl in _SBL.EDITABLE_TEXT else None)
            if not nm:
                skipped.append(f"record {idx} ({lbl or 'unnamed'}) — no matching offline element")
                continue
            sx, sy = struct.unpack_from(">2f", self.stock[idx], 0x10)
            nx, ny = struct.unpack(">2f", writes[0][1])
            dx, dy = nx - sx, ny - sy
            if abs(dx) > 1e-3 or abs(dy) > 1e-3:
                edits[nm] = {"dx": dx, "dy": dy}
            if len(writes) > 1:
                log(f"  note: record {idx} ({lbl}) scale is live-only; not baked.")
        if not edits:
            raise RuntimeError("Nothing to bake — no identified position changes. "
                               + ("; ".join(skipped) if skipped else ""))
        for s in skipped:
            log(f"  skipped: {s}")
        return _SBL.apply_edits(edits, self.gdir, log)


# ══════════════════════════════════════════════════════════════════════════════
# SOG text elements — a SEPARATE additive live layer (does NOT touch the matrix writer)
# ══════════════════════════════════════════════════════════════════════════════
# The added shots elements (scorebug_add_shots: shots_away / gameclock4 label / shots_home)
# are JOINTLESS — they have no world-matrix record. The engine renders them straight from
# their text record's +0x68 (X,Y scene coords) / +0x7C (size), and does NOT re-assert those
# per frame, so a single write already holds and a light ~10 Hz re-assert is plenty (no tight
# spin). This layer is fully independent of LiveScoreclock's matrix writer above — its own
# located records, own hold dict, own thread — so it can't perturb the working joint paths.
_TXT_KEY    = 0xE9015CE9        # crc32('scorebug_text') — text record key at +0x00
_TXT_POS    = 0x68             # X,Y floats (absolute scene coords, +X right / +Y up)
_TXT_SIZE   = 0x7C            # size float
# (oname as stored in the record's raw-name crc at +0x04, friendly label)
_SHOTS_ELEMENTS = [
    ("shots_away", "Away Shots"),
    ("gameclock4", "Shots Label"),
    ("shots_home", "Home Shots"),
]


def _rawcrc(s):
    return zlib.crc32(s.encode("ascii")) & 0xFFFFFFFF


class LiveScoreText:
    """Locate the jointless SOG text records in live blob0 by their
    [scorebug_text key][raw-name crc] 8-byte signature, then hold an absolute (X, Y) and size
    on +0x68 / +0x7C. Shares the caller's process handle + blob0 base; keeps its own thread."""

    def __init__(self, h, base):
        self.h = h
        self.base = base
        self.recs = {}                # oname -> {'addr','label','x','y','size'} (stock captured)
        self._holds = {}              # oname -> (target_x, target_y, target_size)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._halted = ""
        self._writer = None
        self._locate()
        if self.recs:
            self._start_writer()

    def _locate(self):
        """Bounded search of blob0 memory for each record's key signature (the relocated table
        sits at the very end of blob0, ~2.5 MB in; 4 MB window covers it with margin)."""
        cap, data, off = 0x400000, bytearray(), 0
        while off < cap:
            n = min(0x100000, cap - off)
            chunk = xm.read_bytes(self.h, self.base + off, n)
            if not chunk:
                break
            data += chunk
            off += len(chunk)
            if len(chunk) < n:
                break
        for oname, label in _SHOTS_ELEMENTS:
            j = data.find(struct.pack(">II", _TXT_KEY, _rawcrc(oname)))
            if j < 0:
                continue
            addr = self.base + j
            pb = xm.read_bytes(self.h, addr + _TXT_POS, 8)
            sb = xm.read_bytes(self.h, addr + _TXT_SIZE, 4)
            if not pb or not sb:
                continue
            x, y = struct.unpack(">2f", pb)
            size = struct.unpack(">f", sb)[0]
            self.recs[oname] = {"addr": addr, "label": label, "x": x, "y": y, "size": size}

    # ── queries ──────────────────────────────────────────────────────────────
    def elements(self):
        """[(oname, label, stock_x, stock_y, stock_size)] for each located SOG element."""
        return [(o, r["label"], r["x"], r["y"], r["size"]) for o, r in self.recs.items()]

    def pos(self, oname):
        r = self.recs.get(oname)
        if not r:
            return None
        b = xm.read_bytes(self.h, r["addr"] + _TXT_POS, 8)
        return struct.unpack(">2f", b) if b else None

    def live_values(self, oname):
        """Current on-screen (x, y, size) for a SOG element (what Bake persists)."""
        r = self.recs.get(oname)
        if not r:
            return None
        pb = xm.read_bytes(self.h, r["addr"] + _TXT_POS, 8)
        sb = xm.read_bytes(self.h, r["addr"] + _TXT_SIZE, 4)
        if not pb or not sb:
            return None
        x, y = struct.unpack(">2f", pb)
        return (x, y, struct.unpack(">f", sb)[0])

    # ── holds ─────────────────────────────────────────────────────────────────
    def set_hold(self, oname, dx, dy, scale):
        r = self.recs.get(oname)
        if not r:
            return
        tx, ty, ts = r["x"] + dx, r["y"] + dy, r["size"] * scale
        with self._lock:
            self._holds[oname] = (tx, ty, ts)
            self._halted = ""
        xm.write_bytes(self.h, r["addr"] + _TXT_POS, struct.pack(">2f", tx, ty))
        xm.write_bytes(self.h, r["addr"] + _TXT_SIZE, struct.pack(">f", ts))

    def clear_hold(self, oname):
        with self._lock:
            self._holds.pop(oname, None)
        r = self.recs.get(oname)
        if r:
            xm.write_bytes(self.h, r["addr"] + _TXT_POS, struct.pack(">2f", r["x"], r["y"]))
            xm.write_bytes(self.h, r["addr"] + _TXT_SIZE, struct.pack(">f", r["size"]))

    def restore_all(self):
        with self._lock:
            self._holds.clear()
        for r in self.recs.values():
            xm.write_bytes(self.h, r["addr"] + _TXT_POS, struct.pack(">2f", r["x"], r["y"]))
            xm.write_bytes(self.h, r["addr"] + _TXT_SIZE, struct.pack(">f", r["size"]))

    def blink(self, oname, amp=45.0, cycles=6, period=0.11):
        r = self.recs.get(oname)
        if not r:
            return
        with self._lock:
            held = self._holds.get(oname)

        def run():
            a = r["addr"] + _TXT_POS
            for c in range(cycles):
                dx = amp if c % 2 == 0 else -amp
                end = time.time() + period
                pk = struct.pack(">2f", r["x"] + dx, r["y"])
                while time.time() < end:
                    xm.write_bytes(self.h, a, pk)
            rest = (held[0], held[1]) if held else (r["x"], r["y"])
            xm.write_bytes(self.h, a, struct.pack(">2f", *rest))
        threading.Thread(target=run, daemon=True).start()

    # ── writer (light re-assert; validates the record key each pass) ───────────
    def _anchor_bad(self, r):
        b = xm.read_bytes(self.h, r["addr"], 4)
        if not b or len(b) < 4:
            return False                        # transient read failure — don't count it
        return struct.unpack(">I", b)[0] != _TXT_KEY

    def _start_writer(self):
        self._stop.clear()
        self._writer = threading.Thread(target=self._run, daemon=True)
        self._writer.start()

    def _run(self):
        bad_streak = 0
        while not self._stop.is_set():
            with self._lock:
                holds = list(self._holds.items())
            if not holds:
                bad_streak = 0
                time.sleep(0.1)
                continue
            if any(self._anchor_bad(self.recs[o]) for o, _ in holds):
                bad_streak += 1
                if bad_streak >= 4:
                    with self._lock:
                        self._holds.clear()
                    self._halted = ("The scoreclock scene moved (rebuild/menu). Shots holds stopped "
                                    "for safety — click Re-attach to resume.")
                    continue
            else:
                bad_streak = 0
            for o, (tx, ty, ts) in holds:
                a = self.recs[o]["addr"]
                xm.write_bytes(self.h, a + _TXT_POS, struct.pack(">2f", tx, ty))
                xm.write_bytes(self.h, a + _TXT_SIZE, struct.pack(">f", ts))
            time.sleep(0.1)

    @property
    def halted(self):
        return self._halted

    def close(self):
        self._stop.set()
        try:
            self.restore_all()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# UI — a Toplevel live-tune window (mirrors ros_live_editor's open_* pattern)
# ══════════════════════════════════════════════════════════════════════════════
import tkinter as tk                                  # noqa: E402
from tkinter import ttk, messagebox                   # noqa: E402

# Friendly default names for the identified scoreclock joints, in case the user hasn't blinked
# them yet. These are GUESSES (world-matrix record order); the user confirms via Blink + rename.
# Array now starts at the TRUE record[0] (the away abbreviation). By joint order the first four
# are abbrevs + scores; the rest are the clock/period digits + plumbing (blink to confirm).
_DEFAULT_LABELS = {
    0: "Away Abbreviation", 1: "Home Abbreviation",
    2: "Away Score", 3: "Home Score",
}


class LiveScoreclockFrame(ttk.Frame):
    def __init__(self, master, gdir):
        super().__init__(master)
        self.gdir = gdir
        self.live = None
        self.text_layer = None              # additive SOG-text layer (separate from matrix writer)
        self.labels = {}
        self.hidden = set()                 # records the user marked as clutter (persisted)
        self.sel = None
        self._edits = {}                    # key -> [dx, dy, scaleX, scaleY]
        self._build()
        self._attach()
        self._poll()

    # ── layout ───────────────────────────────────────────────────────────────
    def _build(self):
        top = ttk.Frame(self, padding=6); top.pack(fill=tk.X)
        ttk.Button(top, text="Re-attach", command=self._attach).pack(side=tk.LEFT)
        ttk.Button(top, text="Restore All (stock)", command=self._restore_all).pack(side=tk.LEFT, padx=6)
        self._show_hidden = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show hidden", variable=self._show_hidden,
                        command=self._fill).pack(side=tk.LEFT)
        self._status = ttk.Label(top, text="", foreground="#888"); self._status.pack(side=tk.LEFT, padx=10)

        ttk.Label(self, foreground="#e0a030", font=("Segoe UI", 8), justify=tk.LEFT, wraplength=560,
                  text="LIVE tuning of the running game (Xenia) — instant, but held in memory only and "
                       "lost on relaunch. Be IN a game with the scoreclock on screen. Pick an element, "
                       "Blink to see which glyph it is, then drag. “Bake to file” makes a layout "
                       "permanent.").pack(fill=tk.X, padx=8, pady=(0, 4))

        body = ttk.Frame(self); body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        left = ttk.Frame(body); left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._tv = ttk.Treeview(left, columns=("idx", "pos"), show="tree headings",
                                selectmode="browse", height=12)
        self._tv.heading("#0", text="Element"); self._tv.column("#0", width=170)
        self._tv.heading("idx", text="Rec"); self._tv.column("idx", width=42, anchor=tk.CENTER)
        self._tv.heading("pos", text="Live X,Y"); self._tv.column("pos", width=110, anchor=tk.E)
        sb = ttk.Scrollbar(left, command=self._tv.yview); self._tv.configure(yscroll=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y); self._tv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._tv.bind("<<TreeviewSelect>>", self._on_sel)

        ctl = ttk.Frame(body, padding=(12, 0, 0, 0)); ctl.pack(side=tk.LEFT, fill=tk.Y)
        self._selvar = tk.StringVar(value="(no element selected)")
        ttk.Label(ctl, textvariable=self._selvar, font=("Segoe UI", 9, "bold"),
                  wraplength=220).pack(anchor=tk.W, pady=(0, 6))

        idf = ttk.Frame(ctl); idf.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(idf, text="Blink to identify", command=self._blink).pack(side=tk.LEFT)
        self._name = ttk.Entry(idf, width=16); self._name.pack(side=tk.LEFT, padx=4)
        ttk.Button(idf, text="Rename", command=self._rename).pack(side=tk.LEFT)

        self._hidebtn = ttk.Button(ctl, text="Hide this element", command=self._toggle_hidden)
        self._hidebtn.pack(fill=tk.X, pady=(0, 6))

        self._sx = self._slider(ctl, "Move X  (+right)", -220, 220, self._on_slide)
        self._sy = self._slider(ctl, "Move Y  (+up)", -220, 220, self._on_slide)
        self._ss = self._slider(ctl, "Scale X", 0.3, 3.0, self._on_slide, resolution=0.05, init=1.0)
        self._ss2 = self._slider(ctl, "Scale Y", 0.3, 3.0, self._on_slide, resolution=0.05, init=1.0)
        self._scaley_note = ttk.Label(ctl, foreground="#888", font=("Segoe UI", 8),
                                      wraplength=220, justify=tk.LEFT, text="")
        self._scaley_note.pack(anchor=tk.W)

        self._valvar = tk.StringVar(value="")
        ttk.Label(ctl, textvariable=self._valvar, foreground="#8c8").pack(anchor=tk.W, pady=(2, 6))
        ttk.Button(ctl, text="Reset this element", command=self._reset_sel).pack(fill=tk.X)
        ttk.Separator(ctl).pack(fill=tk.X, pady=8)
        ttk.Button(ctl, text="Bake current layout to file…",
                   command=self._bake).pack(fill=tk.X)
        ttk.Label(ctl, foreground="#888", font=("Segoe UI", 8), wraplength=220, justify=tk.LEFT,
                  text="Bake writes into overlay_static.iff (permanent, shows next launch): "
                       "identified joint POSITION moves, and SOG position+size. Joint scale "
                       "is live-only.").pack(anchor=tk.W, pady=(2, 0))

    def _slider(self, parent, label, lo, hi, cmd, resolution=1.0, init=0.0):
        """A TYPEABLE numeric control (spinbox: type a value or nudge with the arrows), to
        match the offline panels. Keeps a `._var` DoubleVar so the rest of the frame is
        unchanged. Fires `cmd` on Enter / focus-out / arrow-step; partial or empty typing
        is ignored until it parses (see _on_slide's guard)."""
        f = ttk.Frame(parent); f.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(f, text=label, width=16).pack(side=tk.LEFT)
        v = tk.DoubleVar(value=init)
        sp = ttk.Spinbox(f, from_=lo, to=hi, increment=resolution, textvariable=v,
                         width=9, justify=tk.RIGHT, command=cmd)
        sp.pack(side=tk.LEFT)
        sp.bind("<Return>", lambda _=None: cmd())
        sp.bind("<FocusOut>", lambda _=None: cmd())
        sp.bind("<KeyRelease>", lambda _=None: cmd())
        sp._var = v
        return sp

    # ── attach / list ──────────────────────────────────────────────────────
    def _attach(self):
        try:
            if self.text_layer:            # stop the SOG layer BEFORE the shared handle closes
                self.text_layer.close()
                self.text_layer = None
            if self.live:
                self.live.close()
            self.live = LiveScoreclock(self.gdir)
        except Exception as e:
            self.live = None
            self._status.config(text=str(e), foreground="#e06060")
            self._tv.delete(*self._tv.get_children())
            return
        try:                               # additive: locate the jointless SOG text elements
            self.text_layer = LiveScoreText(self.live.h, self.live.base)
            if not self.text_layer.recs:
                self.text_layer = None
        except Exception:
            self.text_layer = None
        self.labels = self.live.load_labels()
        self.hidden = self.live.load_hidden()
        self._edits.clear()
        recs = self.live.real_records()
        self._status.config(text=f"Attached — matrix @0x{self.live.mtx:X}. "
                                 f"{len(recs)} elements ({len(self.hidden & set(recs))} hidden).",
                            foreground="#8c8")
        self._fill()

    def _fill(self):
        self._tv.delete(*self._tv.get_children())
        if not self.live:
            return
        show_hidden = self._show_hidden.get()
        for i in self.live.real_records():
            if i in self.hidden and not show_hidden:
                continue
            nm = self.labels.get(i) or _DEFAULT_LABELS.get(i, f"Record {i}")
            if i in self.hidden:
                nm = "• " + nm                        # mark hidden rows when shown
            x, y = self.live.stock_pos(i)
            self._tv.insert("", tk.END, iid=str(i), text=nm, values=(i, f"{x:.0f}, {y:.0f}"))
        # additive SOG text elements (jointless) — distinct "T:" iids, own layer
        if self.text_layer:
            for oname, label, x, y, _sz in self.text_layer.elements():
                self._tv.insert("", tk.END, iid="T:" + oname, text=label,
                                values=("SOG", f"{x:.0f}, {y:.0f}"))

    def _on_sel(self, _=None):
        s = self._tv.selection()
        if not s or not self.live:
            return
        sid = s[0]
        if sid.startswith("T:"):                        # SOG text element
            self.sel = sid
            oname = sid[2:]
            r = (self.text_layer.recs.get(oname) if self.text_layer else None) or {}
            nm = r.get("label", oname)
            self._selvar.set(f"{nm}  (SOG text)")
            self._name.delete(0, tk.END); self._name.insert(0, nm)
            self._hidebtn.config(text="Hide this element")
            self._load_sliders()
            self._config_scaley()
            return
        self.sel = int(sid)
        nm = self.labels.get(self.sel) or _DEFAULT_LABELS.get(self.sel, f"Record {self.sel}")
        self._selvar.set(f"{nm}  (record {self.sel})")
        self._name.delete(0, tk.END); self._name.insert(0, nm)
        self._hidebtn.config(text="Show this element" if self.sel in self.hidden
                             else "Hide this element")
        self._load_sliders()
        self._config_scaley()

    def _ed(self, key):
        """Current [dx, dy, scaleX, scaleY] for `key`, tolerating old 3-value entries."""
        v = self._edits.get(key)
        if not v:
            return [0.0, 0.0, 1.0, 1.0]
        if len(v) == 3:                                  # legacy [dx, dy, uniform-scale]
            return [v[0], v[1], v[2], v[2]]
        return list(v)

    def _load_sliders(self):
        dx, dy, scx, scy = self._ed(self.sel)
        for sl, val in ((self._sx, dx), (self._sy, dy), (self._ss, scx), (self._ss2, scy)):
            sl._var.set(val)

    def _config_scaley(self):
        """Scale-Y is only meaningful for joint (matrix) elements. Text (SOG) has one size
        field, and the very first joint (record 0) has no parent to scale."""
        is_text = isinstance(self.sel, str) and self.sel.startswith("T:")
        if is_text:
            self._ss.state(["!disabled"]); self._ss2.state(["disabled"])
            self._scaley_note.config(text="SOG text: one size only (Scale X). Number size is "
                                          "set offline, not live.")
        elif self.sel == 0:
            self._ss.state(["disabled"]); self._ss2.state(["disabled"])
            self._scaley_note.config(text="First joint has no parent — not scalable live.")
        else:
            self._ss.state(["!disabled"]); self._ss2.state(["!disabled"])
            self._scaley_note.config(text="")

    # ── edits ────────────────────────────────────────────────────────────────
    def _on_slide(self):
        if self.sel is None or not self.live:
            return
        try:                                   # ignore mid-typing / empty spinbox values
            dx = float(self._sx._var.get())
            dy = float(self._sy._var.get())
            scx = float(self._ss._var.get())
            scy = float(self._ss2._var.get())
        except (tk.TclError, ValueError):
            return
        if isinstance(self.sel, str) and self.sel.startswith("T:"):
            scy = scx                          # text has one size field; mirror X into the store
            self._edits[self.sel] = [dx, dy, scx, scy]
            if self.text_layer:
                self.text_layer.set_hold(self.sel[2:], dx, dy, scx)
            self._valvar.set(f"Δ ({dx:+.0f}, {dy:+.0f})  size {scx:.2f}×")
        else:
            self._edits[self.sel] = [dx, dy, scx, scy]
            self.live.set_hold(self.sel, dx, dy, scx, scy)
            self._valvar.set(f"Δ ({dx:+.0f}, {dy:+.0f})  scale {scx:.2f}×X {scy:.2f}×Y")

    def _reset_sel(self):
        if self.sel is None or not self.live:
            return
        self._edits.pop(self.sel, None)
        if isinstance(self.sel, str) and self.sel.startswith("T:"):
            if self.text_layer:
                self.text_layer.clear_hold(self.sel[2:])
        else:
            self.live.clear_hold(self.sel)
        for sl, val in ((self._sx, 0), (self._sy, 0), (self._ss, 1.0), (self._ss2, 1.0)):
            sl._var.set(val)
        self._valvar.set("reset to stock")

    def _restore_all(self):
        if not self.live:
            return
        self.live.restore_all()
        if self.text_layer:
            self.text_layer.restore_all()
        self._edits.clear()
        for sl, val in ((self._sx, 0), (self._sy, 0), (self._ss, 1.0), (self._ss2, 1.0)):
            sl._var.set(val)
        self._valvar.set("all elements restored to stock")

    def _blink(self):
        if self.sel is None or not self.live:
            return
        if isinstance(self.sel, str) and self.sel.startswith("T:"):
            if self.text_layer:
                self.text_layer.blink(self.sel[2:])
            return
        self.live.blink(self.sel)

    def _rename(self):
        if self.sel is None or (isinstance(self.sel, str) and self.sel.startswith("T:")):
            return                                       # SOG labels are fixed
        nm = self._name.get().strip()
        if not nm:
            return
        self.labels[self.sel] = nm
        self.live.save_labels(self.labels)
        self._tv.item(str(self.sel), text=nm)
        self._selvar.set(f"{nm}  (record {self.sel})")

    def _toggle_hidden(self):
        if self.sel is None or not self.live:
            return
        if isinstance(self.sel, str) and self.sel.startswith("T:"):
            return                                       # SOG rows aren't hideable
        if self.sel in self.hidden:
            self.hidden.discard(self.sel)
            self._hidebtn.config(text="Hide this element")
        else:
            self.hidden.add(self.sel)
            self._hidebtn.config(text="Show this element")
        self.live.save_hidden(self.hidden)
        self._fill()

    def _bake(self):
        if not self.live:
            return
        if not self._edits:
            messagebox.showinfo("Bake", "No live edits to bake."); return
        text_edits = {k for k in self._edits if isinstance(k, str) and k.startswith("T:")}
        matrix_edits = {k for k in self._edits if k not in text_edits}
        logs = []
        # SOG text elements bake straight into their overlay_static text records (+0x68/+0x7C)
        if text_edits and self.text_layer:
            import scorebug_add_shots as _SAS
            positions = {}
            for k in text_edits:
                vals = self.text_layer.live_values(k[2:])
                if vals:
                    positions[k[2:]] = vals
            try:
                up = _SAS.set_element_positions(self.gdir, positions)
                logs.append(f"  SOG baked: {', '.join(up)}")
            except Exception as e:
                messagebox.showerror("Bake to file", f"SOG bake failed: {e}"); return
        # matrix (joint) position moves -> scorebug_layout offline edits
        if matrix_edits:
            try:
                self.live.bake(self.labels, logs.append)
            except Exception as e:
                if not text_edits:
                    messagebox.showerror("Bake to file", f"{e}\n\n" + "\n".join(logs)); return
                logs.append(f"  matrix skipped: {e}")
        messagebox.showinfo("Bake to file",
                            "Baked into overlay_static.iff — shows on the next game launch.\n\n"
                            + "\n".join(logs))

    # ── poll: refresh live pos + surface a safety halt ────────────────────────
    def _poll(self):
        if self.live:
            halt = self.live.halted or (self.text_layer.halted if self.text_layer else "")
            if halt:
                self._status.config(text=halt, foreground="#e0a030")
            elif self.sel is not None:
                if isinstance(self.sel, str) and self.sel.startswith("T:"):
                    p = self.text_layer.pos(self.sel[2:]) if self.text_layer else None
                    if p:
                        self._tv.set(self.sel, "pos", f"{p[0]:.0f}, {p[1]:.0f}")
                else:
                    p = self.live.pos(self.sel)
                    if p:
                        self._tv.set(str(self.sel), "pos", f"{p[0]:.0f}, {p[1]:.0f}")
        try:
            self.after(300, self._poll)
        except tk.TclError:
            pass

    def destroy(self):
        try:
            if self.text_layer:                 # stop SOG layer before the shared handle closes
                self.text_layer.close()
        except Exception:
            pass
        try:
            if self.live:
                self.live.close()
        except Exception:
            pass
        super().destroy()


def open_live_scoreclock(parent, gdir):
    win = tk.Toplevel(parent)
    win.title("NHL 2K10 — Live Scoreclock Tune (running game)")
    win.geometry("620x560")
    frame = LiveScoreclockFrame(win, gdir)
    frame.pack(fill=tk.BOTH, expand=True)
    win.protocol("WM_DELETE_WINDOW", lambda: (frame.destroy(), win.destroy()))
    return win
