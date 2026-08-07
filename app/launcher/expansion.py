"""expansion.py — build a brand-new NHL club in Roster.ROS out of a spare team record.

This is the roster half of expansion-team support.  The other half (asset keys, TOC entries,
logo atlas tile, front-end jerseys) lives in `extra_teams.py` and `logos_atlas.py`; nothing
here touches an archive.

## What a team record actually is

`team_order.py` frames the record table: chunk 0x8489FAF3, 96 records of 412 bytes, every
string reached through a **self-relative** pointer (`target = field_offset + value - 1`).
Records 79..90 are the *spare* created-team slots the in-game "create a team" screen hands
out — no players, placeholder `****************` names, league nibble 5/6.  One of those is
what an expansion club is built in.

Cloning a SHIPPED record (never a created one) is the whole trick: a created record has
`+0xF4`, `+0xF8` and the `+0x160..+0x170` record-book block zeroed and a `+0xD0` that does not
resolve, and the game reads all of those.  Copy 412 bytes from a real club, rebase every
pointer, and the result is structurally indistinguishable from the 30 that shipped.

## The fields that must change, and why

| field  | what it is            | what we write                                            |
|--------|-----------------------|----------------------------------------------------------|
| 0x08.. | roster (26 slots)     | pointers to freshly cloned player rows, zero-terminated  |
| 0xA8.. | city/nick/code/lower  | new strings out of the pool allocator                    |
| 0xB8   | asset key             | the new key — this is what names every art asset         |
| 0xC8   | team id               | `n<<24 | n<<16`, n = one past the highest league-0 index  |
| 0x10C  | AUDIO id              | `audio_id_for(n)` — a different id space, see below      |
| 0x10E  | AUDIO id              | the same value again; stock records never disagree      |
| 0xCC   | arena                 | the donor's (see the caveat below)                        |
| 0xD0   | PRO pointer           | own `record+8` — a club is its own pro club              |
| 0xD4   | FARM pointer          | own `record+8` until `affiliates.update_ahl()` builds one|
| 0xD8   | head coach            | the donor's                                              |
| 0xF0   | uniform owner slot    | a value no other record uses (see `UNIFORM_SLOT_BASE`)   |
| 0x110  | league/division word  | the donor's, i.e. league 0 and the donor's division      |

## The AUDIO id space (`+0x10C`/`+0x10E`) is NOT the `+0xC8` id space

Every stock record carries an id in all three fields and stock ids never disagree, which is why
`+0xC8` looked like the one that mattered.  It is not.  Dumping `+0x10C` across all 96 records
of the live roster gives the real space:

    0..29     the 30 NHL clubs          (an AHL affiliate repeats its parent club's id)
    60, 61    Eastern / Western All-Stars   (league word 0x2A......)
    62..77    the 16 national teams         (league word 0x3A......)
    137       the generic "Home Team" / "Road Team" — what every created team carries

**30..59 is a hole**, and the hole is the whole story.  `teams.bin` is not indexed by slot: it
carries a speech-DB block whose key array holds these ids *verbatim*, binary-searched at runtime.
`cueStart[i] == 2i` in stock data, which is exactly why "slot = id - 30" looked like arithmetic.
An id that is not in the key array simply misses, and a miss falls back to the generic pair —
which is why Vegas at id 31 was announced as "the Home Team".

Because the map is data, **a club does not have to borrow — a slot can be added.**
`expansion_audio.apply()` splices new ids into the key array and their takes into the bank, and
`AUDIO_ADDED_IDS` lists the ones a given install has.  `audio_id_for()` hands those out first and
only falls back to borrowing (All-Stars, then nations) once they run out.  The added ids also land
inside `pamusic`'s 32-entry reserved block (goal-song cue `43 + id`, so 30/31 -> 73/74), which was
dead data no stock id could reach.  See the `project_goal_songs` note.

⚠ **The arena is shared with the donor.**  That is what the working Seattle and Vegas records
do, and it is why `roster_editor.apply_edits` refuses to rename ANA/SEA/VGK's arena — the
three point at one record.  Giving an expansion club its own arena record means allocating out
of the 40-record arena table (5 spare), which is not mapped here yet.

⚠ **Division realignment is deliberately not done.**  The new club inherits the donor's
division, so that division carries an extra team.  Rebalancing to the modern 4x8 is a separate,
explicitly deferred job.

## Verified against the working Seattle and Vegas records

`verify_against()` diffs a record this module builds against a reference record field by field.
Run over the live roster it reports the team record as an exact match apart from the fields the
user has since edited through the launcher (colours `+0xE8/+0xEC`, record book `+0x160..0x170`).

One intentional difference in the *players*: the ad-hoc script that first built Seattle and
Vegas grabbed spare player rows and only repointed their name pointers, so those clubs carry
Anaheim's names over unrelated attributes.  This module byte-clones the donor's rows, which is
what doc 21 specifies and what "behaves like a shipped team" means.
"""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import team_order as TO
import affiliates as AF
import expansion_audio as EA

