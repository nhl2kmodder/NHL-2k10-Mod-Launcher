"""expansion_audio.py — give an expansion club its OWN audio slot instead of borrowing one.

Cloning a team left Seattle and Vegas sharing Anaheim's audio id, and pointing them at a spare id
only moved the problem: the All-Star slot then said whatever we recorded there. This module ADDS
slots, so a club owns the line the announcer reads and nothing else changes.

## How the game picks a team's audio

`teams.bin` (the "the Vancouver Canucks" name calls) is one of the banks covered by the speech DB
— see `speech_lines.py`. Its block is two parallel arrays the engine binary-searches:

    ids[59]       ascending    the roster's `+0x10C` AUDIO id, VERBATIM
    cueStart[59]  ascending    that id's first cue in teams.bin's cue table

Stock ids are `0..29` (NHL) | `60..77` (2 All-Star + 16 nations) | `95, 99, 103, 123..126, 130,
132` (classic clubs) | `997, 998` (generic Home / Road), every run exactly 2 takes so
`cueStart[i] == 2i`. That `== 2i` is why a "slot = id − 30" rule looked real for years — it was
the array being dense, not arithmetic. **There is no slot mapping in code at all.** A created team
carries id `137`, which is simply absent from the array, so the lookup misses and the club is
announced as *"The Home Team"*.

`pamusic.bin` has no block, which is why goal songs are positional: **cue = 43 + id**. That block
is 32 entries (cues 43..74) but only ids 0..29 exist, so cues **73/74 are unreachable in stock
data** — reserved slots the game shipped and never used.

## What this module does

Adds ids **30 and 31** — the bottom of the hole, and exactly the ids cues 73/74 were reserved for,
so a new club gets a working goal song for free with no out-of-range cue index. Per club:

  * one entry inserted into `teams.bin`'s key array in sorted position,
  * two new cues (two takes) inserted into the cue table at that run's position,
  * two streams inserted into the bank data at the matching byte offset.

Cue offsets are **strictly ascending and the sentinel record holds the bank size**, i.e. a stream's
length is `off[i+1] - off[i]`. So new cues cannot simply be appended while their id sorts into the
middle — the arrays would stop being monotonic and every take count after the insert would be
wrong. The bank is small (1.6 MB), so this rebuilds it with the new streams physically in place and
every later offset shifted. That keeps every invariant the engine relies on.

The new streams start as a copy of a donor run (the All-Star name calls by default) so the slot is
audible immediately. Replace them from the Audio tab to make the club say its own name.

Both container edits land in slack that already exists — 8 bytes after the key arrays (237 free)
and 32 bytes after the cue table (32 free, an exact fit) — so `global.iff` does not change
decompressed size and nothing downstream of the edit moves.
"""
import struct
from pathlib import Path

import json

try:
    from . import archive_textures as AT, bank_parser as BP, resources
except ImportError:                                     # flat import when run un-packaged
    import archive_textures as AT
    import bank_parser as BP
    import resources

ALIGN = 0x800
CONTAINER = "global.iff"                                # where teams.bin's tables live
BANK = "teams.bin"

# The banks a slot can be added to. `key_head` is the start of the bank's key array, which is what
# pins its block down — every bank's block has the same shape, so the ids are the only tell.
#
# teams.bin keys every id 0..29 and its cueStart is 2i, so its runs cover the whole bank.
# horns.bin is different and worth understanding before editing it: its key array is an OVERRIDE
# of four entries over a positional base. Cues 0..28 belong to no run at all — they are indexed
# positionally — and only ids 1/8/9 (ATL/CBJ/DAL, whose horns sit in the tail) and 999 (every
# created team) are keyed. That is why `build` has to carry the pre-run prefix across untouched.
#
# loadingaudio_teams.bin is the LOADING SCREEN's team-name bank and is shaped exactly like
# teams.bin: 59 keys (0..29 clubs | 60..77 All-Star + nations | 9 classic clubs | 997/998 generic)
# and two takes each, so cueStart == 2i. Ids 30/31 are absent, so an expansion club misses the
# lookup and the caller falls back to 997/998 — the "the home team" / "the road team" the user
# hears. Adding the ids is the whole fix.
TARGETS = {
    "teams.bin": {"container": "global.iff", "key_head": [0, 1, 2, 3], "donors": [60, 61]},
    "horns.bin": {"container": "gamedata.iff", "key_head": [1, 8, 9, 999], "donors": [999, 999]},
    "loadingaudio_teams.bin": {"container": "Loading.iff", "key_head": [0, 1, 2, 3],
                               "donors": [60, 61]},
}

