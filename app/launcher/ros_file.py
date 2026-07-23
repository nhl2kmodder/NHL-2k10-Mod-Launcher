"""
ros_file.py — parser / in-place editor for an NHL 2K10 Roster.ROS save (big-endian, Xbox 360).

Container format (reverse-engineered):
  0x00  u32  total size (filesize - 4)
  0x04  u32  version (1)
  0x08  u32  chunk_count (N)
  0x0C  N x [ u32 type_hash | u32 count | u32 data_offset ]        # the chunk directory
  0xB28 ...  data section; each chunk = data_base + data_offset, tiling with no gaps.

A record table is a chunk of `count` fixed-stride records. Directory `size//count` rounds the
stride DOWN (the region is a hair short of count*stride), so the true stride is detected by
byte-autocorrelation and the usable record count is size//stride. Chunks with count<=1 or whose
records don't autocorrelate are treated as raw BLOBS (e.g. the string pool 0xEB69DFB9).

Editing is strictly IN PLACE (file size never changes) so every fixed offset the game holds stays
valid — the same invariant roster_editor.py relies on. A .bak is written before the first save.
CAVEAT: a few fields are scrambled/encoded per-save (e.g. the goalie-mask slot) and won't round-trip
predictably; most numeric fields do. Verify changes in-game.
"""
from __future__ import annotations
import struct
from pathlib import Path

DATA_BASE = 0xB28


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
        self.chunks: list[Chunk] = []
        self._parse()

    def _parse(self):
        d = self.data
        n = _be(d, 8)
        ents = [(_be(d, 0x0C + i * 12), _be(d, 0x0C + i * 12 + 4), _be(d, 0x0C + i * 12 + 8))
                for i in range(n)]
        order = sorted(range(n), key=lambda i: ents[i][2])          # by data offset -> boundaries
        for k, i in enumerate(order):
            h, cnt, off = ents[i]
            nxt = ents[order[k + 1]][2] if k + 1 < len(order) else (self.orig_size - 4 - (DATA_BASE - 4))
            size = nxt - off
            self.chunks.append(Chunk(i, h, cnt, DATA_BASE + off, size))
        # sort back to directory order for a stable display
        self.chunks.sort(key=lambda c: c.index)
        for c in self.chunks:
            self._classify(c)

    def _classify(self, c: Chunk):
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
