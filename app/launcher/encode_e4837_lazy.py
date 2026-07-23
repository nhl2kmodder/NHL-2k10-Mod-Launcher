#!/usr/bin/env python3
"""Lazy-matching 0x0E4837 codec-8 encoder (9-bit offset, offset>=8, decoder-safe).

The decoder copies matches in 8-byte chunks, but since this encoder only ever emits
offsets >= 8, for any chunk the source [q-off : q-off+8] lies entirely before the chunk
being written (q-off+8 <= q) — so the chunked copy is byte-for-byte identical to a plain
byte-wise copy. That lets match validation be a direct self-referential compare against
`data` (data[pos+i] == data[pos+i-off]) instead of simulating the decoder into a scratch
buffer — much faster. Hash-chain match finder (int trigram keys), chunked compare with a
quick-reject, capped chain walk, and lazy lookahead only for short matches.
"""
import struct, sys
from array import array
sys.path.insert(0, '.')
import decode_e4837_fixed as DF

MINM = 3; LAZY_MAX = 32


def _auto_tries(wp: int) -> int:
    """Bigger LZ windows have many more chain candidates per position and need a deeper walk
    to actually reach the long-range matches the cooker used (else the re-encode inflates).
    Small windows saturate quickly, so a shallow walk is enough (and fast)."""
    return 4096 if wp >= 11 else 256


def encode_stream(data: bytes, wp: int = 9, max_tries: int = 128) -> bytes:
    """Flag-byte LZ77 at window size `wp`: back-offset = low `wp` bits of the BE16 token,
    length = high (16-wp) bits + 3. Must match the blob's header wp (the decoder/game reads
    `off & ((1<<wp)-1)`, `len = (tok>>wp)+3`). Offsets are kept >= 8 (chunked-copy safe)."""
    n = len(data)
    if n == 0:
        return b""
    WIN = (1 << wp) - 1            # max back-offset for this window
    MAXM = (1 << (16 - wp)) + 2    # max match length the (16-wp)-bit length field can encode
    head = {}
    prev = array('i', [-1]) * n

    def find_best(pos):
        if pos + 3 > n:
            return (0, 0)
        lim = MAXM if n - pos > MAXM else n - pos
        p = head.get((data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2], -1)
        bo = 0; bl = 0; tries = 0
        while p >= 0 and pos - p <= WIN and tries < max_tries:
            tries += 1
            if pos - p >= 8 and (bl == 0 or data[p + bl] == data[pos + bl]):
                i = 0
                while i + 8 <= lim and data[p + i:p + i + 8] == data[pos + i:pos + i + 8]:
                    i += 8
                while i < lim and data[p + i] == data[pos + i]:
                    i += 1
                if i > bl:
                    bl = i; bo = pos - p
                    if bl >= lim:
                        break
            p = prev[p]
        return (bo, bl)

    def insert(i):
        if i + 3 <= n:
            k = (data[i] << 16) | (data[i + 1] << 8) | data[i + 2]
            prev[i] = head.get(k, -1); head[k] = i

    tokens = []; ap = tokens.append; pos = 0
    while pos < n:
        off, ln = find_best(pos)
        if ln >= MINM:
            if ln < LAZY_MAX:                       # lazy lookahead only helps short matches
                _o2, l2 = find_best(pos + 1)
                if l2 > ln:
                    ap((0, data[pos])); insert(pos); pos += 1; continue
            ap((1, off, ln))
            end = pos + ln
            while pos < end:
                insert(pos); pos += 1
        else:
            ap((0, data[pos])); insert(pos); pos += 1

    res = bytearray()
    for g in range(0, len(tokens), 8):
        grp = tokens[g:g + 8]; flag = 0; body = bytearray()
        for bi, tk in enumerate(grp):
            if tk[0] == 1:
                flag |= (1 << bi); val = ((tk[2] - 3) << wp) | tk[1]
                body += bytes(((val >> 8) & 0xFF, val & 0xFF))
            else:
                body.append(tk[1])
        if flag == 0 and len(body) < 8:
            body += bytes(8 - len(body))
        res.append(flag); res += body
    return bytes(res)


def encode_payload(data: bytes, wparam: int = 9, codec: int = 8, max_tries: int = None) -> bytes:
    """Re-compress `data` at window `wparam`, preserving the original blob's `codec`. The
    header's wparam MUST equal the window used to encode (the game decodes by it). Pass the
    SOURCE blob's wp/codec when replacing in place, so the re-encode matches the cooker's
    window (a wrong/smaller window inflates the blob and breaks the in-place fit)."""
    if max_tries is None:
        max_tries = _auto_tries(wparam)
    s = encode_stream(data, wparam, max_tries)
    return struct.pack('>IIIII', 0x0E4837C3, len(data), 20 + len(s), codec, wparam) + s


if __name__ == '__main__':
    import time
    from pathlib import Path
    CLEAN = Path(r"C:\Users\cloug\Documents\NHL_2k10_CLEAN_Files"); OFF = 0x3FB76080
    with open(CLEAN / '0B', 'rb') as f:
        f.seek(OFF); raw = f.read(0x20000)
    tot = struct.unpack_from('>I', raw, 8)[0]; dsz = struct.unpack_from('>I', raw, 4)[0]
    orig = DF.decompress_codec(raw[20:tot], dsz, 0x1FF, 9)
    t = time.time(); enc = encode_payload(orig); dt = time.time() - t
    ok = DF.decompress_codec(enc[20:], dsz, 0x1FF, 9) == orig
    print(f"encode {len(orig)} -> {len(enc)} (orig blob {tot})  "
          f"fits={len(enc) <= tot}  roundtrip={ok}  {dt:.3f}s")