# Audio ids handed to expansion clubs, in order. 30/31 are the bottom of the 30..59 hole AND the
# ids pamusic reserved cues 73/74 for, so a club added here gets a goal song without any pamusic
# edit. Ids past 31 need a pamusic cue appended too, which this module does not do yet.
NEW_AUDIO_IDS = [30, 31]
# The run each new slot is cloned from, so it says something sensible before the user records over
# it. 60/61 are the Eastern / Western All-Stars.
DONOR_IDS = [60, 61]


# ─────────────────────────────────────────── locating ───────────────────────────────────────────

def _blocks(dec: bytes):
    """Every speech-DB block in a decompressed container.

    Header is 0x14 bytes and fully self-checking, which is what makes locating it safe on any
    install rather than a hardcoded offset::

        [u32 0x15][u32 sz1 = 2*w*n + 17][u32 sz2 = sz1 + 2n - 4][u16 a][u16 b][u32 tail]
        ids[n]      at hdr+0x14   (w bytes each)
        cueStart[n] at ids + w*n  (same width)

    Both arrays are placed implicitly by count — there is no pointer to either — so growing the
    table means bumping the count and the two size words and inserting the bytes.
    """
    out = []
    for o in range(0, len(dec) - 0x18, 4):
        if struct.unpack_from(">I", dec, o)[0] != 0x15:
            continue
        sz1, sz2 = struct.unpack_from(">II", dec, o + 4)
        for fi, n in enumerate(struct.unpack_from(">HH", dec, o + 12)):
            if n < 2:
                continue
            for w in (2, 4):
                if sz1 == 2 * w * n + 17 and sz2 == sz1 + 2 * n - 4:
                    out.append({"hdr": o, "n": n, "w": w, "cnt_off": o + 12 + fi * 2,
                                "ids": o + 0x14, "starts": o + 0x14 + w * n})
                    break
            else:
                continue
            break
    return out


def _read_arr(dec, off, n, w):
    f = ">H" if w == 2 else ">I"
    return [struct.unpack_from(f, dec, off + w * i)[0] for i in range(n)]


def locate(dec: bytes, bank_size: int, bank: str = BANK) -> dict:
    """Find a bank's cue table and speech block in its decompressed container.

    The cue table is anchored on a fact rather than an offset: its **sentinel record holds the
    bank's size**, and that size is an exact archive fact from the 0A TOC. The speech block is
    anchored on the head of its key array — no other block in either container repeats it.
    """
    head = TARGETS[bank]["key_head"]
    tbl = None
    for o in range(0, len(dec) - 0x20, 4):
        one, rate, align, _k = struct.unpack_from(">IIII", dec, o)
        if one != 1 or align != ALIGN or rate not in BP.SAMPLE_RATES:
            continue
        cnt = struct.unpack_from(">I", dec, o - 8)[0]           # count u32, then a crc, then hdr
        if not (0 < cnt < 100000) or o + 0x10 + (cnt + 1) * 8 > len(dec):
            continue
        if struct.unpack_from(">I", dec, o + 0x10 + cnt * 8 + 4)[0] == bank_size:
            tbl = {"hdr": o, "cnt_off": o - 8, "count": cnt, "rate": rate}
            break
    if tbl is None:
        raise ValueError(f"{bank}: no cue table whose sentinel matches bank size 0x{bank_size:x}")

    blk = None
    for b in _blocks(dec):
        ids = _read_arr(dec, b["ids"], min(b["n"], len(head)), b["w"])
        if ids == head and b["w"] == 2:
            blk = b
            break
    if blk is None:
        raise ValueError(f"{bank}: speech block not found")
    return {"table": tbl, "block": blk}


