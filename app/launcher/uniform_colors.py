"""uniform_colors.py — the uniform table in Roster.ROS: identity, slots and the colour palette.

**This is where jersey, font/number and equipment colours live** — NOT the team record. The team
record (chunk `0x8489FAF3`, see `team_colors.py`) holds only `primary`/`secondary`, which drive the
scoreclock and team-select chrome. Everything you actually see ON a player comes from the UNIFORM
record, chunk `0x1AEB24EC`, 407 rows x 284 (0x11C) bytes.

## The record

    +0x00  ->  BASE key string     `uniform_base_<key>_<slot>.iff`   (the shape/normal art)
    +0x04  ->  ASSET key string    `uniform_<key>_<slot>.iff`        (the stamps/overlay art)
    +0x08  ->  display label       "Home" / "Away" / "Alternate" / "Winter Classic '09" / ...
    +0x0C      u32 label id        an opaque localisation id, 1:1 with the label string
    +0x10      u32 uid             shared by every row of the same jersey family
    +0x14      u16 owning team id  (team record +0xF0, team_order frame)
    +0x18      u32 flags           slot = >>29 & 7; bit 0x8000 = "created team"
    +0x1C/0x20/0x24  3 x ARGB      accent colours (0x00000000 when unset)
    +0x28/0x2C/0x30  3 x u32       created-player bitfields — NOT colour, do not touch
    +0x034..+0x114   57 x ARGB     the palette (alpha is 0xFF in 407/407 rows at every one)
    +0x118           ARGB          constant #9C9C9C league-wide — left alone

The three pointers are self-relative (`target = field_offset + value - 1`) into the shared UTF-16BE
string pool, and `Uniform_FixupPointers @0x840B8408` resolves them at load.

## The palette split (CORRECTED 2026-08-04)

The old "39 jersey / 13 equipment" split was the wrong organising principle. Comparing each team's
home row against its away row, slot by slot, across all 30 teams:

    0x034 .. 0x0E0   44 slots   IDENTICAL home vs away in 29-30 of 30 teams  -> a PER-TEAM block
    0x0E4 .. 0x114   13 slots   home == away in only 0-21 of 30              -> a PER-UNIFORM block

So the boundary is at 0x0E4, and it is a team/uniform boundary, not a jersey/equipment one. The
per-uniform block is 1 lead colour then 4 items x 3 colours (verified on ANA, EDM, MIN, CGY, DAL).

Those five are now identified (2026-08-05, see ELEMENT_SLOTS): the lead colour is the HELMET
SHELL, and the four triples are the four GLYPH decals — name, number, sleeve number, helmet
number — each as outer / mid / fill. Not equipment at all. The gloves turned out to live in the
per-team block instead, which is why a club wears one pair across every kit.

What the palette does NOT paint: the jersey body, the pants and the socks. Those are texture,
in `uniform_base_<key>_<slot>.iff` — no palette slot moves them.

`#FF00FF` appears in the per-uniform block for WPG, BUF, CAR, CHI, DET, COL, CBJ and others. It is a
**sentinel**, not a colour: "unused, fall back to the default". `is_sentinel()` flags it so the
editor can say so instead of painting a magenta swatch.

The launcher used to expose only 0x048..0x114 (52 slots), which hid five genuinely per-team colours
— 0x038/0x03C/0x040/0x044 alone carry 71-74 distinct values across the 407 rows.

Individual slot *semantics* inside the 44-slot per-team block are still unidentified; the editor
shows them as a numbered swatch grid so you can see what you are changing.

## Adding a uniform

Slot 2 = "Alternate" is stock-supported (row 7, ATL, ships one) and 13 blank spare rows sit at the
end of the table (253-265, owners 106-109/200-201/244), so giving a team an extra jersey needs no
new bytes anywhere: claim a spare, point it at strings that already exist, copy the team's Home row.

The one hard constraint is **file order**. `Team_GetNthUniform` enumerates a team's rows in table
order and `team[+0xF2]/[+0xF3]` are indices into that list, so a new row must have a HIGHER row
index than the team's existing ones or home/away silently shift onto the wrong jersey. Every NHL
team's rows live at 0-146 and the spares at 253+, so claiming a spare is always safe; `add_uniform`
refuses anything that isn't.

Ship three assets alongside it (see extra_teams.FAMILY): `uniform_<asset>_alt.iff`,
`uniform_base_<base>_alt.iff` and `uniform_frontend_<asset>_alt.iff`. The last one is what the
team-select carousel draws — without it the new entry appears but renders nothing.
"""
from __future__ import annotations
import struct

UNIFORM_CHUNK = 0x1AEB24EC
STRIDE = 284

