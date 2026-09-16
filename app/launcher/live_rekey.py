"""live_rekey.py — re-key the audio store from stale physical akeys onto (bank, live cue index).

The store's `.extract_manifest.json` keys every slot by the PHYSICAL address it had at extraction
time ("1A:0x5A128800"). 23 wave banks have moved or been rewritten since, so those keys no longer
name the audio they were recorded against. `live_cues.py` supplies the durable identity; this
module works out which live cue each stale key belongs to, and it does so with PROOF rather than
with alignment guesswork. Anything unproven is left UNRESOLVED and must be refused by callers —
a wrong cue index writes XMA over someone else's audio, which is the fault that produced the 48
clobbered art entries on 2026-09-02 ([[project_patch_audio_stale_bank_offsets]]).

⛔ AN OFFSET THAT LANDS ON A CUE START IS NOT PROOF ON A REFLOWED BANK. This was the first design
and it is wrong. palines.bin was rewritten by pal_build30's 329-cue replace, which shifted every
later cue by a cumulative amount; offsets are 2048-aligned, so a stale offset lands on SOME live
cue start by coincidence often enough to matter. Measured on palines: of 698 offsets that hit a
cue start, content proved 117 of them pointed at the WRONG cue. Offsets are therefore trusted only
on a bank that shows no reflow (`_intact` below), and content overrules them everywhere else.

⛔ THE DECODE IS NOT BIT-REPRODUCIBLE, SO CONTENT PROOF IS NOT A HASH. Decoding a cue known to be
unchanged and comparing it to the store's WAV gives an identical frame count and audibly identical
audio, but individual samples differ by about one LSB — the extraction ran through a different
xma2encode build. A sha1 of the samples matches nothing at all; the first attempt scored 0 content
proofs out of 2,437. What survives the decoder is the ENVELOPE, so a cue is fingerprinted as the
per-block mean absolute amplitude over its leading window and compared by cosine distance.

That comparator was calibrated against a labelled set before it was trusted:

    true pairs        <= 0.0008        (median 6e-05)
    different audio   >= 0.05
    threshold          0.005, and the runner-up must be 10x worse

Two orders of magnitude of separation, so the threshold is not delicate. Three rules keep it sound:
a live cue may be claimed by only one store row; matches must form a strictly increasing chain,
because the reflow replaced and inserted but never REORDERED; and a tie is refused, never guessed.

Order then closes the near-duplicates. palines repeats a lot of audio (per-team phrasings, silence
cues) and 1.5 s of envelope cannot separate copies; frame count cannot either, because the copies
are genuinely identical. Only position can. So between two confident anchors the rows are aligned
onto the cues those anchors bracket by a monotone DP that may SKIP a cue but never reorder --
skipping is what makes it work, since the reflow inserted cues (palines carries 3266 live cues
against 3135 store rows) and an exact-count rule refuses every gap an insertion landed in. Each
pair the alignment proposes must still clear the content threshold on its own, so a gap whose
audio disagrees is refused rather than forced.

    palines.bin: 698 -> 2762 of 3135 resolved, and 132 bad offset keys retired.

  python live_rekey.py palines.bin          # resolve one bank, write data/live_rekey.json
  python live_rekey.py                      # every indexed bank

Related: [[project_relocated_bank_rescan_fails]], [[project_palines_replace_path_30clubs]].
"""
import bisect
import contextlib
import json
import subprocess
import sys
import tempfile
import wave
from collections import Counter
from pathlib import Path

import numpy as np

import live_cues as LC
import volume_store as VS
import wave_banks as WB

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from nhl2k10_launcher import make_riff_xma2          # noqa: E402

GAME = Path(__file__).resolve().parents[2]
STORE = GAME / "NHL2k10_Extracted_Files" / "Audio" / "Extracted"
XMA = str(Path(__file__).resolve().parents[1] / "tools" / "xma2encode.exe")
OUT = Path(__file__).resolve().parent / "data" / "live_rekey.json"

WIN, NBLOCK = 65536, 128    # 1.5 s at 44.1 kHz, as 128 envelope blocks
TH = 0.005                  # cosine distance; true pairs measure <= 0.0008, mismatches >= 0.05
MARGIN = 10.0               # the runner-up must be this much worse before a match is believed
INTACT = 0.99               # a bank whose offsets nearly all still hit cue starts never reflowed
NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW


def _desc(samples):
    """Per-block mean absolute amplitude — the part of the waveform the decoder cannot shift."""
    a = np.zeros(WIN, dtype=np.float32)
    n = min(len(samples), WIN)
    a[:n] = samples[:n]
    return np.abs(a).reshape(NBLOCK, WIN // NBLOCK).mean(axis=1)


def _wav_fp(path):
    """(frame count, descriptor) for a mono 16-bit WAV, or None."""
    try:
        with contextlib.closing(wave.open(str(path), "rb")) as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                return None
            nf = w.getnframes()
            s = np.frombuffer(w.readframes(WIN), dtype="<i2")
    except Exception:
        return None
    return nf, _desc(s)


def _decode(raw, rate, td):
    """Decode one cue's XMA packets to a temp WAV, or None."""
    xp, out = Path(td) / "c.xma", Path(td) / "c.wav"
    try:
        xp.write_bytes(make_riff_xma2(raw, 1, rate))
        out.unlink(missing_ok=True)
        subprocess.run([XMA, str(xp), "/DecodeToPCM", str(out)],
                       capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        return None
    return out if out.exists() else None


def _shipped_rows(bank):
    """Store entries whose SHIPPED address fell inside this bank, ascending by that address."""
    lay = WB._load()
    span = None
    for arc, (vol, spans, first) in lay.items():
        for n, lo, hi in spans:
            if n == bank:
                span = (arc, lo, hi)
                break
    if span is None:
        return []
    _arc, lo, hi = span
    man = json.loads((STORE / ".extract_manifest.json").read_text(encoding="utf-8"))["entries"]
    rows = []
    for k, e in man.items():
        up = e["fid"].upper()
        if up not in lay:
            continue
        vol, _sp, first = lay[up]
        lg = e["offset"] if up == first else e["offset"] + vol
        if lo <= lg < hi:
            rows.append({"akey": k, "rel": lg - lo, "wav": e.get("wav", ""),
                         "name": e.get("name", "")})
    rows.sort(key=lambda r: r["rel"])
    return rows


def _norm(m):
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return m / n


def _live_fps(bank, tbl, rate, log):
    """Fingerprint every live cue in the bank by decoding it. ~3 min for palines."""
    e = VS.entry_named(GAME, bank)
    tree = VS.tree_dir(GAME) / e["file"]
    idx, fps = [], []
    with open(tree, "rb") as f, tempfile.TemporaryDirectory() as td:
        for n, c in enumerate(tbl):
            f.seek(c["rel"])
            w = _decode(f.read(c["size"]), rate, td)
            fp = _wav_fp(w) if w else None
            if fp:
                idx.append(c["cue"])
                fps.append(fp[1])
            if n and n % 500 == 0:
                log(f"    decoded {n}/{len(tbl)}")
    return np.array(idx), np.array(fps, dtype=np.float32)


def _monotone(anchors):
    """The longest strictly increasing run of (row position, cue). Anything off it is discarded."""
    tails, back, at = [], [0] * len(anchors), []
    for k, (_p, c, _j) in enumerate(anchors):
        q = bisect.bisect_left(tails, c)
        back[k] = at[q - 1] if q else -1
        if q == len(tails):
            tails.append(c)
            at.append(k)
        else:
            tails[q], at[q] = c, k
    if not tails:
        return []
    keep, k = [], at[len(tails) - 1]
    while k != -1:
        keep.append(k)
        k = back[k]
    return [anchors[k] for k in sorted(keep)]


def _align(D, gap_rows, gap_cues):
    """Monotone least-cost assignment of every row onto a distinct cue, cues may be skipped.

    Skipping is the whole point: the reflow inserted cues, so a gap usually holds more cues than
    rows and an exact-count rule would refuse it. Rows are never skipped and never reordered.
    """
    R, C = len(gap_rows), len(gap_cues)
    if R == 0 or C < R:
        return None
    INF = 1e9
    dp = np.full((R + 1, C + 1), INF)
    dp[0, :] = 0.0
    took = np.zeros((R + 1, C + 1), dtype=np.int8)
    for i in range(1, R + 1):
        for k in range(i, C + 1):
            take = dp[i - 1, k - 1] + D[gap_rows[i - 1], gap_cues[k - 1]]
            skip = dp[i, k - 1]
            if take <= skip:
                dp[i, k], took[i, k] = take, 1
            else:
                dp[i, k] = skip
    if dp[R, C] >= INF:
        return None
    out, i, k = [], R, C
    while i > 0:
        if took[i, k]:
            out.append((gap_rows[i - 1], gap_cues[k - 1]))
            i -= 1
        k -= 1
    return out[::-1]


def rekey(bank, log=print):
    """Map every shipped store key for `bank` onto a live cue index, with the proof used."""
    tbl = LC.table(bank)
    rate = LC.rate(bank)
    rows = _shipped_rows(bank)
    log(f"{bank}: {len(rows)} shipped store rows, {len(tbl)} live cues")
    if not rows:
        return {}, {}, []

    hits = {r["akey"]: LC.cue_at(bank, r["rel"]) for r in rows}
    nhit = sum(1 for v in hits.values() if v is not None)
    log(f"  offsets still landing on a cue start: {nhit}/{len(rows)}")

    if nhit >= INTACT * len(rows):
        # The bank never reflowed, so its offsets are still the addresses they were recorded as.
        res = {k: v for k, v in hits.items() if v is not None}
        why = dict.fromkeys(res, "offset")
        unres = [r["akey"] for r in rows if r["akey"] not in res]
        log(f"  bank intact - offset proof accepted for all {len(res)}")
        log(f"  RESOLVED {len(res)}/{len(rows)}   unresolved {len(unres)}")
        return res, why, unres

    log("  bank was REFLOWED - offsets are not proof here, matching on content")
    cue_ix, cue_fp = _live_fps(bank, tbl, rate, log)
    keep = [(j, r) for j, r in enumerate(rows) if r["wav"]]
    sfp, srow = [], []
    for _j, r in keep:
        fp = _wav_fp(STORE / r["wav"])
        if fp:
            sfp.append(fp[1])
            srow.append(r)
    if not sfp or not len(cue_fp):
        log("  nothing fingerprintable — all unresolved")
        return {}, {}, [r["akey"] for r in rows]
    sfp = np.array(sfp, dtype=np.float32)

    D = 1.0 - _norm(sfp) @ _norm(cue_fp).T
    best = np.argmin(D, axis=1)
    bd = D[np.arange(len(srow)), best]
    alt = D.copy()
    alt[np.arange(len(srow)), best] = np.inf
    good = (bd < TH) & (alt.min(axis=1) > bd * MARGIN)

    dup = {c for c, n in Counter(best[good].tolist()).items() if n > 1}   # one row per cue
    good &= ~np.isin(best, list(dup))
    log(f"  content proof: {int(good.sum())} (refused {len(dup)} cues claimed by several rows)")

    pos = {r["akey"]: p for p, r in enumerate(srow)}
    anchors = [(pos[srow[j]["akey"]], int(cue_ix[best[j]]), j) for j in np.where(good)[0]]
    mono = _monotone(anchors)
    if len(mono) != len(anchors):
        log(f"  dropped {len(anchors) - len(mono)} matches that broke the order of the bank")

    res, why = {}, {}
    for _p, c, j in mono:
        res[srow[j]["akey"]] = c
        why[srow[j]["akey"]] = "content"

    # Order closes the near-duplicates: between two anchors the rows are aligned onto the cues.
    edges = [(-1, -1, -1)] + mono + [(len(srow), len(cue_ix), -1)]
    forced = 0
    for (p0, c0, _a), (p1, c1, _b) in zip(edges, edges[1:]):
        gap_rows = list(range(p0 + 1, p1))
        gap_cues = list(range(c0 + 1, c1))
        if not gap_rows:
            continue
        pairs = _align(D, gap_rows, gap_cues)
        if pairs is None or any(D[j, i] >= TH for j, i in pairs):
            continue                                   # order allows it but the audio disagrees
        for j, i in pairs:
            res[srow[j]["akey"]] = int(cue_ix[i])
            why[srow[j]["akey"]] = "content+order"
            forced += 1
    log(f"  placed by order: {forced}")

    contra = sum(1 for k, c in res.items() if hits.get(k) is not None and hits[k] != c)
    if contra:
        log(f"  WARNING: {contra} stale offsets pointed at the WRONG cue - content overruled them")

    unres = [r["akey"] for r in rows if r["akey"] not in res]
    log(f"  RESOLVED {len(res)}/{len(rows)}   unresolved {len(unres)}")
    return res, why, unres


def main():
    banks = sys.argv[1:] or list(LC.banks())
    out = {}
    if OUT.exists():
        out = json.loads(OUT.read_text(encoding="utf-8"))
    out["_comment"] = ("Stale physical akey -> live cue index, per bank, with the proof used. "
                       "'offset' = the bank never reflowed, so its shipped offsets are still its "
                       "addresses; 'content' = the live cue decodes to the store's audio; "
                       "'content+order' = the reflow preserved order and the gap was forced. "
                       "Anything absent is UNRESOLVED and must be refused, never guessed. "
                       "Rebuild after any bank rewrite.")
    out.setdefault("banks", {})
    for b in banks:
        try:
            res, why, unres = rekey(b)
        except Exception as exc:
            print(f"{b}: FAILED - {exc}")
            continue
        out["banks"][b] = {"map": {k: {"cue": v, "why": why[k]} for k, v in res.items()},
                           "unresolved": unres}
    OUT.write_text(json.dumps(out), encoding="utf-8")
    print(f"wrote {OUT.name}")


if __name__ == "__main__":
    main()