def _span(bank: str):
    """(span, vol) for a wave bank from the layout `wave_banks.py` ships. `span` carries the
    bank's `base`/`size` in the pair's LOGICAL space plus its 0A TOC index — every bank is an
    ordinary TOC entry, which is what makes a grown bank relocatable like any other asset."""
    doc = json.loads(resources.data_path("audio_wave_banks.json").read_text(encoding="utf-8"))
    vol = int(doc.get("vol") or 0)
    for banks in (doc.get("banks") or {}).values():
        if bank in banks:
            return banks[bank], vol
    raise ValueError(f"{bank} is not in the wave-bank layout")


def _slack(dec: bytes, end: int) -> int:
    n = 0
    while end + n < len(dec) and dec[end + n] == 0:
        n += 1
    return n


# ──────────────────────────────────────────── editing ───────────────────────────────────────────

def installed(game_dir, new_ids=None, bank: str = BANK) -> bool:
    """True if `game_dir`'s bank already carries the ids. Reads the LIVE container and takes the
    bank size from the LIVE TOC, because a patched bank is bigger than the size the shipped layout
    records — checking against the clean size is what made a second `apply` blow up instead of
    no-opping, and the modpack applies packs onto installs that may already have this."""
    game_dir = Path(game_dir)
    want = list(NEW_AUDIO_IDS if new_ids is None else new_ids)
    loc = AT.resolve(bank, game_dir)
    if not loc:
        return False
    try:
        dec, _ = BP.decompress_bank(TARGETS[bank]["container"], clean_dir=game_dir)
        blk = locate(dec, loc[2], bank)["block"]
    except Exception:
        return False
    ids = _read_arr(dec, blk["ids"], blk["n"], blk["w"])
    return all(i in ids for i in want)


def plan(game_dir=None, clean_dir=None, new_ids=None, donors=None, log=print, bank=BANK,
         takes=None) -> dict:
    """Work out the whole edit without writing anything. Returns everything `apply` needs.

    `takes` caps how many of the donor's cues each new id clones. The cue table can only grow into
    the zero slack that already follows it — the container's decompressed size must not change,
    because every DRAM pointer around it is self-relative and inserting bytes would move the arrays
    out from under them. loadingaudio_teams.bin has 19 bytes of slack, so two ids at the stock two
    takes each (32 B) does not fit and `takes=1` (16 B) does. One take means the club is announced
    with the same clip wherever the second ("And <team>") form would have been used.
    """
    clean_dir = Path(clean_dir or AT.CLEAN_DIR)
    tgt = TARGETS[bank]
    container = tgt["container"]
    new_ids = list(NEW_AUDIO_IDS if new_ids is None else new_ids)
    donors = list(tgt["donors"] if donors is None else donors)[:len(new_ids)]

    span, vol = _span(bank)
    bank_size = span["size"]

    dec, meta = BP.decompress_bank(container, clean_dir=clean_dir)
    if dec is None:
        raise ValueError(f"{container} not found in {clean_dir}")
    loc = locate(dec, bank_size, bank)
    tbl, blk = loc["table"], loc["block"]
    ids = _read_arr(dec, blk["ids"], blk["n"], blk["w"])
    starts = _read_arr(dec, blk["starts"], blk["n"], blk["w"])

    already = [i for i in new_ids if i in ids]
    if already:
        return {"noop": f"audio ids {already} already present — slots already added", "ids": ids}
    missing = [d for d in donors if d not in ids]
    if missing:
        raise ValueError(f"donor ids {missing} are not in {bank}'s key array")

    # runs, in key order: (id, [cue indices])
    bounds = starts + [tbl["count"]]
    runs = [(ids[i], list(range(bounds[i], bounds[i + 1]))) for i in range(blk["n"])]
    donor_run = {i: cues for i, cues in runs if i in donors}
    if takes:
        donor_run = {i: cues[:takes] for i, cues in donor_run.items()}
    insert_before = {}                                          # key position -> list of new ids
    for nid, d in zip(new_ids, donors):
        p = next((k for k, (i, _) in enumerate(runs) if i > nid), len(runs))
        insert_before.setdefault(p, []).append((nid, d))

    # growth accounting, so a caller can see it fits before anything is written
    d_cues = sum(len(donor_run[d]) for _, d in zip(new_ids, donors))
    arr_grow = 2 * blk["w"] * len(new_ids)
    tbl_grow = 8 * d_cues
    arr_slack = _slack(dec, blk["starts"] + blk["w"] * blk["n"])
    tbl_slack = _slack(dec, tbl["hdr"] + 0x10 + (tbl["count"] + 1) * 8)
    if arr_grow > arr_slack:
        raise ValueError(f"key arrays need {arr_grow} bytes, only {arr_slack} of slack")
    if tbl_grow > tbl_slack:
        raise ValueError(f"cue table needs {tbl_grow} bytes, only {tbl_slack} of slack")

    log(f"  {bank}: {blk['n']} ids / {tbl['count']} cues @ {tbl['rate']} Hz, bank 0x{bank_size:x}")
    for nid, d in zip(new_ids, donors):
        log(f"    + id {nid} <- {len(donor_run[d])} takes cloned from id {d}")
    log(f"  slack: key arrays {arr_grow}/{arr_slack} B, cue table {tbl_grow}/{tbl_slack} B")
    return {"dec": dec, "meta": meta, "loc": loc, "ids": ids, "starts": starts, "runs": runs,
            "donor_run": donor_run, "insert_before": insert_before, "span": span, "vol": vol, "bank_size": bank_size,
            "new_ids": new_ids, "donors": donors, "clean_dir": clean_dir,
            "bank": bank, "container": container}


