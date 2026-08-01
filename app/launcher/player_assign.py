"""player_assign.py — STATIC portrait + goalie-mask assignment in Roster.ROS.

Supersedes the live-memory approach in portrait_assign.py / goalie_equipment.py. Those two write
the *loaded* player array in Xenia and must be re-applied every launch; this writes the save file,
so the assignment persists, ships in a .n2kpack, and works on real console hardware.

The on-disk player record is the SAME struct as the in-memory one, and since 2026-08-01 RosFile
frames the player chunk correctly, so this module addresses

    record_base(row) = chunk.foff + row*STRIDE

and every field offset below is then IDENTICAL to the in-memory offset that goalie_equipment.py /
portrait_assign.py use against Xenia.

⚠ ROWS SHIFTED +7 on 2026-08-01. This used to be `chunk.foff + 0x73 + row*STRIDE`, a hand
correction for ros_file.py's wrong DATA_BASE. It got the *phase* right (0x73 happened to land on a
record boundary) but started 7 records late, so rows 0..6 — six of which are on a team's roster,
checked via the team records' player pointers — were invisible and every row index was 7 low. The
old row N is now row N+7. Nothing keys off a bare row index except the mod-pack goalie section,
which re-resolves by portrait key and goalie ordinal (see modpack._resolve_goalie_row), so old
packs still land on the right goalies. Record 0 is now the chunk start, confirmed by the team
records' player pointers, which target `player_record + 0x00`.

(Pointer fields — the name pointers at +0x00/+0x04 — are not
stored as offsets on disk; the loader resolves those, so per-player NAMES are still unsolved here.
Identify a row by portrait key or by goalie order instead: find_rows_by_portrait_key / goalie_rows.)

Verified against the live Roster.ROS (player chunk 0x1E159C31, 2714 records x 420B, big-endian):
  +0x1C  u16  portrait key   98.6% of values are keys that actually exist as blobs in
                             disc_b9610aac.iff (chance density 24.5%), 1423 distinct of 1478, and
                             100% of the launcher's own 358 assigned keys are present (incl. all 37
                             rare ones >2400). IN-GAME CONFIRMED 2026-07-31: pointing Demko at key
                             507 rendered that slot's image, Oettinger at 661 rendered Carter Hart.
  +0x40  u32  position       (v>>3)&7 == 0 selects 280 rows, 279 of which are in the goalie set
                             derived independently from the goalie-attribute block.
  +0xB4  u32  mask shell     (v>>23)&0xF; 270/280 goalies = shell 0.
  +0xB8  u32  mask pattern   v&0x1F; goalies span the full 0..31 range, skaters 2385/2411 zero.
                             IN-GAME CONFIRMED 2026-07-31 (pattern 28->3 and 0->22 both took).
  +0x158 u32  mask color 1   0xAARRGGBB, alpha FF. The mask texture is team-RECOLORED: the game
  +0x15C u32  mask color 2   substitutes these three colors weighted by the texture's R/G/B
  +0x160 u32  mask color 3   channels, so a custom TRUE-COLOR mask needs them set to the RGB
                             identity (FFFF0000 / FF00FF00 / FF0000FF) to pass through unchanged.
  +0x168 u32  cage color     0xAARRGGBB, alpha FF.
    Colors PINNED EXACTLY by in-game diff (Roster_Investigate/test_cage_1 vs _2): setting Giguere's
    three mask colors + cage to red/green/blue/magenta in the creation zone changed precisely
    +0x159, +0x15D, +0x161, +0x169 (the RGB bytes of those four dwords) and nothing else.
    +0x164 sits between color 3 and the cage and is something else — it did NOT move.

There is NO per-save scramble or record checksum: one in-game roster edit produces a 7-byte
whole-file diff. The 20-bit field at +0x50..+0x52 (disk +0xC3..+0xC5 in the RosFile frame) does
change on ~every record between saves but does NOT change when other fields change, so nothing needs
recomputing after a write. The "scrambled/checksummed per save" caveat in ros_file.py /
goalie_equipment.py refers to that field and is wrong as stated.

Writes are strictly in place (file size never changes), so every fixed offset the game holds stays
valid — the same invariant ros_file.py relies on.
"""
from __future__ import annotations
import struct
from pathlib import Path

