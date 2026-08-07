"""affiliates.py — bring the AHL block in Roster.ROS up to the modern affiliation map.

NHL 2K10 ships 30 AHL clubs as team records 30..59 (league nibble 1).  They are not playable:
they own a roster and a name and nothing else — no logo, no uniform of their own (their asset
key is the PARENT's, so an affiliate draws the NHL club's art).  So "updating" an affiliate is
purely a matter of names, plus the PRO/FARM pointer pair that ties the organisation together.

Two jobs live here:

1. `update_ahl()` — renames every affiliate to its 2026 club and CREATES the two that never
   existed (Coachella Valley for Seattle, Henderson for Vegas) out of the spare created-team
   records.  Spare records are ideal: their placeholder strings are `****************`, a
   34-byte slot that happens to be exactly big enough for the longest city name we need.

2. `set_lower_nickname()` — the team record's `+0xB4` lowercase-nickname string.  A cloned
   expansion team inherits the DONOR's, so Seattle and Vegas both read "ducks".

## The string pool, and why this needs an allocator

Team display strings are null-terminated UTF-16BE, tightly packed, and reached through a
**self-relative** pointer (`target = field_offset + value - 1`).  Because the pointer can name
any offset, a rename is not limited to the original slot — but the pool has no slack, so a
longer name has to come from somewhere.  Two sources, both genuinely dead:

  * an affiliate's own city/nick string, once every reference to it has been repointed
  * the placeholder strings of the unused spare records (83..90) — 76 bytes each

A slot is only reclaimed when it is referenced by exactly one team-record field AND no
self-relative pointer anywhere else in the file resolves to it.  That second test is the
important one: the three-letter CODE strings each have ~4 external references, which is why
codes are rewritten **in place** (3 chars into an 8-byte slot) and never moved.

Identical text is shared rather than duplicated — "Toronto" already exists, so the Marlies
just point at it.  That is how the pool is built in the first place.
"""
from __future__ import annotations
import shutil
import struct
from pathlib import Path

import team_order as TO

BACKUP_SUFFIX = ".ahlbak"

# league word: league << 28 | division << 24 | <per-team bytes>.  AHL divisions are 6..9.
AHL_LEAGUE_WORD = 0x18060000

# parent asset key -> (affiliate city, 3-letter code).  theahl.com/nhl-affiliations, 2026.
# Keyed on the ASSET key, not the display code, so relocations (WPG keys on ATL, UTA on PHO)
# resolve without a special case — see extra_teams.py for the same rule.
AHL_2026 = {
    "ANA": ("San Diego",        "SD"),    # Gulls
    "BOS": ("Providence",       "PRO"),   # Bruins
    "BUF": ("Rochester",        "ROC"),   # Americans
    "CGY": ("Calgary",          "CWR"),   # Wranglers
    "CAR": ("Chicago",          "CHW"),   # Wolves
    "CHI": ("Rockford",         "RFD"),   # IceHogs
    "COL": ("Colorado",         "COE"),   # Eagles
    "CBJ": ("Cleveland",        "CLE"),   # Monsters
    "DAL": ("Texas",            "TEX"),   # Stars
    "DET": ("Grand Rapids",     "GR"),    # Griffins
    "EDM": ("Bakersfield",      "BAK"),   # Condors
    "FLA": ("Charlotte",        "CLT"),   # Checkers
    "LAK": ("Ontario",          "ONT"),   # Reign
    "MIN": ("Iowa",             "IA"),    # Wild
    "MTL": ("Laval",            "LAV"),   # Rocket
    "NSH": ("Milwaukee",        "MIL"),   # Admirals
    "NJD": ("Utica",            "UTC"),   # Comets
    "NYI": ("Hamilton",         "HAM"),   # Hammers
    "NYR": ("Hartford",         "HFD"),   # Wolf Pack
    "OTT": ("Belleville",       "BEL"),   # Senators
    "PHI": ("Lehigh Valley",    "LV"),    # Phantoms
    "PIT": ("Wilkes-Barre",     "WBS"),   # Penguins
    "SEA": ("Coachella Valley", "CV"),    # Firebirds   -- NEW
    "SJS": ("San Jose",         "SJB"),   # Barracuda
    "STL": ("Springfield",      "SPR"),   # Thunderbirds
    "TBL": ("Syracuse",         "SYR"),   # Crunch
    "TOR": ("Toronto",          "MRL"),   # Marlies
    "PHO": ("Tucson",           "TUC"),   # Roadrunners (Utah keys on PHO)
    "VAN": ("Abbotsford",       "ABB"),   # Canucks
    "VGK": ("Henderson",        "HSK"),   # Silver Knights -- NEW
    "WSH": ("Hershey",          "HER"),   # Bears
    "ATL": ("Manitoba",         "MB"),    # Moose (Winnipeg keys on ATL)
}