def build(p: dict, bank: bytes) -> tuple:
    """Return (new_container_dec, new_bank_bytes). Pure — no I/O, so it is unit-testable.

    Rebuilds the bank stream-by-stream in key order, splicing each new club's cloned takes in at
    its sorted position. Offsets are recomputed cumulatively, which is what keeps them ascending
    and the sentinel honest.
    """
    dec = bytearray(p["dec"])
    tbl, blk = p["loc"]["table"], p["loc"]["block"]
    off = [struct.unpack_from(">I", dec, tbl["hdr"] + 0x14 + i * 8)[0]
           for i in range(tbl["count"] + 1)]
    # A cue record is OFFSET-FIRST: [u32 offset @hdr+0x14+8i][f32 duration @hdr+0x18+8i]. Reading
    # it as [f32 dur][u32 off] starting at +0x10 is self-consistent for an untouched table -- which
    # is why it went unnoticed -- but it pairs every offset with the PREVIOUS cue's duration, so a
    # reordered or inserted cue carries the wrong length, and record 0's "duration" is really a
    # header word. Proven by decoding 28 loading cues (f_(i+1) matched 25/28, f_i 1/28) and 20
    # teams cues, and structurally by Res_GetRecordCursor @0x83b5cf08:
    #     param_2[3] = *(param_1 + 8*i + 0x60)   -- one slot past the offset at +0x5c.
    dur = [struct.unpack_from(">f", dec, tbl["hdr"] + 0x18 + i * 8)[0]
           for i in range(tbl["count"])]
    cue = [(dur[i], bank[off[i]:off[i + 1]]) for i in range(tbl["count"])]
    by_id = p["donor_run"]                      # already capped by plan()'s `takes`

    # Cues before the FIRST cueStart belong to no run — they are the positional base a key array
    # overrides (horns.bin: 29 of them). They are not ours to touch, but they are part of the bank
    # and every offset after them depends on them, so they lead the rebuild unchanged.
    new_ids, new_starts = [], []
    out = [cue[c] for c in range(p["starts"][0] if p["starts"] else 0)]
    for pos, (tid, cues) in enumerate(p["runs"]):
        for nid, d in p["insert_before"].get(pos, []):
            new_ids.append(nid); new_starts.append(len(out))
            out += [cue[c] for c in by_id[d]]
        new_ids.append(tid); new_starts.append(len(out))
        out += [cue[c] for c in cues]
    for nid, d in p["insert_before"].get(len(p["runs"]), []):
        new_ids.append(nid); new_starts.append(len(out))
        out += [cue[c] for c in by_id[d]]

    # ── bank data + cue table ──────────────────────────────────────────────────────────────────
    new_bank = b"".join(c[1] for c in out)
    # Written OFFSET-FIRST to match the read above. The sentinel record is an offset only -- the
    # float slot that follows the last real cue's offset is that cue's own duration, not a
    # sentinel duration, so there is nothing to carry over from the old table.
    o = 0
    for i, (d, data) in enumerate(out):
        struct.pack_into(">I", dec, tbl["hdr"] + 0x14 + i * 8, o)
        struct.pack_into(">f", dec, tbl["hdr"] + 0x18 + i * 8, float(d))
        o += len(data)
    struct.pack_into(">I", dec, tbl["hdr"] + 0x14 + len(out) * 8, len(new_bank))   # sentinel
    struct.pack_into(">I", dec, tbl["cnt_off"], len(out))

    # ── key arrays (both implicit, so cueStart moves when ids grows) ───────────────────────────
    n, w = len(new_ids), blk["w"]
    f = ">H" if w == 2 else ">I"
    for i, v in enumerate(new_ids):
        struct.pack_into(f, dec, blk["ids"] + w * i, v)
    for i, v in enumerate(new_starts):
        struct.pack_into(f, dec, blk["ids"] + w * n + w * i, v)
    struct.pack_into(">I", dec, blk["hdr"] + 4, 2 * w * n + 17)          # sz1
    struct.pack_into(">I", dec, blk["hdr"] + 8, 2 * w * n + 17 + 2 * n - 4)   # sz2
    struct.pack_into(">H", dec, blk["cnt_off"], n)
    return bytes(dec), new_bank