BASEKEY_OFF = 0x00         # -> `uniform_base_<key>_<slot>.iff`
ASSETKEY_OFF = 0x04        # -> `uniform_<key>_<slot>.iff`
LABEL_OFF = 0x08           # -> the display name shown in the jersey carousel
LABELID_OFF = 0x0C         # u32, 1:1 with the label string
UID_OFF = 0x10             # u32, shared across a jersey family
OWNER_OFF = 0x14           # u16 owning team id (team record +0xF0, team_order frame)
SLOT_OFF = 0x18            # >>29 & 7 -> 0 home, 1 away, 2 alt, 3 mii, 4/5 created-player band
CREATED_BIT = 0x8000       # set in +0x18 => render from the generic palette, not authored art

# ── byte 2 of +0x18: how this jersey wears a CHEST NUMBER, plus the throwback marker ──────────
# The stamps sheet carries ONE digit set and its DRAM slot table (see jersey_convert_profile.json)
# is byte-identical across all 30 clubs, so nothing in the ART says whether a jersey gets a number
# on the front. This is where it is decided, per UNIFORM ROW — which is why Dallas HOME and AWAY
# differ and why only Atlanta's ALTERNATE has one.
#
# BIG and SMALL are mutually exclusive across all 407 shipped rows (never both), so treat them as
# a 3-value enum rather than independent flags. Cross-checked against the shipped jerseys:
#   BIG    ATL alt, DAL home, DAL alt
#   SMALL  BUF home/away/alt, DAL away, NYI home/away, SJS home/away/alt, TBL home/away
# and nothing else in slots 0-2. (The `_mii`/`_CRT` created-player rows mirror their parent club.)
FRONTNUM_BIG = 0x00200000    # large number centred on the chest, below the crest
FRONTNUM_SMALL = 0x00400000  # small number on the upper RIGHT chest
FRONTNUM_MASK = FRONTNUM_BIG | FRONTNUM_SMALL
CLASSIC_BIT = 0x00800000     # throwback kit — set on every `*_CLxx` row (+ CGY's alt), nothing else

# ── the palette ──────────────────────────────────────────────────────────────
ACCENT_OFFS = (0x01C, 0x020, 0x024)     # ARGB, or 0x00000000 when unset
TEAM_LO, TEAM_HI = 0x034, 0x0E4         # 44 slots, shared by all of a team's uniforms
UNI_LO, UNI_HI = 0x0E4, 0x118           # 13 slots, this uniform only
TAIL_OFF = 0x118                        # constant #9C9C9C league-wide — never written

SLOT_OFFSETS = tuple(ACCENT_OFFS) + tuple(range(TEAM_LO, UNI_HI, 4))
NSLOTS = len(SLOT_OFFSETS)              # 60 = 3 accent + 44 team + 13 uniform

N_ACCENT = len(ACCENT_OFFS)                       # indices 0..2
N_TEAM = (TEAM_HI - TEAM_LO) // 4                 # indices 3..46
N_UNIFORM = (UNI_HI - UNI_LO) // 4                # indices 47..59
TEAM_FIRST = N_ACCENT
UNIFORM_FIRST = N_ACCENT + N_TEAM

# The per-uniform block: one lead colour then four equipment items of three colours each.
EQUIP_ITEMS = 4
EQUIP_COLORS = 3

# ── which slot paints what (MEASURED 2026-08-05) ──────────────────────────────────────────────
# Established by painting San Jose's live palette with test patterns and reading the result off
# in-game screenshots. Two coordinates were used so every slot is pinned by two independent
# observations: one pass colours by `slot % 8`, another by `slot // 8`, and an element's pair of
# colours names exactly one slot. Confirmation passes then greyed all but a candidate group.
#
# The stock values corroborate the whole thing: San Jose ships 141414/ce7b1a/f5f5f5 -- its real
# black/orange/white lettering -- at 48/49/50, and again at 51/52/53, 54/55/56 and 57/58/59. Four
# identical triplets is not a coincidence; the per-uniform block is one lead colour (the helmet
# shell) followed by four glyph sets of outer/mid/fill. That also settles what the "13 per-uniform
# slots = 4 equipment items" note above was groping at.
#
# Every glyph triplet is per-UNIFORM, so home and away can letter differently. Every glove slot is
# per-TEAM, so a club wears one pair of gloves across all its kits.
#
# The GLOVE is a 16-zone model, not a painted one. Its draw binds no albedo texture at all: the
# colour comes from a 32x32 palette texture the game rebuilds per team (RenderDoc id 16698),
# whose 16 filled COLUMNS are 16 solid colours, and every glove vertex carries a second UV whose
# u lands on exactly one of them -- so u*32 is a zone id and a glove is 15 flat regions plus a
# wordmark decal. The regions were read straight off the capture (tools/build_player_mesh.py
# bakes them per vertex) and NAMED from the UV layout, which is why they are specific: four
# back-of-hand panels with piping between them, eight finger segments, a thumb, a rolled cuff.
#
# Zone id == palette slot is the reading that fits every probe: the seven slots that visibly
# moved when poked in-game (3,4,5,6,7,9,10) are exactly the seven big outward-facing zones, and
# the ones that did not (8,11,13,14,15) are the palm and the welts, which the camera never sees.
ELEMENT_SLOTS = {
    # gloves (per-team) -- zone id == slot, see above
    "glove_cuff_opening": 0, "glove_cuff": 1, "glove_cuff_trim": 2, "glove_cuff_roll": 3,
    "glove_back_panels": 4, "glove_piping": 5, "glove_fingers": 6, "glove_gussets": 7,
    "glove_binding": 8, "glove_thumb": 9, "glove_welt": 10, "glove_wrist": 11,
    "glove_palm_fingers": 13, "glove_palm": 14, "glove_palm_heel": 15, "glove_logo": 30,
    # per-uniform
    "helmet_shell": 47,
    "name_outer": 48, "name_mid": 49, "name_fill": 50,
    "number_outer": 51, "number_mid": 52, "number_fill": 53,
    "sleeve_number_outer": 54, "sleeve_number_mid": 55, "sleeve_number_fill": 56,
    "helmet_number_outer": 57, "helmet_number_mid": 58, "helmet_number_fill": 59,
}
SLOT_ELEMENT = {v: k for k, v in ELEMENT_SLOTS.items()}