ROSTER_SIZE = 20                # players to stock a brand-new affiliate with
OFF_BACKPTR = 0x190             # player record -> its team record, +8
PLAYER_CHUNK = 0x1E159C31
PLAYER_STRIDE = 420
OFF_LAST, OFF_FIRST = 0x17C, 0x180
PLAYER_ROSTER_BIAS = OFF_LAST   # a roster pointer names the player's SURNAME field, not the row


class AffiliateError(RuntimeError):
    pass


# ── strings ─────────────────────────────────────────────────────────────────────

def _read_str(d, off):
    e = d.find(b"\x00\x00", off)
    if e < 0:
        return ""
    if (e - off) % 2:
        e += 1
    try:
        return d[off:e].decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _enc(text):
    return text.encode("utf-16-be") + b"\x00\x00"


class Pool:
    """Free-space allocator over dead string slots, plus a reuse index by exact text."""

    def __init__(self, d, base, count):
        self.d, self.base, self.count = d, base, count
        self.refs = {}              # string offset -> [(record, field name)]
        for i in range(count):
            o = base + i * TO.STRIDE
            for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK), ("code", TO.OFF_CODE),
                          ("lower", TO.OFF_LOWER), ("akey", TO.OFF_AKEY)):
                t = TO._target(d, o + f)
                if t is not None:
                    self.refs.setdefault(t, []).append((i, nm))
        self.starts = sorted(self.refs)
        self.extern, self.foreign = self._scan_extern()
        self.by_text = {}
        for t in self.starts:
            self.by_text.setdefault(_read_str(d, t), t)
        self.free = []              # [(offset, length)], kept merged and sorted
        self.reserve = []           # same, but only drawn on when `free` runs dry
        self.sacrificed = set()     # records whose slot the reserve tier actually gave up

    def _scan_extern(self):
        """One pass over every self-relative pointer in the file, returning two things.

        `extern` — how many pointers OUTSIDE the team table resolve to each of OUR strings
        (a string with any of these can never be moved or reclaimed).

        `foreign` — the start of every OTHER string living in the same pool.  The pool is not
        ours alone: arena names are reached through `+0xCC` -> arena record -> `+0` -> name,
        coach names through `+0xD8`, and neither is a team-record field, so neither shows up
        in `self.refs`.  Without them the allocator is blind exactly where it must not be:
        `capacity()` would run a reclaimed slot straight through the next arena name, and
        `zero_gaps()` would read an invisible string's `00 00` terminator as free space and
        eat it (that is how "TD Garden" once became "TD GardenMRL").
        """
        d, lo, hi = self.d, self.base, self.base + self.count * TO.STRIDE
        want = set(self.starts)
        plo, phi = self.starts[0], self.starts[-1]
        phi = d.find(b"\x00\x00", phi) + 2
        out, foreign = {}, set()
        for off in range(0, len(d) - 4, 4):
            v = struct.unpack_from(">I", d, off)[0]
            if not v:
                continue
            sv = v - (1 << 32) if v & 0x80000000 else v
            t = off + sv - 1
            if t in want:
                if not (lo <= off < hi):
                    out[t] = out.get(t, 0) + 1
            elif plo <= t < phi and (t - plo) % 2 == 0 and d[t] == 0 and 32 <= d[t + 1] < 127:
                foreign.add(t)          # looks like the head of a UTF-16BE string in our pool
        return out, foreign

    def capacity(self, off):
        """Bytes usable at `off` — up to the next string of ANY kind, ours or foreign."""
        k = self.starts.index(off)
        nxt = self.starts[k + 1] if k + 1 < len(self.starts) else None
        for t in self.foreign:
            if t > off and (nxt is None or t < nxt):
                nxt = t
        return (nxt - off) if nxt else 0

    def is_dead(self, off, owner):
        """True if `off` is referenced by exactly `owner` (one record/field) and nothing else."""
        return self.refs.get(off) == [owner] and not self.extern.get(off)

    def release(self, off, reserve=False, owner=None):
        """Mark a dead string slot reusable.  `reserve=True` puts it in the fallback tier."""
        lst = self.reserve if reserve else self.free
        lst.append((off, self.capacity(off), {owner}))
        lst.sort(key=lambda e: e[0])
        merged = []
        for o, n, ow in lst:
            if merged and merged[-1][0] + merged[-1][1] == o:
                merged[-1] = (merged[-1][0], merged[-1][1] + n, merged[-1][2] | ow)
            else:
                merged.append((o, n, set(ow)))
        if reserve:
            self.reserve = merged
        else:
            self.free = merged

    def zero_gaps(self, min_len=6):     # 6 == the shortest useful string: 2 chars + terminator
        """Runs of zero bytes inside the string pool that no live string reaches into.

        The pool is otherwise packed solid, but a handful of small holes survive between
        strings.  They are the only free space in the file that costs nothing at all to
        spend — no record gives anything up for it.
        """
        d = self.d
        lo, hi = self.starts[0], self.starts[-1]
        hi = d.find(b"\x00\x00", hi) + 2
        covered = bytearray(hi - lo)
        for s in sorted(set(self.starts) | self.foreign):    # foreign = arena/coach names
            e = d.find(b"\x00\x00", s)
            if e < 0:
                continue
            e += 2 if (e - s) % 2 == 0 else 3
            covered[s - lo:e - lo] = b"\x01" * (e - s)
        out, run = [], None
        for i in range(hi - lo):
            free = not covered[i] and d[lo + i] == 0
            if free and run is None:
                run = i
            elif not free and run is not None:
                if i - run >= min_len:
                    out.append((lo + run, i - run))
                run = None
        if run is not None and (hi - lo) - run >= min_len:
            out.append((lo + run, (hi - lo) - run))
        # a string has to start on the pool's 2-byte grid
        out = [(o + ((o - lo) & 1), n - ((o - lo) & 1)) for o, n in out]
        safe = []
        for o, n in out:
            # A gap that opens right after a NON-zero byte pair is not free: those first two
            # bytes are the terminator of the string that ends there, and writing over them
            # runs it into whatever we allocate ("TD Garden" + "MRL" -> "TD GardenMRL").
            # `covered` already protects every string we can see; this catches the ones we
            # can't -- a name reached by a pointer shape this module never learned about.
            if o >= 2 and d[o - 2:o] != b"\x00\x00":
                o, n = o + 2, n - 2
            if n >= min_len:
                safe.append((o, n))
        return safe

    def reuse(self, text):
        """An existing pool offset holding exactly `text`, or None.

        Never hands back an offset that has since been reclaimed — that string is about to be
        overwritten by whatever gets allocated on top of it.
        """
        off = self.by_text.get(text)
        if off is None:
            return None
        if any(o <= off < o + n for o, n, _ in self.free + self.reserve):
            return None
        return off

    def alloc(self, buf, text):
        """Write `text` into free space and return its offset.

        Reclaimed affiliate slots are spent first; the spare created-team records are only
        raided once those run out, because the game writes a created team's name into that
        record's slot at a fixed offset — spending one costs the user a usable team slot.
        """
        blob = _enc(text)
        for lst in (self.free, self.reserve):
            # best fit, not first fit: the pool is a scatter of 14-36 byte holes and a couple
            # of the names are long, so spending a big run on a short name is how you end up
            # having to raid the reserve for the one string that no longer fits anywhere.
            fits = [k for k, e in enumerate(lst) if e[1] >= len(blob)]
            # best fit within the reclaimed tier; within the reserve, fit is irrelevant — take
            # the HIGHEST spare record, the last slot the create-a-team screen would hand out.
            order = (lambda k: (lst[k][1], k)) if lst is self.free else \
                    (lambda k: (-max(lst[k][2], default=0), lst[k][1]))
            for k in sorted(fits, key=order)[:1]:
                o, n, ow = lst[k]
                buf[o:o + len(blob)] = blob
                if lst is self.reserve:
                    self.sacrificed |= {x for x in ow if x is not None}
                lst[k] = (o + len(blob), n - len(blob), ow)
                if not lst[k][1]:
                    lst.pop(k)
                self.by_text.setdefault(text, o)
                return o
        raise AffiliateError(
            f"no free string space for {text!r} ({len(blob)} bytes); largest free slot is "
            f"{max([n for _, n, _ in self.free + self.reserve], default=0)}")


