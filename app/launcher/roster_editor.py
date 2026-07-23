"""
roster_editor.py — read/edit NHL 2K10 team display names in a Roster.ROS save.

Team display strings live in the save as null-terminated UTF-16BE, tightly packed,
in a string pool (~0x24b000): per-team block [arena "X|TM|"][city][state?][CODE],
then a nickname pool [Nickname][lowercase] (some teams add a short city-override
token like "LA"/"NJ"/"SJ"/"TB" between Nickname and lowercase).

The game resolves each string from its FIXED byte offset (read until the 00 00 terminator),
so an edit is only safe if it stays within that string's existing byte slot (the gap to the
next packed string). A shorter/equal name is written in place — the leftover bytes become
harmless junk before the next string. A LONGER name CANNOT be written in place (the pool is
tightly packed) and is NOT auto-grown here: doing so requires relocating the string into
free space and repointing its stored offset reference, which is not yet implemented. Over-
length edits are REFUSED rather than corrupting the save. A .bak backup is written first.

(History: an earlier version tried to grow by shifting the pool tail into the EOF padding.
That corrupts the save — the records hold fixed offsets, not sequential indices, so moving
the tail invalidates every reference after the edit. Removed.)

Usage (standalone):
  python roster_editor.py "<Roster.ROS>" --export teams.csv
  python roster_editor.py "<Roster.ROS>" --apply  teams.csv          # writes <ROS>.bak then patches
"""
from __future__ import annotations
import re, csv, struct, shutil, zlib
from pathlib import Path

# ---- team-string region + identifiers -------------------------------------------------
REGION = (0x24A000, 0x24C060)           # generous bounds around the NHL team table
NHL_CODES = ["ANA","ATL","BOS","BUF","CGY","CAR","CHI","COL","CBJ","DAL","DET","EDM","FLA",
             "LAK","MIN","MTL","NSH","NJD","NYI","NYR","OTT","PHI","PHO","PIT","SJS","STL",
             "TBL","TOR","VAN","WSH"]
_CODESET = set(NHL_CODES)
# US states + Canadian provinces seen in the table (to tell city from state)
_REGIONS = {"california","georgia","massachusetts","new york","alberta","north carolina",
            "illinois","colorado","ohio","texas","michigan","florida","minnesota","quebec",
            "tennessee","new jersey","ontario","pennsylvania","arizona","missouri",
            "british columbia","d.c.","district of columbia"}
_VENUE_RX = re.compile(r"Arena|Cent(er|re)|Stadium|Field|Place|Dome|Forum|Pavilion|Garden|Saddledome|Coliseum|Igloo|Rink", re.I)


def _is_region(s: str) -> bool:
    """True if this string is a state/province name rather than a city."""
    return s.lower().rstrip("|tm|").strip() in _REGIONS
_STR_RX = re.compile(rb'(?:\x00[\x20-\x7e]){2,}')   # UTF-16BE ascii run, >=2 chars

class Slot:
    """One editable UTF-16BE string occupying [off, next_off) bytes (incl. its 00 00 terminator)."""
    __slots__ = ("off", "text", "cap_chars")
    def __init__(self, off, text, cap_chars):
        self.off, self.text, self.cap_chars = off, text, cap_chars

class Team:
    """One NHL team's editable strings, as they actually exist in the save.

    Layout per team: [Arena][City?][State?][CODE], then in the nickname pool
    [Team?][Nickname][ShortCode?][nickname-lowercase].

    `team` is the TEAM prefix ("Carolina Hurricanes" -> "Carolina"). It is only STORED for the
    three teams whose prefix matches neither their city nor their state — Carolina (city Raleigh),
    Phoenix (city Glendale) and Tampa Bay (city Tampa). For everyone else the game composes the
    prefix itself and there is nothing in the save to show or edit, so `team` is None. Verified
    against a stock roster and by searching default.xex (which contains no team city names at all).
    Do not synthesise it from city/state: that produced the bogus "Glendale Coyotes" display.
    """
    __slots__ = ("code", "arena", "city", "state", "team", "nickname", "nick_lower")
    def __init__(self, code):
        self.code=code; self.arena=None; self.city=None; self.state=None
        self.team=None; self.nickname=None; self.nick_lower=None
    @property
    def full_name(self):
        """"<Team> <Name>" when the prefix is stored, else just the name — never invented."""
        return " ".join(x for x in (self.team.text if self.team else "",
                                    self.nickname.text if self.nickname else "") if x).strip()