BACKUP_SUFFIX = ".expansionbak"

ROSTER_MAX = len(list(TO.OFF_PLAYERS))      # 26 pointer slots in the record

# Player-row self-relative pointers.  Everything else in the 420-byte row is plain data —
# established by diffing a cloned row against its source: only these four resolve to the same
# targets, and 0x190 is the only one that has to be re-aimed.
P_LAST, P_FIRST = AF.OFF_LAST, AF.OFF_FIRST      # 0x17C, 0x180
P_TEAM = AF.OFF_BACKPTR                          # 0x190 -> team record + 8
P_MISC = 0x1A0                                   # shared table every player points at

# `+0xF0` high half is the record's uniform-owner slot: the 30 clubs use 0..29, their
# affiliates repeat the parent's, the national teams 60..77, the spare created-team records
# 100..109 and 200..201.  The full semantics are not mapped; what IS established is that the
# working Seattle and Vegas records carry 110 and 111 and that their jerseys resolve, so new
# clubs are allocated from 110 upward, skipping anything already in use.
UNIFORM_SLOT_BASE = 110


class ExpansionError(RuntimeError):
    pass


# ── reading the table ───────────────────────────────────────────────────────────

def _put_ptr(buf, off, target):
    struct.pack_into(">I", buf, off, TO._selfrel(target, off))


def _team_index(d, base, rec):
    """The `n` encoded in a record's id — `n<<24 | n<<16`."""
    v = TO._u32(d, base + rec * TO.STRIDE + TO.OFF_ID)
    return v >> 24


def _next_team_index(d, base, count):
    return max(_team_index(d, base, i) for i in TO.league0_slots(d, base, count)) + 1


# Ids ADDED to the speech DB by `expansion_audio.apply()` — handed out first, because they are
# the only ones that are nobody else's.  They sit in the 30..59 hole, so they also fall inside
# pamusic's reserved goal-song block.
AUDIO_ADDED_IDS = list(EA.NEW_AUDIO_IDS)

# Ids a club BORROWS once the added ones run out, in the order they go: the two All-Star slots
# first (All-Star mode is the least-played thing that has to share), then the 16 nations.
# Anything past that gets 137 — the generic "Home Team", which is the game's own fallback.
AUDIO_SPARE_IDS = [60, 61] + list(range(62, 78))
AUDIO_GENERIC = 137


def audio_id_for(n: int) -> int:
    """`+0x10C` for the club whose `+0xC8` index is `n` — see the id-space note in the docstring."""
    if n < 30:
        return n                                    # a real NHL club, id == index
    i = n - 30
    pool = AUDIO_ADDED_IDS + AUDIO_SPARE_IDS
    return pool[i] if i < len(pool) else AUDIO_GENERIC


def audio_id_is_added(audio_id: int) -> bool:
    """True if the id only resolves on an install that has run `expansion_audio.apply()`."""
    return audio_id in AUDIO_ADDED_IDS


def goal_song_cue_for(audio_id: int) -> int | None:
    """`pamusic.bin` cue for a club's goal song.  pamusic has no speech block, so it is purely
    positional: cue `43 + id` over a 32-entry reserved block, i.e. ids 0..31 only.  A borrowed
    id (60+) indexes past the block and has no goal song of its own."""
    return 43 + audio_id if 0 <= audio_id <= 31 else None


