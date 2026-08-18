"""player_ids.py — one stable id per PERSON in the roster.

The Portraits and Goalie tabs list players out of the running game and key everything (the tree
row, the saved-assignment config, the Roster.ROS write) off `"First|Last"`. That string does two
jobs, and it only does one of them right:

  * A player occupies SEVERAL records — the club roster plus the free-agent / all-star / prospect
    pools — and the game may draw any of them, so those copies must collapse into one row and one
    assignment. `"First|Last"` does that correctly.
  * Two DIFFERENT players can carry the same name. The shipped roster has them, and community
    rosters have more. `"First|Last"` collapses those too, so the second one is INVISIBLE: you
    cannot see him, cannot select him, and an assignment meant for one silently lands on both.
    (Reported as "only one Elias Pettersson shows up in the Portraits filter".)

The fix is to key on the fields that distinguish namesakes but stay identical across pool copies
of one player: the **jersey number** and the **birth date**. Both are plain bit-fields in the
420-byte record (ros_live_editor's field map), so this costs nothing to read.

The id stays the bare `"First|Last"` whenever a name is unique in the roster — which is the
overwhelming majority, and which keeps every previously saved assignment working untouched. Only a
name that really is shared grows a suffix:

    "Elias|Pettersson#40"     jersey number, when the namesakes wear different numbers
    "Elias|Pettersson#40~2"   plus a stable ordinal, in the rare case they also share a number

⚠ VERIFICATION STATUS: the grouping RULE is verified by construction (it only ever splits records
that differ in number or birth date, and only within a shared name). The jersey-number field
offset itself is inherited from ros_live_editor.py's field map ("Jersey #", +0x20 bits 12..18) and
has NOT been re-confirmed in-game as part of this change. If it is wrong, the failure mode is
benign — namesakes fall back to being told apart by birth date, or stay collapsed as before.
"""
from __future__ import annotations
import struct

# Player-record bit-fields, offsets shared by the live array and Roster.ROS (same 420-byte struct).
OFF_BIRTH = 0x1C          # u32: bits 0-3 birth month, bits 4-15 birth year (bits 16-31 = portrait)
OFF_NUM   = 0x20          # u32: bits 12-18 jersey number, bits 27-31 birth day


def bio_fields(rec_bytes, base=0):
    """(jersey_number, birth_fingerprint) out of a 420-byte player record at `base`."""
    v1c = struct.unpack_from(">I", rec_bytes, base + OFF_BIRTH)[0]
    v20 = struct.unpack_from(">I", rec_bytes, base + OFF_NUM)[0]
    return (v20 >> 12) & 0x7F, (v1c & 0xFFFF, (v20 >> 27) & 0x1F)


def bare_id(rec) -> str:
    """The legacy name-only id. Still what a unique name resolves to, and the fallback that keeps
    assignments saved before this module existed pointing at the right player."""
    return f"{rec.get('first', '')}|{rec.get('last', '')}"


def _fingerprint(rec):
    """What makes this record a PERSON rather than a name: number + birth date."""
    return (rec.get("num"), rec.get("bio"))


def assign_player_ids(records):
    """Set `rec['pid']` on every record in `records` (mutates in place) and return the list.

    Records that are the same person get the same pid; namesakes get different ones. Safe to call
    on records that carry no number/birth fields — they simply all share the name's fingerprint and
    behave exactly as they did before."""
    by_name = {}
    for r in records:
        by_name.setdefault(bare_id(r), {}).setdefault(_fingerprint(r), []).append(r)
    for name, groups in by_name.items():
        if len(groups) == 1:                       # unique name — the bare id, as before
            for recs in groups.values():
                for r in recs:
                    r["pid"] = name
            continue
        # Namesakes. Order by (number, birth) so the ordinal is the same on every refresh.
        ordered = sorted(groups.items(), key=lambda kv: (kv[0][0] is None, kv[0]))
        used = {}
        for fp, recs in ordered:
            num = fp[0]
            pid = f"{name}#{num}" if num is not None else name
            n = used.get(pid, 0) + 1
            used[pid] = n
            if n > 1:                              # same name AND same number — keep them distinct
                pid = f"{pid}~{n}"
            for r in recs:
                r["pid"] = pid
    return records


def player_id(rec) -> str:
    """The pid of a record, falling back to the bare name for records that never went through
    assign_player_ids()."""
    return rec.get("pid") or bare_id(rec)


def label(rec) -> str:
    """How to show this player in a list: the name, plus the jersey number when the name alone is
    ambiguous (i.e. when the pid carries a suffix)."""
    name = rec.get("name") or bare_id(rec).replace("|", " ").strip()
    pid = player_id(rec)
    if "#" not in pid:
        return name
    num = rec.get("num")
    return f"{name}  #{num}" if num is not None else name


def resolve_saved(saved, rec):
    """Look a record up in a saved {pid: value} map, accepting a pre-existing bare-name entry.

    Returns (key_that_matched, value) or (None, None). Assignments saved before player ids existed
    are keyed by name only; they must keep applying, and they apply to every namesake — which is
    exactly what they did when they were written."""
    pid = player_id(rec)
    if pid in saved:
        return pid, saved[pid]
    bare = bare_id(rec)
    if bare in saved:
        return bare, saved[bare]
    return None, None
