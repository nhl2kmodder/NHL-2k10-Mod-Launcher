"""live_cue_durations.py — the second half of an audio install: the cue record's f32 duration.

Writing a cue's stream bytes is only half the job. Each cue's record in the game's audio directory
is a pair, `[u32 bank-relative offset][f32 duration]`, and the f32 is what the game scrubs the
playback cursor by. Install a shorter take without updating it and the game plays the new audio and
then runs on into whatever is left of the old cue — the take sounds right and then trails into a
stranger's voice. That is why `pa_generic_install` emits `_durations.json` and says it is NOT
FINISHED until this pass has run.

Those records do not live in the bank. They live in a COMPRESSED section of a container
(gamedata.iff, global.iff, Loading.iff), so they cannot be patched one at a time the way
`live_cues.write_cue` patches a stream: the container has to be decompressed, patched, re-encoded
and relocated once. Hence one batched pass over every cue an install touched, rather than a write
per take.

Safety, in the same spirit as live_cues:

  * the record's OFFSET field is checked against the offset `live_cue_index.json` recorded for that
    cue before its duration is touched. If they disagree the index is stale and the pass refuses —
    writing an f32 into a misidentified record is exactly the class of fault that clobbered 48 art
    entries on 2026-09-02;
  * a duration must be positive and no longer than the cue's own block can physically hold;
  * the re-encoded blob is verified to decode back to what we assembled before anything is written;
  * dry run by default.

  python live_cue_durations.py "..\\PA Ear Test\\Al Generic numbers\\_durations.json"
  python live_cue_durations.py <json> --apply

Related: [[project_live_cue_addressing]], [[project_iff_section_dec_size]].
"""
import json
import struct
import sys
from pathlib import Path

import live_cues as LC

GAME = Path(__file__).resolve().parents[2]
REC = 0x14          # the records start this far past the table header
MAX_SEC = 600.0


def _blob_of(blobs, off):
    """(blob index, offset inside that blob) for an offset into the concatenated decompression."""
    base = 0
    for i, b in enumerate(blobs):
        d = b.get("dec") or b""
        if base <= off < base + len(d):
            return i, off - base
        base += len(d)
    return None, None


def apply(updates, game_dir=GAME, apply=False, log=print):
    """Set the f32 duration of every (bank, cue) in `updates` -> {bank: {cue: seconds}}."""
    import archive_textures as AT

    AT.set_game_dir(game_dir)
    idx = LC._load_index()["banks"]

    by_cont = {}
    for bank, cues in updates.items():
        if bank not in idx:
            raise KeyError(f"{bank} is not in live_cue_index.json — rebuild the index first")
        by_cont.setdefault(idx[bank]["container"], []).append(bank)

    total = 0
    for cont, banks in sorted(by_cont.items()):
        loc = AT.resolve(cont, game_dir)
        if not loc:
            raise KeyError(f"{cont} is not in the TOC")
        with open(game_dir / loc[0], "rb") as f:
            f.seek(loc[1])
            res = bytearray(f.read(loc[2]))
        blobs = AT._walk_blobs(res, loc[2])
        decs = [bytearray(b.get("dec") or b"") for b in blobs]

        touched, n = set(), 0
        for bank in banks:
            meta = idx[bank]
            offs, tbl = meta["offsets"], LC.table(bank)
            for cue, secs in sorted(updates[bank].items(), key=lambda kv: int(kv[0])):
                cue = int(cue)
                if not 0 <= cue < len(tbl):
                    raise IndexError(f"{bank}: cue {cue} is outside the bank's {len(tbl)} cues")
                secs = float(secs)
                if not 0 < secs <= MAX_SEC:
                    raise ValueError(f"{bank} cue {cue}: duration {secs} is not sane")
                at = meta["hdr"] + REC + 8 * cue
                bi, off = _blob_of(blobs, at)
                if bi is None:
                    raise ValueError(f"{bank} cue {cue}: record at 0x{at:X} is outside the "
                                     f"decompressed container — the index is stale")
                have = struct.unpack_from(">I", decs[bi], off)[0]
                if have != offs[cue]:
                    raise ValueError(
                        f"{bank} cue {cue}: the record at 0x{at:X} holds offset 0x{have:X} but the "
                        f"index says 0x{offs[cue]:X}. The index is stale — run "
                        f"`live_cues.py rebuild` before installing. Refusing.")
                struct.pack_into(">f", decs[bi], off + 4, secs)
                touched.add(bi)
                n += 1
        log(f"{cont}: {n} cue records in {len(banks)} bank(s)")
        total += n
        if not apply:
            continue
        if len(touched) != 1:
            raise NotImplementedError(
                f"{cont}: the records span {len(touched)} compressed blobs. Every cue table this "
                f"game has ever shown lives in blob 0, so rather than guess at a multi-blob "
                f"re-encode this refuses. Handle it deliberately.")

        bi = touched.pop()
        b = blobs[bi]
        new = bytes(decs[bi])
        comp = AT.EE.encode_payload(new, wparam=b["wp"], codec=b["codec"])
        if (e := AT._verify_blob(comp, new)):
            raise RuntimeError(f"{cont}: blob {bi} re-encode failed verification — {e}")
        nr = bytearray(res[:b["off"]]) + comp + res[b["off"] + b["tot"]:]
        shift = len(comp) - b["tot"]
        for q in (0x20 + i * 0x20 for i in range(AT._BE(nr, 0x10))):
            do = AT._BE(nr, q + 0x14)
            if do == b["off"]:
                struct.pack_into(">I", nr, q + 0x18, len(comp))
                struct.pack_into(">I", nr, q + 0x0C, len(new))   # ⚠ the DECOMPRESSED size too
            elif do > b["off"]:
                struct.pack_into(">I", nr, q + 0x14, do + shift)
        struct.pack_into(">I", nr, 8, len(nr))
        log("  " + AT._relocate(cont, bytes(nr), loc[3], game_dir, 0, 0, "RAW", log))

    if apply:
        LC.forget()
        log(f"wrote {total} durations; re-run `live_cues.py rebuild` to refresh the index")
    else:
        log(f"\nDRY RUN — {total} records would be written. Re-run with --apply.")
    return total


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__.strip().splitlines()[-3].strip())
    upd = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    apply(upd, apply="--apply" in sys.argv)


if __name__ == "__main__":
    main()
