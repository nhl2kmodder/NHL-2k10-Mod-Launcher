"""roster_move.py — move players between clubs in a Roster.ROS, on ALL THREE sides of the link.

Club membership is stored THREE times and the game reads the array, not the back-pointer:

    team record  +0x08..+0x70   27 self-relative slots -> each player's SURNAME field, 0-terminated
    player record +0x14         self-relative back-pointer -> that team record + 8
    chunk 0x7FADC8B2            the FREE-AGENT POOL: [u32 count][count self-relative pointers]

Writing only the back-pointer is the trap this module exists to avoid: every offline tool (and
roster_verify) reads +0x14, so a half-move looks completely fixed while the game still dresses the
old roster. The SEA/VGK repair on 2026-08-08 did exactly that before this module existed — the two
expansion clubs' arrays still listed Anaheim's rows.

The pool is the same trap one level up, and it bit us on 2026-08-17: a floor repair that signed
players out of spares() put 16 of them on a club WITHOUT taking them out of the pool, so they showed
up in free agency and on a roster at the same time. move()/release()/set_roster() now maintain the
pool themselves, which is the whole reason it lives in this module — see sign() and waive().

The array is 27 slots and **27 players is the hard cap** — measured, not assumed: BUF ships in this
save with every slot 0x08..0x70 filled, and `+0x74..+0xA4` is zero in all 88 records (the next live
field is the city pointer at +0xA8), so a 27th member has nothing after it to read as a 28th.
Moves that would overflow are refused, not truncated.

Offsets and the self-relative rule come from team_order.py; the player frame from player_assign.py.
"""
from __future__ import annotations
import struct
from pathlib import Path

try:
    from . import team_order as TO
    from .player_assign import PlayerTable, OFF_TEAM, OFF_LAST
except ImportError:
    import team_order as TO
    from player_assign import PlayerTable, OFF_TEAM, OFF_LAST

SLOTS = list(TO.OFF_PLAYERS)          # 0x08..0x70 step 4
MAX_ROSTER = len(SLOTS)               # 27 — BUF already fills every slot in the shipping save
TEAM_REC_BIAS = 8                     # a player's +0x14 targets team_record + 8, not + 0

OFF_COUNT = 0xE8                      # u8 roster size, and the field the game actually trusts.
# The 27-slot array is NOT self-describing. The game reads +0xE8 to know how many slots to walk, so
# a club whose stored count disagrees with its array SOFT-HANGS the roster load. Measured 2026-08-17
# against the emulator, one boot per hypothesis:
#   permute a club's array, count unchanged  -> LOADS   (slot order is free; the writer is sound)
#   swap two players between clubs, sizes equal -> LOADS (membership changes are free)
#   move one player, ANA 22->21, STL 23->24  -> HANGS
#   remove one player, ANA 22->21, nothing else -> HANGS
# and every saved file that loads has +0xE8 == len(members) for every club, while every file that
# hangs has at least one club where it does not. Two dormant records (CV, HSK) ship with 0 there and
# load fine, so only clubs you actually touch should be rewritten.
OFF_POS, POS_LO, POS_W = 0x40, 3, 3   # primary position, bits 3..5 of the u32 at +0x40
POS_GOALIE = 0

FA_CHUNK = "0x7FADC8B2"               # the free-agent pool: [u32 count][count self-relative ptrs]
# Pointers here target the player record START (+0x00), same convention as the team array.
# ⚠ The chunk is NOT only this array. It is 0x2264 bytes = 2200 slots, and slots 1801.. hold an
# unrelated structure; an early version that zeroed "to the end of the chunk" wrecked 365 of them.
# Everything between the count and that structure is zero in every save, so the array's real
# capacity is ~1800 — but it is MEASURED per file by _pool_capacity(), never hardcoded.
MIN_GOALIES = 2                       # see lineup_floor()
MIN_ROSTER = 20                       # 18 skaters + 2 goalies — see lineup_floor()


class MoveError(RuntimeError):
    pass


def _set_ptr(d, field_off, target):
    """Write a self-relative pointer, or 0 to clear the slot."""
    struct.pack_into(">i", d, field_off, 0 if target is None else target - field_off + 1)