ELEMENT_LABEL = {
    "glove_cuff_opening": "Glove cuff opening", "glove_cuff": "Glove cuff",
    "glove_cuff_trim": "Glove cuff trim", "glove_cuff_roll": "Glove cuff roll",
    "glove_back_panels": "Glove back panels", "glove_piping": "Glove piping",
    "glove_fingers": "Glove fingers", "glove_gussets": "Glove gussets",
    "glove_binding": "Glove binding", "glove_thumb": "Glove thumb",
    "glove_welt": "Glove welt", "glove_wrist": "Glove wrist patch",
    "glove_palm_fingers": "Glove palm fingers", "glove_palm": "Glove palm",
    "glove_palm_heel": "Glove palm heel", "glove_logo": "Glove logo",
    "helmet_shell": "Helmet shell",
    "name_outer": "Name outline", "name_mid": "Name mid", "name_fill": "Name fill",
    "number_outer": "Number outline", "number_mid": "Number mid", "number_fill": "Number fill",
    "sleeve_number_outer": "Sleeve # outline", "sleeve_number_mid": "Sleeve # mid",
    "sleeve_number_fill": "Sleeve # fill",
    "helmet_number_outer": "Helmet # outline", "helmet_number_mid": "Helmet # mid",
    "helmet_number_fill": "Helmet # fill",
}

# (fill, mid, outer) per glyph decal. That is also the order the glyph sheets encode: each cell
# carries three disjoint layer masks in its colour channels — B = fill, G = mid, R = outer — so
# this tuple maps straight onto them (see jersey_preview._key_recolour).
GLYPH_TRIPLETS = {
    "name": (50, 49, 48),
    "number": (53, 52, 51),
    "sleeve_number": (56, 55, 54),
    "helmet_number": (59, 58, 57),
}

SENTINEL = "#FF00FF"       # "unused / use the default" — not a real colour

# Palette a created-team slot ships with — the tell-tale of an unfixed expansion team.
GREY_DEFAULTS = {"202020", "b2b2b2", "868698", "ffffa0", "dba758", "231f20"}

SLOT_NAMES = {0: "Home", 1: "Away", 2: "Alternate", 3: "Mii"}
SLOT_SUFFIX = {0: "home", 1: "away", 2: "alt"}      # Uniform_GetSlot; anything else -> "mii"


class UniformError(RuntimeError):
    pass


# ── chunk / row plumbing ─────────────────────────────────────────────────────

def chunk(ros):
    """The 407 x 284 uniform chunk. (0x1AEB24EC also names a 3922 x 12 chunk — pick by stride.)"""
    for c in ros.chunks:
        if c.hash == UNIFORM_CHUNK and c.stride == STRIDE:
            return c
    raise UniformError("no 407x284 uniform chunk (0x1AEB24EC) in this Roster.ROS")


def row_count(ros) -> int:
    return chunk(ros).nrec


def _off(ros, i) -> int:
    return ros.rec_off(chunk(ros), i)


def _row(ros, i) -> memoryview:
    b = _off(ros, i)
    return memoryview(ros.data)[b:b + STRIDE]


def owner(ros, i) -> int:
    return struct.unpack_from(">H", _row(ros, i), OWNER_OFF)[0]


def flags(ros, i) -> int:
    return struct.unpack_from(">I", _row(ros, i), SLOT_OFF)[0]


def slot(ros, i) -> int:
    return (flags(ros, i) >> 29) & 7


