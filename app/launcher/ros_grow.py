"""ros_grow.py — grow a Roster.ROS record table past its shipped size.

    ## ⚠ THE FILE SIZE IS A FIXED STRIDE, NOT A CEILING

    Roster.ROS is one *section* of the game's save-blob format, and every section has a fixed
    length. The save deserializer's jump table dispatches the roster case like this:

        0x83BFF3CC  addi r4,r0,1
        0x83BFF3D0  mr   r3,r29             ; r29 = cursor into the save blob
        0x83BFF3D4  bl   Function_83D58420  ; install the roster from the cursor
        0x83BFF3D8  bl   FUN_83d4e718       ; bytes consumed = DAT_84d8e9f8 + 4

    So the roster section is **always exactly `DAT_84d8e9f8 + 4` bytes**: a u32 length followed by
    a `DAT_84d8e9f8`-byte payload region. The u32 at offset 0x00 only says how much of that region
    is *meaningful* — `Function_83D58420` installs it only when it is `<=` the limit:

        if ((param_1 != NULL) && (iVar1 = *param_1, iVar1 <= DAT_84d8e9f8)) { ...install... }
        return;                                  // else: SILENTLY nothing

    `DAT_84d8e9f8` is set once at init by `Function_83D4E628` to a hardcoded 0x26A2A0 = 2,532,000,
    which is also the size of the single roster buffer it allocates there. That makes the shipped
    file 2,532,004 bytes **to the byte** — not a coincidence, it is the section stride.

    Two rules follow, and BOTH must hold:

      1. `u32 @0x00` (= meaningful image size) must be `<= DAT_84d8e9f8`, else the game silently
         keeps whatever roster it already had.
      2. **The FILE must be exactly `DAT_84d8e9f8 + 4` bytes.** Short, and the section read runs
         off the end: the game reports *"Unable to load Roster on HDD. The saved content is
         damaged or unreadable."* — boot-observed 2026-08-04 on a 2,533,936-byte file against a
         patched (2,663,076-stride) executable.

    Growth therefore means raising `DAT_84d8e9f8` — `roster_cap.py` patches it — and then writing
    a file **padded to the new stride**. `grow_arenas` does both: it refuses to exceed the cap, and
    it zero-pads out to `cap + 4`. Nothing points into the pad, so it costs only disk.

Everything else in this launcher edits the save strictly IN PLACE, because a fixed file size is the
one invariant that can never silently corrupt anything. This module is the deliberate exception, and
it exists because of one finding:

## Why growth is possible at all

`Roster_FixupPointers @0x83D53AA0` is the complete pointer schema of the save. It walks one
hardcoded block per table; each block reads that table's COUNT and POINTER out of the file's own
19-entry directory (the in-memory "roster manager" IS the file image at `filebase+4`, so directory
entry i is count@`0x0c+12i`, ptr@`0x10+12i`), and then rewrites every pointer in place with the
self-relative rule `*p = &p + *p - 1`.

Two consequences:

1. **Nothing hardcodes a table's length.** `Arena_GetByIndex @0x83D4B3B0` bound-checks the index
   against the directory count and `Tex_PrecacheArenas @0x83FDBB48` loops `0..Arena_GetCount()`.
   Raise the count and the game follows.
2. **A pointer only needs recomputing if its field or its target MOVES.** So if we append a bigger
   copy of a table at the end of the file and leave every existing byte exactly where it is, the
   only pointers in the entire save that change are (a) the four inside each arena record, whose
   FIELD moved, and (b) the pointers aimed AT arena records, whose TARGET moved. Everything else is
   provably untouched — which is what makes this safe enough to try.

The old table's bytes are abandoned where they lie. Chunks are located by their own directory
pointer, so a gap is legal; the no-gaps tiling `ros_file.py` assumes is an observation about the
shipped file, not a rule. The one cosmetic side effect is that ros_file infers a chunk's size from
where the NEXT chunk starts, so the dead space gets attributed to the chunk in front of the hole
(0x7FADC8B2 reads 1600 bytes longer). Nothing reads that chunk.

## The arena record

    +0x00 name   +0x04 city   +0x08 state   +0x0C ARENA ART ASSET KEY
    +0x1A u16 capacity   +0x1E u8 arena id   +0x20 dasher ARGB   +0x24 second tier

`+0x0C` is what the building's art is loaded by — `Asset_GetArenaIffName @0x83FD61C8` formats the
.iff name from it, NOT from the team's asset key. (Which is why Winnipeg's record still says `ATL`
and Utah's still says `PHO`: the relocations renamed the buildings but never moved the art.)

## Where the space would come from

The size check is `<=`, so the file may be SMALLER than 2,532,004 — it just may never be larger.
Four extra arena records need 160 bytes of table plus the two novel arena-name strings; the other
six strings (`Seattle`/`Washington`/`SEA`, `Las Vegas`/`Nevada`/`VGK`) need no bytes at all, since
a self-relative pointer can aim at any existing string in the file. So the budget is ~200 bytes,
and the candidates are:

* **dead strings in the pool** (0xEB69DFB9, 242,556 bytes) — orphaned by every rename, relocation
  and expansion done so far. Compacting it to only-referenced-and-deduped should free far more
  than 200 bytes. Needs the full reference set first, which `Roster_FixupPointers`' schema gives.
* **shifting the chunks after the arena table down by 160** and taking the 160 out of that
  reclaimed pool tail. Any chunk that moves needs its internal pointer fields recomputed, and the
  fixup schema enumerates exactly those, per table.

Neither is a small change, and both rewrite the whole file rather than editing it in place.

## What was never verified, and now can't be from this path

* whether saving the roster IN GAME writes the larger count back or clamps it to the shipped 40;
* whether arena SFX bank loading (`ArenaSfx_LoadBankForArena`) indexes a static 40-entry table.

Both need a roster the game will actually load, so they wait on the compaction work above.
"""
from __future__ import annotations
import shutil
import struct
from pathlib import Path