# ── the table ───────────────────────────────────────────────────────────────────

def _player_base(d):
    n = TO._u32(d, 8)
    for i in range(min(n, 64)):
        h, cnt, off = struct.unpack_from(">III", d, 0x0C + i * 12)
        if h == PLAYER_CHUNK and cnt > 1000:
            return off + 0x47, cnt
    raise AffiliateError("no player chunk in this Roster.ROS")


def _org_map(d, base, count):
    """{league-0 record -> affiliate record or None}, via the FARM pointer.

    A CLONED expansion team inherits the donor's FARM pointer, so Seattle and Vegas both claim
    Anaheim's Iowa.  The tie-break is the asset key: an affiliate carries its parent's, so the
    club that really owns Iowa is the one whose key Iowa is wearing.  Anything else gets None
    and is treated as needing an affiliate built for it.
    """
    end = base + count * TO.STRIDE
    out = {}
    for i in TO.league0_slots(d, base, count):
        o = base + i * TO.STRIDE
        farm = TO._target(d, o + TO.OFF_FARM)
        if farm is None or not (base <= farm < end):
            out[i] = None
            continue
        f = (farm - 8 - base) // TO.STRIDE
        if f == i or TO._text(d, base + f * TO.STRIDE + TO.OFF_AKEY).upper() != \
                TO._text(d, o + TO.OFF_AKEY).upper():
            out[i] = None
            continue
        out[i] = f
    return out


