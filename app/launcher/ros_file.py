"""
ros_file.py — parser / in-place editor for an NHL 2K10 Roster.ROS save (big-endian, Xbox 360).

Container format (reverse-engineered; CORRECTED 2026-08-01 — see "the directory" below):
  0x00  u32  total size (filesize - 4)
  0x04  u32  version (1)
  0x08  u32  0xED — NOT the chunk count; treated here as an upper bound only
  0x0C  N x [ u32 type_hash | u32 count | u32 data_ptr ]           # the chunk directory
  ...        data section, tiling with no gaps, first chunk immediately after the directory.

## The directory (this was wrong until 2026-08-01)

`data_ptr` is a **self-relative pointer**, the same convention every other pointer in this
file uses:  ``target = <offset of the data_ptr field itself> + value - 1``.  It is NOT an
offset from a fixed data base.  The old reading (a constant DATA_BASE = 0xB28, i.e. the end
of a 237-entry directory) put every chunk 0xB28 too far in and, worse, at the wrong *phase*
— the framing error that ros_editor_gui.py's "REBASED" note and player_assign.py's
DELTA = 0x73 were both compensating for by hand.

There are **19 chunks**, not the 237 the header's 0x08 field suggests; entry 19 onward is
already chunk-0 data being misread as directory. 19 is confirmed by `Roster_FixupPointers
@0x83D53AA0`, which hardcodes one block per table for i = 0..18 and none past it.

Chunks are located *only* by their own directory pointer. They happen to tile the file in
directory order in a shipped save, but nothing reads them that way, so a relocated chunk and
the dead space it leaves behind are both legal — see ros_grow.py, which grows the arena table
by appending a longer copy at EOF.

With the pointers read correctly every record table tiles **exactly** — `size == count *
stride` to the byte for all 19 chunks (2715x420 players, 96x412 teams, 40x40 arenas, ...).
So the stride is just `size // count`; the byte-autocorrelation guess is kept only as a
fallback for a save whose chunks do not divide evenly. Chunks with count<=1 that don't
divide are raw BLOBS (e.g. the string pool 0xEB69DFB9, whose `count` is not a record count).

Editing is strictly IN PLACE (file size never changes) so every fixed offset the game holds stays
valid — the same invariant roster_editor.py relies on. A .bak is written before the first save.

CORRECTED 2026-07-31: there is NO per-save scramble or record checksum. One in-game roster edit
produces a 7-byte whole-file diff. The field that misled this note is the 20-bit region at player
record +0xC3..+0xC5, which does change on ~every record between saves (its low 4 bits are preserved)
but does NOT change when other fields change — so it is not a checksum over the record, and nothing
needs recomputing after a write. The goalie mask in particular is a plain bitfield and round-trips
fine; see player_assign.py, which maps the disk record onto the in-memory struct (disk = mem + 0x73)
and writes portrait keys and goalie masks statically.
"""
from __future__ import annotations
import struct
from pathlib import Path

DIR_OFF = 0x0C              # first directory entry
DIR_ENT = 12                # u32 type_hash | u32 count | u32 data_ptr
DIR_MAX = 19                # exactly 19 — see _read_dir


def _be(d, o): return struct.unpack_from(">I", d, o)[0]