def front_number(ros, i) -> str:
    """`'big'`, `'small'` or `'none'` — the chest number this uniform wears."""
    f = flags(ros, i)
    return "big" if f & FRONTNUM_BIG else "small" if f & FRONTNUM_SMALL else "none"


def set_front_number(ros, i, mode: str):
    """Set the chest number to `'big'` / `'small'` / `'none'`.

    Clears the other bit first: no shipped row has both, and the two placements would collide on
    the chest anyway. Costs nothing in the art — the digits come from the stamps sheet either way.
    """
    if mode not in ("big", "small", "none"):
        raise UniformError(f"front number must be big/small/none, got {mode!r}")
    fo = _off(ros, i) + SLOT_OFF
    cur = struct.unpack_from(">I", ros.data, fo)[0] & ~FRONTNUM_MASK
    bit = FRONTNUM_BIG if mode == "big" else FRONTNUM_SMALL if mode == "small" else 0
    struct.pack_into(">I", ros.data, fo, cur | bit)


def set_classic(ros, i, on: bool):
    """Mark/unmark this row as a throwback. Cosmetic-only as far as anything found so far goes."""
    fo = _off(ros, i) + SLOT_OFF
    cur = struct.unpack_from(">I", ros.data, fo)[0]
    struct.pack_into(">I", ros.data, fo, (cur | CLASSIC_BIT) if on else (cur & ~CLASSIC_BIT))


def is_classic(ros, i) -> bool:
    """Throwback kit — set on every shipped `*_CLxx` row and on Calgary's alternate."""
    return bool(flags(ros, i) & CLASSIC_BIT)


def slot_name(s: int) -> str:
    return SLOT_NAMES.get(s, f"Slot {s}")


def slot_suffix(s: int) -> str:
    """The filename suffix the game composes for this slot (`Uniform_GetSlot`)."""
    return SLOT_SUFFIX.get(s, "mii")


def rows_for_team(ros, team_id: int) -> list:
    """Uniform row indices owned by `team_id`, in file order — the order the carousel shows and
    the order `team[+0xF2]/[+0xF3]` index into."""
    return [i for i in range(row_count(ros)) if owner(ros, i) == team_id]


# ── strings ──────────────────────────────────────────────────────────────────

def _target(d, o):
    """Self-relative pointer at `o` -> absolute file offset, or None when null."""
    v = struct.unpack_from(">I", d, o)[0]
    if v == 0:
        return None
    sv = v - (1 << 32) if v & 0x80000000 else v
    t = o + sv - 1
    return t if 0 <= t < len(d) - 1 else None


def _selfrel(target_off, field_off) -> int:
    return (target_off - field_off + 1) & 0xFFFFFFFF


def _read_str(d, off) -> str:
    if off is None:
        return ""
    e = d.find(b"\x00\x00", off)
    if e < 0 or e - off > 256:
        return ""
    if (e - off) % 2:
        e += 1
    try:
        return d[off:e].decode("utf-16-be")
    except UnicodeDecodeError:
        return ""


def _enc(text: str) -> bytes:
    return text.encode("utf-16-be") + b"\x00\x00"


def _str_at(ros, i, field) -> str:
    return _read_str(ros.data, _target(ros.data, _off(ros, i) + field))


def base_key(ros, i) -> str:
    return _str_at(ros, i, BASEKEY_OFF)


def asset_key(ros, i) -> str:
    return _str_at(ros, i, ASSETKEY_OFF)


def label(ros, i) -> str:
    return _str_at(ros, i, LABEL_OFF)


def label_id(ros, i) -> int:
    return struct.unpack_from(">I", _row(ros, i), LABELID_OFF)[0]


def uid(ros, i) -> int:
    return struct.unpack_from(">I", _row(ros, i), UID_OFF)[0]


def read_row(ros, i) -> dict:
    """Everything the editor needs about one uniform row."""
    s = slot(ros, i)
    return {"row": i, "owner": owner(ros, i), "slot": s, "slot_name": slot_name(s),
            "suffix": slot_suffix(s), "label": label(ros, i), "label_id": label_id(ros, i),
            "base_key": base_key(ros, i), "asset_key": asset_key(ros, i),
            "uid": uid(ros, i), "flags": flags(ros, i),
            "created": bool(flags(ros, i) & CREATED_BIT),
            "blank": not (base_key(ros, i) or asset_key(ros, i))}


def label_catalog(ros) -> dict:
    """{label text: label id} for every name already used in this roster's uniform table.

    Read from the file rather than hard-coded: the mapping is 1:1 across all 407 stock rows (23
    distinct names), and the id is opaque — there is no hash of the string that reproduces it, so
    reusing a shipped pair is the only way to get one that is certainly right.
    """
    out = {}
    for i in range(row_count(ros)):
        t = label(ros, i)
        if t:
            out.setdefault(t, label_id(ros, i))
    return out


