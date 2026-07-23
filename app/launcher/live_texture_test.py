"""LIVE TEXTURE PATCH — experiment harness (game must be RUNNING in Xenia).

Investigates whether we can refresh a texture in the running game from the launcher.
  Stage A  : write new bytes straight into the texture's guest VRAM (in-place).
  Stage B  : ALSO force Xenia's OWN texture-cache invalidation by triggering its write-watch
             from INSIDE the Xenia process (a tiny injected thread touches the page → Xenia's
             VEH marks it dirty → re-uploads from guest memory = our new bytes).

Xenia (this build) tracks writes with access-violation page guards, NOT GetWriteWatch, so an
external WriteProcessMemory is invisible to its cache — hence Stage B. Reads the original bytes
first so --restore puts it back.

USAGE (from the launcher/ dir, game open on a screen showing the asset):
  python live_texture_test.py logo_van.iff                 # Stage A only  (paint tex 0 red)
  python live_texture_test.py logo_van.iff --invalidate     # Stage A + Stage B
  python live_texture_test.py logo_van.iff --tex 2 --color 0,255,0
  python live_texture_test.py logo_van.iff --restore        # undo (restore original bytes)
"""
import sys, ctypes, struct, argparse, json, tempfile, os
from ctypes import wintypes as wt
sys.path.insert(0, '.')
import xenia_mem as xm
import live_capture as lc
import archive_textures as at
from PIL import Image

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.VirtualAllocEx.restype = wt.LPVOID
_k32.VirtualAllocEx.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.DWORD, wt.DWORD]
_k32.VirtualFreeEx.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.DWORD]
_k32.CreateRemoteThread.restype = wt.HANDLE
_k32.CreateRemoteThread.argtypes = [wt.HANDLE, wt.LPVOID, ctypes.c_size_t, wt.LPVOID,
                                    wt.LPVOID, wt.DWORD, ctypes.POINTER(wt.DWORD)]
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]

BACKUP = os.path.join(tempfile.gettempdir(), "nhl2k10_livetex_backup.json")

# x64 stub: RCX -> {start:u64, end:u64}; touch first byte of each page (read+write same byte, no
# data change) to trip Xenia's read-only watch page → invalidate. See module docstring.
STUB = bytes.fromhex("488B01" "488B5108" "448A00" "448800" "480500100000" "4839D0" "72EF" "31C0" "C3")


def attach():
    pid = xm.find_pid()
    if not pid:
        print("Xenia not found — start the game first."); sys.exit(1)
    h = xm.open_process(pid)
    phys = xm.find_phys_base(h) or xm.PHYS_BASE
    print(f"attached pid={pid}  phys_base=0x{phys:X}")
    return h, phys


def find_loaded(iff, h, phys):
    """Every currently-loaded texture record of `iff` with its live host address."""
    loc, data, size = at._read_asset(iff, at.CLEAN_DIR)
    if loc is None:
        print(f"{iff}: not in archive"); return []
    blobs = [b["dec"] for b in at._walk_blobs(data, size) if b["dec"]]
    if len(blobs) < 2:
        print(f"{iff}: not a decodable multi-blob texture asset"); return []
    fdram = min(blobs, key=len)
    rec_base = lc._scan(h, phys, fdram[:16])
    if rec_base is None:
        so, sw = lc._strong_sig(fdram)
        if sw:
            m = lc._scan(h, phys, sw)
            if m is not None:
                rec_base = m - so
    if rec_base is None:
        print(f"{iff}: records blob not found in guest RAM — is the asset loaded on-screen?"); return []
    mem = lc._read_region(h, rec_base, min(len(fdram), 0x1800000))
    out = []; b = 0
    while b + 0xE0 <= len(mem):
        r = lc._parse_rec(mem, b)
        if r is not None and 0xA0000000 <= r["ptr"] < 0xC0000000:
            r["host"] = phys + (r["ptr"] & 0x1FFFFFFF)
            out.append(r); b += 0xE0
        else:
            b += 4
    return out


