#!/usr/bin/env python3
"""Extract textures from a Xenia/RexGlue GPU trace (.xtr), STREAMING (handles
multi-GB stream traces). Pass 1: read register writes -> all texture fetch
constants (correct fmt/dims/base). Pass 2: read only the memory regions each
texture needs, decode with the verified GetTiledOffset2D pipeline -> PNG.
Usage: python nhl2k10_trace_dump.py <trace.xtr> [out_dir] [max_textures]"""
import struct, sys
from pathlib import Path
from PIL import Image

def snappy_decompress(data):
    pos=0; ln=0; sh=0
    while True:
        b=data[pos]; pos+=1; ln|=(b&0x7f)<<sh
        if not(b&0x80): break
        sh+=7
    out=bytearray()
    while pos<len(data):
        tag=data[pos]; pos+=1; t=tag&3
        if t==0:
            l=tag>>2
            if l>=60:
                nb=l-59; l=int.from_bytes(data[pos:pos+nb],'little'); pos+=nb
            l+=1; out+=data[pos:pos+l]; pos+=l
        else:
            if t==1: l=((tag>>2)&7)+4; off=((tag>>5)<<8)|data[pos]; pos+=1
            elif t==2: l=(tag>>2)+1; off=data[pos]|data[pos+1]<<8; pos+=2
            else: l=(tag>>2)+1; off=int.from_bytes(data[pos:pos+4],'little'); pos+=4
            s=len(out)-off
            if s<0 or pos>len(data): return bytes(out)   # malformed back-ref/overrun -> stop safely
            for i in range(l): out.append(out[s+i])
    return bytes(out)

U32=lambda b,o=0: struct.unpack_from("<I",b,o)[0]
FMT={6:("8888",4,0),18:("DXT1",8,1),19:("DXT2_3",16,1),20:("DXT4_5",16,1),58:("DXT3A",8,1),
     59:("DXT5A",8,1),61:("DXT3A1111",8,1),2:("8",1,0),10:("8_8",2,0),3:("1555",2,0),4:("565",2,0),15:("4444",2,0),
     49:("DXN",16,1)}

def try_cmd(f, pos, fsize):
    """Read one command header at pos. Returns (kind,next_pos,*fields) or None.
    Robust to this Xenia build's Gamma/EDRAM sizing: only Reg/Mem/Packet/buf/end/
    event are decoded; anything else (incl. gamma/edram/garbage) -> None (resync)."""
    f.seek(pos); tb=f.read(4)
    if len(tb)<4: return None
    t=U32(tb)
    if t==10:  # Registers (24B header)
        h=f.read(20)
        if len(h)<20: return None
        first,count=struct.unpack("<II",h[:8]); ecb=U32(h,8); enc,elen=struct.unpack("<II",h[12:20])
        if not(first<0x10000 and 1<=count<=0x8000 and ecb<=1 and enc<=1 and 0<elen<=count*4): return None
        if enc==0 and elen!=count*4: return None
        return ("reg", pos+24+elen, first,count,enc,elen,pos+24)
    if t in (6,7):  # Memory (20B header)
        h=f.read(16)
        if len(h)<16: return None
        base,enc,elen,dlen=struct.unpack("<IIII",h)
        if not(enc<=1 and 0<dlen<=0x4000000 and base<0x40000000 and 0<elen<=dlen): return None
        if enc==0 and elen!=dlen: return None
        return ("mem", pos+20+elen, base,enc,elen,dlen,pos+20)
    if t==4:  # PacketStart (12B + count dwords of PM4 packet: header + body)
        h=f.read(8)
        if len(h)<8: return None
        base,count=struct.unpack("<II",h)
        if count>0x100000 or count<1: return None
        return ("packet", pos+12+count*4, pos+12, count)
    if t in (0,2): return ("skip", pos+12)      # prim/ind buffer start
    if t in (1,3,5): return ("skip", pos+4)      # ends
    if t==9: return ("skip", pos+8)              # event
    if t==8:  # EdramSnapshot: type, enc, encoded_length + data (12B header)
        h=f.read(8)
        if len(h)<8: return None
        enc,elen=struct.unpack("<II",h)
        if enc>1 or elen>0x1400000: return None
        return ("skip", pos+12+elen)
    if t==11:  # GammaRamp: type, rw_component+pad, enc, encoded_length + data (16B header)
        h=f.read(12)
        if len(h)<12: return None
        rw,enc,elen=struct.unpack("<III",h)
        if enc>1 or elen>0x100000: return None
        return ("skip", pos+16+elen)
    return None