try:
    from .ros_file import RosFile
except ImportError:
    from ros_file import RosFile

PLAYER_HASH = 0x1E159C31
STRIDE = 420

# Offsets are relative to record_base(row) = foff + row*STRIDE, i.e. identical to the
# in-memory offsets used by goalie_equipment.py / portrait_assign.py.
OFF_TEAM     = 0x14                          # self-relative ptr back to this player's team record
OFF_PORTRAIT = 0x1C                          # u16
OFF_POSITION = 0x40                          # u32, goalie iff (v>>3)&7 == 0
OFF_SHELL    = 0xB4                          # u32, shell   = (v>>23)&0xF
OFF_PATTERN  = 0xB8                          # u32, pattern = v&0x1F
OFF_COLORS   = 0x158                         # 3 x u32 0xAARRGGBB at 0x158/0x15C/0x160
OFF_CAGE     = 0x168                         # u32 0xAARRGGBB

SHELL_SHIFT, SHELL_MASK = 23, 0xF
PATTERN_MASK = 0x1F

# Setting the 3 recolor slots to an RGB identity makes the team-recolor substitution a pass-through,
# so a custom TRUE-COLOR mask texture renders exactly as authored.
IDENTITY_COLORS = (0xFFFF0000, 0xFF00FF00, 0xFF0000FF)

# Rows the game parks on a sentinel rather than a real portrait. Seen in the wild at +0x8F;
# they are not valid asset keys and must not be treated as assignable.
SENTINEL_KEYS = {9000, 9001}


