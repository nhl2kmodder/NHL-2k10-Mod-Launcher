#!/usr/bin/env python3
"""Corrected 0x0E4837 codec-7/8 decoder matching VCDecompress_Codec7/8 (8-byte
chunked back-ref copies with overshoot, verified against Ghidra VMX128 decompile)."""
import struct

def decompress_codec(src: bytes, decomp_size: int, off_mask: int, len_shift: int) -> bytes:
    # Hot path (every pack decompress goes through here): the back-ref copy is inlined —
    # the old domatch() closure alone cost ~60% of the runtime in call overhead — and a
    # match whose source window can't touch its own output (off >= chunk-rounded length)
    # collapses to ONE slice copy. That copy includes the 8-byte overshoot bytes, so the
    # output is bit-identical to the chunked loop (later off<8 matches READ overshoot).
    out = bytearray(decomp_size + 16)   # +16 for 8-byte-chunk overshoot
    osize = len(out)
    pos = 0; i = 0; n = len(src)
    while pos < decomp_size and i < n:
        flag = src[i]; i += 1
        if flag == 0:
            out[pos:pos+8] = src[i:i+8]; i += 8; pos += 8
            continue
        for bit in range(8):
            if pos >= decomp_size:
                break
            if not (flag >> bit) & 1:
                out[pos] = src[i]; i += 1; pos += 1
            else:
                val = (src[i] << 8) | src[i+1]; i += 2       # BE16 token
                off = val & off_mask
                length = (val >> len_shift) + 3
                lc = (length + 7) & ~7                        # chunk-rounded (incl. overshoot)
                if off >= lc and pos + lc <= osize:
                    s = pos - off
                    out[pos:pos+lc] = out[s:s+lc]
                    pos += length
                else:                                         # overlapping: exact chunk replay
                    end = pos + length
                    p = pos
                    while p < end:
                        s = p - off
                        out[p:p+8] = out[s:s+8]
                        p += 8
                    pos = end
    return bytes(out[:decomp_size])

def decompress_payload(payload: bytes) -> bytes:
    magic = struct.unpack_from('>I', payload, 0)[0]
    assert magic == 0x0E4837C3, f"bad magic {magic:08X}"
    decomp = struct.unpack_from('>I', payload, 4)[0]
    codec  = struct.unpack_from('>I', payload, 12)[0]
    src = payload[20:]
    if codec % 2 == 1:   # codec 7 (Ghidra-confirmed): offset=token&0x7F, length=(token>>7)+3
        return decompress_codec(src, decomp, 0x7F, 7)
    else:                # codec 8 (Ghidra-confirmed): offset=token&0xFF, length=(token>>8)+3
        return decompress_codec(src, decomp, 0xFF, 8)

if __name__ == '__main__':
    import sys, io, contextlib
    sys.path.insert(0,'.')
    import nhl2k10_trace_dump as T
    from pathlib import Path
    CLEAN=Path(r"C:\Users\cloug\Documents\NHL_2k10_CLEAN_Files")
    with open(CLEAN/"0B","rb") as f: f.seek(0x3FB76080); raw=f.read(0x40000)
    tot=struct.unpack_from('>I',raw,8)[0]
    dec=decompress_payload(raw[:tot])
    open('blob_dec_fixed.bin','wb').write(dec)
    print("decomp",len(dec))
    # coverage vs live logo blocks
    src=open('src_sabres.bin','rb').read()
    blocks=set(src[i:i+16] for i in range(0,len(src),16)) - {bytes(16)}
    found=sum(1 for b in blocks if b in dec)
    print(f"unique logo blocks in fixed dec: {found}/{len(blocks)} ({100*found/len(blocks):.0f}%)")