# ── the string pool (for a label the game never shipped) ─────────────────────

def _pool_bounds(ros) -> tuple:
    """(lo, hi) of the region the uniform table's strings live in."""
    d = ros.data
    targets = []
    for i in range(row_count(ros)):
        b = _off(ros, i)
        for f in (BASEKEY_OFF, ASSETKEY_OFF, LABEL_OFF):
            t = _target(d, b + f)
            if t is not None:
                targets.append(t)
    if not targets:
        raise UniformError("no uniform string pointers resolve — wrong chunk framing?")
    lo, hi = min(targets), max(targets)
    e = d.find(b"\x00\x00", hi)
    return lo, (e + 2 if e >= 0 else hi)


def _live_spans(ros, lo, hi) -> bytearray:
    """A coverage map of [lo,hi): 1 where a string (ours OR anyone else's) lives.

    The pool is shared — team names, arena names and coach names sit in the same run — so the map
    is built by walking it linearly rather than from our own pointers. That is what stops a gap
    hunt from eating a string this module cannot see (the bug that once produced "TD GardenMRL").
    """
    d = ros.data
    covered = bytearray(hi - lo)
    p = lo
    while p < hi - 1:
        e = d.find(b"\x00\x00", p)
        if e < 0 or e >= hi:
            break
        if (e - p) % 2:
            e += 1
        if e > p:                                   # a real string, not a run of padding
            covered[p - lo:e + 2 - lo] = b"\x01" * min(e + 2 - p, hi - p)
            p = e + 2
        else:
            p += 2
    return covered


def free_gaps(ros, min_len=6) -> list:
    """[(offset, length)] — runs of zero bytes inside the pool that no string reaches into.

    These are the only bytes in the file that can be spent for free: the pool is otherwise packed
    solid, and growing it is impossible (every write here is size-invariant).
    """
    d = ros.data
    lo, hi = _pool_bounds(ros)
    covered = _live_spans(ros, lo, hi)
    out, run = [], None
    for k in range(hi - lo):
        isfree = not covered[k] and d[lo + k] == 0
        if isfree and run is None:
            run = k
        elif not isfree and run is not None:
            if k - run >= min_len:
                out.append((lo + run, k - run))
            run = None
    if run is not None and (hi - lo) - run >= min_len:
        out.append((lo + run, (hi - lo) - run))
    safe = []
    for o, n in out:
        o += (o - lo) & 1                           # strings start on the pool's 2-byte grid
        n -= (o - lo) & 1
        # A gap opening right after a NON-zero pair is not free: those two bytes terminate the
        # string that ends there, and writing over them runs it into whatever we allocate.
        if o >= 2 and d[o - 2:o] != b"\x00\x00":
            o, n = o + 2, n - 2
        if n >= min_len:
            safe.append((o, n))
    return safe


def find_string(ros, text: str):
    """Pool offset already holding exactly `text`, or None. Sharing beats allocating."""
    if not text:
        return None
    d, blob = ros.data, _enc(text)
    lo, hi = _pool_bounds(ros)
    p = d.find(blob, lo, hi)
    while p >= 0:
        # must be a whole string, i.e. start right after another string's terminator
        if (p - lo) % 2 == 0 and (p < 2 or d[p - 2:p] == b"\x00\x00"):
            return p
        p = d.find(blob, p + 2, hi)
    return None


def intern_string(ros, text: str) -> int:
    """Pool offset for `text`, reusing an existing copy or writing it into a free gap.

    Raises UniformError if there is nowhere to put it — which is a real outcome, not a bug: the
    pool has no slack, and the file size may never change.
    """
    off = find_string(ros, text)
    if off is not None:
        return off
    blob = _enc(text)
    gaps = [g for g in free_gaps(ros) if g[1] >= len(blob)]
    if not gaps:
        biggest = max([n for _, n in free_gaps(ros)], default=0)
        raise UniformError(
            f"no room in the string pool for {text!r} ({len(blob)} bytes); the largest free gap is "
            f"{biggest} bytes. Pick a shorter name, or one the game already ships.")
    gaps.sort(key=lambda g: g[1])                   # best fit — long names need the big gaps
    o, _n = gaps[0]
    ros.data[o:o + len(blob)] = blob
    return o


def _empty_string_off(ros):
    """A pool offset holding the empty string.

    The blank spare rows all point their key fields at one, so borrow theirs rather than hunt for
    a bare `00 00` — a terminator that happens to sit between two live strings would work today
    and break the moment either of them is rewritten.
    """
    d = ros.data
    for i in range(row_count(ros)):
        b = _off(ros, i)
        for f in (BASEKEY_OFF, ASSETKEY_OFF):
            t = _target(d, b + f)
            if t is not None and d[t:t + 2] == b"\x00\x00":
                return t
    return None


