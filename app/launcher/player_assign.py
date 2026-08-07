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

NAMES ARE SOLVED ON DISK (2026-08-06) — this file used to say they weren't. The name pointers at
+0x00 (last) and +0x04 (first) follow the same SELF-RELATIVE rule as the team-name pointers:

    target = field_file_offset + value - 1          # string is UTF-16BE, NUL-terminated

The target lands either in the shared name pool (chunk 0xEB69DFB9) for stock names, or INSIDE the
player's own 420-byte record for created/edited players, which store the string inline — scanning
only the pool is what made this look unsolvable. 2,676 of 2,715 rows resolve on the live 2026 save.
Use name(row) / find_rows_by_name() to address a player; portrait key and goalie order still work.

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
  +0xB8  u32  EYE COLOR      (v>>10)&3 — same dword as the mask pattern, different bits.
                             SOLVED 2026-08-06 from the render path, not by guessing. The four
                             eyeball BaseMap sheets are the only ones in the game (global.iff,
                             512x512 DXT1) and sit contiguously in g_MeshDescriptorTable
                             (= DAT_8208f638, 302 entries, stride 0x12e per side) as asset ids
                             75 blue / 76 brown / 77 green / 78 hazel. Function_84090AE0 binds the
                             eye part (0x1D) with Model_GetMeshHandleById({76,75,77,78}[e]) where
                             e = FUN_840a8f28 = rotl32(*(u32*)(rec+0xB8), 18) >> 28, i.e. these
                             bits. Confirmed on the live save: the field holds ONLY 0..3 across all
                             2,715 records, and the spread is textbook — brown 1283 / blue 718 /
                             hazel 505 / green 209. Spot checks match reality (Crosby brown,
                             Ovechkin blue, Makar brown, Matthews brown).
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
OFF_LAST     = 0x00                          # self-relative ptr -> UTF-16BE last name
OFF_FIRST    = 0x04                          # self-relative ptr -> UTF-16BE first name
OFF_TEAM     = 0x14                          # self-relative ptr back to this player's team record
OFF_PORTRAIT = 0x1C                          # u16
OFF_HEAD     = 0xB2                          # u16 head asset id -> player_head_id_%04d.iff
OFF_POSITION = 0x40                          # u32, goalie iff (v>>3)&7 == 0
OFF_SHELL    = 0xB4                          # u32, shell   = (v>>23)&0xF
OFF_PATTERN  = 0xB8                          # u32, pattern = v&0x1F
OFF_COLORS   = 0x158                         # 3 x u32 0xAARRGGBB at 0x158/0x15C/0x160
OFF_CAGE     = 0x168                         # u32 0xAARRGGBB

# The team record a player's OFF_TEAM pointer lands on carries its own name pointers here — the
# same layout the live editor reads out of memory, which holds on disk too because the whole
# record frame is shared (see the +0x73 reframe note above).
OFF_TEAM_CITY, OFF_TEAM_NICK, OFF_TEAM_CODE = 0xA0, 0xA4, 0xA8

SHELL_SHIFT, SHELL_MASK = 23, 0xF
PATTERN_MASK = 0x1F