try:
    from . import ros_file as RF
    from . import team_order as TO
    from . import arena_colors as AC
except ImportError:
    import ros_file as RF
    import team_order as TO
    import arena_colors as AC

DIR_OFF, DIR_ENT = RF.DIR_OFF, RF.DIR_ENT

# The loader's hard cap on the roster image (header u32 at 0x00 = filesize-4). Hardcoded in the
# game at `Function_83D4E628` as both the buffer size and the limit `Function_83D58420` tests
# against; `FUN_83d4e718` reports the save section's stride as this + 4 = 2,532,004, which is the
# shipped Roster.ROS size exactly. Over it, the roster is silently discarded.
MAX_IMAGE = 0x26A2A0            # 2,532,000 bytes -> max file size 2,532,004

# ...unless the executable has been patched to raise it — `roster_cap.py` bumps exactly this
# constant. Pass `max_image=` (or `game_dir=`, which reads it back out of default.xex) to grow
# into that headroom. Growing past whatever the CURRENT executable enforces is still refused.
ALIGN = 16


class GrowError(RuntimeError):
    pass


def _align(n, a=ALIGN):
    return (n + a - 1) // a * a


def _utf16(s: str) -> bytes:
    return s.encode("utf-16-be") + b"\x00\x00"


def _dir_fields(chunk_index: int):
    """(count_field, ptr_field) file offsets for a directory entry."""
    fo = DIR_OFF + chunk_index * DIR_ENT
    return fo + 4, fo + 8


def find_refs(data, lo: int, hi: int, stride: int = 0, step: int = 4):
    """Every 4-aligned u32 in the file that resolves, self-relatively, into [lo, hi).

    A brute-force safety net rather than a parser: `Roster_FixupPointers` names the pointer fields
    we know about, but a field this launcher has never identified would break silently. Anything
    this reports and the caller doesn't explicitly repoint is a reason to stop.

    With `stride` given, only hits that land exactly ON a record boundary are returned. Over 2.5 MB
    of ratings, colours and packed bitfields a fair number of u32s resolve into any given 1.6 KB
    window by pure coincidence — 51 of them here — but a pointer that means "this arena" has to
    address a record, not a byte 23 into one, so the boundary test drops the noise without hiding
    anything that could actually matter.
    """
    out = []
    for o in range(0, len(data) - 4, step):
        v = struct.unpack_from(">I", data, o)[0]
        if v == 0:
            continue
        sv = v - (1 << 32) if v & 0x80000000 else v
        t = o + sv - 1
        if lo <= t < hi and not (stride and (t - lo) % stride):
            out.append((o, t))
    return out