BE=lambda b,o=0: struct.unpack_from(">I",b,o)[0]   # PM4 packet dwords are guest big-endian
def parse_packet(f, datapos, count, reg, snap, loads):
    """Parse one PM4 packet (count dwords incl header) for fetch-constant writes."""
    f.seek(datapos); hb=f.read(4)
    if len(hb)<4: return
    h=BE(hb); ptype=h>>30
    if ptype==0:  # TYPE0 direct register write
        base=h&0x7FFF; n=((h>>16)&0x3FFF)+1; one=(h>>15)&1
        if base<0x48C0 and base+(1 if one else n)>0x4800:
            body=f.read(n*4); vals=struct.unpack(">%dI"%(len(body)//4),body)
            for m,v in enumerate(vals): reg[(base if one else base+m)]=v
            snap()
    elif ptype==3:  # TYPE3 command
        op=(h>>8)&0x7F
        if op==0x2d:  # SET_CONSTANT (index = offset&0x7FF, type 1 = FETCH -> 0x4800+index)
            body=f.read((count-1)*4)
            if len(body)>=4:
                vals=struct.unpack(">%dI"%(len(body)//4),body); ot=vals[0]
                if (ot>>16)&0xFF==1:
                    idx=ot&0x7FF
                    for i,v in enumerate(vals[1:]): reg[0x4800+idx+i]=v
                    snap()
        elif op==0x55:  # SET_CONSTANT2 (direct register index)
            body=f.read((count-1)*4)
            if len(body)>=4:
                vals=struct.unpack(">%dI"%(len(body)//4),body); idx=vals[0]&0xFFFF
                for i,v in enumerate(vals[1:]): reg[idx+i]=v
                if idx<0x48C0 and idx+len(vals)>0x4800: snap()
        elif op==0x2f:  # LOAD_ALU_CONSTANT (fetch from memory) -> resolve in pass2
            body=f.read(min(count-1,3)*4)
            if len(body)>=12:
                addr,ot,size=struct.unpack(">3I",body[:12])
                if (ot>>16)&0xFF==1: loads.append((addr&0x3FFFFFFF,0x4800+(ot&0x7FF),size&0xFFF))

def pass1(path):
    """Stream the trace with resync; collect fetch constants + memory-command index."""
    import os
    f=open(path,"rb"); fsize=os.path.getsize(path)
    reg={}; fetches={}; memidx=[]; loads=[]; resyncs=0; npk=0
    def snap():
        for s in range(32):
            b=0x4800+s*6
            if b+2 not in reg: continue
            f0,f1,f2,f3=reg.get(b,0),reg.get(b+1,0),reg.get(b+2,0),reg.get(b+3,0)
            if (f0&3)==2 and (f0|f1|f2): fetches.setdefault((f0,f1,f2,f3),s)
    pos=48
    while pos < fsize-4:
        r=try_cmd(f,pos,fsize)
        if r is None:
            # resync: scan forward for next valid command confirmed by a valid successor
            npos=None
            for q in range(pos+4, min(pos+0x40000, fsize-24), 4):
                r2=try_cmd(f,q,fsize)
                if r2 and try_cmd(f,r2[1],fsize): npos=q; r=r2; break
            if npos is None: break
            pos=npos; resyncs+=1
        if r[0]=="reg":
            first,count,enc,elen,dpos=r[2],r[3],r[4],r[5],r[6]
            f.seek(dpos); payload=f.read(elen)
            if enc==1:
                try: payload=snappy_decompress(payload)
                except Exception: payload=b""
            for i in range(count):
                if i*4+4<=len(payload): reg[first+i]=U32(payload,i*4)
            if first<0x48C0 and first+count>0x4800: snap()
        elif r[0]=="mem":
            base,enc,elen,dlen,dpos=r[2],r[3],r[4],r[5],r[6]
            memidx.append((dpos,base,enc,elen,dlen))
        elif r[0]=="packet":
            parse_packet(f, r[2], r[3], reg, snap, loads); npk+=1
        pos=r[1]
    snap()
    # resolve LOAD_ALU_CONSTANT fetches (constants came from guest memory, captured in trace)
    for addr,regbase,size in loads:
        d=read_region(f,memidx,addr,size*4)
        if len(d)<size*4: d=read_region(f,memidx,addr&0x1FFFFFFF,size*4)
        if len(d)<size*4: continue
        for i,v in enumerate(struct.unpack(">%dI"%size,d[:size*4])): reg[regbase+i]=v
        snap()
    print(f"  ({resyncs} resyncs, {npk} packets, {len(loads)} loads)")
    return f, fetches, memidx

def read_region(f, memidx, start, need):
    """Assemble [start,start+need) from memory commands (latest write wins)."""
    # fast path: a single memcmd fully contains it (latest such)
    best=None
    for pos,base,enc,elen,dlen in memidx:
        if base<=start and start+need<=base+dlen: best=(pos,base,enc,elen,dlen)
    if best:
        pos,base,enc,elen,dlen=best
        f.seek(pos); raw=f.read(elen)
        if enc==1: raw=snappy_decompress(raw)
        return raw[start-base:start-base+need]
    # fallback: fill from overlapping commands, latest first
    buf=bytearray(need); have=bytearray(need)
    for pos,base,enc,elen,dlen in reversed(memidx):
        if base+dlen<=start or base>=start+need: continue
        f.seek(pos); raw=f.read(elen)
        if enc==1: raw=snappy_decompress(raw)
        for i in range(max(start,base), min(start+need, base+dlen)):
            j=i-start
            if not have[j]: buf[j]=raw[i-base]; have[j]=1
        if all(have): break
    return bytes(buf)

# ---- decode ---------------------------------------------------------------
def gto(x,y,pitch,bl):
    pitch=(pitch+31)&~31
    macro=((x>>5)+(y>>5)*(pitch>>5))<<(bl+7)
    micro=((x&7)+((y&0xE)<<2))<<bl
    o=macro+((micro&~0xF)<<1)+(micro&0xF)+((y&1)<<4)
    return ((o&~0x1FF)<<3)+((y&16)<<7)+((o&0x1C0)<<2)+(((((y&8)>>2)+(x>>3))&3)<<6)+(o&0x3F)
def _565(w): return ((w>>11&31)*255//31,(w>>5&63)*255//63,(w&31)*255//31)
def _pal(c0,c1,p):
    a,b=_565(c0),_565(c1)
    if p and c0<=c1: return [a+(255,),b+(255,),tuple((a[i]+b[i])//2 for i in range(3))+(255,),(0,0,0,0)]
    return [a+(255,),b+(255,),tuple((2*a[i]+b[i])//3 for i in range(3))+(255,),tuple((a[i]+2*b[i])//3 for i in range(3))+(255,)]
def _at(a0,a1): return [a0,a1]+([((7-i)*a0+i*a1)//7 for i in range(1,7)] if a0>a1 else [((5-i)*a0+i*a1)//5 for i in range(1,5)]+[0,255])
def decode(v,W,H,fmt,bpu,block,tiled,pitch_field):
    bl=bpu.bit_length()-1; px=bytearray(W*H*4)
    pu=(pitch_field*8 if block else pitch_field*32) if pitch_field else 0
    pu=max(pu, W//4 if block else W)
    def put(x,y,c):
        if 0<=x<W and 0<=y<H: p=(y*W+x)*4; px[p:p+4]=bytes(c)
    if block:
        for by in range(H//4):
            for bx in range(W//4):
                o=gto(bx,by,pu,bl) if tiled else (by*pu+bx)*bpu; b=v[o:o+bpu]
                if len(b)<bpu: continue
                if fmt=="DXT1": c0=b[1]|b[0]<<8;c1=b[3]|b[2]<<8;bits=b[4]|b[5]<<8|b[6]<<16|b[7]<<24;P=_pal(c0,c1,True);al=None
                elif fmt in("DXT4_5","DXT2_3"):
                    c0=b[9]|b[8]<<8;c1=b[11]|b[10]<<8;bits=b[12]|b[13]<<8|b[14]<<16|b[15]<<24;P=_pal(c0,c1,False)
                    if fmt=="DXT4_5": t=_at(b[0],b[1]);ai=int.from_bytes(b[2:8],"little");al=[t[(ai>>(3*i))&7] for i in range(16)]
                    else: ab=int.from_bytes(b[0:8],"little");al=[((ab>>(4*i))&0xF)*17 for i in range(16)]
                elif fmt=="DXN":          # BC5: two BC4 channels (X in [0:8], Y in [8:16]); reconstruct Z
                    tx=_at(b[0],b[1]);aix=int.from_bytes(b[2:8],"little");rx=[tx[(aix>>(3*i))&7] for i in range(16)]
                    ty=_at(b[8],b[9]);aiy=int.from_bytes(b[10:16],"little");ry=[ty[(aiy>>(3*i))&7] for i in range(16)]
                    for i in range(16):
                        nx=rx[i]/127.5-1;ny=ry[i]/127.5-1;nz=(max(0.0,1-nx*nx-ny*ny))**0.5
                        put(bx*4+i%4,by*4+i//4,(rx[i],ry[i],int((nz+1)*127.5),255))
                    continue
                else:
                    if fmt=="DXT5A": t=_at(b[0],b[1]);ai=int.from_bytes(b[2:8],"little");g=[t[(ai>>(3*i))&7] for i in range(16)]
                    else: ab=int.from_bytes(b[0:8],"little");g=[((ab>>(4*i))&0xF)*17 for i in range(16)]
                    for i in range(16): put(bx*4+i%4,by*4+i//4,(g[i],g[i],g[i],255))
                    continue
                for i in range(16):
                    c=P[(bits>>(2*i))&3]; put(bx*4+i%4,by*4+i//4,(c[0],c[1],c[2],al[i] if al else c[3]))
    else:
        for y in range(H):
            for x in range(W):
                o=gto(x,y,pu,bl) if tiled else (y*pu+x)*bpu; b=v[o:o+bpu]
                if len(b)<bpu: continue
                if fmt=="565": put(x,y,_565(b[1]|b[0]<<8)+(255,))
                elif fmt=="1555": w=b[1]|b[0]<<8; put(x,y,((w>>10&31)*255//31,(w>>5&31)*255//31,(w&31)*255//31,255 if w&0x8000 else 0))
                elif fmt=="4444": w=b[1]|b[0]<<8; put(x,y,((w>>8&15)*17,(w>>4&15)*17,(w&15)*17,(w>>12&15)*17))
                elif fmt=="8888": put(x,y,(b[1],b[2],b[3],b[0]))
                elif fmt=="8": put(x,y,(b[0],b[0],b[0],255))
                elif fmt=="8_8": put(x,y,(b[0],b[1],0,255))
    return Image.frombytes("RGBA",(W,H),bytes(px))

def main():
    if len(sys.argv)<2: print("usage: nhl2k10_trace_dump.py <trace.xtr> [out] [max]"); return
    trace=sys.argv[1]; out=Path(sys.argv[2] if len(sys.argv)>2 else "trace_textures"); out.mkdir(exist_ok=True)
    maxn=int(sys.argv[3]) if len(sys.argv)>3 else 4000
    print("pass 1: scanning trace for fetch constants..."); sys.stdout.flush()
    f,fetches,memidx=pass1(trace)
    print(f"  {len(fetches)} unique texture fetches, {len(memidx)} memory commands")
    n=0; seen=set()
    for (f0,f1,f2,f3),slot in fetches.items():
        code=f1&0x3F
        if code not in FMT: continue
        w=(f2&0x1FFF)+1; h=((f2>>13)&0x1FFF)+1
        if not(8<=w<=4096 and 8<=h<=4096): continue
        base=(f1>>12)<<12; tiled=(f0>>31)&1; pitch=(f0>>22)&0x1FF
        name,bpu,blk=FMT[code]; key=(base,w,h,code)
        if key in seen: continue
        seen.add(key)
        need=((w//4)*(h//4)*bpu if blk else w*h*bpu)
        v=read_region(f,memidx,base,need)
        if len(v)<need or v.count(0)==len(v): continue   # missing or all-zero
        try: img=decode(v,w,h,name,bpu,blk,tiled,pitch)
        except Exception: continue
        img.save(out/f"{name}_{w}x{h}_0x{base:08X}.png"); n+=1
        if n%25==0: print(f"  ...{n} exported"); sys.stdout.flush()
        if n>=maxn: break
    print(f"\n=== {n} textures -> {out}/ ===")

if __name__=="__main__": main()