def _next_uniform_slot(d, base, count):
    used = {TO._u32(d, base + i * TO.STRIDE + 0xF0) >> 16 for i in range(count)}
    n = UNIFORM_SLOT_BASE
    while n in used:
        n += 1
    return n


def _rostered_rows(d, base, count):
    """Player rows any team record points at.  A roster pointer names the SURNAME field, so
    the row is that target minus the bias."""
    out = set()
    for i in range(count):
        o = base + i * TO.STRIDE
        for f in TO.OFF_PLAYERS:
            t = TO._target(d, o + f)
            if t is not None:
                out.add(t - AF.PLAYER_ROSTER_BIAS)
    return out


def _live_name_spans(d, taken):
    """[(start, end)] of every name string a ROSTERED player reads.

    "Unclaimed player row" is not the same as "free space": the player NAME pool is laid over
    the unused rows — Anaheim's Cutter Gauthier keeps his first name inside row 78 — so writing
    into a row no team points at can silently rename a player on a different club.  That is
    exactly what happened before this screen existed.

    Only rostered players' names are protected.  An unclaimed row's own name is by definition
    read by nobody, and reclaiming it is the entire point.  (A string shared between a claimed
    and an unclaimed player is protected, because the claimed one still names it.)
    """
    spans = []
    for row in taken:
        for f in (AF.OFF_LAST, AF.OFF_FIRST):
            t = TO._target(d, row + f)
            if t is None:
                continue
            e = d.find(b"\x00\x00", t)
            spans.append((t, (e + 2) if e >= 0 else t + 2))
    spans.sort()
    return spans


def _free_player_rows(d, base, count):
    """Rows that are unclaimed by a team AND hold no rostered player's name string."""
    pbase, pcount = AF._player_base(d)
    taken = _rostered_rows(d, base, count)
    spans = _live_name_spans(d, taken)
    out, k = [], 0
    for i in range(pcount):
        row = pbase + i * AF.PLAYER_STRIDE
        while k < len(spans) and spans[k][1] <= row:
            k += 1
        if row in taken:
            continue
        if k < len(spans) and spans[k][0] < row + AF.PLAYER_STRIDE:
            continue
        out.append(row)
    return out


def spare_records(ros_path):
    """Unused created-team slots, lowest first."""
    d = Path(ros_path).read_bytes()
    base, count = TO.find_table(d)
    return AF._spare_records(d, base, count)


def capacity(ros_path):
    """{'records': n, 'players': n, 'string_bytes': n} — what is left to build clubs out of."""
    d = Path(ros_path).read_bytes()
    base, count = TO.find_table(d)
    pool = AF.Pool(d, base, count)
    spares = AF._spare_records(d, base, count)
    free = sum(n for _, n in pool.zero_gaps())
    for s in spares:
        for f in (TO.OFF_CITY, TO.OFF_NICK, TO.OFF_CODE):
            t = TO._target(d, base + s * TO.STRIDE + f)
            if t is not None:
                free += pool.capacity(t)
    return {"records": len(spares),
            "players": len(_free_player_rows(d, base, count)),
            "string_bytes": free}


# ── building one ────────────────────────────────────────────────────────────────

def plan(ros_path, code, city, nick, akey=None, lower=None, donor=None):
    """What `create_expansion_team` would do, without writing anything."""
    d = Path(ros_path).read_bytes()
    base, count = TO.find_table(d)
    rec, don = _pick_slots(d, base, count, code, donor)
    donor_o = base + don * TO.STRIDE
    n_players = sum(1 for f in TO.OFF_PLAYERS if TO._u32(d, donor_o + f))
    free = _free_player_rows(d, base, count)
    return {
        "record": rec,
        "donor": don,
        "donor_code": TO._text(d, donor_o + TO.OFF_CODE),
        "team_index": _next_team_index(d, base, count),
        "uniform_slot": _next_uniform_slot(d, base, count),
        "code": code, "city": city, "nick": nick,
        "akey": (akey or code).upper(),
        "lower": lower or nick.split()[-1].lower(),
        "players": min(n_players, len(free)),
        "player_rows_free": len(free),
        "spare_records": len(AF._spare_records(d, base, count)),
    }