class Roster:
    """The two-sided club/player membership graph of one Roster.ROS."""

    def __init__(self, path):
        self.t = PlayerTable(path)
        self.d = self.t.ros.data
        self.base, self.count = TO.find_table(self.d)
        self.rec = {}                                  # code -> record index
        for i in range(self.count):
            c = self._code(i)
            # A code can repeat (an affiliate reuses its parent's, e.g. CHI); the NHL clubs are
            # records 0..29, so the first hit in that range is the pro club.
            if c and c not in self.rec:
                self.rec[c] = i
        self._fa_chunk = next((c for c in self.t.ros.chunks
                               if c.name == FA_CHUNK and c.size), None)
        self._fa = self._read_pool()
        self._fa_cap = self._pool_capacity()

    # ── the free-agent pool ──────────────────────────────────────────────────
    def _pool_slot(self, k):
        return self._fa_chunk.foff + k * 4          # slot 0 is the count, entries start at 1

    def _pool_target(self, k):
        """Row addressed by slot k, or None if the slot is empty or not an aligned player."""
        o = self._pool_slot(k)
        v = struct.unpack_from(">i", self.d, o)[0]
        if not v:
            return None
        t = o + v - 1 - self.t.base
        return t // 420 if t >= 0 and t % 420 == 0 and t // 420 < self.t.nrec else None

    def _read_pool(self):
        if self._fa_chunk is None:
            return []
        n = struct.unpack_from(">I", self.d, self._fa_chunk.foff)[0]
        return [r for r in (self._pool_target(k) for k in range(1, n + 1)) if r is not None]

    def _pool_capacity(self):
        """How many entries the array can hold before it runs into the structure that follows.

        Measured per file rather than assumed: walk forward from the current count and stop at the
        first slot that is non-zero but does NOT decode as an aligned player pointer. In both the
        pre-sync and post-sync saves that lands on slot 1801, with 446..1800 all zero."""
        if self._fa_chunk is None:
            return 0
        slots = (self._fa_chunk.size - 4) // 4
        n = struct.unpack_from(">I", self.d, self._fa_chunk.foff)[0]
        for k in range(n + 1, slots + 1):
            if struct.unpack_from(">i", self.d, self._pool_slot(k))[0] and self._pool_target(k) is None:
                return k - 1
        return slots

    def _flush_pool(self):
        """Rewrite the array in place. Clears only as far as the count we found on load — never
        past it, or the structure at slot 1801 gets zeroed (see FA_CHUNK)."""
        if self._fa_chunk is None:
            return
        was = struct.unpack_from(">I", self.d, self._fa_chunk.foff)[0]
        struct.pack_into(">I", self.d, self._fa_chunk.foff, len(self._fa))
        for k, row in enumerate(self._fa, start=1):
            o = self._pool_slot(k)
            struct.pack_into(">i", self.d, o, self.t.base + row * 420 - o + 1)
        for k in range(len(self._fa) + 1, max(was, len(self._fa)) + 1):
            struct.pack_into(">i", self.d, self._pool_slot(k), 0)

    def free_agents(self) -> list[int]:
        """Rows the game offers in free agency, in pool order."""
        return list(self._fa)

    def is_free_agent(self, row) -> bool:
        return row in self._fa

    def sign(self, row) -> bool:
        """Take a player OUT of the pool. Called for you by move() and set_roster()."""
        if row not in self._fa:
            return False
        self._fa = [r for r in self._fa if r != row]
        self._flush_pool()
        return True

    def waive(self, row) -> bool:
        """Put an unrostered player INTO the pool. Called for you by release() and set_roster()."""
        if row in self._fa:
            return False
        if len(self._fa) >= self._fa_cap:
            raise MoveError(f"the free-agent pool is full ({self._fa_cap} entries); writing past it "
                            f"would overwrite the structure that follows it in chunk {FA_CHUNK}")
        self._fa.append(row)
        self._flush_pool()
        return True

    # ── reading ──────────────────────────────────────────────────────────────
    def _rec_off(self, i):
        return self.base + i * TO.STRIDE

    def _code(self, i):
        t = TO._target(self.d, self._rec_off(i) + TO.OFF_CODE)
        if t is None:
            return ""
        e = self.d.find(b"\x00\x00", t)
        return self.d[t:e].decode("utf-16-be", errors="replace") if e > 0 else ""

    def members(self, i) -> list[int]:
        """Player ROWS on record i, in array order (stops at the terminator)."""
        out = []
        for f in SLOTS:
            t = TO._target(self.d, self._rec_off(i) + f)
            if t is None:
                break
            out.append((t - self.t.base) // 420)
        return out

    def row_offset(self, row) -> int:
        return self.t.base + row * 420 + OFF_LAST

    # ── writing ──────────────────────────────────────────────────────────────
    def _write_members(self, i, rows):
        if len(rows) > MAX_ROSTER:
            raise MoveError(f"record {i} ({self._code(i)}) would hold {len(rows)} players; "
                            f"the array only has room for {MAX_ROSTER}")
        o = self._rec_off(i)
        for f, row in zip(SLOTS, rows):
            _set_ptr(self.d, o + f, self.row_offset(row))
        for f in SLOTS[len(rows):]:                    # terminate and clear the tail
            _set_ptr(self.d, o + f, None)
        self.d[o + OFF_COUNT] = len(rows)              # the array is NOT self-describing — see below

    def set_roster(self, i, rows):
        """Replace record i's array wholesale and repoint every listed player's back-pointer.

        Anyone dropped who ends up on no club at all becomes a free agent, which is what he now is;
        if a later move() picks him up, that move signs him back out of the pool."""
        dropped = [r for r in self.members(i) if r not in rows]
        self._write_members(i, rows)
        for row in rows:
            self.set_team_of(row, i)
            self.sign(row)
        for row in dropped:
            if not self.clubs_of(row):
                _set_ptr(self.d, self.t.base + row * 420 + OFF_TEAM, None)
                self.waive(row)

    def set_team_of(self, row, i):
        _set_ptr(self.d, self.t.base + row * 420 + OFF_TEAM, self._rec_off(i) + TEAM_REC_BIAS)

    def farm_of(self, i):
        """This club's AHL affiliate record, read from +0xD4 (never assume 30+i — the expansion
        work inserted SEA/VGK into the pro block, so the stock offset no longer holds)."""
        t = TO._target(self.d, self._rec_off(i) + 0xD4)
        if t is None:
            return None
        return (t - TEAM_REC_BIAS - self.base) // TO.STRIDE

    def clubs_of(self, row) -> list[int]:
        """Every record whose array lists this row — normally one, more than one is a defect."""
        return [i for i in range(self.count) if row in self.members(i)]

    def release(self, row, to_pool=True):
        """Take a player off every roster and put him in the free-agent pool.

        `to_pool=False` leaves him in neither structure — an orphan record, which is the old
        behaviour of this method and what you want for a redundant DUPLICATE of someone who is
        already rostered under another row."""
        for i in range(self.count):
            m = self.members(i)
            if row in m:
                self._write_members(i, [r for r in m if r != row])
        _set_ptr(self.d, self.t.base + row * 420 + OFF_TEAM, None)
        if to_pool:
            self.waive(row)

    def move(self, row, dst_rec):
        """Move one player: out of whatever array holds him, into dst's, back-pointer follows.

        Signing him out of the free-agent pool is part of the move — the pool is the third place
        membership lives, and forgetting it is what put 16 players in free agency AND on a club."""
        for i in range(self.count):
            m = self.members(i)
            if row in m and i != dst_rec:
                self._write_members(i, [r for r in m if r != row])
        m = self.members(dst_rec)
        if row not in m:
            self._write_members(dst_rec, m + [row])
        self.set_team_of(row, dst_rec)
        self.sign(row)

    def save(self, backup_suffix=".movebak", log=print, force=False):
        """Write the save back, refusing to ship a roster the game would hang on.

        The lineup floor is checked HERE rather than in each caller because the callers are one-off
        sync scripts: the promotion that empties a farm club's crease looks completely correct at the
        point it is written, and only the finished file shows the club can no longer dress a side."""
        short = self.lineup_floor()
        if short and not force:
            detail = ", ".join(f"{c or f'rec {i}'}: {why}" for i, c, g, n, why in short)
            raise MoveError(
                f"{len(short)} club(s) cannot dress a lineup ({MIN_ROSTER} players incl. "
                f"{MIN_GOALIES} goalies): {detail}. The game soft-hangs loading a roster like this. "
                f"Backfill from spares() (currently {len(self.spares())} unrostered players, "
                f"{len(self.spare_goalies())} of them goalies), or pass force=True.")
        p = Path(self.t.ros.path)
        bak = Path(str(p) + backup_suffix)
        if not bak.exists():
            bak.write_bytes(p.read_bytes())
        self.t.save(backup=False)
        if log:
            log(f"wrote {p} (backup {bak.name})")

    # ── consistency ──────────────────────────────────────────────────────────
    def position_of(self, row) -> int:
        """Primary position code (0=G 1=D 2=LW 3=RW 4=C) — bits 3..5, NOT bits 0..2.

        +0x40 carries TWO position fields; bits 0..2 are the SECONDARY position and agree with the
        primary for every goalie and defenceman, so reading the wrong one looks fine until a forward
        turns up. Only bits 3..5 are what the game dresses a player as."""
        v = struct.unpack_from(">I", self.d, self.t.base + row * 420 + OFF_POS)[0]
        return (v >> POS_LO) & ((1 << POS_W) - 1)

    def lineup_floor(self, min_players=MIN_ROSTER, min_goalies=MIN_GOALIES) -> list[tuple]:
        """Clubs that cannot dress a lineup — (record, code, goalies, size, reason).

        **A club that cannot dress 20 players including 2 goalies SOFT-HANGS the game on roster
        load.** 20 is a dressed NHL lineup: 18 skaters + 2 goalies. Both halves are one invariant —
        fixing only the goalie count is not enough, and that mistake cost a whole debugging session.

        Measured, not assumed: across four known-good saves (the shipped .bak, .orderbak, .fixbak and
        .movebak) the minimum club size is EXACTLY 20 and the minimum goalie count EXACTLY 2, with
        zero clubs below either. The first save that hung had clubs at 17/19 players and five at one
        goalie. Note the floor is a floor on SHAPE, not on depth: clubs with 5 D or 11 F ship fine, so
        do not "fix" positional balance.

        Empty clubs are skipped — a record with no players is dormant, not broken.

        How it breaks is never a bad write, it is a legitimate promotion: pulling an AHL goalie up to
        his parent club (Greaves -> CBJ, Korpisalo BOS -> NYR) leaves the farm club short and nothing
        backfills it. On 2026-08-16 an NHL.com sync left 11 clubs under 2 goalies and 22 under 20
        players that way. Call this after any batch of moves."""
        short = []
        for i in range(self.count):
            m = self.members(i)
            if not m:
                continue
            g = sum(1 for row in m if self.position_of(row) == POS_GOALIE)
            why = []
            if len(m) < min_players:
                why.append(f"{len(m)} players")
            if g < min_goalies:
                why.append(f"{g} goalie(s)")
            if why:
                short.append((i, self._code(i), g, len(m), " and ".join(why)))
        return short

    def goalie_floor(self, minimum=MIN_GOALIES) -> list[tuple]:
        """Clubs under the goalie half of the floor. Prefer lineup_floor() — it checks both halves."""
        return [(i, c, g, n) for i, c, g, n, _ in self.lineup_floor(min_players=0, min_goalies=minimum)]

    def spares(self, position=None) -> list[int]:
        """Rows on no club's array at all — the pool a floor violation is repaired from.

        MOSTLY FREE AGENTS: check is_free_agent() if you care. Repairing through move() is safe
        either way, because move() signs the player out of the pool for you; it is writing the
        array directly that leaves him in both places.

        Pass `position` (0=G 1=D 2=LW 3=RW 4=C) to filter. Includes unnamed template rows; callers
        that care about names should filter on the surname themselves."""
        listed = {row for i in range(self.count) for row in self.members(i)}
        return [r for r in range(self.t.nrec)
                if r not in listed and (position is None or self.position_of(r) == position)]

    def spare_goalies(self) -> list[int]:
        return self.spares(POS_GOALIE)

    def audit(self) -> dict:
        """Rows whose two sides disagree — the failure mode this module exists to prevent."""
        in_array, bad_back, orphan = {}, [], []
        for i in range(self.count):
            for row in self.members(i):
                in_array.setdefault(row, []).append(i)
        for row, recs in in_array.items():
            po = self.t.base + row * 420 + OFF_TEAM
            v = struct.unpack_from(">i", self.d, po)[0]
            back = ((po + v - 1) - TEAM_REC_BIAS - self.base) // TO.STRIDE if v else None
            if back not in recs:
                bad_back.append((row, recs, back))
            if len(recs) > 1:
                orphan.append((row, recs))
        return {"listed": len(in_array), "backptr_mismatch": bad_back, "in_two_arrays": orphan,
                "below_lineup_floor": self.lineup_floor(), "count_mismatch": self.count_mismatch(),
                "free_agent_and_rostered": sorted(set(self._fa) & set(in_array)),
                "duplicate_free_agents": sorted(r for r in set(self._fa)
                                                if self._fa.count(r) > 1)}

    def count_mismatch(self) -> list[tuple]:
        """Clubs whose stored +0xE8 disagrees with their array — the roster-load hang. See OFF_COUNT.

        Dormant records that ship with 0 there are left alone; only non-empty clubs are checked."""
        return [(i, self._code(i), len(m), self.d[self._rec_off(i) + OFF_COUNT])
                for i in range(self.count)
                for m in [self.members(i)]
                if m and len(m) != self.d[self._rec_off(i) + OFF_COUNT]]