def _spare_records(d, base, count):
    """Unused created-team records — no players and a placeholder name."""
    out = []
    for i in range(count):
        o = base + i * TO.STRIDE
        if any(TO._u32(d, o + f) for f in TO.OFF_PLAYERS):
            continue
        if set(TO._text(d, o + TO.OFF_CITY)) <= {"*"} and TO._text(d, o + TO.OFF_CITY):
            out.append(i)
    return out


def plan(ros_path):
    """What update_ahl() would do, without writing.  [(parent rec, code, affiliate rec or None,
    old city, new city, old code, new code, 'rename'|'create')]"""
    d = Path(ros_path).read_bytes()
    base, count = TO.find_table(d)
    orgs = _org_map(d, base, count)
    rows = []
    for parent, aff in orgs.items():
        po = base + parent * TO.STRIDE
        akey = TO._text(d, po + TO.OFF_AKEY).upper()
        want = AHL_2026.get(akey)
        if want is None:
            continue
        if aff is None:
            rows.append((parent, TO._text(d, po + TO.OFF_CODE), None, "", want[0], "", want[1],
                         "create"))
        else:
            ao = base + aff * TO.STRIDE
            rows.append((parent, TO._text(d, po + TO.OFF_CODE), aff,
                         TO._text(d, ao + TO.OFF_CITY), want[0],
                         TO._text(d, ao + TO.OFF_CODE), want[1], "rename"))
    return rows