# Eye colour shares the +0xB8 dword with the mask pattern — always read-modify-write it.
OFF_EYES = 0xB8
EYE_SHIFT, EYE_MASK = 10, 0x3
EYE_COLORS = ("brown", "blue", "green", "hazel")
# The g_MeshDescriptorTable asset id each value binds, for anyone extracting/replacing the sheet.
EYE_ASSET_IDS = (76, 75, 77, 78)

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

    # ── names (self-relative pointer -> UTF-16BE) ────────────────────────────
    def _str_at(self, ptr_foff) -> str:
        """The UTF-16BE string a self-relative pointer field at `ptr_foff` targets, or ''."""
        d = self.ros.data
        v = struct.unpack_from(">I", d, ptr_foff)[0]
        if not v:
            return ""
        t = ptr_foff + v - 1                          # same rule as the team-name pointers
        if not 0 <= t < len(d) - 1:
            return ""
        end = t
        while end + 1 < len(d) and d[end:end + 2] != b"\0\0":
            end += 2
        if end - t > 128:                             # runaway = not a string
            return ""
        try:
            return bytes(d[t:end]).decode("utf-16-be")
        except UnicodeDecodeError:
            return ""

    def name(self, row):
        """(first, last) for a row. Either may be '' if the pointer doesn't resolve."""
        return (self._str_at(self._off(row, OFF_FIRST)), self._str_at(self._off(row, OFF_LAST)))

    def team(self, row):
        """(city, nickname, code) of this player's club, or ('', '', '').

        The player record's +0x14 is a self-relative pointer straight at the team record, so this
        needs no team table and no index arithmetic — follow it and read the three name pointers.
        It reaches minor-league and national clubs too ('Utica / NJD Minors / UTC', 'Team /
        Denmark / DMK'), which is exactly what makes it useful for telling apart the several
        thousand rows that are not NHL players."""
        d = self.ros.data
        po = self._off(row, OFF_TEAM)
        v = struct.unpack_from(">I", d, po)[0]
        if not v:
            return ("", "", "")
        t = po + v - 1
        if not 0 <= t + OFF_TEAM_CODE + 4 <= len(d):
            return ("", "", "")
        return tuple(self._str_at(t + o) for o in
                     (OFF_TEAM_CITY, OFF_TEAM_NICK, OFF_TEAM_CODE))

    def find_rows_by_name(self, first=None, last=None):
        """Rows matching first and/or last name, case-insensitively. Duplicate names DO occur in the
        shipped roster (and community rosters often leave a stale duplicate behind), so this returns
        every match rather than one row — the caller decides."""
        f = (first or "").strip().lower()
        l = (last or "").strip().lower()
        out = []
        for r in range(self.nrec):
            fr, lr = self.name(r)
            if (not f or fr.lower() == f) and (not l or lr.lower() == l):
                out.append(r)
        return out

    # ── head asset (the player's face) ───────────────────────────────────────
    def head(self, row) -> int:
        """Head asset id: <9000 resolves to player_head_id_%04d.iff, >=9000 (and the 65535 sentinel)
        means the game generates the face procedurally and no asset is read."""
        return self._u16(row, OFF_HEAD)

    def set_head(self, row, head_id: int, validate=True, game_dir=None):
        """Point `row` at head asset `head_id`. With validate=True the id must exist as a shipped or
        added player_head_id_*.iff — an id below 9000 with no asset renders as a missing face."""
        if not 0 <= head_id <= 0xFFFF:
            raise ValueError(f"head id {head_id} out of u16 range")
        if validate and head_id < 9000:
            try:
                from .char_model import head_ids
            except ImportError:
                from char_model import head_ids
            if head_id not in set(head_ids(game_dir)):
                raise ValueError(f"head id {head_id} has no player_head_id_{head_id:04d}.iff")
        self._set_u16(row, OFF_HEAD, head_id)

    def head_usage(self):
        """{head_id: [rows]} — how many players share each face. A prototype face must go into an id
        used by nobody, or it repaints every player pointing at it."""
        out = {}
        for r in range(self.nrec):
            out.setdefault(self.head(r), []).append(r)
        return out

    # ── eye colour ───────────────────────────────────────────────────────────
    def eye_color(self, row) -> int:
        """0..3 -> EYE_COLORS[v]. Applies to every player, generated face or authored head."""
        return (self._u32(row, OFF_EYES) >> EYE_SHIFT) & EYE_MASK

    def eye_color_name(self, row) -> str:
        return EYE_COLORS[self.eye_color(row)]

    def set_eye_color(self, row, value):
        """Set eye colour by index 0..3 or by name ('brown'/'blue'/'green'/'hazel').

        Read-modify-write: this dword also carries the goalie mask pattern (bits 0..4), so a blind
        store would wipe it."""
        if isinstance(value, str):
            key = value.strip().lower()
            if key not in EYE_COLORS:
                raise ValueError(f"unknown eye colour {value!r} (want one of {EYE_COLORS})")
            value = EYE_COLORS.index(key)
        if not 0 <= value <= EYE_MASK:
            raise ValueError(f"eye colour {value} out of range 0..{EYE_MASK}")
        v = self._u32(row, OFF_EYES)
        self._set_u32(row, OFF_EYES, (v & ~(EYE_MASK << EYE_SHIFT)) | (value << EYE_SHIFT))

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
                        "colors": list(self.mask_colors(r)), "cage": self.cage_color(r),
                        "head": self.head(r), "eyes": self.eye_color_name(r)})
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