def _point_at(ros, i, field, text: str):
    """Point row `i`'s `field` at `text` (interning it if need be). Empty text -> the pool's
    empty string, which is what the blank spare rows already use."""
    fo = _off(ros, i) + field
    if not text:
        off = _empty_string_off(ros)
        if off is None:
            raise UniformError("cannot clear this field: no empty string in the pool to point at")
    else:
        off = intern_string(ros, text)
    struct.pack_into(">I", ros.data, fo, _selfrel(off, fo))


def set_base_key(ros, i, text: str):
    """`uniform_base_<text>_<slot>.iff` — the shape/normal art."""
    _point_at(ros, i, BASEKEY_OFF, text.strip())


def set_asset_key(ros, i, text: str):
    """`uniform_<text>_<slot>.iff` and `uniform_frontend_<text>_<slot>.iff` — the stamps art."""
    _point_at(ros, i, ASSETKEY_OFF, text.strip())


def set_label(ros, i, text: str, label_id_=None):
    """Set the carousel display name.

    A name from `label_catalog()` brings its shipped id with it. A novel name has no id that can
    be derived (the id is not a hash of the string — crc32 of the text, its uppercase and its
    UTF-16 encoding were all checked and none match), so `label_id_` defaults to the row's current
    id: the string is what the carousel draws, and leaving a valid id in place is safer than
    inventing one.
    """
    text = text.strip()
    if not text:
        raise UniformError("a uniform needs a name")
    if label_id_ is None:
        label_id_ = label_catalog(ros).get(text)
    _point_at(ros, i, LABEL_OFF, text)
    if label_id_ is not None:
        struct.pack_into(">I", ros.data, _off(ros, i) + LABELID_OFF, label_id_ & 0xFFFFFFFF)


def set_slot(ros, i, s: int):
    """Rewrite only the slot bits, keeping every other flag the donor row carried."""
    if not 0 <= s <= 7:
        raise UniformError(f"slot {s} out of range 0..7")
    fo = _off(ros, i) + SLOT_OFF
    cur = struct.unpack_from(">I", ros.data, fo)[0]
    struct.pack_into(">I", ros.data, fo, (s << 29) | (cur & 0x1FFFFFFF))


def set_owner(ros, i, team_id: int):
    struct.pack_into(">H", ros.data, _off(ros, i) + OWNER_OFF, team_id & 0xFFFF)


def set_created_flag(ros, i, on: bool):
    """Bit 0x8000 of +0x18. Set => the game renders the generic palette instead of the authored
    texture, which is how a cloned expansion team ends up looking black and white."""
    fo = _off(ros, i) + SLOT_OFF
    cur = struct.unpack_from(">I", ros.data, fo)[0]
    struct.pack_into(">I", ros.data, fo, (cur | CREATED_BIT) if on else (cur & ~CREATED_BIT))


# ── colours ──────────────────────────────────────────────────────────────────

def group_of(index: int) -> str:
    """'accent' | 'team' | 'uniform' — which block a palette index falls in."""
    if index < TEAM_FIRST:
        return "accent"
    return "team" if index < UNIFORM_FIRST else "uniform"


def color_label(index: int) -> str:
    """A human name for a palette index.

    Slots in `ELEMENT_SLOTS` were measured in-game and are named for what they paint. The rest
    are still only known by position, and say so rather than guessing.
    """
    el = SLOT_ELEMENT.get(index)
    if el:
        return ELEMENT_LABEL[el]
    if index < TEAM_FIRST:
        return f"Accent {index + 1}"
    if index < UNIFORM_FIRST:
        return f"Team {index - TEAM_FIRST + 1}"
    return f"Kit {index - UNIFORM_FIRST + 1}"


def element_color(ros, i, element: str) -> str:
    """'#RRGGBB' for a named element on uniform row `i`."""
    return get_colors(ros, i)[ELEMENT_SLOTS[element]]


def glyph_colors(ros, i, glyph: str) -> tuple:
    """(fill, mid, outer) hex for one of `GLYPH_TRIPLETS` — the renderer's layer order."""
    cols = get_colors(ros, i)
    return tuple(cols[s] for s in GLYPH_TRIPLETS[glyph])


def get_colors(ros, i) -> list:
    """The row's 60 palette entries as '#RRGGBB'. Unset accents (ARGB 0x00000000) read '#000000';
    use `is_unset` to tell them apart."""
    r = _row(ros, i)
    return ["#%02X%02X%02X" % tuple(r[o + 1:o + 4]) for o in SLOT_OFFSETS]


def get_argb(ros, i) -> list:
    r = _row(ros, i)
    return [struct.unpack_from(">I", r, o)[0] for o in SLOT_OFFSETS]