class PlayerTable:
    """The player record table of one Roster.ROS, addressed at foff + row*420."""

    def __init__(self, path):
        self.ros = RosFile(path)
        self.chunk = next((c for c in self.ros.chunks if c.hash == PLAYER_HASH), None)
        if self.chunk is None:
            raise ValueError("no player chunk 0x1E159C31 in this save")
        self.foff = self.chunk.foff
        self.base = self.chunk.foff                  # record 0 IS the chunk start
        self.nrec = self.chunk.size // STRIDE
        if self.nrec < 100:
            raise ValueError(f"implausible player count {self.nrec}")

    # ── raw field IO ─────────────────────────────────────────────────────────
    def _off(self, row, off):
        if not 0 <= row < self.nrec:
            raise IndexError(f"row {row} out of range (0..{self.nrec-1})")
        o = self.base + row * STRIDE + off
        if o + 4 > self.foff + self.chunk.size:
            raise ValueError("field runs past the chunk end")
        return o

    def _u16(self, row, off):
        return struct.unpack_from(">H", self.ros.data, self._off(row, off))[0]

    def _u32(self, row, off):
        return struct.unpack_from(">I", self.ros.data, self._off(row, off))[0]

    def _set_u16(self, row, off, v):
        struct.pack_into(">H", self.ros.data, self._off(row, off), v & 0xFFFF)

    def _set_u32(self, row, off, v):
        struct.pack_into(">I", self.ros.data, self._off(row, off), v & 0xFFFFFFFF)

    # ── portrait ─────────────────────────────────────────────────────────────
    def portrait(self, row) -> int:
        return self._u16(row, OFF_PORTRAIT)

    def set_portrait(self, row, key: int, validate=True):
        """Point `row` at portrait `key`. With validate=True the key must exist as a blob in
        disc_b9610aac.iff (the game silently shows nothing for a key it can't resolve)."""
        if not 0 <= key <= 0xFFFF:
            raise ValueError(f"portrait key {key} out of u16 range")
        if validate and key not in valid_portrait_keys():
            raise ValueError(f"portrait key {key} has no blob in disc_b9610aac.iff")
        self._set_u16(row, OFF_PORTRAIT, key)

    # ── position / goalie ────────────────────────────────────────────────────
    def position(self, row) -> int:
        return (self._u32(row, OFF_POSITION) >> 3) & 7

    def is_goalie(self, row) -> bool:
        return self.position(row) == 0

    def goalie_rows(self):
        return [r for r in range(self.nrec) if self.is_goalie(r)]

    # ── goalie mask ──────────────────────────────────────────────────────────
    def mask(self, row):
        """(shell, pattern) -> texture helmet_g{shell+1:02d}_pattern_{pattern:02d}.iff."""
        return ((self._u32(row, OFF_SHELL) >> SHELL_SHIFT) & SHELL_MASK,
                self._u32(row, OFF_PATTERN) & PATTERN_MASK)

    def set_mask(self, row, shell: int, pattern: int):
        """Read-modify-write BOTH dwords, preserving every other bit — the shell and pattern bits
        share their dwords with unrelated player data, so a blind store would corrupt the record."""
        if not 0 <= shell <= SHELL_MASK:
            raise ValueError(f"shell {shell} out of range 0..{SHELL_MASK}")
        if not 0 <= pattern <= PATTERN_MASK:
            raise ValueError(f"pattern {pattern} out of range 0..{PATTERN_MASK} (5-bit field)")
        v = self._u32(row, OFF_SHELL)
        self._set_u32(row, OFF_SHELL,
                      (v & ~(SHELL_MASK << SHELL_SHIFT)) | (shell << SHELL_SHIFT))
        v = self._u32(row, OFF_PATTERN)
        self._set_u32(row, OFF_PATTERN, (v & ~PATTERN_MASK) | pattern)

    # ── mask recolor + cage colors ───────────────────────────────────────────
    # Stored 0xAARRGGBB with alpha FF. The public API speaks plain 0xRRGGBB so callers (and
    # .n2kpack JSON) never have to think about the alpha byte; it is added/stripped here.
    def mask_colors(self, row):
        """(color1, color2, color3) as 0xRRGGBB — the 3 team-recolor slots."""
        return tuple(self._u32(row, OFF_COLORS + i * 4) & 0xFFFFFF for i in range(3))

    def set_mask_colors(self, row, colors):
        """colors: 3 x 0xRRGGBB, or None to leave a slot alone."""
        if len(colors) != 3:
            raise ValueError("need exactly 3 mask colors")
        for i, c in enumerate(colors):
            if c is None:
                continue
            if not 0 <= c <= 0xFFFFFF:
                raise ValueError(f"color {c:#x} out of 0xRRGGBB range")
            self._set_u32(row, OFF_COLORS + i * 4, 0xFF000000 | c)

    def set_identity_colors(self, row):
        """Neutralize the team recolor so a custom true-color mask renders as authored."""
        for i, c in enumerate(IDENTITY_COLORS):
            self._set_u32(row, OFF_COLORS + i * 4, c)

    def cage_color(self, row) -> int:
        return self._u32(row, OFF_CAGE) & 0xFFFFFF

    def set_cage_color(self, row, color: int):
        if not 0 <= color <= 0xFFFFFF:
            raise ValueError(f"cage color {color:#x} out of 0xRRGGBB range")
        self._set_u32(row, OFF_CAGE, 0xFF000000 | color)

    def goalie_look(self, row):
        """Everything that defines a goalie's mask appearance — the unit a modpack ships."""
        shell, pattern = self.mask(row)
        return {"shell": shell, "pattern": pattern,
                "colors": list(self.mask_colors(row)), "cage": self.cage_color(row)}

    def set_goalie_look(self, row, look):
        if "shell" in look and "pattern" in look:
            self.set_mask(row, look["shell"], look["pattern"])
        if look.get("colors"):
            self.set_mask_colors(row, look["colors"])
        if look.get("cage") is not None:
            self.set_cage_color(row, look["cage"])

    # ── lookup ───────────────────────────────────────────────────────────────
    def find_rows_by_portrait_key(self, key: int):
        """Rows currently pointing at `key`. The launcher config maps 'First|Last' -> key, so this
        is the practical way to locate a named player until disk name resolution is solved."""
        return [r for r in range(self.nrec) if self.portrait(r) == key]

    def rows(self):
        """[{row, portrait, position, is_goalie, shell, pattern, colors, cage}] for every record."""
        out = []
        for r in range(self.nrec):
            shell, pattern = self.mask(r)
            out.append({"row": r, "portrait": self.portrait(r), "position": self.position(r),
                        "is_goalie": self.is_goalie(r), "shell": shell, "pattern": pattern,
                        "colors": list(self.mask_colors(r)), "cage": self.cage_color(r)})
        return out

    # ── save ─────────────────────────────────────────────────────────────────
    def dirty(self):
        return self.ros.dirty()

    def save(self, out_path=None, backup=True):
        self.ros.save(out_path, backup)