def update_ahl(ros_path, lower=None, backup=True, log=print) -> int:
    """Rename every AHL affiliate to its 2026 club and create the missing ones.

    `lower` — optional {team CODE: lowercase nickname} written to `+0xB4` in the same pass.
    It rides along here rather than standing alone so that the space left over from the
    affiliate rename pays for it, instead of a spare team slot.

    Returns the number of affiliate records written.  Writes in place; the file size never
    changes (new strings come out of reclaimed slots, never out of the tail).
    """
    ros_path = Path(ros_path)
    old = ros_path.read_bytes()
    buf = bytearray(old)
    base, count = TO.find_table(old)
    end = base + count * TO.STRIDE
    pool = Pool(old, base, count)
    orgs = _org_map(old, base, count)
    spares = [s for s in _spare_records(old, base, count)]

    # ── decide, before touching anything ──────────────────────────────────────
    jobs = []                       # (parent, affiliate record, city, code, created?)
    for parent, aff in sorted(orgs.items()):
        akey = TO._text(old, base + parent * TO.STRIDE + TO.OFF_AKEY).upper()
        want = AHL_2026.get(akey)
        if want is None:
            log(f"  ! no 2026 affiliate known for asset key {akey!r} — record {parent} left alone")
            continue
        created = aff is None
        if created:
            if not spares:
                raise AffiliateError("no spare team record left to build an affiliate in")
            aff = spares.pop(0)
        jobs.append((parent, aff, want[0], want[1], created))

    aff_recs = {a for _, a, _, _, _ in jobs}
    if len(aff_recs) != len(jobs):
        raise AffiliateError("two organisations resolved to the same affiliate record")

    # ── strings: reuse first, then reclaim, then allocate ─────────────────────
    wanted = {}                     # (record, field) -> text
    for parent, aff, city, _code, _new in jobs:
        # the shipping nick is keyed on the ASSET key, not the display code — Los Angeles'
        # affiliate reads "LAK Minors".  Following that keeps 4 strings reusable as-is.
        pkey = TO._text(old, base + parent * TO.STRIDE + TO.OFF_AKEY)
        wanted[(aff, "city")] = city
        wanted[(aff, "nick")] = f"{pkey} Minors"

    lower_recs = {}
    for code, text in (lower or {}).items():
        hits = [i for i in range(count)
                if TO._text(old, base + i * TO.STRIDE + TO.OFF_CODE).upper() == code.upper()]
        if not hits:
            raise AffiliateError(f"no team record with code {code!r}")
        for i in hits:
            lower_recs[i] = text
            wanted[(i, "lower")] = text

    pinned = {pool.reuse(t) for t in wanted.values()}
    pinned.discard(None)
    # every string the affiliates are about to stop using, plus the leftover spares
    # A record built out of a spare keeps its OWN placeholder slots — they are 34-byte
    # blocks, the roomiest in the pool, and letting the general allocator spend them on some
    # other club's name is what forces the reserve to be raided for the new club's own.
    preassign = {}
    for _parent, aff, _city, _code, created in jobs:
        if not created:
            continue
        for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK)):
            t = TO._target(old, base + aff * TO.STRIDE + f)
            if t is not None and t not in pinned and pool.is_dead(t, (aff, nm)) \
                    and pool.capacity(t) >= len(_enc(wanted[(aff, nm)])):
                preassign[(aff, nm)] = t

    for _parent, aff, _city, _code, created in jobs:
        for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK)):
            if (aff, nm) in preassign:
                continue
            t = TO._target(old, base + aff * TO.STRIDE + f)
            if t is not None and t not in pinned and pool.is_dead(t, (aff, nm)):
                pool.release(t, owner=aff)
    for o, n in pool.zero_gaps():   # holes between strings — free for nothing
        pool.free.append((o, n, set()))
    pool.free.sort(key=lambda e: e[0])
    for s in spares:                # spare records we did NOT consume — fallback tier only
        for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK), ("code", TO.OFF_CODE)):
            t = TO._target(old, base + s * TO.STRIDE + f)
            if t is not None and t not in pinned and pool.is_dead(t, (s, nm)):
                pool.release(t, reserve=True, owner=s)
    # spend the LAST spare first: the create-a-team screen hands out the lowest free slot, so
    # a name written into the highest one is the least likely to ever be overwritten.
    pool.reserve.sort(key=lambda e: -max(e[2]))
    log(f"  reclaimed {sum(n for _, n, _ in pool.free)} bytes from the affiliate block, "
        f"{sum(n for _, n, _ in pool.reserve)} held in reserve")

    resolved = {}
    for key, off in preassign.items():
        blob = _enc(wanted[key])
        buf[off:off + pool.capacity(off)] = blob.ljust(pool.capacity(off), b"\x00")
        pool.by_text.setdefault(wanted[key], off)
        resolved[key] = off
    for key, text in sorted(wanted.items(), key=lambda kv: -len(kv[1])):   # longest first
        if key in resolved:
            continue
        off = pool.reuse(text)
        resolved[key] = off if off is not None else pool.alloc(buf, text)

    # ── free-agent rows for brand-new affiliates ──────────────────────────────
    pbase, pcount = _player_base(old)
    on_roster = set()
    for i in range(count):
        o = base + i * TO.STRIDE
        for f in TO.OFF_PLAYERS:
            t = TO._target(old, o + f)
            if t is not None:
                on_roster.add(t)
    # Stock a new affiliate from the BOTTOM of the free-agent pool.  The pool is roughly
    # ordered by pedigree — its head is Hall/Kessel/Trouba, its depths are the AHL-caliber
    # filler an affiliate should actually be made of — and its tail is unnamed blank rows.
    named = [TO._target(old, pbase + i * PLAYER_STRIDE + OFF_LAST) for i in range(pcount)
             if pbase + i * PLAYER_STRIDE + PLAYER_ROSTER_BIAS in on_roster]
    named = [x for x in named if x]
    lo, hi = min(named), max(named)     # where real player names live

    spare_players = []
    for i in reversed(range(pcount)):
        row = pbase + i * PLAYER_STRIDE
        if row + PLAYER_ROSTER_BIAS in on_roster:
            continue
        t = TO._target(old, row + OFF_LAST)
        last = TO._text(old, row + OFF_LAST)
        # a blank created-player slot, or a stale row whose name pointer has drifted out of
        # the player-name pool entirely (a few point into the TEAM strings — "WPG", "Manitoba")
        if t is None or not lo <= t <= hi or not last or set(last) <= {"*"} \
                or not last[0].isalpha():
            continue
        spare_players.append(row)

    # ── write ─────────────────────────────────────────────────────────────────
    def put_ptr(off, target):
        struct.pack_into(">I", buf, off, TO._selfrel(target, off))

    written = 0
    for parent, aff, city, code, created in jobs:
        po, ao = base + parent * TO.STRIDE, base + aff * TO.STRIDE
        if created:
            log(f"  + record {aff}: {city} ({code}) — new affiliate for "
                f"{TO._text(old, po + TO.OFF_CODE)}")
            struct.pack_into(">I", buf, ao + TO.OFF_LEAGUE, AHL_LEAGUE_WORD)
            struct.pack_into(">H", buf, ao + 0xF0,
                             struct.unpack_from(">H", old, po + 0xF0)[0])
            put_ptr(ao + TO.OFF_ARENA, TO._target(old, po + TO.OFF_ARENA))
            put_ptr(ao + TO.OFF_MISC, TO._target(old, po + TO.OFF_MISC))
            for k, f in enumerate(TO.OFF_PLAYERS):
                if k < ROSTER_SIZE and spare_players:
                    row = spare_players.pop(0)
                    put_ptr(ao + f, row + PLAYER_ROSTER_BIAS)
                    put_ptr(row + OFF_BACKPTR, ao + 8)
                else:
                    struct.pack_into(">I", buf, ao + f, 0)
        # the organisation's pointer pair — both records carry the same two
        put_ptr(po + TO.OFF_PRO, po + 8)
        put_ptr(po + TO.OFF_FARM, ao + 8)
        put_ptr(ao + TO.OFF_PRO, po + 8)
        put_ptr(ao + TO.OFF_FARM, ao + 8)
        # asset key: an affiliate draws the parent's art
        put_ptr(ao + TO.OFF_AKEY, TO._target(old, po + TO.OFF_AKEY))
        put_ptr(ao + TO.OFF_CITY, resolved[(aff, "city")])
        put_ptr(ao + TO.OFF_NICK, resolved[(aff, "nick")])
        # CODE prefers an IN-PLACE rewrite: pointers from outside the team table name these
        # strings, and an affiliate's code sits in an 8-byte slot that always fits 3 chars.
        # But only when this record's code field is the string's SOLE team-table reference —
        # several affiliate codes are the very same string as another record's ASSET KEY
        # (Rockford's code "CHI" is Chicago's key), and rewriting those in place renames the
        # other record too.  A shared string is repointed instead, which leaves every other
        # reference — team-table or external — looking at the untouched original.
        cto = TO._target(old, ao + TO.OFF_CODE)
        blob = _enc(code)
        if cto is not None and pool.refs.get(cto) == [(aff, "code")]:
            if pool.capacity(cto) < len(blob):
                raise AffiliateError(f"record {aff}: no room to write code {code!r} in place")
            buf[cto:cto + len(blob)] = blob
            buf[cto + len(blob):cto + pool.capacity(cto)] = \
                b"\x00" * (pool.capacity(cto) - len(blob))
        else:
            off = pool.reuse(code)
            put_ptr(ao + TO.OFF_CODE, off if off is not None else pool.alloc(buf, code))
        written += 1

    for rec, text in lower_recs.items():
        o = base + rec * TO.STRIDE
        was = TO._text(old, o + TO.OFF_LOWER)
        put_ptr(o + TO.OFF_LOWER, resolved[(rec, "lower")])
        log(f"  {TO._text(old, o + TO.OFF_CODE)}: lowercase nickname {was!r} -> {text!r}")

    if len(buf) != len(old):
        raise AffiliateError("internal error: file size changed")

    # Nothing outside the affiliate block may have changed name.  Strings are shared across
    # records, so a careless in-place rewrite renames a bystander — this is the net that
    # catches it before the file is written.
    new = bytes(buf)
    touched = {a for _, a, _, _, _ in jobs} | pool.sacrificed
    if pool.sacrificed:
        log(f"  ! ran short of reclaimed space — spare team slot(s) "
            f"{sorted(pool.sacrificed)} gave up their placeholder name")
    for i in range(count):
        if i in touched:
            continue
        o = base + i * TO.STRIDE
        for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK), ("code", TO.OFF_CODE),
                      ("akey", TO.OFF_AKEY), ("lower", TO.OFF_LOWER)):
            if nm == "lower" and i in lower_recs:
                continue
            if TO._text(new, o + f) != TO._text(old, o + f):
                raise AffiliateError(
                    f"verify failed: record {i} {nm} changed from "
                    f"{TO._text(old, o + f)!r} to {TO._text(new, o + f)!r}")
    for i in TO.league0_slots(new, base, count):
        o = base + i * TO.STRIDE
        if [TO._target(new, o + f) for f in TO.OFF_PLAYERS] != \
           [TO._target(old, o + f) for f in TO.OFF_PLAYERS]:
            raise AffiliateError(f"verify failed: record {i}'s roster changed")

    if backup:
        bak = ros_path.with_suffix(BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copyfile(ros_path, bak)
            log(f"  backup -> {bak.name}")
    ros_path.write_bytes(new)
    log(f"  {written} affiliate(s) updated")
    return written


# ── repairing a pool the old allocator damaged ──────────────────────────────────

def find_overlaps(ros_path) -> list:
    """Strings that were written on top of another string's terminator.

    The allocator used to be blind to every string in the pool that is not a team-record
    field — arena names, coach names — so it could hand out the six zero bytes at the end of
    one as free space.  The victim then reads straight through into its new neighbour
    ("TD Garden" + "MRL" = "TD GardenMRL").  This finds any that already happened.

    Only strings THIS module can write are reported as intruders: `pool.foreign` is a pointer
    scan, and a 4-byte-aligned value landing mid-name is common enough that it would otherwise
    bury the real hits under a few hundred fake ones ("Grabovski" "overrun" by "bovski").
    A false foreign start only ever makes the allocator more conservative, so it costs nothing
    there — but here it has to be filtered out.

    Returns [{'victim', 'victim_text', 'intruder', 'intruder_text', 'refs', 'movable'}].
    """
    d = bytearray(Path(ros_path).read_bytes())
    base, count = TO.find_table(d)
    pool = Pool(d, base, count)
    starts = sorted(set(pool.starts) | pool.foreign)
    out = []
    for a, b in zip(starts, starts[1:]):
        e = d.find(b"\x00\x00", a)
        if e < 0:
            continue
        if (e - a) % 2:
            e += 1
        if e >= b:                              # a runs into b: b is the intruder
            refs = pool.refs.get(b, [])
            if not refs:
                continue                        # foreign start — see the note above
            out.append({"victim": a, "victim_text": _read_str(d, a),
                        "intruder": b, "intruder_text": _read_str(d, b),
                        "refs": refs,
                        "movable": bool(refs) and not pool.extern.get(b)})
    return out


def repair_overlaps(ros_path, backup=True, log=print) -> int:
    """Move each overlapping string to real free space and give the victim its terminator back.

    Strictly in place: the intruder is re-written into a zero gap (or, failing that, a spare
    created-team slot, same fallback tier update_ahl uses), every team-record field pointing
    at it is repointed, and the bytes it vacated are zeroed — which is exactly what the victim
    was missing.  Returns the number of strings moved.
    """
    ros_path = Path(ros_path)
    old = ros_path.read_bytes()
    buf = bytearray(old)
    base, count = TO.find_table(old)
    bad = find_overlaps(ros_path)
    if not bad:
        log("  string pool is clean — no overlaps")
        return 0
    pool = Pool(old, base, count)
    for o, n in pool.zero_gaps():
        pool.free.append((o, n, set()))
    pool.free.sort(key=lambda e: e[0])
    for s in _spare_records(old, base, count):
        for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK), ("code", TO.OFF_CODE)):
            t = TO._target(old, base + s * TO.STRIDE + f)
            if t is not None and pool.is_dead(t, (s, nm)):
                pool.release(t, reserve=True, owner=s)
    pool.reserve.sort(key=lambda e: -max(e[2]))

    moved = 0
    for b in bad:
        if not b["movable"]:
            log(f"  ! {b['intruder_text']!r} @0x{b['intruder']:X} overruns "
                f"{b['victim_text']!r} but nothing here can repoint it — left alone")
            continue
        text, old_off = b["intruder_text"], b["intruder"]
        # sharing an identical string costs nothing at all; only fall through to the
        # allocator when there isn't one (and never "share" the copy we're moving)
        new_off = pool.reuse(text)
        if new_off is None or new_off == old_off or b["victim"] <= new_off <= old_off:
            new_off = pool.alloc(buf, text)
        for rec, nm in b["refs"]:
            f = {"city": TO.OFF_CITY, "nick": TO.OFF_NICK, "code": TO.OFF_CODE,
                 "lower": TO.OFF_LOWER, "akey": TO.OFF_AKEY}[nm]
            fo = base + rec * TO.STRIDE + f
            struct.pack_into(">I", buf, fo, TO._selfrel(new_off, fo))
        buf[old_off:old_off + len(_enc(text))] = b"\x00" * len(_enc(text))
        log(f"  {text!r}: 0x{old_off:X} -> 0x{new_off:X}; "
            f"{b['victim_text'][:len(b['victim_text']) - len(text)]!r} restored")
        moved += 1

    if pool.sacrificed:
        log(f"  spare created-team record(s) {sorted(pool.sacrificed)} gave up their "
            f"placeholder name to hold a moved string")
    if len(buf) != len(old):
        raise AffiliateError("refusing to write: roster size changed")
    if backup:
        bak = ros_path.with_suffix(".poolbak")
        if not bak.exists():
            shutil.copyfile(ros_path, bak)
            log(f"  backup -> {bak.name}")
    ros_path.write_bytes(buf)
    left = find_overlaps(ros_path)
    if left:
        raise AffiliateError(f"{len(left)} overlap(s) still present after repair")
    log(f"  {moved} string(s) moved; pool verifies clean")
    return moved