def is_unset(ros, i, index: int) -> bool:
    """True for an accent slot the roster leaves at 0x00000000 (48 of the 407 rows do)."""
    return get_argb(ros, i)[index] == 0


def is_sentinel(color: str) -> bool:
    """`#FF00FF` in the per-uniform block means 'unused, fall back to the default' — the game
    never paints it, so the editor should say so rather than show a magenta swatch."""
    return (color or "").upper().lstrip("#") == SENTINEL.lstrip("#")


def set_color(ros, i, index: int, rgb: str):
    """Write one palette slot. `rgb` is '#RRGGBB' or 'RRGGBB'. Alpha is forced to 0xFF."""
    if not 0 <= index < NSLOTS:
        raise IndexError(f"colour slot {index} out of range 0..{NSLOTS - 1}")
    h = rgb.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad colour {rgb!r}")
    ros.set_bytes(chunk(ros), i, SLOT_OFFSETS[index], bytes([0xFF]) + bytes.fromhex(h))


def clear_color(ros, i, index: int):
    """Zero an accent slot back to 'unset'. Only meaningful for indices 0..2."""
    ros.set_bytes(chunk(ros), i, SLOT_OFFSETS[index], b"\x00\x00\x00\x00")


_SPANS = ((ACCENT_OFFS[0], ACCENT_OFFS[-1] + 4), (TEAM_LO, UNI_HI))
PALETTE_LEN = sum(hi - lo for lo, hi in _SPANS)


def palette_bytes(ros, i) -> bytes:
    r = _row(ros, i)
    return b"".join(bytes(r[lo:hi]) for lo, hi in _SPANS)


def set_palette_bytes(ros, i, blob: bytes):
    if len(blob) != PALETTE_LEN:
        raise ValueError(f"palette must be {PALETTE_LEN} bytes, got {len(blob)}")
    p = 0
    for lo, hi in _SPANS:
        ros.set_bytes(chunk(ros), i, lo, blob[p:p + hi - lo])
        p += hi - lo


def is_default_grey(ros, i) -> bool:
    """True when the row still carries the created-team grey palette (=> renders black & white)."""
    cols = {c.lstrip("#").lower() for c in get_colors(ros, i)}
    return len(cols & GREY_DEFAULTS) >= 4


def clone_team(ros, donor_team: int, target_team: int, log=print) -> int:
    """Copy the donor team's uniform palettes onto the target's rows, matched by uniform slot
    (home->home, away->away). Only colour bytes move — every identity field (the three
    self-relative pointers, unique id, owning team, slot flags) is left untouched. Returns the
    number of rows written."""
    donor = {slot(ros, i): i for i in reversed(rows_for_team(ros, donor_team))}
    n = 0
    for i in rows_for_team(ros, target_team):
        s = slot(ros, i)
        src = donor.get(s)
        if src is None:
            log(f"  uniform row {i} ({slot_name(s)}): donor team {donor_team} has no {slot_name(s)} row — skipped")
            continue
        set_palette_bytes(ros, i, palette_bytes(ros, src))
        log(f"  uniform row {i} ({slot_name(s)}) <- donor row {src}: {NSLOTS} colours copied")
        n += 1
    return n


# ── which team owns which rows ───────────────────────────────────────────────

TEAMID_OFF = 0xF0          # team_order frame; the u16 the uniform table's +0x14 matches


def team_index(ros) -> list:
    """[{rec, code, akey, team_id}] for every league-0 team, in record order.

    The owning id is the **u16 at team record +0xF0**, not the byte at +0xC8: the "2K" team's id is
    333, which a byte cannot hold, and its uniforms (rows 266-268) carry owner 333.
    """
    try:
        import team_order as TO
    except ImportError:                                  # imported as launcher.uniform_colors
        from launcher import team_order as TO
    d = ros.data
    base, n = TO.find_table(d)
    out = []
    for i in range(n):
        r = base + i * TO.STRIDE
        if (TO._u32(d, r + TO.OFF_LEAGUE) >> 28) != 0:
            continue
        code = TO._text(d, r + TO.OFF_CODE).strip()
        if not code:
            continue
        out.append({"rec": i, "code": code.upper(),
                    "akey": TO._text(d, r + TO.OFF_AKEY).strip(),
                    "team_id": struct.unpack_from(">H", d, r + TEAMID_OFF)[0]})
    return out


HOME_PICK_OFF = 0xF2       # team_order frame; both are indices into the team's OWN uniform list
AWAY_PICK_OFF = 0xF3


def selected_indices(ros, team_rec: int) -> tuple:
    """(home, away) — the team record's +0xF2/+0xF3, which are positions in the team's own uniform
    list (0 = its first row in file order), not row numbers.

    This is what makes row ORDER matter: renumber the list and these two silently point at a
    different jersey. `add_uniform` refuses to claim a row that would shift them.
    """
    try:
        import team_order as TO
    except ImportError:
        from launcher import team_order as TO
    base, _n = TO.find_table(ros.data)
    r = base + team_rec * TO.STRIDE
    return ros.data[r + HOME_PICK_OFF], ros.data[r + AWAY_PICK_OFF]


