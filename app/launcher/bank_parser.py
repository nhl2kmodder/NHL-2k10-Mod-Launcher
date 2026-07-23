"""bank_parser.py — parse NHL 2k10 IFF *audio banks* (sound directories).

A "bank" is an 0x0E4837-compressed IFF (in 0A/0B) that the game loads by name and uses as
a directory: each sound record carries a sample-rate + a 0x800-aligned BYTE OFFSET that
points into the raw-XMA wave archives 1A/1B. (See Nhl2k10_Findings/09_Audio_Bank_System.txt.)

Two record shapes exist, so we extract both:
  * rate-anchored  (clean crowdloops/bootup style): sample_rate dword, wave offset 8 bytes later.
  * loose ref      (arena banks, richer records): any dword that equals a REAL catalogued
                    stream offset -> that's a wave pointer (deduped to distinct sounds).
"""
import struct
from pathlib import Path

try:
    from . import archive_textures as AT
except ImportError:
    import archive_textures as AT

try:
    import numpy as np
    _NP = True
except ImportError:
    _NP = False

ALIGN = 0x800
SAMPLE_RATES = (48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 6000)

STATIC_BANKS = ["bootup_audio.iff", "crowdloops.iff", "gamedata.iff",
                "face_speech.iff", "face_ambient.iff"]

CODE_NAME = {
    "ana": "Anaheim Ducks", "atl": "Atlanta Thrashers", "bos": "Boston Bruins",
    "buf": "Buffalo Sabres", "car": "Carolina Hurricanes", "cbj": "Columbus Blue Jackets",
    "cgy": "Calgary Flames", "chi": "Chicago Blackhawks", "col": "Colorado Avalanche",
    "dal": "Dallas Stars", "det": "Detroit Red Wings", "edm": "Edmonton Oilers",
    "fla": "Florida Panthers", "lak": "Los Angeles Kings", "min": "Minnesota Wild",
    "mtl": "Montreal Canadiens", "njd": "New Jersey Devils", "nsh": "Nashville Predators",
    "nyi": "NY Islanders", "nyr": "NY Rangers", "ott": "Ottawa Senators",
    "phi": "Philadelphia Flyers", "pho": "Phoenix Coyotes", "pit": "Pittsburgh Penguins",
    "sjs": "San Jose Sharks", "stl": "St. Louis Blues", "tbl": "Tampa Bay Lightning",
    "tor": "Toronto Maple Leafs", "van": "Vancouver Canucks", "wsh": "Washington Capitals",
}


def bank_menu():
    """List of (bank_name, label) for the UI selector — static banks + per-team arenas."""
    items = [(b, b) for b in STATIC_BANKS]
    for code, name in CODE_NAME.items():
        items.append((f"arena_{code}.iff", f"arena_{code}.iff  ({name})"))
    return items


def decompress_bank(name, clean_dir=None):
    """Resolve a bank IFF in the TOC and return (decompressed_bytes, meta) or (None, None)."""
    clean_dir = Path(clean_dir or AT.CLEAN_DIR)
    loc = AT.resolve(name, clean_dir)
    if not loc:
        return None, None
    arc, off, size, idx, f3 = loc
    with open(clean_dir / arc, "rb") as f:
        f.seek(off); raw = f.read(size)
    blobs = AT._walk_blobs(raw, size)
    dec = b"".join(b["dec"] for b in blobs if b["dec"])
    meta = {"name": name, "archive": arc, "offset": off, "size": size,
            "toc_index": idx, "decompressed": len(dec), "blobs": len(blobs)}
    return dec, meta


def parse_bank(name, offsets_by_arc, clean_dir=None):
    """Parse a bank into wave-pointer records.

    offsets_by_arc: {fid: set(offsets)} of every catalogued stream (for linking + ref scan).
    Returns (meta, records). record = {bank_off, wave_off, archive, sample_rate, via}.
    """
    dec, meta = decompress_bank(name, clean_dir)
    if dec is None:
        return None, []
    order = ("1B", "1A", "0B", "0A")

    def arc_of(v):
        for fid in order:
            if v in offsets_by_arc.get(fid, ()):
                return fid
        return None

    union = set().union(*offsets_by_arc.values()) if offsets_by_arc else set()
    records, seen = [], set()
    n = len(dec) & ~3

    if _NP and n >= 4:
        arr = np.frombuffer(dec, dtype=">u4", count=n // 4)
        sr_set = np.array(SAMPLE_RATES, dtype=np.uint64)
        sr_pos = np.nonzero(np.isin(arr.astype(np.uint64), sr_set))[0]
        for i in sr_pos:
            j = int(i) + 2
            if j < arr.size:
                wo = int(arr[j])
                if wo and wo % ALIGN == 0:
                    bo = int(i) * 4 - 0x10
                    records.append({"bank_off": bo if bo >= 0 else int(i) * 4,
                                    "wave_off": wo, "archive": arc_of(wo),
                                    "sample_rate": int(arr[i]), "via": "rate"})
                    seen.add(wo)
        if union:
            uni = np.array(sorted(union), dtype=np.uint64)
            ref_pos = np.nonzero(np.isin(arr.astype(np.uint64), uni))[0]
            for i in ref_pos:
                v = int(arr[i])
                if v and v % ALIGN == 0 and v not in seen:
                    records.append({"bank_off": int(i) * 4, "wave_off": v,
                                    "archive": arc_of(v), "sample_rate": None, "via": "ref"})
                    seen.add(v)
    else:
        for o in range(0, n - 4, 4):
            v = struct.unpack_from(">I", dec, o)[0]
            if v in SAMPLE_RATES and o + 12 <= n:
                wo = struct.unpack_from(">I", dec, o + 8)[0]
                if wo and wo % ALIGN == 0:
                    bo = o - 0x10
                    records.append({"bank_off": bo if bo >= 0 else o, "wave_off": wo,
                                    "archive": arc_of(wo), "sample_rate": v, "via": "rate"})
                    seen.add(wo); continue
            if v and v % ALIGN == 0 and v not in seen and v in union:
                records.append({"bank_off": o, "wave_off": v, "archive": arc_of(v),
                                "sample_rate": None, "via": "ref"})
                seen.add(v)

    records.sort(key=lambda r: r["bank_off"])
    meta["linked"] = sum(1 for r in records if r["archive"])
    return meta, records