_KEYS = None


def valid_portrait_keys():
    """Keys that actually resolve to a portrait blob. Empty set if the archive isn't reachable,
    in which case set_portrait(validate=True) will refuse rather than write a dud key."""
    global _KEYS
    if _KEYS is None:
        try:
            try:
                from . import archive_textures as at
            except ImportError:
                import archive_textures as at
            _KEYS = set(at.portrait_key_blob_map())
        except Exception:
            _KEYS = set()
    return _KEYS


# ── live roster -> Roster.ROS row mapping ─────────────────────────────────────
#
# The Goalie / Portraits tabs list players out of the RUNNING game, because that is the only place
# player names resolve (the record holds a pointer, and nothing in the save file spells the name).
# But the WRITE has to land in Roster.ROS, or the work only exists in Xenia's RAM and never reaches
# a console. This is the bridge.
#
# The live player array and the save file's player table are the same table — same 420-byte stride,
# same order — so the live `index` IS the file row. That is asserted rather than assumed: the array
# length must equal the file's record count, and each row's portrait key must still match the live
# record's. Anything that fails is reported and skipped, never written blind.

class RowMapError(Exception):
    """The live roster and this Roster.ROS are not the same table — refuse to write by row."""

def rows_for_live(table, live_recs):
    """[(row, rec)] for records that line up, plus a list of notes about ones that look off.

    `live_recs` are enumerate_goalies / enumerate_players dicts. Raises RowMapError when the two
    tables disagree on LENGTH — that is the hard check, and it means the loaded roster isn't the one
    on disk (a different save, or the game is mid-load), where writing by index would land on
    unrelated players.

    A portrait-key mismatch is only a NOTE, not a skip. Once the length matches, index->row is
    structural; a differing key just means the two copies have drifted — which is exactly what
    happens after an old live-only assignment, and refusing to write would strand precisely the
    users this change exists to rescue.
    """
    counts = {r.get("roster_count") for r in live_recs if r.get("roster_count")}
    if counts and table.nrec not in counts:
        raise RowMapError(
            f"the roster loaded in the game has {sorted(counts)[0]} player records but this "
            f"Roster.ROS has {table.nrec} — they aren't the same save file")
    ok, notes = [], []
    for rec in live_recs:
        row = rec.get("index")
        who = rec.get("name") or f"record {row}"
        if row is None or not (0 <= row < table.nrec):
            notes.append(f"{who}: record {row} is outside this save's {table.nrec} rows — skipped")
            continue
        live_key = rec.get("portrait", rec.get("key"))
        if live_key is not None and table.portrait(row) != live_key:
            notes.append(f"{who}: row {row} has portrait {table.portrait(row)}, the game has "
                         f"{live_key} (save and running game have drifted)")
        ok.append((row, rec))
    return ok, notes