def verify(dec: bytes, bank_size: int, want_ids: list, bank: str = BANK) -> str:
    """Re-read the patched container from scratch and check every invariant the engine relies on.
    Returns "" when clean. Cheap insurance against a half-applied edit reaching the game."""
    loc = locate(dec, bank_size, bank)
    tbl, blk = loc["table"], loc["block"]
    ids = _read_arr(dec, blk["ids"], blk["n"], blk["w"])
    starts = _read_arr(dec, blk["starts"], blk["n"], blk["w"])
    off = [struct.unpack_from(">I", dec, tbl["hdr"] + 0x14 + i * 8)[0]
           for i in range(tbl["count"] + 1)]
    problems = []
    if ids != sorted(ids):
        problems.append("ids not ascending")
    if len(set(ids)) != len(ids):
        problems.append("duplicate ids")
    if starts != sorted(starts):
        problems.append("cueStart not non-decreasing")
    if starts and starts[-1] >= tbl["count"]:
        problems.append("last run has no cues")
    if any(off[i] > off[i + 1] for i in range(tbl["count"])):
        problems.append("cue offsets not ascending")
    if any(o % ALIGN for o in off):
        problems.append("cue offset not 0x800-aligned")
    if off[-1] != bank_size:
        problems.append(f"sentinel 0x{off[-1]:x} != bank size 0x{bank_size:x}")
    for i in want_ids:
        if i not in ids:
            problems.append(f"id {i} missing")
        elif starts[ids.index(i)] == (starts + [tbl["count"]])[ids.index(i) + 1]:
            problems.append(f"id {i} has 0 takes")
    return "; ".join(problems)