def _autocorr_stride(region, lo, hi, sample=48000):
    """Best record stride in [lo,hi] by fraction of bytes equal to their counterpart `stride` away."""
    n = len(region)
    best, best_s = -1.0, lo
    for s in range(lo, hi + 1):
        m = min(n - s, sample)
        if m <= 0:
            continue
        step = max(1, m // 8000)                     # sample for speed on big chunks
        eq = tot = 0
        for k in range(0, m, step):
            eq += region[k] == region[k + s]; tot += 1
        sc = eq / tot if tot else 0
        if sc > best:
            best, best_s = sc, s
    return best_s, best


class Chunk:
    __slots__ = ("index", "hash", "count", "foff", "size", "stride", "nrec", "score", "kind")

    def __init__(self, index, h, count, foff, size):
        self.index, self.hash, self.count, self.foff, self.size = index, h, count, foff, size
        self.stride = 0; self.nrec = 0; self.score = 0.0; self.kind = "blob"

    @property
    def name(self):
        return f"0x{self.hash:08X}"

    def label(self):
        if self.kind == "records":
            return f"0x{self.hash:08X}  ·  {self.nrec} rows × {self.stride}B"
        return f"0x{self.hash:08X}  ·  blob {self.size}B" + (f" (count {self.count})" if self.count else "")


class RosFile:
    def __init__(self, path):
        self.path = Path(path)
        self.data = bytearray(self.path.read_bytes())
        self.orig_size = len(self.data)
        # The file is a fixed-length save SECTION; the u32 at 0x00 is the size of the *meaningful*
        # image inside it, and anything past that is pad the game never reads (see ros_grow.py).
        # A stock file has no pad, so image_size == orig_size and nothing below changes. On a
        # padded file every offset must be bounded by the image, not by EOF, or the last chunk
        # swallows 129 KB of zeros and the header-size check fails against a legal file.
        img = _be(self.data, 0) + 4 if len(self.data) >= 4 else 0
        self.image_size = img if 0 < img <= self.orig_size else self.orig_size
        self.pad = self.orig_size - self.image_size
        self.chunks: list[Chunk] = []
        self._parse()

    def _read_dir(self):
        """[(index, hash, count, data_offset)] — the 19 directory entries.

        The count at 0x08 is an upper bound, not the entry count (see the module header). The
        real length is **exactly 19**, and that is not a guess: `Roster_FixupPointers @0x83D53AA0`
        contains one hardcoded block per table reading count@`0x0c+12i` / ptr@`0x10+12i` of the
        manager struct (which IS this file image at `filebase+4`) for i = 0..18, and nothing
        past entry 18. Entry 19 onward is chunk-0 data.

        CORRECTED 2026-08-04: this used to stop early on an entry whose data offset went
        *backwards*, on the assumption that the chunks tile the file in directory order. They
        do in a shipped save, but that is an artifact of how the game writes it, not a rule the
        game reads by — each table is located solely by its own directory slot. Growing a table
        by relocating it to EOF (see ros_grow.py) leaves every later entry pointing "backwards"
        and used to truncate the directory to 5 chunks here. Order is now irrelevant.
        """
        d, out = self.data, []
        dir_end = DIR_OFF + DIR_MAX * DIR_ENT
        for i in range(min(DIR_MAX, (len(d) - DIR_OFF) // DIR_ENT)):
            fo = DIR_OFF + i * DIR_ENT
            h, cnt, ptr = _be(d, fo), _be(d, fo + 4), _be(d, fo + 8)
            if ptr == 0:
                break
            off = fo + 8 + ptr - 1                                  # self-relative
            if off < dir_end or off > self.image_size:
                break                                               # landed in the directory / past EOF
            out.append((i, h, cnt, off))
        if not out:
            raise ValueError("no readable chunk directory — is this a Roster.ROS?")
        return out

    def _parse(self):
        ents = self._read_dir()
        order = sorted(range(len(ents)), key=lambda k: (ents[k][3], k))   # by data offset, ties by index
        for k, j in enumerate(order):
            i, h, cnt, off = ents[j]
            nxt = ents[order[k + 1]][3] if k + 1 < len(order) else self.image_size
            self.chunks.append(Chunk(i, h, cnt, off, nxt - off))
        self.chunks.sort(key=lambda c: c.index)                     # directory order, for a stable display
        for c in self.chunks:
            self._classify(c)

    def _classify(self, c: Chunk):
        # With the directory read correctly every record table divides exactly, so trust that
        # first and only fall back to the autocorrelation guess if it doesn't.
        if c.count > c.size:
            c.kind = "blob"          # `count` can't be a record count — it's a blob (the string pool)
            return
        if c.count >= 2 and c.size >= 8 and c.size % c.count == 0:
            c.kind, c.stride, c.nrec, c.score = "records", c.size // c.count, c.count, 1.0
            return
        if c.count >= 2 and c.size >= 8:
            base = c.size // c.count                                  # rounds down; true stride >= base
            lo = max(2, base); hi = base + 8
            region = self.data[c.foff:c.foff + c.size]
            stride, score = _autocorr_stride(region, lo, hi)
            if stride > 0 and score >= 0.20 and c.size // stride >= 2:
                c.kind, c.stride, c.nrec, c.score = "records", stride, c.size // stride, score
                return
        if c.count == 1 and c.size > 0:
            c.kind, c.stride, c.nrec = "records", c.size, 1
            return
        c.kind = "blob"

    # ── record access ────────────────────────────────────────────────────────
    def rec_off(self, c: Chunk, row: int) -> int:
        return c.foff + row * c.stride

    def record(self, c: Chunk, row: int) -> bytes:
        o = self.rec_off(c, row)
        return bytes(self.data[o:o + c.stride])

    def get(self, c: Chunk, row: int, off: int, fmt: str):
        return struct.unpack_from(fmt, self.data, self.rec_off(c, row) + off)[0]

    def set(self, c: Chunk, row: int, off: int, fmt: str, val):
        o = self.rec_off(c, row) + off
        if o + struct.calcsize(fmt) > c.foff + c.size:
            raise ValueError("field runs past the record/chunk end")
        struct.pack_into(fmt, self.data, o, val)

    def set_bytes(self, c: Chunk, row: int, off: int, b: bytes):
        o = self.rec_off(c, row) + off
        if o + len(b) > c.foff + c.size:
            raise ValueError("bytes run past the record/chunk end")
        self.data[o:o + len(b)] = b

    # ── save (size-invariant, one-time .bak) ─────────────────────────────────
    def dirty(self):
        return self.data != self.path.read_bytes()

    def save(self, out_path=None, backup=True):
        if len(self.data) != self.orig_size:
            raise RuntimeError(f"refusing to write: size changed {self.orig_size}->{len(self.data)}")
        out = Path(out_path) if out_path else self.path
        if backup and out.exists():
            bak = out.with_suffix(out.suffix + ".bak")
            if not bak.exists():
                bak.write_bytes(out.read_bytes())
        out.write_bytes(self.data)


# ── field decode helpers for the GUI (offset -> value at several widths) ─────
FMT = {"u8": ">B", "i8": ">b", "u16": ">H", "i16": ">h", "u32": ">I", "i32": ">i",
       "f32": ">f"}


if __name__ == "__main__":
    import sys
    r = RosFile(sys.argv[1] if len(sys.argv) > 1 else
                r"C:\Users\cloug\Documents\xenia_master\Xenia Stable\content\54540853\00000001\Roster.ROS\Roster.ROS")
    recs = [c for c in r.chunks if c.kind == "records"]
    print(f"{len(r.chunks)} chunks, {len(recs)} record-tables")
    for c in sorted(recs, key=lambda c: -c.nrec)[:15]:
        print(f"  {c.name}  {c.nrec:6d} rows × {c.stride:4d}B  (dir count {c.count}, autocorr {c.score:.2f})")