def team_id_for(ros, code: str):
    """Owning team id for a team CODE, or None."""
    c = code.strip().upper()
    return next((t["team_id"] for t in team_index(ros) if t["code"] == c), None)


# ── adding a uniform ─────────────────────────────────────────────────────────

def spare_rows(ros) -> list:
    """Row indices with no base or asset key — free to claim.

    On a stock roster these are 253-265 (owners 106-109, 200-201 and 244), thirteen of them, all
    sitting past every NHL team's rows so claiming one can never disturb `team[+0xF2]/[+0xF3]`.
    """
    return [i for i in range(row_count(ros)) if read_row(ros, i)["blank"]]


def add_uniform(ros, team_id: int, name: str, uniform_slot: int = 2,
                donor_row: int = None, base_key_: str = None, asset_key_: str = None,
                row: int = None, log=print) -> int:
    """Give `team_id` another jersey. Returns the claimed row index.

    The new row is a byte-for-byte copy of `donor_row` (the team's Home row unless told otherwise)
    with the slot bits, owner, name and the two asset keys rewritten — so it inherits a palette
    that already looks like this team, plus whatever the donor's other +0x18 flags were, instead of
    the grey created-team default.

    Nothing grows: the row already exists, and the strings are shared with rows that already use
    them. `name` may be any label the roster already ships (see `label_catalog`) or a novel one,
    which is written into a dead gap in the string pool if one is big enough.
    """
    mine = rows_for_team(ros, team_id)
    if donor_row is None:
        home = [i for i in mine if slot(ros, i) == 0]
        if not home:
            raise UniformError(f"team {team_id} has no Home uniform to copy")
        donor_row = home[0]
    if row is None:
        spares = spare_rows(ros)
        if not spares:
            raise UniformError(
                "no spare uniform rows left in this roster. The table is a fixed 407 rows and the "
                "file size can never change, so the 13 blank rows are the whole budget.")
        row = spares[0]
    if mine and row < max(mine):
        raise UniformError(
            f"row {row} sits before team {team_id}'s existing rows (highest is {max(mine)}). "
            "Team_GetNthUniform walks the table in order and the team record's home/away indices "
            "point into that list, so an earlier row would shift them onto the wrong jersey.")

    c = chunk(ros)
    ros.set_bytes(c, row, 0, ros.record(c, donor_row))       # whole record, palette included
    set_owner(ros, row, team_id)
    set_slot(ros, row, uniform_slot)
    set_created_flag(ros, row, False)
    set_label(ros, row, name)
    set_base_key(ros, row, base_key_ if base_key_ is not None else base_key(ros, donor_row))
    set_asset_key(ros, row, asset_key_ if asset_key_ is not None else asset_key(ros, donor_row))

    r = read_row(ros, row)
    log(f"  uniform row {row} <- row {donor_row}: team {team_id}, {r['slot_name']} \"{r['label']}\"")
    log(f"    needs uniform_{r['asset_key']}_{r['suffix']}.iff, "
        f"uniform_base_{r['base_key']}_{r['suffix']}.iff and "
        f"uniform_frontend_{r['asset_key']}_{r['suffix']}.iff")
    return row


NO_OWNER = 0xFFFF          # an id no team has — the shipped spares use 106-109/200-201/244


def release_uniform(ros, row: int, log=print):
    """Hand a row back to the spare pool — blank both keys and disown it.

    The owner goes to `NO_OWNER`, **not** 0: Anaheim's team id IS 0, so zeroing would quietly hand
    the row to the Ducks. The shipped blank rows do the same thing with ids no club uses.

    Only ever call this on a row this module added. Blanking a shipped row would leave the team
    record's home/away indices pointing past the end of its (now shorter) list.
    """
    set_base_key(ros, row, "")
    set_asset_key(ros, row, "")
    set_owner(ros, row, NO_OWNER)
    log(f"  uniform row {row} released back to the spare pool")


def required_assets(ros, row: int) -> list:
    """The three .iff files this row makes the game ask for, in the order they matter.

    `uniform_frontend_*` is last but not least — it is the team-select carousel's own art, a
    separate asset from the in-game uniform, and its absence is exactly why a hand-added jersey
    shows up in the list and then renders nothing.
    """
    r = read_row(ros, row)
    s = r["suffix"]
    return [f"uniform_{r['asset_key']}_{s}.iff",
            f"uniform_base_{r['base_key']}_{s}.iff",
            f"uniform_frontend_{r['asset_key']}_{s}.iff"]