def apply_assignments(ros_path, portraits=None, masks=None, heads=None, eyes=None,
                      log=print, backup=True):
    """Apply {row: key} portraits, {row: (shell, pattern)} masks, {row: head_id} heads and
    {row: eye colour} to a Roster.ROS in place.

    Rows sitting on a sentinel key are skipped for portraits (they are placeholder records).
    Returns (n_portraits, n_masks, skipped)."""
    t = PlayerTable(ros_path)
    npor = nmask = nhead = neyes = 0
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
    for row, head_id in (heads or {}).items():
        cur = t.head(row)
        try:
            t.set_head(row, head_id)
        except (ValueError, IndexError) as e:
            skipped.append((row, str(e))); continue
        if cur != head_id:
            nhead += 1
            log(f"  row {row}: head {cur} -> {head_id}")
    for row, colour in (eyes or {}).items():
        cur = t.eye_color(row)
        try:
            t.set_eye_color(row, colour)
        except (ValueError, IndexError) as e:
            skipped.append((row, str(e))); continue
        if cur != t.eye_color(row):
            neyes += 1
            log(f"  row {row}: eyes {EYE_COLORS[cur]} -> {t.eye_color_name(row)}")
    if npor or nmask or nhead or neyes:
        t.save(backup=backup)
        log(f"saved {ros_path} ({npor} portraits, {nmask} masks, {nhead} heads, {neyes} eyes)")
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
    ap.add_argument("--set-head", nargs=2, type=int, metavar=("ROW", "HEAD_ID"))
    ap.add_argument("--set-eyes", nargs=2, metavar=("ROW", "COLOR"),
                    help=f"eye colour: index 0..3 or name {EYE_COLORS}")
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
        first, last = t.name(r)
        print(f"row {r}: {first} {last} portrait={t.portrait(r)} position={t.position(r)} "
              f"head={t.head(r)} eyes={t.eye_color_name(r)} "
              f"goalie={t.is_goalie(r)} look={t.goalie_look(r)}")
    if a.set_portrait:
        row, key = a.set_portrait
        apply_assignments(a.ros, portraits={row: key})
    if a.set_mask:
        row, shell, pattern = a.set_mask
        apply_assignments(a.ros, masks={row: (shell, pattern)})
    if a.set_head:
        row, head_id = a.set_head
        apply_assignments(a.ros, heads={row: head_id})
    if a.set_eyes:
        row, colour = int(a.set_eyes[0]), a.set_eyes[1]
        apply_assignments(a.ros, eyes={row: int(colour) if colour.isdigit() else colour})
    if a.set_colors:
        row = int(a.set_colors[0]); cols = [int(x, 16) for x in a.set_colors[1:]]
        apply_assignments(a.ros, masks={row: {"colors": cols}})
    if a.set_cage:
        apply_assignments(a.ros, masks={int(a.set_cage[0]): {"cage": int(a.set_cage[1], 16)}})
    if a.identity_colors is not None:
        apply_assignments(a.ros, masks={a.identity_colors:
                                        {"colors": [c & 0xFFFFFF for c in IDENTITY_COLORS]}})