class RosterEditor:
    def __init__(self, path):
        self.path = Path(path)
        self.data = bytearray(self.path.read_bytes())
        self.teams = []
        # scan window over the team table + NHL nickname pool. The upper bound is anchored
        # on CONTENT (the minor-league table that follows always begins with " Minors"
        # strings) rather than a fixed offset, which is robust against junk left by prior
        # in-place shortenings.
        self.region = [REGION[0], REGION[1]]
        self.region[1] = self._find_region_end()
        self._parse()
        self.orig_size = len(self.data)

    def _find_region_end(self):
        """Offset where the NHL team/nickname pool ends = start of the minor-league table.
        Those entries always end in ' Minors' (e.g. 'NA Minors'); no NHL name does. Falls
        back to the static bound if the marker isn't found. Growth-proof (re-derived from
        content), so a fresh reload of an already-grown save still captures all 30 nicks."""
        lo = self.region[0]
        for m in _STR_RX.finditer(self.data[lo: lo + 0x20000]):
            if m.group().decode("utf-16-be", "ignore").endswith(" Minors"):
                return lo + m.start()
        return REGION[1]

    # --- low-level string IO ---
    def _read_str(self, off):
        e = off
        d = self.data
        while e + 1 < len(d) and d[e] == 0 and 0x20 <= d[e+1] < 0x7f:
            e += 2
        return d[off:e].decode("utf-16-be", "ignore")

    def _all_strings(self):
        lo, hi = self.region
        out = []
        for m in _STR_RX.finditer(self.data[lo:hi]):
            out.append((lo + m.start(), m.group().decode("utf-16-be", "ignore")))
        return out

    # --- parse the team table ---
    def _parse(self):
        strs = self._all_strings()
        # slot capacity = gap (in chars) to the next packed string, minus the 00 00 terminator
        nexts = {strs[i][0]: strs[i+1][0] for i in range(len(strs)-1)}
        def slot(off, text):
            nb = nexts.get(off, off + (len(text)+1)*2)
            cap = max(len(text), (nb - off)//2 - 1)
            return Slot(off, text, cap)

        # Part 1: per-code blocks. Walk forward; remember strings since last code.
        teams = {}
        block = []
        for off, s in strs:
            if s in _CODESET:
                t = teams.get(s) or Team(s)
                # arena = first string in block with a venue word (or first string)
                arena = next((b for b in block if _VENUE_RX.search(b[1])), block[0] if block else None)
                # what's left is [City?][State?] — BOTH are optional and the save really does omit
                # them (DAL ships [Arena][Texas] with no city; NYR ships [Arena] with neither).
                # A state/province is identified BY NAME, so a lone 'Texas' is the STATE, not a city
                # called Texas — the old "first non-region, else tail[0]" fallback mislabelled it.
                tail = block[block.index(arena)+1:] if arena in block else block
                if len(tail) >= 2:
                    city, state = tail[0], tail[1]      # positional: [City][State], always in order
                elif len(tail) == 1:
                    # Only one string: genuinely ambiguous, so fall back to recognising the NAME
                    # (DAL ships 'Texas' = a state with no city; EDM ships 'Edmonton' = a city with
                    # no state). Renaming a city to a state's name will flip it — unavoidable here.
                    state, city = (tail[0], None) if _is_region(tail[0][1]) else (None, tail[0])
                else:
                    city = state = None
                if arena: t.arena = slot(*arena)
                if city:  t.city  = slot(*city)
                if state: t.state = slot(*state)
                teams.setdefault(s, t)
                block = []
            else:
                block.append((off, s))
        # Part 2: nicknames. A title-case string whose lowercased(no-space) form appears
        # within the next 2 strings = a nickname (the lowercase is an internal key kept in
        # sync). A short all-caps token (LA/NJ/SJ/TB) may sit between. NHL_CODES order.
        nicks = []
        for i, (off, s) in enumerate(strs):
            if not s or not s[0].isupper():
                continue
            low = s.lower().replace(" ", "")
            jpair = next((j for j in range(i+1, min(i+3, len(strs))) if strs[j][1] == low), None)
            if jpair is not None:
                # The string right before a nickname is the TEAM prefix when it's a real name:
                # Title-case, not the previous team's lowercase twin, and not an ALL-CAPS tag
                # (the arena section ends in 'SKILL', which would otherwise look like a prefix for
                # Anaheim). Only Carolina / Phoenix / Tampa Bay actually have one.
                p = strs[i-1] if i > 0 else None
                pref = p if (p and p[1][:1].isupper() and not p[1].isupper()
                             and p[1] not in _CODESET) else None
                nicks.append((off, s, strs[jpair][0], strs[jpair][1], pref))
        # pair nicknames with codes by order
        for i, code in enumerate(NHL_CODES):
            if code in teams and i < len(nicks):
                noff, ntext, loff, ltext, pref = nicks[i]
                teams[code].nickname   = slot(noff, ntext)
                teams[code].nick_lower = slot(loff, ltext)
                if pref:
                    teams[code].team = slot(*pref)
        self.teams = [teams[c] for c in NHL_CODES if c in teams]

    def set_nickname(self, team: "Team", new_text: str):
        """Set a team's display nickname AND keep its lowercase internal key in sync."""
        if team.nickname is None:
            return 0
        e = [(team.nickname, new_text)]
        if team.nick_lower is not None:
            e.append((team.nick_lower, new_text.lower().replace(" ", "")))
        return self.apply_edits(e)

    # --- write strings strictly IN PLACE (no byte shifting; see module docstring) ---
    def apply_edits(self, edits):
        """edits: list of (Slot, new_text).

        Writes each string IN PLACE within its existing byte slot (the gap to the next
        packed string). Nothing is moved and the file size never changes, so all fixed
        offset references the game holds stay valid. A name that does NOT fit its slot is
        REFUSED (ValueError) — growing it would require relocating the string and repointing
        its reference, which corrupts the save if done by shifting. Returns the count changed.
        """
        plan, too_long = [], []
        for slot, new in edits:
            if slot is None or new == slot.text:
                continue
            if len(new) > slot.cap_chars:        # cap_chars = bytes-to-next-string//2 - 1
                too_long.append((slot, new))
            else:
                plan.append((slot, new))
        if too_long:
            lines = "\n".join(f"  • '{s.text}' → '{n}'  "
                              f"(slot fits {s.cap_chars} chars, name is {len(n)})"
                              for s, n in too_long)
            raise ValueError(
                "These names are longer than their fixed slot in the save and can't be "
                "written without corrupting it:\n" + lines +
                "\n\nKeep each name within its slot length (shorter or equal). "
                "Longer-name support needs relocation + repointing, which isn't built yet.")
        for slot, new in plan:
            old_span = (len(slot.text) + 1) * 2           # bytes the old string occupied
            nb = new.encode("utf-16-be") + b"\x00\x00"
            nb = nb + b"\x00" * (old_span - len(nb))       # zero-fill leftover (no shift, no phantom)
            self.data[slot.off:slot.off + old_span] = nb   # exact same span -> file size unchanged
            slot.text = new
        return len(plan)

    def set_string(self, slot: Slot, new_text: str):
        return self.apply_edits([(slot, new_text)])

    def save(self, out_path=None, backup=True):
        # hard safety net: this tool must NEVER change the file size (all edits are in place).
        if len(self.data) != self.orig_size:
            raise RuntimeError(f"refusing to write: size changed {self.orig_size} -> "
                               f"{len(self.data)} (in-place invariant violated)")
        out = Path(out_path) if out_path else self.path
        if backup and out.exists():
            bak = out.with_suffix(out.suffix + ".bak")
            if not bak.exists():
                shutil.copy2(out, bak)
        out.write_bytes(self.data)

    # --- CSV import/export ---
    FIELDS = ["code", "city", "nickname", "arena",
              "city_off", "city_cap", "nick_off", "nick_cap", "arena_off", "arena_cap"]
    def export_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(self.FIELDS)
            for t in self.teams:
                c, n, a = t.city, t.nickname, t.arena
                w.writerow([t.code,
                            c.text if c else "", n.text if n else "", a.text if a else "",
                            f"0x{c.off:X}" if c else "", c.cap_chars if c else "",
                            f"0x{n.off:X}" if n else "", n.cap_chars if n else "",
                            f"0x{a.off:X}" if a else "", a.cap_chars if a else ""])

    def apply_csv(self, path):
        # gather every field change, then apply in one batch (one validation pass; any
        # over-length name aborts the whole import via ValueError before anything is written).
        by_code = {t.code: t for t in self.teams}
        edits, changes = [], []
        for row in csv.DictReader(open(path, encoding="utf-8")):
            t = by_code.get(row["code"])
            if not t:
                continue
            for field, slot in (("city", t.city), ("arena", t.arena)):
                new = (row.get(field) or "").strip()
                if slot is not None and new and new != slot.text:
                    edits.append((slot, new)); changes.append((t.code, field, new))
            new = (row.get("nickname") or "").strip()
            if t.nickname is not None and new and new != t.nickname.text:
                edits.append((t.nickname, new)); changes.append((t.code, "nickname", new))
                if t.nick_lower is not None:                # keep the internal lowercase key in sync
                    edits.append((t.nick_lower, new.lower().replace(" ", "")))
        self.apply_edits(edits)                             # one pass; raises if over budget
        return changes


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="NHL 2K10 Roster.ROS team-name editor")
    ap.add_argument("ros"); ap.add_argument("--export"); ap.add_argument("--apply")
    a = ap.parse_args()
    ed = RosterEditor(a.ros)
    if a.export:
        ed.export_csv(a.export); print(f"exported {len(ed.teams)} teams -> {a.export}")
    elif a.apply:
        ch = ed.apply_csv(a.apply)
        if ch:
            ed.save()
            print(f"applied {len(ch)} change(s); backup at {a.ros}.bak")
            for c in ch: print("   ", c)
        else:
            print("no changes")
    else:
        for t in ed.teams:
            print(f"  {t.code}  {t.display_name:24s}  arena={t.arena.text if t.arena else '?'}")

if __name__ == "__main__":
    _cli()