def revert(ros_path, log=print) -> bool:
    """Put back the roster as it was before the first affiliate update."""
    ros_path = Path(ros_path)
    bak = ros_path.with_suffix(BACKUP_SUFFIX)
    if not bak.exists():
        return False
    shutil.copyfile(bak, ros_path)
    bak.unlink()
    log("  affiliates reverted")
    return True


# ── the lowercase nickname (+0xB4) ──────────────────────────────────────────────

def shared_nicknames(ros_path) -> dict:
    """{team CODE: the lowercase nickname it SHOULD have} for league teams sharing one.

    A cloned expansion team points at the donor's string, so Seattle and Vegas both read
    "ducks".  The convention is the nickname lowercased with the spaces taken out — "Maple
    Leafs" is stored as "mapleleafs" — so the right value can be derived rather than guessed.
    The donor keeps the string; the clones are the ones that need their own.
    """
    d = Path(ros_path).read_bytes()
    base, count = TO.find_table(d)
    l0 = TO.league0_slots(d, base, count)
    users = {}
    for i in l0:
        t = TO._target(d, base + i * TO.STRIDE + TO.OFF_LOWER)
        if t is not None:
            users.setdefault(t, []).append(i)
    out = {}
    for t, recs in users.items():
        if len(recs) < 2:
            continue
        for i in recs:
            o = base + i * TO.STRIDE
            want = TO._text(d, o + TO.OFF_NICK).lower().replace(" ", "")
            if want and want != TO._text(d, o + TO.OFF_LOWER):
                out[TO._text(d, o + TO.OFF_CODE)] = want
    return out