def grow_arenas(ros_path, extra: int = 4, assign=None, backup=True, log=print,
                max_image=None, game_dir=None) -> dict:
    """Append a copy of the arena table with `extra` spare records, and repoint everything at it.

    `assign` is an optional {TEAMCODE: {name, city, state, key, capacity, dasher, dasher2}} — each
    named team is moved onto one of the new records, which is seeded from that team's CURRENT arena
    so anything left unspecified keeps the value it already had.

    `max_image` overrides the size ceiling; `game_dir` reads the ceiling out of that folder's
    default.xex, which is the honest answer whenever `roster_cap.py` has patched it. With neither,
    the stock `MAX_IMAGE` applies — the safe assumption, since an unpatched game silently discards
    an oversized roster.

    Returns a report dict. The file is rewritten only if every step succeeds.
    """
    ros_path = Path(ros_path)
    ros = RF.RosFile(str(ros_path))
    data = bytearray(ros.data)
    c = AC.chunk(ros)
    old_off, n, stride = c.foff, c.nrec, c.stride
    old_end = old_off + n * stride
    rep = {"old_off": old_off, "old_count": n, "stride": stride, "extra": extra}

    # ── 1. who currently points into the arena table ─────────────────────────
    # The known reference is each team's +0xCC. Anything else the scan turns up is unexplained, and
    # unexplained references into a table we are about to abandon are exactly the failure mode this
    # whole design is trying to avoid — so they are reported and, unless benign, they stop us.
    base, nteams = TO.find_table(data)
    known = {_dir_fields(c.index)[1]}            # the table's own directory pointer
    team_of = {}
    for i in range(nteams):
        f = base + i * TO.STRIDE + TO.OFF_ARENA
        t = TO._target(data, f)
        if t is not None and old_off <= t < old_end:
            known.add(f); team_of[f] = i
    found = find_refs(data, old_off, old_end, stride)
    unknown = [(o, t) for o, t in found if o not in known]
    rep["refs_known"] = len(known)
    rep["refs_unknown"] = unknown
    if unknown:
        log(f"  ! {len(unknown)} unexplained reference(s) into the arena table:")
        for o, t in unknown[:12]:
            log(f"      field 0x{o:06X} -> arena record {(t - old_off) // stride}"
                f" (+0x{(t - old_off) % stride:02X})")

    # ── 2. lay the new table down at EOF ─────────────────────────────────────
    # Any new STRINGS go down first, because the table has to be the last thing in the file: every
    # tool here (ros_file included) infers a chunk's size from where the next one starts, so a table
    # that ends at EOF with trailing bytes after it reads back with a bogus stride of size//count.
    strtab = {}
    for code, v in (assign or {}).items():
        for key in ("name", "city", "state", "key"):
            if key in v:
                off = _align(len(data), 2)
                data += b"\x00" * (off - len(data))
                data += _utf16(v[key])
                strtab[(code.upper(), key)] = off

    new_off = _align(len(data))
    data += b"\x00" * (new_off - len(data))
    data += data[old_off:old_end]                       # the existing records, verbatim
    donor = bytes(data[old_off:old_off + stride])       # record 0, as the seed for the spares
    for _ in range(extra):
        data += donor
    rep["new_off"] = new_off
    rep["new_count"] = n + extra

    # ── 3. the four pointers inside every record that moved ──────────────────
    # Field moved, target didn't: value = target - new_field + 1.
    moved = 0
    for r in range(n + extra):
        src = r if r < n else 0                         # spares inherit record 0's strings for now
        for k in (0x00, 0x04, 0x08, 0x0C):
            t = TO._target(data, old_off + src * stride + k)
            f = new_off + r * stride + k
            struct.pack_into(">I", data, f, 0 if t is None else TO._selfrel(t, f))
            moved += 1
    rep["ptrs_rewritten"] = moved

    # ── 4. the directory entry: count, and its own self-relative pointer ─────
    cf, pf = _dir_fields(c.index)
    struct.pack_into(">I", data, cf, n + extra)
    struct.pack_into(">I", data, pf, TO._selfrel(new_off, pf))

    # ── 5. every team's +0xCC — target moved, field didn't ───────────────────
    for f, i in team_of.items():
        t = TO._target(ros.data, f)                     # resolve against the ORIGINAL bytes
        struct.pack_into(">I", data, f, TO._selfrel(new_off + (t - old_off), f))
    rep["teams_repointed"] = len(team_of)

    # ── 6. optional: give named teams a record of their own ──────────────────
    rep["assigned"] = {}
    if assign:
        codes = {}
        for i in range(nteams):
            r = base + i * TO.STRIDE
            code = TO._text(data, r + TO.OFF_CODE).strip().upper()
            if code:
                codes.setdefault(code, i)
        nxt = n
        for code, v in assign.items():
            code = code.upper()
            if code not in codes:
                log(f"  {code}: not a team in this roster — skipped"); continue
            if nxt >= n + extra:
                log(f"  {code}: no spare arena record left (grow by more) — skipped"); continue
            row, trec = nxt, codes[code]; nxt += 1
            src = TO._target(data, base + trec * TO.STRIDE + TO.OFF_ARENA)
            data[new_off + row * stride:new_off + (row + 1) * stride] = \
                data[src:src + stride]                  # seed from the team's CURRENT arena
            for k, key in ((0x00, "name"), (0x04, "city"), (0x08, "state"), (0x0C, "key")):
                f = new_off + row * stride + k
                t = strtab.get((code, key))              # a new string, else keep the seed's
                if t is None:
                    t = TO._target(data, src + k)
                struct.pack_into(">I", data, f, 0 if t is None else TO._selfrel(t, f))
            if "capacity" in v:
                struct.pack_into(">H", data, new_off + row * stride + AC.CAPACITY_OFF,
                                 int(v["capacity"]))
            # +0x1E is the arena id, and the seed copy above brings the DONOR's along. It keys
            # the per-arena audio (the goal horn lands on the donor's cue if this is left alone),
            # so a club with its own record needs its own id.
            if "arena_id" in v:
                data[new_off + row * stride + AC.ARENA_ID_OFF] = int(v["arena_id"]) & 0xFF
            for key, o in (("dasher", AC.DASHER_OFF), ("dasher2", AC.DASHER2_OFF)):
                if key in v:
                    data[new_off + row * stride + o:new_off + row * stride + o + 4] = \
                        b"\xFF" + bytes(AC._as_rgb(v[key]))
            f = base + trec * TO.STRIDE + TO.OFF_ARENA
            struct.pack_into(">I", data, f, TO._selfrel(new_off + row * stride, f))
            rep["assigned"][code] = row
            log(f"  {code} -> new arena record {row}")

    # ── 7. the header's total size, the loader's cap, and the section stride ──
    struct.pack_into(">I", data, 0, len(data) - 4)
    image = len(data) - 4
    rep["size"] = (ros.orig_size, len(data))

    # `Function_83D58420` installs the roster only when this header value is <= the executable's
    # limit, and silently keeps the built-in default roster otherwise. Refuse to write a file the
    # game will throw away — a rejected roster looks exactly like catastrophic data loss.
    cap = max_image
    if cap is None and game_dir is not None:
        try:
            from . import roster_cap as RC
        except ImportError:
            import roster_cap as RC
        cap = RC.read_cap(Path(game_dir) / "default.xex")
    if cap is None:
        cap = MAX_IMAGE
    rep["max_image"] = cap
    if image > cap:
        raise RuntimeError(
            f"refusing to write: roster image would be {image} bytes, over the loader's "
            f"cap of {cap} (0x{cap:X}). The game would silently discard this file and boot the "
            f"default roster. Either free space inside the image (see this module's docstring) "
            f"or raise the cap with roster_cap.apply().")

    # The roster is a save-blob SECTION of fixed length `cap + 4` (see the module docstring): the
    # deserializer advances the cursor by `FUN_83d4e718()` whatever the header says. A short file
    # makes that read run past the end, and the game refuses the save as "damaged or unreadable".
    # So pad out to the stride. Nothing points into the pad; the header still bounds every read.
    if len(data) < cap + 4:
        data.extend(b"\0" * (cap + 4 - len(data)))
    rep["padded_to"] = len(data)

    if backup:
        bak = ros_path.with_suffix(ros_path.suffix + ".growbak")
        if not bak.exists():
            shutil.copy2(ros_path, bak)
            log(f"  backup -> {bak.name}")
    ros_path.write_bytes(data)
    log(f"  arena table {n} -> {n + extra} records, moved 0x{old_off:X} -> 0x{new_off:X}, "
        f"image {ros.orig_size - 4} -> {image} bytes, file padded to the {len(data)}-byte "
        f"section stride")
    return rep