def apply(game_dir, clean_dir=None, new_ids=None, donors=None, log=print, bank=BANK,
          takes=None) -> str:
    """Add the slots to a real install. Re-encodes the container and relocates both it and the
    grown bank through the normal TOC path, so Restore-from-Clean still undoes it."""
    game_dir = Path(game_dir)
    if installed(game_dir, new_ids, bank):
        msg = f"audio ids {list(NEW_AUDIO_IDS if new_ids is None else new_ids)} already in {bank}"
        log(f"  {msg}")
        return msg
    p = plan(game_dir, clean_dir, new_ids, donors, log, bank, takes)
    if "noop" in p:
        log(f"  {p['noop']}")
        return p["noop"]
    BANK_, CONTAINER_ = p["bank"], p["container"]

    span, size, vol = p["span"], p["bank_size"], p["vol"]
    second = span["base"] >= vol                        # the pair is ONE logical space, split at vol
    local = span["base"] - vol if second else span["base"]
    src = p["clean_dir"] / ("1B" if second else "1A")
    with open(src, "rb") as f:
        f.seek(local); bank = f.read(size)
    if len(bank) != size:
        raise ValueError(f"{BANK_}: read {len(bank)} of {size} bytes from {src.name}")

    new_dec, new_bank = build(p, bank)
    err = verify(new_dec, len(new_bank), p["new_ids"], BANK_)
    if err:
        raise ValueError(f"patched container failed verification: {err}")
    log(f"  {BANK_}: 0x{size:x} -> 0x{len(new_bank):x} bytes, container size unchanged "
        f"({len(new_dec)} B)")

    # ── re-encode the container, keeping every blob we did not touch byte-identical ────────────
    loc = AT.resolve(CONTAINER_, game_dir)
    if not loc:
        raise ValueError(f"{CONTAINER_} not in {game_dir} TOC")
    arc, coff, csize, cidx, _f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(coff); res = bytearray(f.read(csize))
    blobs = AT._walk_blobs(res, csize)
    # `dec` is every blob concatenated, so map the edited byte range back to the one blob that
    # owns it and re-encode only that — the rest are copied compressed, byte for byte.
    lo = min(p["loc"]["block"]["hdr"], p["loc"]["table"]["cnt_off"])
    hi = max(p["loc"]["block"]["starts"] + p["loc"]["block"]["w"] * (p["loc"]["block"]["n"] + 4),
             p["loc"]["table"]["hdr"] + 0x10 + (p["loc"]["table"]["count"] + 8) * 8)
    edited = base = None
    at_off = 0
    for i, b in enumerate(blobs):
        if b["dec"] and at_off <= lo and hi <= at_off + len(b["dec"]):
            edited, base = i, at_off
            break
        at_off += len(b["dec"] or b"")
    if edited is None:
        raise ValueError(f"{CONTAINER_}: edit spans blob boundaries — cannot re-encode piecewise")
    b = blobs[edited]
    payload = new_dec[base:base + len(b["dec"])]
    comp = AT.EE.encode_payload(payload, wparam=b["wp"], codec=b["codec"])
    e = AT._verify_blob(comp, payload)
    if e:
        raise ValueError(f"{CONTAINER_} blob {edited} re-encode failed: {e}")

    new_res = bytearray(res[:b["off"]]) + comp + res[b["off"] + b["tot"]:]
    shift = len(comp) - b["tot"]
    # The section table is `count` entries at 0x20, stride 0x20 — and only `count`. The bytes
    # after the last one look like more entries but are not, so walking until a zero type
    # rewrites live data.
    for q in (0x20 + i * 0x20 for i in range(AT._BE(new_res, 0x10))):
        dataoff = AT._BE(new_res, q + 0x14)
        if dataoff == b["off"]:
            struct.pack_into(">I", new_res, q + 0x18, len(comp))
        elif dataoff > b["off"]:
            struct.pack_into(">I", new_res, q + 0x14, dataoff + shift)
    struct.pack_into(">I", new_res, 8, len(new_res))

    # `span["toc_index"]` is the index in the SHIPPED TOC. An install that has already taken custom
    # assets has more entries, and the TOC is hash-sorted, so every index past the insertion point
    # has moved — trusting the stored one repoints somebody else's asset. Resolve by name instead,
    # and refuse to write unless the entry we land on still describes the clean bank.
    bloc = AT.resolve(BANK_, game_dir)
    if not bloc:
        raise ValueError(f"{BANK_} not in {game_dir} TOC")
    bidx, bsize = bloc[3], bloc[2]
    if bsize != size:
        raise ValueError(f"{BANK_}: TOC#{bidx} is {bsize} bytes, expected the clean {size} — "
                         f"already patched, or the wrong entry")

    # Back up every archive either write can land in. The list used to be the hardcoded 0A/1B pair
    # that teams.bin needs; loadingaudio_teams.bin lives in 0B, which would have gone unbacked.
    for a in ("0A", "0B", "1A", "1B"):
        if (game_dir / a).exists():
            AT._backup_once(game_dir / a, log)
    r1 = AT._relocate(CONTAINER_, bytes(new_res), cidx, game_dir, 0, 0, "RAW", log)
    r2 = AT._relocate(BANK_, new_bank, bidx, game_dir, 0, 0, "RAW", log)
    log("  " + r1); log("  " + r2)
    return (f"added audio ids {p['new_ids']} to {BANK_} "
            f"({p['loc']['block']['n']} -> {p['loc']['block']['n'] + len(p['new_ids'])} slots)")


if __name__ == "__main__":
    import sys
    g = Path(sys.argv[1]) if len(sys.argv) > 1 else AT.CLEAN_DIR
    plan(g, g)
