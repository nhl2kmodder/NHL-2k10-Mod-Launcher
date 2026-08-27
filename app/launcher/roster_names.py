"""roster_names.py — write player names into a Roster.ROS.

Names are UTF-16BE, NUL-terminated, reached by a SIGNED self-relative pointer
(`target = field_offset + value - 1`) from the player record's +0x00 (last) and +0x04 (first).
The file size is fixed — `RosFile.save()` refuses any change — so a longer name cannot simply be
appended. It has to go somewhere that is already free.

**Free space is the 0xFF filler**, not the zero runs: ~94 KB spread over ~1,540 gaps inside the
string pool, left behind by shorter names overwriting longer ones. The big zero runs are live
zeroed structures, so this module never allocates from them.

⚠ A string's "slack to the next name" is NOT free space. `Stastny` at 0x231E58 is followed
immediately by `Jachym`, a live string no player record points at (staff/other pool), even though
the next PLAYER name is 68 bytes away. In-place growth is therefore only allowed over bytes that
are actually 0xFF.

Shared strings are real: 26 rows share one "Alex". Renaming a row whose string is shared always
allocates a fresh copy instead of editing in place, or it would rename 25 other players.
"""
from __future__ import annotations
import re, struct

try:
    from .player_assign import PlayerTable, STRIDE, OFF_LAST, OFF_FIRST
except ImportError:
    from player_assign import PlayerTable, STRIDE, OFF_LAST, OFF_FIRST

POOL_LO, POOL_HI = 0x5000, 0x24C000     # observed name-pool span, padded out a little
MIN_RUN = 8                             # smallest 0xFF run worth allocating from


class PoolFull(RuntimeError):
    pass


def encode(s: str) -> bytes:
    return s.encode("utf-16-be") + b"\x00\x00"


class NamePool:
    """A bump allocator over the 0xFF filler, plus the map of who points where."""

    def __init__(self, table: PlayerTable):
        self.t = table
        self.d = table.ros.data
        self.refs = {}                                  # string start -> [(row, off), ...]
        for e in self.t.rows():
            for off in (OFF_LAST, OFF_FIRST):
                a = self._target(e["row"], off)
                if a is not None:
                    self.refs.setdefault(a, []).append((e["row"], off))
        self.free = [(m.start(), m.end() - m.start())
                     for m in re.finditer(rb"\xff{%d,}" % MIN_RUN,
                                          bytes(self.d[POOL_LO:POOL_HI]))]
        self.free = [(POOL_LO + a, n) for a, n in self.free]

    # ── reading ──────────────────────────────────────────────────────────────
    def _field(self, row, off):
        return self.t.base + row * STRIDE + off

    def _target(self, row, off):
        fo = self._field(row, off)
        v = struct.unpack_from(">i", self.d, fo)[0]
        return None if v == 0 else fo + v - 1

    def _end(self, a) -> int:
        """First terminator at or after `a`. 0xFFFF ends a string exactly as 0x0000 does — it
        is this module's OWN filler marker (see `set`, which hands a shortened tail back as
        0xFF), so a reader that only looks for 0x0000 reads the reclaimed tail back as text."""
        e = a
        n = len(self.d)
        while e + 1 < n and self.d[e:e + 2] not in (b"\x00\x00", b"\xff\xff"):
            e += 2
        return e

    def read(self, row, off) -> str:
        a = self._target(row, off)
        if a is None:
            return ""
        return bytes(self.d[a:self._end(a)]).decode("utf-16-be", "replace")

    def _size(self, a) -> int:
        """Bytes the string at `a` occupies, terminator included."""
        return self._end(a) + 2 - a

    # ── writing ──────────────────────────────────────────────────────────────
    def alloc(self, n: int) -> int:
        for idx, (a, avail) in enumerate(self.free):
            if avail >= n:
                self.free[idx] = (a + n, avail - n)
                return a
        raise PoolFull(f"no 0xFF run big enough for {n} bytes")

    def set(self, row, off, text: str, log=None) -> str:
        """Point (row, off) at `text`. In place when that is provably safe, else reallocated."""
        blob = encode(text)
        a = self._target(row, off)
        how = "realloc"
        if a is not None and len(self.refs.get(a, [])) <= 1:
            have = self._size(a)
            grow = len(blob) - have
            # Growing is only allowed over bytes that really are filler.
            if grow <= 0 or all(self.d[a + have + i] == 0xFF for i in range(grow)):
                self.d[a:a + len(blob)] = blob
                if grow < 0:                            # hand the tail back as filler
                    self.d[a + len(blob):a + have] = b"\xff" * (-grow)
                how = "in place"
                if log:
                    log(f"row {row} +0x{off:02X} -> {text!r} ({how}, {len(blob)}B at 0x{a:06X})")
                return how
        b = self.alloc(len(blob))
        self.d[b:b + len(blob)] = blob
        struct.pack_into(">i", self.d, self._field(row, off), b - self._field(row, off) + 1)
        self.refs.setdefault(b, []).append((row, off))
        if a is not None and (row, off) in self.refs.get(a, []):
            self.refs[a].remove((row, off))
        if log:
            log(f"row {row} +0x{off:02X} -> {text!r} ({how}, {len(blob)}B at 0x{b:06X})")
        return how

    def rename(self, row, first=None, last=None, log=None):
        if last is not None:
            self.set(row, OFF_LAST, last, log)
        if first is not None:
            self.set(row, OFF_FIRST, first, log)

    def free_bytes(self) -> int:
        return sum(n for _, n in self.free)