def _pick_slots(d, base, count, code, donor):
    """(spare record to build in, donor record to clone).  Raises rather than guess."""
    for i in range(count):
        if TO._text(d, base + i * TO.STRIDE + TO.OFF_CODE).upper() == code.upper():
            raise ExpansionError(f"a team record already uses the code {code!r} (record {i})")
    spares = AF._spare_records(d, base, count)
    if not spares:
        raise ExpansionError("no spare created-team record left to build a club in")

    l0 = TO.league0_slots(d, base, count)
    if donor is None:
        don = l0[0]
    else:
        hits = [i for i in l0
                if TO._text(d, base + i * TO.STRIDE + TO.OFF_CODE).upper() == donor.upper()]
        if not hits:
            raise ExpansionError(f"no league-0 team with code {donor!r} to clone")
        don = hits[0]
    return spares[0], don


def create_expansion_team(ros_path, code, city, nick, akey=None, lower=None, donor=None,
                          backup=True, log=print) -> dict:
    """Clone a shipping club into a spare record and return what was built.

    `code`  — 3-letter display code ("SEA").
    `akey`  — asset key; defaults to `code`.  Every art asset is named from this
              (`logo_<akey>`, `uniform_<akey>_home`, `ice_<akey>_playoffs`, ...), so the
              archive side has to be prepared for the same key — see `extra_teams`.
    `lower` — lowercase nickname at `+0xB4`; defaults to the last word of `nick`.
    `donor` — display code of the club to clone.  Defaults to the first league-0 record.

    The file is rewritten in place and never changes size: new strings come out of the pool's
    dead space, players out of unclaimed rows.
    """
    ros_path = Path(ros_path)
    old = ros_path.read_bytes()
    buf = bytearray(old)
    base, count = TO.find_table(old)

    rec, don = _pick_slots(old, base, count, code, donor)
    akey = (akey or code).upper()
    lower = lower or nick.split()[-1].lower()
    o = base + rec * TO.STRIDE
    donor_o = base + don * TO.STRIDE
    log(f"  building {city} {nick} ({code}) in record {rec}, cloned from "
        f"{TO._text(old, donor_o + TO.OFF_CODE)} (record {don})")

    # ── strings, before anything is written ───────────────────────────────────
    # The spare record's own placeholders are the roomiest slots in the pool (34 bytes) and
    # nothing else can reach them, so they pay for this club's names.  Reuse comes first:
    # "Seattle" may already exist as some other record's string.
    pool = AF.Pool(old, base, count)
    wanted = {"city": city, "nick": nick, "code": code, "lower": lower, "akey": akey}
    pinned = {pool.reuse(t) for t in wanted.values()}
    pinned.discard(None)
    for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK), ("code", TO.OFF_CODE),
                  ("lower", TO.OFF_LOWER), ("akey", TO.OFF_AKEY)):
        t = TO._target(old, o + f)
        if t is not None and t not in pinned and pool.is_dead(t, (rec, nm)):
            pool.release(t, owner=rec)
    for gap, n in pool.zero_gaps():
        pool.free.append((gap, n, set()))
    pool.free.sort(key=lambda e: e[0])
    resolved = {}
    for nm, text in sorted(wanted.items(), key=lambda kv: -len(kv[1])):   # longest first
        hit = pool.reuse(text)
        resolved[nm] = hit if hit is not None else pool.alloc(buf, text)

    # ── the record: 412 bytes verbatim, then every pointer re-aimed ───────────
    buf[o:o + TO.STRIDE] = old[donor_o:donor_o + TO.STRIDE]
    for f in TO.PTR_FIELDS:
        v = TO._u32(old, donor_o + f)
        if v:
            _put_ptr(buf, o + f, TO._target(old, donor_o + f))

    # ── players: byte-clone the donor's roster into unclaimed rows ────────────
    free = _free_player_rows(old, base, count)
    donor_rows = []
    for f in TO.OFF_PLAYERS:
        t = TO._target(old, donor_o + f)
        if t is None:
            break
        donor_rows.append(t - AF.PLAYER_ROSTER_BIAS)
    if len(free) < len(donor_rows):
        raise ExpansionError(
            f"{len(donor_rows)} player rows needed, only {len(free)} unclaimed")
    for k, f in enumerate(TO.OFF_PLAYERS):
        if k >= len(donor_rows):
            struct.pack_into(">I", buf, o + f, 0)        # zero-terminate the roster
            continue
        src, dst = donor_rows[k], free[k]
        buf[dst:dst + AF.PLAYER_STRIDE] = old[src:src + AF.PLAYER_STRIDE]
        # the row's own self-relative pointers: same targets, new field offsets
        for pf in (P_LAST, P_FIRST, P_MISC):
            _put_ptr(buf, dst + pf, TO._target(old, src + pf))
        _put_ptr(buf, dst + P_TEAM, o + 8)               # ...except the team back-pointer
        _put_ptr(buf, o + f, dst + AF.PLAYER_ROSTER_BIAS)

    # ── identity ──────────────────────────────────────────────────────────────
    n = _next_team_index(old, base, count)
    slot = _next_uniform_slot(old, base, count)
    struct.pack_into(">I", buf, o + TO.OFF_ID, (n << 24) | (n << 16))
    # `+0x10C`/`+0x10E` are the AUDIO id — a *different* id space from `+0xC8`.  Cloning left them
    # at the donor's, which is why a new club announced as the donor (Vegas was called "the Anaheim
    # Ducks") even with `+0xC8` written correctly.  Writing `n` there is equally wrong: `n` is 30+
    # and 30..59 is a HOLE in the audio space, so the club falls through to the generic "Home Team".
    # `audio_id_for` hands out ids `expansion_audio` has ADDED to the speech DB first — those are
    # in the hole, but they are only there on an install that ran `expansion_audio.apply()`, which
    # is why `modpack.apply_expansion` runs it before building a club.
    aid = audio_id_for(n)
    struct.pack_into(">H", buf, o + 0x10C, aid)
    struct.pack_into(">H", buf, o + 0x10E, aid)
    struct.pack_into(">H", buf, o + 0xF0, slot)
    _put_ptr(buf, o + TO.OFF_PRO, o + 8)
    _put_ptr(buf, o + TO.OFF_FARM, o + 8)                # until update_ahl() builds one
    for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK), ("code", TO.OFF_CODE),
                  ("lower", TO.OFF_LOWER), ("akey", TO.OFF_AKEY)):
        _put_ptr(buf, o + f, resolved[nm])

    # ── verify before writing ─────────────────────────────────────────────────
    new = bytes(buf)
    if len(new) != len(old):
        raise ExpansionError("internal error: file size changed")
    for nm, f in (("city", city), ("nick", nick), ("code", code),
                  ("lower", lower), ("akey", akey)):
        got = TO._text(new, o + dict(city=TO.OFF_CITY, nick=TO.OFF_NICK, code=TO.OFF_CODE,
                                     lower=TO.OFF_LOWER, akey=TO.OFF_AKEY)[nm])
        if got != f:
            raise ExpansionError(f"verify failed: {nm} reads {got!r}, wanted {f!r}")
    # No bystander may have been renamed — strings are shared, and the allocator writes into
    # space it believes is dead.  This is the net under that belief.
    for i in range(count):
        if i == rec:
            continue
        ro = base + i * TO.STRIDE
        for f in (TO.OFF_CITY, TO.OFF_NICK, TO.OFF_CODE, TO.OFF_LOWER, TO.OFF_AKEY):
            if TO._text(new, ro + f) != TO._text(old, ro + f):
                raise ExpansionError(
                    f"verify failed: record {i} renamed {TO._text(old, ro + f)!r} -> "
                    f"{TO._text(new, ro + f)!r}")
        if [TO._target(new, ro + f) for f in TO.OFF_PLAYERS] != \
           [TO._target(old, ro + f) for f in TO.OFF_PLAYERS]:
            raise ExpansionError(f"verify failed: record {i}'s roster changed")
    # ...and no player anywhere may have changed NAME.  The name pool lives on top of the
    # unused rows, so a row that looks free can hold a rostered player's surname; this is the
    # check that caught it.
    for row in sorted(_rostered_rows(old, base, count)):
        for f in (AF.OFF_LAST, AF.OFF_FIRST):
            if TO._text(new, row + f) != TO._text(old, row + f):
                raise ExpansionError(
                    f"verify failed: player at 0x{row:X} renamed "
                    f"{TO._text(old, row + f)!r} -> {TO._text(new, row + f)!r}")
    if TO.league0_slots(new, base, count) != sorted(TO.league0_slots(old, base, count) + [rec]):
        raise ExpansionError("verify failed: the league-0 set is not the old one plus the new club")

    if backup:
        bak = ros_path.with_suffix(BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copyfile(ros_path, bak)
            log(f"  backup -> {bak.name}")
    ros_path.write_bytes(new)
    log(f"  record {rec}: id {n}, uniform slot {slot}, {len(donor_rows)} players, key {akey}")
    return {"record": rec, "donor": don, "team_index": n, "uniform_slot": slot,
            "players": len(donor_rows), "code": code, "akey": akey,
            "city": city, "nick": nick, "lower": lower}


# ── verification ────────────────────────────────────────────────────────────────

# Fields a user edits through the launcher after the club exists, so a mismatch against a
# reference record means nothing.  0xE8/0xEC are the two team colours; 0x160..0x170 is the
# record book (founded year, cups, best season).
EDITABLE = {0xE8, 0xEC, 0x160, 0x164, 0x168, 0x16C, 0x170}


def verify_against(ros_path, code, ref_path, ref_code, log=print):
    """Field-by-field diff of `code` in `ros_path` against `ref_code` in `ref_path`.

    Returns [(offset, ours, theirs)] for the fields that genuinely differ.  Pointer fields are
    compared by what they RESOLVE TO — the raw self-relative values differ whenever the two
    records sit at different offsets, which is every time.
    """
    a, b = Path(ros_path).read_bytes(), Path(ref_path).read_bytes()
    ao, ab = _find_code(a, code)
    bo, bb = _find_code(b, ref_code)
    ptr = set(TO.PTR_FIELDS)
    out = []
    for f in range(0, TO.STRIDE, 4):
        if f in EDITABLE:
            continue
        if f in ptr:
            # a resolved pointer is only comparable as what it NAMES: strings by text, table
            # pointers by which record they land in
            x, y = _describe(a, ab, ao + f), _describe(b, bb, bo + f)
        else:
            x, y = TO._u32(a, ao + f), TO._u32(b, bo + f)
        if x != y:
            out.append((f, x, y))
            log(f"  0x{f:03X}  {x!r}  !=  {y!r}")
    return out


def _find_code(d, code):
    base, count = TO.find_table(d)
    for i in range(count):
        if TO._text(d, base + i * TO.STRIDE + TO.OFF_CODE).upper() == code.upper():
            return base + i * TO.STRIDE, base
    raise ExpansionError(f"no team record with code {code!r}")


def _describe(d, base, field):
    """What a pointer field names, in terms comparable across two different files."""
    t = TO._target(d, field)
    if t is None:
        return None
    _, count = TO.find_table(d)
    if base <= t < base + count * TO.STRIDE:
        return ("record", (t - 8 - base) // TO.STRIDE)
    # the player range is tested BEFORE text: decoding a player row as UTF-16BE always yields
    # *some* string, so a text-first order silently reports every roster slot as garbage text
    pbase, pcount = AF._player_base(d)
    if pbase <= t < pbase + pcount * AF.PLAYER_STRIDE:
        row = t - AF.PLAYER_ROSTER_BIAS
        return ("player", TO._text(d, row + AF.OFF_FIRST), TO._text(d, row + AF.OFF_LAST))
    s = TO._text(d, field)
    return ("text", s) if s else ("raw", t)