def set_lower_nickname(ros_path, wanted: dict, backup=True, log=print) -> int:
    """{team display CODE: lowercase nickname}.  Gives a cloned team its OWN string.

    An expansion team cloned from a donor keeps pointing at the donor's, so editing the
    donor's nickname would drag the clone along with it.
    """
    ros_path = Path(ros_path)
    old = ros_path.read_bytes()
    buf = bytearray(old)
    base, count = TO.find_table(old)
    pool = Pool(old, base, count)
    spares = _spare_records(old, base, count)
    for s in spares:
        for nm, f in (("city", TO.OFF_CITY), ("nick", TO.OFF_NICK), ("code", TO.OFF_CODE)):
            t = TO._target(old, base + s * TO.STRIDE + f)
            if t is not None and pool.is_dead(t, (s, nm)):
                pool.release(t, owner=s)

    n = 0
    for i in range(count):
        o = base + i * TO.STRIDE
        code = TO._text(old, o + TO.OFF_CODE).upper()
        if code not in wanted:
            continue
        text = wanted[code]
        off = pool.reuse(text)
        if off is None:
            off = pool.alloc(buf, text)
        was = TO._text(old, o + TO.OFF_LOWER)
        struct.pack_into(">I", buf, o + TO.OFF_LOWER, TO._selfrel(off, o + TO.OFF_LOWER))
        log(f"  {code}: lowercase nickname {was!r} -> {text!r} @ {off:#08x}")
        n += 1

    missing = set(wanted) - {TO._text(old, base + i * TO.STRIDE + TO.OFF_CODE).upper()
                             for i in range(count)}
    if missing:
        raise AffiliateError(f"no team record with code(s) {sorted(missing)}")
    if backup:
        bak = ros_path.with_suffix(BACKUP_SUFFIX)
        if not bak.exists():
            shutil.copyfile(ros_path, bak)
            log(f"  backup -> {bak.name}")
    ros_path.write_bytes(bytes(buf))
    return n
