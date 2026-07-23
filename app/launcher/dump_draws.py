"""Parse a single-frame .xtr: list every DRAW with num_indices, prim, and the
texture/vertex fetch-constant base addresses bound at that draw."""
import struct, sys, os
sys.path.insert(0, r"C:\Users\cloug\Documents\NHL 2k10 Extracted\NHL2K10 Mod Launcher\launcher")
import nhl2k10_trace_dump as td

TRACE = r"C:\Users\cloug\Documents\xenia_master\Xenia Stable\scratch\gpu\54540853_6524.xtr"
U32, BE = td.U32, td.BE

PRIM = {1:"POINT",2:"LINE",3:"LSTRIP",4:"TRI",5:"TFAN",6:"TSTRIP",7:"?7",8:"RECT",
        0x11:"2POINT"}

def bound_fetches(reg):
    """Return (textures, vertices) lists of (slot, base, fmt/size) currently set."""
    texs, verts = [], []
    for s in range(32):
        b = 0x4800 + s*6
        f0 = reg.get(b); f1 = reg.get(b+1); f2 = reg.get(b+2)
        if f0 is None: continue
        t = f0 & 3
        if t == 2 and f1:                      # texture
            base = (f1>>12)<<12
            if base: texs.append((s, base, f1 & 0x3F))
        elif t == 3 and f0 > 3:                # vertex
            base = f0 & 0xFFFFFFFC
            size = (f1 or 0)                    # dword1 = size/stride-ish
            if base: verts.append((s, base, size))
    return texs, verts

def walk(path):
    f = open(path, "rb"); fsize = os.path.getsize(path)
    reg = {}; draws = []; pos = 48; ndraw = 0
    def apply_reg_cmd(first, count, enc, elen, dpos):
        f.seek(dpos); payload = f.read(elen)
        if enc == 1:
            try: payload = td.snappy_decompress(payload)
            except Exception: payload = b""
        for i in range(count):
            if i*4+4 <= len(payload): reg[first+i] = U32(payload, i*4)
    def handle_packet(datapos, count):
        nonlocal ndraw
        f.seek(datapos); hb = f.read(4)
        if len(hb) < 4: return
        h = BE(hb); ptype = h >> 30
        if ptype == 0:
            base = h & 0x7FFF; n = ((h>>16)&0x3FFF)+1; one = (h>>15)&1
            body = f.read(n*4)
            if len(body) >= n*4:
                vals = struct.unpack(">%dI"%n, body)
                for m,v in enumerate(vals): reg[(base if one else base+m)] = v
        elif ptype == 3:
            op = (h>>8)&0x7F
            body = f.read((count-1)*4)
            if op == 0x2d and len(body) >= 4:                 # SET_CONSTANT fetch
                vals = struct.unpack(">%dI"%(len(body)//4), body); ot = vals[0]
                if (ot>>16)&0xFF == 1:
                    idx = ot & 0x7FF
                    for i,v in enumerate(vals[1:]): reg[0x4800+idx+i] = v
            elif op == 0x55 and len(body) >= 4:               # SET_CONSTANT2
                vals = struct.unpack(">%dI"%(len(body)//4), body); idx = vals[0]&0xFFFF
                for i,v in enumerate(vals[1:]): reg[idx+i] = v
            elif op in (0x22, 0x36):                          # DRAW_INDX / DRAW_INDX_2
                vals = struct.unpack(">%dI"%(len(body)//4), body) if len(body) >= 4 else ()
                # exact layout (xenia command_processor.cc): 0x22 body=[viz][initiator][idxbase][idxsize];
                # 0x36 body=[initiator][inline indices...]
                init = None; idxbase = 0
                if op == 0x22 and len(vals) >= 2:
                    init = vals[1]
                    if len(vals) >= 3: idxbase = vals[2]      # kDMA index buffer address
                elif op == 0x36 and len(vals) >= 1:
                    init = vals[0]
                if init is not None:
                    prim = init & 0x3F; nidx = (init >> 16) & 0xFFFF
                    src = (init >> 6) & 3                      # 0=kDMA(mem indices) 2=kAutoIndex(inline)
                    texs, verts = bound_fetches(reg)
                    draws.append((ndraw, prim, nidx, src, idxbase, texs, verts)); ndraw += 1
    while pos < fsize-4:
        r = td.try_cmd(f, pos, fsize)
        if r is None:
            npos = None
            for q in range(pos+4, min(pos+0x40000, fsize-24), 4):
                r2 = td.try_cmd(f, q, fsize)
                if r2 and td.try_cmd(f, r2[1], fsize): npos = q; r = r2; break
            if npos is None: break
        if r[0] == "reg":
            apply_reg_cmd(r[2], r[3], r[4], r[5], r[6])
        elif r[0] == "packet":
            handle_packet(r[2], r[3])
        pos = r[1]
    return draws

draws = walk(TRACE)
print(f"{len(draws)} draws\n")
lines = []
for nd, prim, nidx, src, idxbase, texs, verts in draws:
    tstr = " ".join("0x%08X"%b for _,b,_ in texs) or "-"
    lines.append(f"#{nd:4} {PRIM.get(prim,'?'):7} nidx={nidx:5} src={src} idxbase=0x{idxbase:08X}  TEX[{tstr}]")
outp = r"C:\Users\cloug\AppData\Local\Temp\claude\C--Users-cloug-Documents-NHL-2k10-Extracted\a5410155-c843-45c8-ad8e-7fff7a51bf6c\scratchpad\draws.txt"
open(outp, "w").write("\n".join(lines))
print("wrote", outp)
# The scorebug elements are SMALL quads (nidx<=8). List every small-quad draw with its
# ordinal + first texture — the scorebug is a contiguous cluster of these near frame end.
print("\n-- SMALL-QUAD draws (nidx<=8): scorebug candidates --")
for nd, prim, nidx, src, idxbase, texs, verts in draws:
    if 0 < nidx <= 8:
        t0 = ("0x%08X"%texs[0][1]) if texs else "-"
        print(f"#{nd:4} {PRIM.get(prim,'?'):7} nidx={nidx} tex0={t0}  ntex={len(texs)}  alltex={['0x%08X'%b for _,b,_ in texs][:4]}")
print(f"\ntotal small-quad draws: {sum(1 for d in draws if 0<d[2]<=8)}")
