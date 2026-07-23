#!/usr/bin/env python3
"""Corrected 0x0E4837 codec-7/8 decoder matching VCDecompress_Codec7/8 (8-byte
chunked back-ref copies with overshoot, verified against Ghidra VMX128 decompile)."""
import struct

def decompress_codec(src: bytes, decomp_size: int, off_mask: int, len_shift: int) -> bytes:
    out = bytearray(decomp_size + 16)   # +16 for 8-byte-chunk overshoot
    pos = 0; i = 0; n = len(src)
    def domatch(p_pos):
        val = (src[i_ref[0]] << 8) | src[i_ref[0]+1]   # BE16 token
        off = val & off_mask
        length = (val >> len_shift) + 3
        i_ref[0] += 2
        end = p_pos + length
        p = p_pos
        while p < end:
            s = p - off
            out[p:p+8] = out[s:s+8]
            p += 8
        return end
    i_ref = [0]
    while pos < decomp_size and i_ref[0] < n:
        flag = src[i_ref[0]]; i_ref[0] += 1
        if flag == 0:
            out[pos:pos+8] = src[i_ref[0]:i_ref[0]+8]; i_ref[0] += 8; pos += 8
        else:
            for bit in range(8):
                if pos >= decomp_size: break
                if (flag >> bit) & 1 == 0:
                    out[pos] = src[i_ref[0]]; i_ref[0] += 1; pos += 1
                else:
                    pos = domatch(pos)
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