def apply_assignments(ros_path, portraits=None, masks=None, log=print, backup=True):
    """Apply {row: key} portraits and {row: (shell, pattern)} masks to a Roster.ROS in place.

    Rows sitting on a sentinel key are skipped for portraits (they are placeholder records).
    Returns (n_portraits, n_masks, skipped)."""
    t = PlayerTable(ros_path)
    npor = nmask = 0
    skipped = []
    for row, key in (portraits or {}).items():
        cur = t.portrait(row)
        if cur in SENTINEL_KEYS:
            skipped.append((row, f"sentinel key {cur}")); continue
        try:
            t.set_portrait(row, key)
        except ValueError as e:
            skipped.append((row, str(e))); continue
        if cur != key:
            npor += 1
            log(f"  row {row}: portrait {cur} -> {key}")
    for row, look in (masks or {}).items():
        if not t.is_goalie(row):
            skipped.append((row, "not a goalie")); continue
        # Accept either the legacy (shell, pattern) tuple or a full goalie_look dict.
        if not isinstance(look, dict):
            shell, pattern = look
            look = {"shell": shell, "pattern": pattern}
        cur = t.goalie_look(row)
        try:
            t.set_goalie_look(row, look)
        except ValueError as e:
            skipped.append((row, str(e))); continue
        new = t.goalie_look(row)
        if cur != new:
            nmask += 1
            log(f"  row {row}: mask {cur['shell']},{cur['pattern']} -> {new['shell']},{new['pattern']}"
                f"  colors {[f'{c:06X}' for c in cur['colors']]} -> {[f'{c:06X}' for c in new['colors']]}"
                f"  cage {cur['cage']:06X} -> {new['cage']:06X}")
    if npor or nmask:
        t.save(backup=backup)
        log(f"saved {ros_path} ({npor} portraits, {nmask} masks)")
    else:
        log("nothing to change")
    for row, why in skipped:
        log(f"  SKIP row {row}: {why}")
    return npor, nmask, skipped


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="static portrait/mask assignment in Roster.ROS")
    ap.add_argument("ros")
    ap.add_argument("--list-goalies", action="store_true")
    ap.add_argument("--find-key", type=int, help="rows currently using this portrait key")
    ap.add_argument("--show", type=int, metavar="ROW")
    ap.add_argument("--set-portrait", nargs=2, type=int, metavar=("ROW", "KEY"))
    ap.add_argument("--set-mask", nargs=3, type=int, metavar=("ROW", "SHELL", "PATTERN"))
    ap.add_argument("--set-colors", nargs=4, metavar=("ROW", "C1", "C2", "C3"),
                    help="3 mask recolor slots as RRGGBB hex")
    ap.add_argument("--set-cage", nargs=2, metavar=("ROW", "RRGGBB"))
    ap.add_argument("--identity-colors", type=int, metavar="ROW",
                    help="neutralize the team recolor for a custom true-color mask")
    a = ap.parse_args()

    t = PlayerTable(a.ros)
    print(f"{t.nrec} player records @ 0x{t.foff:X}; "
          f"{len(valid_portrait_keys())} valid portrait keys")
    if a.list_goalies:
        g = t.goalie_rows()
        print(f"{len(g)} goalies:")
        for r in g:
            L = t.goalie_look(r)
            print(f"  row {r:5d}  shell {L['shell']} pattern {L['pattern']:2d}  "
                  f"colors {'/'.join('%06X' % c for c in L['colors'])}  cage {L['cage']:06X}  "
                  f"portrait {t.portrait(r)}")
    if a.find_key is not None:
        print(f"rows using portrait {a.find_key}: {t.find_rows_by_portrait_key(a.find_key)}")
    if a.show is not None:
        r = a.show
        print(f"row {r}: portrait={t.portrait(r)} position={t.position(r)} "
              f"goalie={t.is_goalie(r)} look={t.goalie_look(r)}")
    if a.set_portrait:
        row, key = a.set_portrait
        apply_assignments(a.ros, portraits={row: key})
    if a.set_mask:
        row, shell, pattern = a.set_mask
        apply_assignments(a.ros, masks={row: (shell, pattern)})
    if a.set_colors:
        row = int(a.set_colors[0]); cols = [int(x, 16) for x in a.set_colors[1:]]
        apply_assignments(a.ros, masks={row: {"colors": cols}})
    if a.set_cage:
        apply_assignments(a.ros, masks={int(a.set_cage[0]): {"cage": int(a.set_cage[1], 16)}})
    if a.identity_colors is not None:
        apply_assignments(a.ros, masks={a.identity_colors:
                                        {"colors": [c & 0xFFFFFF for c in IDENTITY_COLORS]}})