def verify(ros_path, log=print) -> bool:
    """Re-read the grown file with the ordinary tooling and check it still makes sense."""
    ros = RF.RosFile(str(ros_path))
    d = ros.data
    ok = True
    # The header sizes the IMAGE, which on a padded file stops short of EOF; the pad past it must
    # be zero so nothing can mistake it for data.
    if struct.unpack_from(">I", d, 0)[0] != ros.image_size - 4:
        log("  FAIL: header size field"); ok = False
    if ros.pad and any(d[ros.image_size:]):
        log("  FAIL: section pad is not zero"); ok = False
    c = AC.chunk(ros)
    log(f"  arena chunk: {c.nrec} records x {c.stride}B at 0x{c.foff:X}")
    if c.stride != AC.STRIDE:
        log("  FAIL: stride is no longer 40"); ok = False
    for i in range(c.nrec):
        v = AC.read(ros, i)
        strs = [TO._text(d, AC._off(ros, i) + k) for k in (0, 4, 8, 0xC)]
        if not v["name"] or not strs[3]:
            log(f"  FAIL: arena {i} lost a string: {strs}"); ok = False
    base, n = TO.find_table(d)
    seen = {}
    for i in range(n):
        code = TO._text(d, base + i * TO.STRIDE + TO.OFF_CODE).strip().upper()
        lg = (TO._u32(d, base + i * TO.STRIDE + TO.OFF_LEAGUE) >> 28)
        if not code or lg != 0:
            continue
        ar = AC.arena_record(ros, i)
        t = TO._target(d, base + i * TO.STRIDE + TO.OFF_ARENA)
        inside = t is not None and c.foff <= t < c.foff + c.size and (t - c.foff) % c.stride == 0
        if not inside:
            log(f"  FAIL: {code}'s arena pointer no longer lands on a record"); ok = False
        seen.setdefault(ar, []).append(code)
    for ar, who in sorted(seen.items()):
        if len(who) > 1:
            log(f"  shared arena {ar} ({AC.name(ros, ar)}): {', '.join(who)}")
    log(f"  {sum(len(v) for v in seen.values())} league-0 clubs across {len(seen)} arenas")
    return ok


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        r"C:/Users/cloug/Documents/xenia_canary/content/B13EBABEBABEBABE"
        r"/54540853/00000001/Roster.ROS/Roster.ROS")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("./_grow_test/Roster.ROS")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"scratch copy: {dst}")
    rep = grow_arenas(dst, extra=4, assign={
        "SEA": {"name": "Climate Pledge Arena", "city": "Seattle", "state": "Washington",
                "key": "SEA", "capacity": 17151, "dasher": "#001628", "arena_id": 31},
        "VGK": {"name": "T-Mobile Arena", "city": "Las Vegas", "state": "Nevada",
                "key": "VGK", "capacity": 17500, "dasher": "#333F42", "arena_id": 30},
    })
    print("report:", {k: v for k, v in rep.items() if k != "refs_unknown"})
    print("verify:", "OK" if verify(dst) else "FAILED")