def stage_a(h, rec, rgb):
    """In-place write: paint the texture a solid colour (mip0) in its live VRAM."""
    host, mip0 = rec["host"], rec["mip0"]
    orig = xm.read_bytes(h, host, mip0)
    json.dump({"host": host, "size": mip0, "bytes": orig.hex()}, open(BACKUP, "w"))
    img = Image.new("RGBA", (rec["w"], rec["h"]), tuple(rgb) + (255,))
    payload = at._encode_tiled(img, rec["fmt"], rec["tiled"])[:mip0]
    payload = payload + orig[len(payload):]                # pad to exact mip0 if encoder shorter
    ok = xm.write_bytes(h, host, payload)
    print(f"  Stage A: wrote {len(payload)} bytes @ host 0x{host:X} (guest 0x{rec['ptr']:X})  ok={ok}")
    print(f"           backup saved -> {BACKUP}")
    return host, mip0


def stage_b(h, host, size):
    """Force Xenia to re-upload: inject a thread that touches each page → trips its write-watch."""
    PAGE = 0x1000
    start = host & ~(PAGE - 1); end = (host + size + PAGE - 1) & ~(PAGE - 1)
    remote = _k32.VirtualAllocEx(h, None, 0x1000, 0x3000, 0x40)  # MEM_COMMIT|RESERVE, RWX
    if not remote:
        print("  Stage B: VirtualAllocEx failed"); return
    param = remote + 0x80
    xm.write_bytes(h, remote, STUB)
    xm.write_bytes(h, param, struct.pack("<QQ", start, end))
    tid = wt.DWORD(0)
    th = _k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(remote),
                                 ctypes.c_void_p(param), 0, ctypes.byref(tid))
    if not th:
        print(f"  Stage B: CreateRemoteThread failed err={ctypes.get_last_error()}")
    else:
        _k32.WaitForSingleObject(th, 3000)
        print(f"  Stage B: fault-touched pages 0x{start:X}..0x{end:X} (tid={tid.value}) — "
              f"Xenia should re-upload the texture now")
    _k32.VirtualFreeEx(h, ctypes.c_void_p(remote), 0, 0x8000)   # MEM_RELEASE


def restore(h):
    if not os.path.exists(BACKUP):
        print("no backup to restore"); return
    d = json.load(open(BACKUP))
    ok = xm.write_bytes(h, d["host"], bytes.fromhex(d["bytes"]))
    print(f"restored {d['size']} bytes @ 0x{d['host']:X}  ok={ok}")
    if d.get("host"):
        stage_b(h, d["host"], d["size"])                   # invalidate so the restore shows too


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("iff", nargs="?")
    ap.add_argument("--tex", type=int, default=0)
    ap.add_argument("--color", default="255,0,0")
    ap.add_argument("--invalidate", action="store_true")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()
    h, phys = attach()
    if a.restore:
        restore(h); return
    if not a.iff:
        print("give an iff name (that's visible on screen right now)"); return
    recs = find_loaded(a.iff, h, phys)
    print(f"{a.iff}: {len(recs)} loaded texture(s)")
    for i, r in enumerate(recs):
        print(f"  [{i}] {r['w']}x{r['h']} {r['fmt']}  guest=0x{r['ptr']:X} host=0x{r['host']:X} mip0={r['mip0']}")
    if not recs:
        return
    rec = recs[a.tex]
    rgb = [int(x) for x in a.color.split(",")]
    print(f"\npainting tex[{a.tex}] {tuple(rgb)} …")
    host, size = stage_a(h, rec, rgb)
    if a.invalidate:
        stage_b(h, host, size)
    print("\n>>> LOOK AT THE GAME. Did the texture change?")
    print(">>> If NOT and you didn't use --invalidate, re-run with --invalidate.")
    print(">>> Run with --restore to undo.")


if __name__ == "__main__":
    main()
