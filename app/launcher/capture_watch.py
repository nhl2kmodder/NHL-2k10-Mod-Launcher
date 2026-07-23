"""Efficient multi-IFF live accumulator. ONE memory sweep per cycle checks strong signatures for ALL
target loader-repacked packs at once (instead of a full sweep per iff), then runs the real content-
matching capture only for the ones present. Accumulates into the live catalog. Survives Xenia restart.
Just play through the game normally; every texture pack is captured as it loads."""
import sys, time, struct
from pathlib import Path
APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import xenia_mem as xm
import archive_textures as at
import live_capture as lc
import json

TARGETS = sys.argv[1:] or ["gamedata.iff","franchise.iff","online.iff","jukebox.iff",
                            "playercreate.iff","frontend_sync.iff","crowdanim.iff"]
DEADLINE = time.time() + 3600          # 1 hour
IDLE_STOP = 1800                       # 30 min quiet

def strong_sig(dram):
    """A distinctive 24-byte window (16-aligned) with many distinct nonzero bytes."""
    best=None
    for o in range(0, min(len(dram), 0x4000), 16):
        w = dram[o:o+24]
        if len(w) < 24: break
        nz = len(set(w)) - (1 if 0 in w else 0)
        if nz >= 14 and w.count(0) <= 6:
            return o, w
        if best is None or (len(set(w)) > len(set(best[1]))):
            best = (o, w)
    return best

# build sigs + seed seen
sigs = {}; seen = {}
for t in TARGETS:
    try:
        loc, data, size = at._read_asset(t, at.CLEAN_DIR)
        if loc is None: continue
        blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
        if len(blobs) < 2: continue
        dram = min(blobs, key=len)
        o, w = strong_sig(dram)
        sigs[t] = w; seen[t] = set()
    except Exception as e:
        print(f"  {t}: sig err {e}")
if lc.CATALOG.exists():
    for e in json.loads(lc.CATALOG.read_text()):
        if e.get("iff") in seen: seen[e["iff"]].add(e["file_offset"])
print("watching: " + ", ".join(f"{t}({len(seen[t])})" for t in sigs), flush=True)

def present(h, phys):
    """One sweep; return set of target iffs whose strong sig is in memory."""
    found=set(); pend=dict(sigs)
    for base, sz in xm.enum_committed_regions(h, phys, xm.PHYS_SIZE):
        o=0
        while o < sz and pend:
            n=min(0x100000, sz-o)
            chunk=xm.read_bytes(h, base+o, n)
            if chunk:
                for t in list(pend):
                    if chunk.find(pend[t]) >= 0:
                        found.add(t); del pend[t]
            o += n-64 if n>64 else n
        if not pend: break
    return found

cur=None; h=None; phys=None; n=0; last_new=time.time()
while time.time() < DEADLINE and time.time()-last_new < IDLE_STOP:
    n += 1
    pid = xm.find_pid()
    if not pid:
        cur=None; last_new=time.time()
        if n%10==0: print(f"  poll {n}: Xenia down (waiting)", flush=True);
        time.sleep(0.6); continue
    if pid != cur:
        try:
            if h: xm.close_handle(h)
        except Exception: pass
        h=xm.open_process(pid); phys=xm.find_phys_base(h) or xm.PHYS_BASE; cur=pid; last_new=time.time()
        print(f"  attached pid={pid}", flush=True)
    try:
        here = present(h, phys)
    except Exception:
        cur=None; time.sleep(0.5); continue
    for t in here:
        try:
            e = lc.capture(t, h, phys, save_png=True)
        except Exception:
            e=[]
        if e:
            new=[x for x in e if x["file_offset"] not in seen[t]]
            if new:
                lc._merge_catalog(e)
                for x in sorted(new, key=lambda z: z['file_offset']):
                    seen[t].add(x["file_offset"])
                print(f"  + {t} +{len(new)} new -> {t}={len(seen[t])}  (e.g. 0x{new[0]['file_offset']:X} {new[0]['w']}x{new[0]['h']} {new[0]['fmt']})", flush=True)
                last_new=time.time()
    if here:
        print(f"  poll {n}: resident={sorted(here)}", flush=True)
    elif n%10==0:
        print(f"  poll {n}: none resident", flush=True)
    time.sleep(0.5)
print("\nDONE totals: " + ", ".join(f"{t}={len(seen[t])}" for t in sigs), flush=True)
try:
    if h: xm.close_handle(h)
except Exception: pass
