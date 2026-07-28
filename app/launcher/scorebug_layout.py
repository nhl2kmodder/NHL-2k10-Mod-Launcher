"""scorebug_layout.py — per-element scoreclock (scorebug) LAYOUT editor.

The in-game scorebug is a small 3D scene serialized in overlay_static.iff DRAM blob0
(Maya-style: joints + textured quad meshes, names kept as UTF-16BE strings, name refs as
crc32(name) of the RAW case-sensitive string — unlike asset hashes, which uppercase first).

Two element kinds, both position-editable:

TEXT elements (font-rendered at runtime) draw from the TEXT RECORD TABLE (verified in-game
2026-07-16: skeleton-joint edits do nothing — the joints are only the bind pose; this table
is the baked copy the game consumes):
  text record, stride 0xE0, keyed by [crc32('scorebug_text')][crc32(name)]:
    +0x00 table key (crc 'scorebug_text')   +0x04 element name hash
    +0x68 X (f32 BE)    +0x6C Y      +0x70 Z      +0x74 W (1.0)
    +0x7C max text width (clip box)   +0x84 second metric (15.0)   +0x88 color u32
    +0xD8 bind hash     +0xDC FONT hash (corrected 2026-07-28; +0x00 is NOT the font)
  All 10 text elements have a record: team1/team2 (abbrevs), team1/2_score, quarter,
  gameclock1-4, gameclock_semi. The same hash pair also starts the (shorter, stride 0x38)
  anim-binding table records — disambiguated by validating W==1.0 and a sane position.
  A matching 13-joint bind-pose skeleton (stride 0x30, [crc][parent/child/sib s16][XYZW
  @+0xC]) exists at hash('scorebug_text'); it is kept in sync on edit but not consumed.

MESH elements (logos, team color bars, glints, separator, glows, 2K/SN logo) carry their
placement in their VERTEX BUFFERS:
  vertex = [X f32][Y f32][Z f32][color u32][optional uv u32 ...][0x000000FF][0x00000000]
  Stride varies per mesh (0x18 untextured, 0x1C textured, bigger for the glow cylinder) —
  the trailing 000000FF/00000000 pair is constant, so consecutive pair spacing reveals the
  stride and the vertex start. A mesh block starts with [crc32(name)][FF FF FF FF]
  [FF FF 00 00] and ends at the element's inline UTF-16BE name (stored twice). Moving a
  mesh = translating every vertex X/Y in its block. gameclock4's joint slot is a
  different (mesh-style) record — not yet editable, needs its own RE.

Edits are written back with overlay_editor.load_dram/apply_dram (re-encode DRAM +
relocate the iff — the path already proven in-game for overlay_static).
Verified data points: team1_color verts X=-42.7/-37.2 vs team2_color X=-129.7/-125.0
with identical Y/Z (the two side-by-side color bars) — position really is per-vertex.
"""
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import overlay_editor as oe
import archive_textures as at
import scorebug_anim_descs as sad
import scorebug_add_shots as sas

IFF = "overlay_static.iff"

TEXT_JOINTS = ["scorebug_text", "scorebug_dummy", "joint13", "team2", "team1",
               "team2_score", "team1_score", "quarter", "gameclock1", "gameclock2",
               "gameclock_semi", "gameclock3", "gameclock4"]

MESHES = ["logo_away", "logo_home", "team1_colorblur", "team2_colorblur", "team1_color",
          "team1_glint1", "team1_glint2", "team2_color", "team2_glint1", "team2_glint2",
          "glint_separator", "glint_separator1", "logo_2k_mesh", "glow_cylinder_color",
          "glow_white"]

# Elements exposed to the user (scorebug_text/dummy/joint13 are rig plumbing). All ten
# text elements have a draw record — including gameclock4, whose skeleton slot is odd.
# shots_away/shots_home = the SOG elements added by scorebug_add_shots (2026-07-28);
# gameclock4 is repurposed as their "Shots" label. Their records live in the RELOCATED
# table at the end of blob0, found by the same signature scan.
EDITABLE_TEXT = ["away_abbrev", "team1", "team2", "team1_score", "team2_score", "quarter",
                 "gameclock1", "gameclock2", "gameclock_semi", "gameclock3", "gameclock4",
                 "shots_away", "shots_home"]
# The three SOG elements (Away Shots, Home Shots, and the "SHOTS" label = the repurposed
# gameclock4 record) are JOINTLESS — they render straight from their draw record, with no
# scene joint. The anim TRANSFORM-DESC consts scale lever (set_text_scale_by_bind) only takes
# effect through a joint, so it is inert for these three (in-game report 2026-07-28: they are
# the ONLY elements that don't scale in the tab). For them, glyph scale is driven by the draw
# record's OWN local-matrix diagonal instead (m00 @+0x38 = X, m11 @+0x4C = Y; stock 1.0).
SOG_JOINTLESS = {"shots_away", "shots_home", "gameclock4"}
TXMATRIX_M00 = 0x38            # local 4x4 matrix diagonal: X-scale (m00), Y-scale (m11 @+0x4C)
TXMATRIX_M11 = 0x4C
# Record +0xB0 (big-endian u32) = a per-element UPPERCASE flag applied at text-draw time: the
# rendered glyphs are forced to caps, independent of the string source. PROVEN by the stock
# pattern — the only two elements that are always caps in-game (the two team abbreviations:
# records away_abbrev + team2) are the only records with this flag == 1; every other text record
# has 0. Setting it on the "Shots" label (gameclock4) -> "SHOTS"; on the PERIOD element -> "1ST"/
# "2ND"/"3RD". NOTE the period is rendered by record 'team1_score' (its bind is QUARTER); the
# record literally named 'quarter' binds a CLOCK DIGIT, not the period (2K reused record names).
UPPERCASE_FLAG = 0xB0
# away_abbrev = the AWAY team abbreviation. Its draw record has a BLANK font/name hash (0x0), so
# it can't be found by name — it sits exactly one record (0xE0) BEFORE team2, same size (38), same
# row. Identified structurally in _find_text_records. (The user's "go back one" hunch was right.)

# Text elements move by writing their POSITIONING JOINT's pos2 (+0x1C); the draw-record +0x68 matrix
# row does NOT move text. Named elements pair with a same-named joint by hash. The away abbrev's draw
# record has a BLANK name hash, so it has no same-named joint — but the skeleton has exactly 11
# positioning joints for 11 text elements, and its one otherwise-unclaimed joint, "joint13", sits at
# the away abbrev's exact stock position (-207.28, -149.48). That joint drives the away abbrev — so
# read/write its position THROUGH joint13 (this is what makes the away abbrev actually move in-game,
# the same way the home abbrev does via its own joint). joint13 is a sibling of the other joints (they
# store WORLD positions, not offsets from it), so moving it moves only the away abbrev, not the clock.
_JOINT_FOR = {"away_abbrev": "joint13"}

# Sides verified in-game 2026-07-16 by geometry + marker tests: the strip reads AWAY (left)
# then HOME (right). 2K numbered TEXT and MESH elements OPPOSITELY: text team1=left/away,
# team2=right/home (team2 +10y moved the home abbrev); meshes team2_*=left/away,
# team1_*=right/home (team2_color -10x moved the away bar).
# Element->glyph mapping DEFINITIVELY VERIFIED 2026-07-17 by the user's per-element recolor
# screenshot (Result.JPG): each element got a unique colour, matched to the in-game glyph.
#   team1(red)=AWAY SCORE  team2(green)=HOME ABBREV  team2_score(cyan)=HOME SCORE
#   quarter(magenta)=clock D1  gameclock1(blue)=clock D2  gameclock_semi(yellow)=COLON
#   gameclock2(cyan)=clock D3  gameclock3(purple)=clock D4
#   team1_score(dark) + gameclock4(orange) = NOT visible in the bar (unused / other state)
#  TODO Record names are misleading — 2K reused them.
# Positions: abbrevs/logos/bars MOVE via file edit; scores/clock/period are RUNTIME-PLACED
# (colour changes, position does NOT) — flagged below.
LABELS = {
    "away_abbrev": "Away Abbreviation",
    "team2": "Home Abbreviation",
    "team1": "Away Score",
    "team2_score": "Home Score",
    "logo_away": "Away Logo", "logo_home": "Home Logo",
    "team1_color": "Home Color Bar", "team2_color": "Away Color Bar",
    "team1_colorblur": "Home Color Glow", "team2_colorblur": "Away Color Glow",
    "team1_glint1": "Home Color Bar Separator 1", "team1_glint2": "Home Color Bar Separator 2",
    "team2_glint1": "Away Color Bar Separator 1", "team2_glint2": "Away Color Bar Separator 2",
    "quarter": "Clock — digit 1",
    "gameclock1": "Clock — digit 2",
    "gameclock_semi": "Clock — digit 3",
    "gameclock2": "Clock — colon",
    "gameclock3": "Clock — digit 4",
    "team1_score": "Period",
    "glint_separator": "Bottom Cyan Separator Bar 1", "glint_separator1": "Bottom Cyan Separator Bar 2",
    "gameclock4": "Shots Label (\"Shots\")",
    "shots_away": "Away Shots (SOG)", "shots_home": "Home Shots (SOG)",
    "logo_2k_mesh": "2K/SN logo panel", "glow_cylinder_color": "Bottom glow bar",
    "glow_white": "White glow",
}

# Pristine positions (text: joint pos; mesh: vertex centroid), captured 2026-07-16 from the
# unmodified scene. restore = move each element by (stock - current).
STOCK_POS = {
    "away_abbrev": (-207.28, -149.48),
    "team1": (-147.122, -149.482), "team2": (-118.292, -149.482),
    "team1_score": (12.1044, -150.92), "team2_score": (-58.9993, -149.482),
    "quarter": (-24.6701, -150.92),
    "gameclock1": (-16.2205, -150.92), "gameclock2": (-11.5774, -150.92),
    "gameclock_semi": (-7.19409, -150.92), "gameclock3": (1.2977, -150.92),
    # SOG row (authored 2026-07-28, not 2K stock): gameclock4 = the "Shots" label between
    # the two counters. (gameclock4's 2K-pristine pos was (-183.193, 13.5165), unused.)
    "gameclock4": (-118.0, -166.0),
    "shots_away": (-145.0, -166.0), "shots_home": (-75.0, -166.0),
    "logo_away": (-160.183, -156.738), "logo_home": (-71.7836, -156.738),
    "team1_colorblur": (-76.9628, -166.104), "team2_colorblur": (-165.967, -166.104),
    "team1_color": (-76.9628, -166.071), "team1_glint1": (-93.0074, -166.071),
    "team1_glint2": (-55.0787, -166.071), "team2_color": (-166.357, -166.071),
    "team2_glint1": (-182.156, -166.071), "team2_glint2": (-144.494, -166.071),
    "glint_separator": (-120.601, -171.96), "glint_separator1": (-6.46805, -171.96),
}

# Pristine font sizes/colors (text) and vertex-bbox dims (mesh), captured 2026-07-16.
STOCK_META = {
    "away_abbrev": {"size": 38, "color": "#FFFFFF"},
    "team1": {"size": 20, "color": "#FFFFFF"}, "team2": {"size": 38, "color": "#FFFFFF"},
    "team1_score": {"size": 26, "color": "#00FFFF"},
    "team2_score": {"size": 20, "color": "#FFFFFF"},
    "quarter": {"size": 7, "color": "#FFFFFF"},
    "gameclock1": {"size": 7, "color": "#FFFFFF"},
    "gameclock2": {"size": 7, "color": "#FFFFFF"},
    "gameclock_semi": {"size": 7, "color": "#FFFFFF"},
    "gameclock3": {"size": 7, "color": "#FFFFFF"},
    "gameclock4": {"size": 48, "color": "#FFFFFF"},     # "Shots" label: 48 = untruncated width
    "shots_away": {"size": 20, "color": "#FFFFFF"},
    "shots_home": {"size": 20, "color": "#FFFFFF"},
    "logo_away": {"w": 50.4626, "h": 20.4645}, "logo_home": {"w": 50.4626, "h": 20.4645},
    "team1_colorblur": {"w": 86.0079, "h": 9.12473},
    "team2_colorblur": {"w": 86.2834, "h": 9.12473},
    "team1_color": {"w": 86.0079, "h": 1.91975},
    "team1_glint1": {"w": 47.9751, "h": 1.91975},
    "team1_glint2": {"w": 17.9154, "h": 1.91975},
    "team2_color": {"w": 86.5097, "h": 1.91975},
    "team2_glint1": {"w": 43.6603, "h": 1.91975},
    "team2_glint2": {"w": 24.7182, "h": 1.91975},
    "glint_separator": {"w": 110.717, "h": 2.04973},
    "glint_separator1": {"w": 62.2436, "h": 2.04973},
}

# A text element's draw record holds (a) +0x7C = the max WIDTH the text is clipped to (proven
# in-game: too small -> the game ellipsis-truncates "DE…"; it does NOT scale the glyphs), and (b) a
# 4x4 local transform matrix @+0x38 whose DIAGONAL (m00 @+0x38 X, m11 @+0x4C Y; stock identity 1.0)
# is the glyph SCALE lever. Position comes from the joint, so the matrix's translation row is dead —
# whether the game consumes the matrix's scale is the current in-game test for font sizing.


def _crc(name: str) -> int:
    return zlib.crc32(name.encode()) & 0xFFFFFFFF


def _find_text_skeleton(dram):
    """Offset of the scorebug_text joint table (13 records x 0x30), validated by hash chain.
    Bind pose only (not consumed by the game) — kept in sync so the data stays coherent.
    gameclock4's slot is a mesh-style record, so the chain is validated up to gameclock3."""
    root = struct.pack(">I", _crc("scorebug_text")) + struct.pack(">hh", -1, 1)
    p = dram.find(root)
    while p >= 0:
        offs = {}
        ok = True
        for i, nm in enumerate(TEXT_JOINTS[:-1]):
            o = p + i * 0x30
            if struct.unpack_from(">I", dram, o)[0] != _crc(nm):
                ok = False
                break
            offs[nm] = o
        if ok:
            return offs
        p = dram.find(root, p + 4)
    raise ValueError("scorebug text skeleton not found in overlay_static DRAM")


# Per-element FONT (corrected 2026-07-28): the font hash is the record's LAST dword @+0xDC —
# NOT +0x00 (that is the record-table key, always crc('scorebug_text'); the old +0x00 claim
# came from misreading the key). Per Function_83C9EDA0 (the record property parser), the font
# property (key 0xBF045BDB) stores either an inline name string or a precomputed crc hash; the
# baked scorebug records store the hash @+0xDC. Stock census (record dump 2026-07-28):
#   3DD873F2 = abbrevs + scores font   (name unknown — Avenir Heavy, large)
#   F57C40A5 = clock digits + period   (name unknown)
#   4A63F778 = crc('avenir_heavy_24')  (the SOG/"Shots" elements)
#   1B5494E7 = crc('avenir_heavy_40')  (loaded in-context: used by the shootout scene)
# FONTS maps a friendly label -> hash; +0xDC is repointed to redirect an element's font.
# FONT SYSTEM SOLVED 2026-07-28: the full font-instance registry lives in english.iff
# blob0 @0x129000-0x12CC90 — 103 alias entries, each [utf16 name][hash @name+0x40]
# [base-font hash +0x44][1.0f +0x48][scaleX +0x4C][scaleY +0x50], stride 0x90, over
# 8 base typefaces (avenir_heavy_24/40, avenir_roman_18/22, avenir_light_40,
# avenir_black_95, arial_black_20, prison_aoe; atlases in english blob1). Stock
# scorebug: 3DD873F2='AVENIR_HEAVY_24' (avenir_heavy_24 @0.5), F57C40A5=
# 'AVENIR_ROMAN_22' (avenir_roman_22 @0.5). Any registry hash is assignable —
# this is a curated pick spanning sizes/typefaces.
FONTS = {
    "Stock Large (abbrev/score)": 0x3DD873F2,   # AVENIR_HEAVY_24: avenir_heavy_24 @0.5
    "Stock Clock (clock/period)": 0xF57C40A5,   # AVENIR_ROMAN_22: avenir_roman_22 @0.5
    "Avenir Heavy 24 (Shots)":    0x4A63F778,   # avenir_heavy_24 @0.5
    "Avenir Heavy 20 (small)":    0x4D0E3361,   # avenir_heavy_40 @0.25
    "Avenir Heavy 40 (large)":    0x1B5494E7,   # avenir_heavy_40 @0.5
    "Avenir Roman 18":            0x493F7EF2,   # avenir_roman_18 @0.5
    "Avenir Light 40":            0xA35DE24A,   # avenir_light_40 @0.5
    "Avenir Black 95 (huge)":     0x88D8C0D8,   # avenir_black_95 @0.5
    "Arial Black 20":             0xDBDC8608,   # arial_black_20 @0.5
    "Stencil (LREGULAR 28)":      0x4DB6F1D5,   # prison_aoe @0.74
    "Stratum2 40 (big heavy)":    0x6A9B0A7A,   # avenir_heavy_40 @0.9
}
_FONT_HASH = dict(FONTS)
_HASH_FONT = {h: label for label, h in _FONT_HASH.items()}
# Stock +0xDC per element, for restore/factory.
_STOCK_FONT = {
    "away_abbrev": 0x3DD873F2, "team1": 0x3DD873F2, "team2": 0x3DD873F2,
    "team2_score": 0x3DD873F2,
    "team1_score": 0xF57C40A5, "quarter": 0xF57C40A5, "gameclock1": 0xF57C40A5,
    "gameclock2": 0xF57C40A5, "gameclock_semi": 0xF57C40A5, "gameclock3": 0xF57C40A5,
    "gameclock4": 0x4A63F778, "shots_away": 0x4A63F778, "shots_home": 0x4A63F778,
}
# The record-table key @+0x00 (identical for every record; away_abbrev's is 0).
_RECORD_KEY = _crc("scorebug_text")


def _find_text_records(dram):
    """{name: record_offset} in the consumed text-draw table (stride 0xE0). Keyed by
    [crc('scorebug_text') @+0x00][name hash @+0x04] — the +0x00 dword is the constant table
    key, NOT the font (font = +0xDC; corrected 2026-07-28). The same pair also opens the
    0x1C-stride anim-desc records, so a candidate only counts with W==1.0 @+0x74 + a
    plausible pos @+0x68 (the record table also precedes the desc table in the blob)."""
    recs = {}
    for nm in EDITABLE_TEXT:
        if nm == "away_abbrev":
            continue                         # no name hash — resolved structurally below
        sig = struct.pack(">II", _RECORD_KEY, _crc(nm))
        p = dram.find(sig)
        while p >= 0:
            one, zero = struct.unpack_from(">fI", dram, p + 0x60)
            x, y, z, w = struct.unpack_from(">4f", dram, p + 0x68)
            size = struct.unpack_from(">f", dram, p + 0x7C)[0]
            if (one == 1.0 and zero == 0 and w == 1.0 and 0.5 <= size <= 500
                    and abs(x) < 2000 and abs(y) < 2000 and -100 < z < 100):
                recs[nm] = p
                break
            p = dram.find(sig, p + 4)
        if nm not in recs:
            # the SOG elements only exist after scorebug_add_shots — absence is normal
            if nm in ("shots_away", "shots_home"):
                continue
            raise ValueError(f"text draw record not found for {nm}")
    # away_abbrev: blank-hash draw record exactly one stride (0xE0) before team2 — same body
    # (pos +0x68, size +0x7C, colour +0x89) so it edits like any other text element.
    if "team2" in recs:
        cand = recs["team2"] - 0xE0
        if cand >= 0:
            fh, nh = struct.unpack_from(">II", dram, cand)
            one, zero = struct.unpack_from(">fI", dram, cand + 0x60)
            w = struct.unpack_from(">f", dram, cand + 0x74)[0]
            size = struct.unpack_from(">f", dram, cand + 0x7C)[0]
            if fh == 0 and nh == 0 and one == 1.0 and zero == 0 and w == 1.0 and 1.0 <= size <= 100.0:
                recs["away_abbrev"] = cand
    return recs


def _find_mesh_blocks(dram):
    """{name: (block_start, block_end)} for each scorebug mesh. A block runs from its
    [hash][FFFFFFFF][FFFF0000] header to the inline name string that terminates it.
    'logo_2k' is a composite: the whole logo1 mini-scene (polySurface1 + logo_2k_mesh)."""
    blocks = {}
    for nm in MESHES:
        sig = struct.pack(">I", _crc(nm)) + b"\xFF\xFF\xFF\xFF\xFF\xFF\x00\x00"
        p = dram.find(sig)
        if p < 0:
            continue
        name_be = nm.encode("utf-16-be")
        q = dram.find(name_be, p)
        if q < 0 or q - p > 0x200000:
            continue
        blocks[nm] = (p, q)
    p = dram.find(struct.pack(">I", _crc("logo1")) + struct.pack(">hh", -1, 1))
    q = dram.find("logo_2k_mesh".encode("utf-16-be"), p) if p >= 0 else -1
    if p >= 0 and q > p:
        blocks["logo_2k"] = (p, q)
    return blocks


_VTX_PAIR = b"\x00\x00\x00\xFF\x00\x00\x00\x00"


def _mesh_vertices(dram, start, end):
    """Vertex start offsets in [start,end). Every vertex ends with 000000FF 00000000; runs
    of equally-spaced pairs give the stride s, and the vertex begins at pair - (s - 8)."""
    qs = []
    q = dram.find(_VTX_PAIR, start, end)
    while q >= 0:
        qs.append(q)
        q = dram.find(_VTX_PAIR, q + 8, end)
    verts = []
    i = 0
    while i < len(qs):                                   # split into equal-spacing runs
        j = i + 1
        stride = None
        while j < len(qs):
            d = qs[j] - qs[j - 1]
            if stride is None and 0x10 <= d <= 0x40:
                stride = d
            if d != stride:
                break
            j += 1
        if stride is not None and j - i >= 3:            # a real vertex run (quads = 4+)
            for q2 in qs[i:j]:
                v = q2 - (stride - 8)
                if v < start:
                    continue
                x, y, z = struct.unpack_from(">3f", dram, v)
                if abs(x) < 2000 and abs(y) < 2000 and -100 < z < 100:
                    verts.append(v)
        i = j
    return sorted(set(verts))


def list_elements(gdir):
    """Element rows. text: {name,label,kind,x,y,size,color '#RRGGBB'}; mesh: {name,label,
    kind,x,y (vertex centroid), w,h (bbox), n (vertex count)}."""
    dram, _meta = oe.load_dram(IFF, gdir)
    return list_elements_from_dram(bytes(dram))


def list_elements_from_dram(dram):
    """No-I/O core of list_elements: build rows from an ALREADY-decoded blob0 buffer. Lets a
    single-load rebuild read the CURRENT in-memory state instead of paying another ~2min load."""
    dram = bytes(dram)
    rows = []
    trecs = _find_text_records(dram)
    try:
        joints = _find_text_skeleton(dram)
    except Exception:
        joints = {}
    for nm in EDITABLE_TEXT:
        o = trecs.get(nm)
        if o is None:                        # away_abbrev may be absent in edited files
            continue
        x, y, z = struct.unpack_from(">3f", dram, o + 0x68)
        jn = _JOINT_FOR.get(nm)              # away abbrev: its true position lives on joint13, not
        if jn and jn in joints:              # the (dead-for-position) draw-record matrix row
            x, y = struct.unpack_from(">2f", dram, joints[jn] + 0x1C)
        size = struct.unpack_from(">f", dram, o + 0x7C)[0]
        fonth = struct.unpack_from(">I", dram, o + 0xDC)[0]  # per-element font hash (+0xDC)
        # glyph scale = the anim transform desc's constant channels (consts[3]/[4]); shown
        # as a factor of the element's stock const (1.1 or 1.2). Lookup is by BIND hash —
        # the desc raw names are shifted authoring labels (see scorebug_anim_descs).
        if nm in SOG_JOINTLESS:
            # jointless SOG: scale is the record's own matrix diagonal (stock 1.0), not consts
            ax = struct.unpack_from(">f", dram, o + TXMATRIX_M00)[0]
            ay = struct.unpack_from(">f", dram, o + TXMATRIX_M11)[0]
            sx, sy = ax, ay
            stock_sc = 1.0
        else:
            stock_sc = sad.STOCK_TXSCALE.get(nm)
            cur_sc = sad.get_text_scale_by_bind(dram, struct.unpack_from(">I", dram, o + 0xD8)[0])
            if stock_sc and cur_sc:
                sx, sy = cur_sc[0] / stock_sc, cur_sc[1] / stock_sc
                ax, ay = cur_sc                          # absolute consts (the user-facing number)
            else:
                sx = sy = 1.0
                ax = ay = stock_sc or 0.0
        a, r, g, b = struct.unpack_from(">4B", dram, o + 0x88)
        upper = bool(struct.unpack_from(">I", dram, o + UPPERCASE_FLAG)[0])
        rows.append({"name": nm, "label": LABELS.get(nm, nm), "kind": "text",
                     "x": x, "y": y, "n": 1, "size": size, "scale_x": sx, "scale_y": sy,
                     "scale_ax": ax, "scale_ay": ay,     # absolute glyph-scale consts
                     "stock_scale": stock_sc or 0.0, "uppercase": upper,
                     "font": _HASH_FONT.get(fonth, f"0x{fonth:08X}"), "font_hash": fonth,
                     "color": f"#{r:02X}{g:02X}{b:02X}"})
    for nm, (s, e) in _find_mesh_blocks(dram).items():
        vs = _mesh_vertices(dram, s, e)
        if not vs:
            continue
        xs = [struct.unpack_from(">f", dram, v)[0] for v in vs]
        ys = [struct.unpack_from(">f", dram, v + 4)[0] for v in vs]
        rows.append({"name": nm, "label": LABELS.get(nm, nm), "kind": "mesh",
                     "x": sum(xs) / len(xs), "y": sum(ys) / len(ys), "n": len(vs),
                     "w": max(xs) - min(xs), "h": max(ys) - min(ys)})
    return rows


def _snapshot_from_rows(rows):
    """Build the re-appliable layout dict from element rows. EVERYTHING editable is captured so
    nothing is silently dropped on rebuild: text -> {kind,x,y,size,color,uppercase,font_hash,
    scale_x,scale_y}; mesh -> {kind,x,y,w,h}."""
    snap = {}
    for r in rows:
        if r["kind"] == "text":
            snap[r["name"]] = {"kind": "text", "x": r["x"], "y": r["y"],
                               "size": float(r.get("size", 0) or 0),
                               "color": r.get("color", "#FFFFFF"),
                               "uppercase": bool(r.get("uppercase", False)),
                               "font_hash": int(r.get("font_hash", 0) or 0),
                               "scale_x": float(r.get("scale_x", 1) or 1),
                               "scale_y": float(r.get("scale_y", 1) or 1)}
        else:
            snap[r["name"]] = {"kind": "mesh", "x": r["x"], "y": r["y"],
                               "w": float(r.get("w", 0) or 0), "h": float(r.get("h", 0) or 0)}
    return snap


def capture_snapshot(gdir):
    """ABSOLUTE layout of every element in the CURRENT overlay_static, as a re-appliable record.
    Used to PRESERVE the scoreclock layout across a clean texture apply: ensure_clean() resets the
    whole file (wiping the layout in blob0), so a texture apply must snapshot the layout first and
    restore it after."""
    try:
        return _snapshot_from_rows(list_elements(gdir))
    except Exception:
        return {}


def capture_overlay_state(gdir):
    """SINGLE load_dram: snapshot the full layout AND read the 2K-shadow / teal-bar hide flags in
    ONE decode (was 3 separate ~2min loads: capture_snapshot + scorebug_logo_hidden + teal_bar_
    hidden). Returns (snap, shadow_hidden, teal_hidden) — feed straight to rebuild_overlay_from_
    snapshot() after a clean texture apply."""
    dram, _meta = oe.load_dram(IFF, gdir)
    dram = bytes(dram)
    try:
        snap = _snapshot_from_rows(list_elements_from_dram(dram))
    except Exception:
        snap = {}
    return snap, _mesh_index_hidden_in_dram(dram, "logo_2k_mesh"), _teal_bar_hidden_in_dram(dram)


def snapshot_has_sog(snap):
    """True if the captured layout includes the Shots-on-Goal mod (the two cloned SOG records).
    ensure_clean() wipes SOG, so a texture apply must re-run the SOG mod before restoring layout."""
    return bool(snap) and ("shots_away" in snap or "shots_home" in snap)


def sog_present(gdir):
    """True if the Shots-on-Goal mod is already applied to overlay_static.iff (both cloned
    scorebug records present). The Scoreclock tab is file-driven, so this == 'the tab lists SOG'."""
    try:
        names = {r["name"] for r in list_elements(gdir)}
    except Exception:
        return False
    return "shots_away" in names and "shots_home" in names


def _apply_sog_xex(gdir, log=print):
    """Apply the XEX bind rows for SOG to default.xex. Idempotent (returns 'already patched'
    when present). The rows live in default.xex/.sogbak and survive overlay clean rebuilds, so a
    lost-SOG file usually still has these — re-applying is cheap and safe."""
    import scorebug_xex_rows as sxr
    xex = str(Path(gdir) / "default.xex")
    try:
        st = sxr.apply(xex)
        log(f"  XEX bind rows: {st}")
        return st
    except Exception as e:
        log(f"  XEX bind rows FAILED: {e}")
        return f"failed: {e}"


def install_sog(gdir, log=print):
    """(Re)install the Shots-on-Goal mod. SOG is an ADDED element set (two records cloned from
    gameclock4 + their anim descs), NOT stock — a clean/re-extract of overlay_static.iff drops it,
    and the file-driven Scoreclock tab then can't list it. This restores it in ONE load + ONE
    encode (scorebug_add_shots.build + scorebug_anim_descs.build in a shared buffer, matching the
    optimized rebuild path) plus the idempotent XEX bind rows. No-op on the overlay if already
    present (still verifies the XEX side). Returns a one-line status."""
    dram, meta = oe.load_dram(IFF, gdir)
    dram = bytearray(dram)
    names = {r["name"] for r in list_elements_from_dram(bytes(dram))}
    if "shots_away" in names and "shots_home" in names:
        xstatus = _apply_sog_xex(gdir, log)
        return f"Shots-on-Goal already present — overlay unchanged (XEX: {xstatus})"
    log("  adding Shots-on-Goal records + anim descs…")
    sas.build(dram)          # +2 cloned shots records (in place, grows buffer)
    sad.build(dram)          # + their opacity/transform anim descs (in place)
    oe.apply_dram(bytes(dram), meta, IFF, gdir, log)
    xstatus = _apply_sog_xex(gdir, log)
    return f"Shots-on-Goal installed — overlay written (XEX: {xstatus})"


def rebuild_overlay_from_snapshot(snap, shadow, teal, gdir, log=print):
    """Rebuild the ENTIRE scoreclock state onto a freshly-cleaned overlay_static in ONE load_dram
    + ONE apply_dram — the optimized restore path. The old restore did up to 4 loads + 3 encodes
    (scorebug_add_shots.apply + scorebug_anim_descs.apply + snapshot_edits' list + apply_edits,
    plus a load/encode per hide); each load_dram/apply_dram is a ~2min DRAM decode/re-encode. Here
    we decode once, mutate the shared buffer, encode once:

      1. re-add SOG (scorebug_add_shots.build + scorebug_anim_descs.build) if the snapshot had it —
         the file is stock right now (post ensure_clean), exactly what those builders require. Both
         build() mutate the bytearray in place. XEX bind rows live in default.xex (untouched by
         ensure_clean) so they are NOT rebuilt here.
      2. re-apply every per-element edit (pos/size/color/uppercase/font/glyph-scale) — computed
         against the just-modified in-memory buffer so it sees the SOG records.
      3. re-apply the 2K-shadow and teal-bar hides.

    Doing (1)+(2) in-memory is equivalent to the old apply→reload→apply chain (the DRAM codec is a
    lossless round-trip, and each builder already took original-meta + a grown buffer). Returns a
    one-line status."""
    if not snap:
        return "no scoreclock layout to restore"
    dram, meta = oe.load_dram(IFF, gdir)
    dram = bytearray(dram)
    parts = []
    if snapshot_has_sog(snap):
        log("  re-adding Shots-on-Goal mod (wiped by clean rebuild)…")
        sas.build(dram)          # +2 cloned shots records (in place, grows buffer)
        sad.build(dram)          # + their opacity/transform anim descs (in place)
        parts.append("SOG re-added")
    edits = snapshot_edits_from_dram(snap, bytes(dram))
    if edits:
        _apply_edits_to_dram(edits, dram, log)
    parts.append(f"{len(edits)} element edit(s)")
    if shadow:
        _set_mesh_index_hidden_in_dram(dram, "logo_2k_mesh", True, gdir, log)
        parts.append("2K hidden")
    if teal:
        _set_teal_bar_hidden_in_dram(dram, True, gdir, log)
        parts.append("teal hidden")
    oe.apply_dram(bytes(dram), meta, IFF, gdir, log)
    return "restored scoreclock layout (" + ", ".join(parts) + ")"


def snapshot_edits(snap, gdir):
    """apply_edits() deltas that turn the CURRENT overlay_static into `snap` (the captured layout).
    Call AFTER ensure_clean()+texture apply — the file is back to stock layout, so these deltas
    re-apply exactly what the user had. Returns {} when nothing differs (stock layout)."""
    try:
        cur = {r["name"]: r for r in list_elements(gdir)}
    except Exception:
        return {}
    return _snapshot_edits_against(snap, cur)


def snapshot_edits_from_dram(snap, dram):
    """snapshot_edits computed against an ALREADY-decoded blob0 buffer (no reload) — used by the
    single-load rebuild after it has re-added SOG in memory, so the deltas see the SOG records."""
    try:
        cur = {r["name"]: r for r in list_elements_from_dram(dram)}
    except Exception:
        return {}
    return _snapshot_edits_against(snap, cur)


def _snapshot_edits_against(snap, cur):
    """Shared core: diff captured layout `snap` against current rows `cur` -> apply_edits deltas."""
    edits = {}
    for nm, a in (snap or {}).items():
        r = cur.get(nm)
        if not r:
            continue
        ed = {}
        dx = float(a["x"]) - r["x"]; dy = float(a["y"]) - r["y"]
        if abs(dx) > 1e-4: ed["dx"] = dx
        if abs(dy) > 1e-4: ed["dy"] = dy
        if a["kind"] == "text":
            if abs(float(a.get("size", 0)) - float(r.get("size", 0) or 0)) > 1e-4:
                ed["size"] = float(a["size"])
            ac = (a.get("color") or "").lstrip("#"); rc = (r.get("color") or "").lstrip("#")
            if len(ac) >= 6 and ac.upper() != rc.upper():
                ed["color"] = tuple(int(ac[i:i + 2], 16) for i in (0, 2, 4))
            if bool(a.get("uppercase", False)) != bool(r.get("uppercase", False)):
                ed["uppercase"] = bool(a.get("uppercase", False))
            if int(a.get("font_hash", 0) or 0) and \
               int(a["font_hash"]) != int(r.get("font_hash", 0) or 0):
                ed["font"] = int(a["font_hash"])       # per-element font (+0xDC)
            # glyph scale: scale_x/scale_y are already in apply_edits' sx/sy units
            # (jointless SOG = absolute matrix diagonal; jointed = factor-of-stock).
            asx, asy = float(a.get("scale_x", 1) or 1), float(a.get("scale_y", 1) or 1)
            rsx, rsy = float(r.get("scale_x", 1) or 1), float(r.get("scale_y", 1) or 1)
            if abs(asx - rsx) > 1e-4: ed["sx"] = asx
            if abs(asy - rsy) > 1e-4: ed["sy"] = asy
        else:
            rw = r.get("w", 0) or 0.0; rh = r.get("h", 0) or 0.0
            if rw > 1e-6 and abs(float(a.get("w", rw)) / rw - 1) > 1e-4: ed["sx"] = float(a["w"]) / rw
            if rh > 1e-6 and abs(float(a.get("h", rh)) / rh - 1) > 1e-4: ed["sy"] = float(a["h"]) / rh
        if ed:
            edits[nm] = ed
    return edits


# Hidden text is shoved off-screen (well within the vertex sanity bound so it stays parseable);
# hidden meshes collapse to a sub-pixel scale in place (moving them far would push vertices past
# the scanner bound and drop the element). Both are reversible via restore_stock/reset_to_factory.
HIDE_OFFSET = 1500.0
HIDE_SCALE = 0.001


def apply_edits(edits, gdir, log=print):
    """edits = {name: {dx, dy, sx, sy, size, color, hidden}} — any subset per element.
    dx/dy = move in scene units (~px/2). sx/sy = scale factors (mesh only: vertices scaled
    about the centroid). size = absolute font size (text, tableC +0x7C — EXPERIMENTAL).
    color = (r, g, b) 0-255 (text, tableC +0x88 — in-game verified). hidden=True hides the
    element (text: off-screen; mesh: collapsed) reversibly.
    Text position writes joint pos2 @+0x1C (the consumed copy, live-verified 2026-07-16)
    plus pos1 and the tableC matrix row so every copy stays coherent."""
    dram, meta = oe.load_dram(IFF, gdir)
    _apply_edits_to_dram(edits, dram, log)
    return oe.apply_dram(dram, meta, IFF, gdir, log)


def _apply_edits_to_dram(edits, dram, log=print):
    """No-I/O core of apply_edits: mutate an in-memory blob0 bytearray IN PLACE (no load/apply).
    Lets the single-load rebuild fold the layout edits into a shared buffer. `dram` must be a
    mutable bytearray. Returns it."""
    trecs = _find_text_records(bytes(dram))
    joints = _find_text_skeleton(bytes(dram))
    blocks = _find_mesh_blocks(bytes(dram))
    for nm, ed in edits.items():
        dx, dy = float(ed.get("dx", 0)), float(ed.get("dy", 0))
        sx, sy = float(ed.get("sx", 1)), float(ed.get("sy", 1))
        if ed.get("hidden"):                       # text: off-screen; mesh: collapse in place
            if nm in trecs:
                dx += HIDE_OFFSET
            else:
                sx = sy = HIDE_SCALE
        if nm in trecs:
            o = trecs[nm]
            if dx or dy:
                x, y = struct.unpack_from(">2f", dram, o + 0x68)
                struct.pack_into(">2f", dram, o + 0x68, x + dx, y + dy)
                jname = _JOINT_FOR.get(nm, nm)        # away abbrev -> joint13 (its real position lever)
                if jname in joints:
                    j = joints[jname]
                    for poff in (0x0C, 0x1C):
                        jx, jy = struct.unpack_from(">2f", dram, j + poff)
                        struct.pack_into(">2f", dram, j + poff, jx + dx, jy + dy)
                log(f"  text  {nm}: pos ({x:.2f},{y:.2f}) -> ({x + dx:.2f},{y + dy:.2f})")
            if "sx" in ed or "sy" in ed:             # explicit scale (incl. resetting to 1.0)
                if nm in SOG_JOINTLESS:
                    # Jointless SOG elements: the anim-consts lever is inert (no joint to drive),
                    # so scale rides the draw record's OWN local-matrix diagonal. sx/sy arrive as
                    # absolute factors (stock diagonal = 1.0). Only the axes present are written.
                    if "sx" in ed:
                        struct.pack_into(">f", dram, o + TXMATRIX_M00, float(ed["sx"]))
                    if "sy" in ed:
                        struct.pack_into(">f", dram, o + TXMATRIX_M11, float(ed["sy"]))
                    mx = struct.unpack_from(">f", dram, o + TXMATRIX_M00)[0]
                    my = struct.unpack_from(">f", dram, o + TXMATRIX_M11)[0]
                    log(f"  text  {nm}: glyph scale -> {mx:g}/{my:g} (record matrix diagonal)")
                else:
                    # Glyph SCALE: the element's anim TRANSFORM DESC constant channels
                    # ([tx,ty,tz,sx,sy,sz]; stock 1.1/1.2 — see scorebug_anim_descs). sx/sy
                    # arrive as FACTORS of stock; written value = stock * factor (the absolute
                    # const the UI shows). ONLY the axes present in the edit are written — an
                    # omitted axis keeps its CURRENT const (a single-axis edit used to clobber
                    # the other axis back to stock). Nothing else is touched: no matrix-diagonal
                    # write and NO automatic +0x7C clip-width change (width is user-controlled).
                    stock_sc = sad.STOCK_TXSCALE.get(nm)
                    bindh = struct.unpack_from(">I", dram, o + 0xD8)[0]
                    cur = sad.get_text_scale_by_bind(dram, bindh) if stock_sc else None
                    if stock_sc and cur:
                        ax = stock_sc * float(ed["sx"]) if "sx" in ed else cur[0]
                        ay = stock_sc * float(ed["sy"]) if "sy" in ed else cur[1]
                        sad.set_text_scale_by_bind(dram, bindh, ax, ay)
                        log(f"  text  {nm}: glyph scale -> {ax:g}/{ay:g} (bind-paired anim consts)")
            if "size" in ed:
                # +0x7C is the text's MAX WIDTH (clip box), NOT a font size: enlarging it does nothing
                # (the string already fits) and shrinking it makes the game ellipsis-truncate ("DE…").
                # Proven in-game 2026-07-21. Kept as a width lever; the real glyph-size lever is _sbl.
                struct.pack_into(">f", dram, o + 0x7C, float(ed["size"]))
                log(f"  text  {nm}: text width -> {float(ed['size']):g} (clip box, not glyph size)")
            if "color" in ed:
                r, g, bl = (int(c) & 0xFF for c in ed["color"])
                struct.pack_into(">3B", dram, o + 0x89, r, g, bl)   # keep byte0 as authored
                log(f"  text  {nm}: color -> #{r:02X}{g:02X}{bl:02X}")
            if "font" in ed:                          # per-element FONT: repoint the hash @+0xDC
                fh = int(ed["font"]) & 0xFFFFFFFF
                struct.pack_into(">I", dram, o + 0xDC, fh)
                log(f"  text  {nm}: font -> {_HASH_FONT.get(fh, hex(fh))}")
            if "uppercase" in ed:                     # +0xB0 render-time UPPERCASE flag (u32)
                uv = 1 if ed["uppercase"] else 0
                struct.pack_into(">I", dram, o + UPPERCASE_FLAG, uv)
                log(f"  text  {nm}: uppercase -> {'ON' if uv else 'off'} (+0xB0)")
        elif nm in blocks:
            vs = _mesh_vertices(bytes(dram), *blocks[nm])
            if not vs:
                raise ValueError(f"{nm}: no vertices found")
            xs = [struct.unpack_from(">f", dram, v)[0] for v in vs]
            ys = [struct.unpack_from(">f", dram, v + 4)[0] for v in vs]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            for v in vs:
                x, y = struct.unpack_from(">2f", dram, v)
                struct.pack_into(">2f", dram, v,
                                 cx + (x - cx) * sx + dx, cy + (y - cy) * sy + dy)
            what = []
            if sx != 1 or sy != 1:
                what.append(f"scaled x{sx:g}/x{sy:g}")
            if dx or dy:
                what.append(f"moved ({dx:g},{dy:g})")
            log(f"  mesh  {nm}: {len(vs)} vertices {' + '.join(what) or 'unchanged'}")
        else:
            raise ValueError(f"unknown scorebug element: {nm}")
    return dram


def apply_moves(moves, gdir, log=print):
    """Back-compat wrapper: moves = {name: (dx, dy)}."""
    return apply_edits({nm: {"dx": d[0], "dy": d[1]} for nm, d in moves.items()}, gdir, log)


# The grey "2K Sports" backdrop is a SHADOW mesh (logo_2k_shdw) in overlay_static's second scene,
# a 0x30 transform record keyed by crc('logo_2k_shdw')=0xBE74F920 with an AUTHORED position
# @+0x10 (X,Y,Z) that matches the live resolved position — so shoving X off-screen hides it,
# reversibly. It's NOT a texture (no overlay_static texture is a grey 2K), which is why every
# texture edit missed it. See project_scorebug_layout live-capture notes.
_SHADOW_HASH = zlib.crc32(b"logo_2k_shdw") & 0xFFFFFFFF
_SHADOW_STOCK_X = -294.96939              # authored X (bytes C3 93 7C 11)
_SHADOW_HIDE_X = 9000.0


def _shadow_node(dram):
    """File offset of the logo_2k_shdw transform node (the one with a real position), or None."""
    sig = struct.pack(">I", _SHADOW_HASH)
    p = dram.find(sig)
    while p >= 0:
        node = p - 4
        if node >= 0:
            pos = struct.unpack_from(">3f", dram, node + 0x10)
            if all(abs(v) < 20000 for v in pos):     # a real transform (not an anim ref)
                return node
        p = dram.find(sig, p + 1)
    return None


def shadow_hidden(gdir):
    """True if the grey 2K backdrop shadow is currently shoved off-screen."""
    dram, _ = oe.load_dram(IFF, gdir)
    o = _shadow_node(bytes(dram))
    if o is None:
        return False
    return struct.unpack_from(">f", dram, o + 0x10)[0] > 2000


def set_2k_shadow_hidden(hide, gdir, log=print):
    """Hide (shove off-screen) or restore the grey 2K backdrop shadow. Reversible."""
    dram, meta = oe.load_dram(IFF, gdir)
    o = _shadow_node(bytes(dram))
    if o is None:
        raise ValueError("logo_2k_shdw record not found in overlay_static")
    cur = struct.unpack_from(">f", dram, o + 0x10)[0]
    struct.pack_into(">f", dram, o + 0x10, _SHADOW_HIDE_X if hide else _SHADOW_STOCK_X)
    log(f"  2K backdrop shadow: X {cur:.1f} -> {'hidden (off-screen)' if hide else 'shown'}")
    return oe.apply_dram(dram, meta, IFF, gdir, log)


# ── Indexed-mesh hide (format cracked 2026-07-19) ────────────────────────────
# Every scorebug mesh (simple quad OR complex indexed glow/branding) has the SAME block layout:
#   +0x00 [name-hash][FF FF FF FF][FF FF 00 00][hash2]
#   +0x10 00000000 00000000 00000001 00000006  (constant)
#   +0x24/+0x28/+0x34/+0x3C counts
#   +0x50 INDEX BUFFER (u16, triangle strip; 0xFFFF = restart) — runs up to the first material/
#         GPU-fetch descriptor (a dword 0x20??0000 with ?? in {00,04,08,0C,10,14})
#   material descriptors (0x40 stride) → footer [stride][size=verts*stride] → vertex buffer
# ZEROING the index buffer makes every triangle degenerate (v0,v0,v0) → the mesh draws nothing.
# This is the general, format-independent way to hide the glows (glow_cylinder_color = the teal
# bar) and the branding meshes that _mesh_vertices can't parse. Reversible via a byte backup.


def _mesh_hdr(dram, name):
    # Most scorebug meshes tag the name-hash with FF FF FF FF FF FF 00 00, but the branding mesh
    # logo_2k_mesh uses 00 00 FF FF FF FF 00 00 — try both so its index buffer is reachable too.
    h = struct.pack(">I", _crc(name))
    for tail in (b"\xFF\xFF\xFF\xFF\xFF\xFF\x00\x00", b"\x00\x00\xFF\xFF\xFF\xFF\x00\x00"):
        p = dram.find(h + tail)
        if p >= 0:
            return p
    return None


def _mesh_index_range(dram, name):
    """(start, end) of a mesh's index buffer — +0x50 up to the first material descriptor."""
    mh = _mesh_hdr(dram, name)
    if mh is None:
        return None
    endname = dram.find(name.encode("utf-16-be"), mh)
    if endname < 0:
        endname = min(mh + 0x2000, len(dram))
    start = mh + 0x50
    for o in range(start, endname - 8, 4):
        d = struct.unpack_from(">I", dram, o)[0]
        if (d & 0xF0FF0000) == 0x20000000 and ((d >> 16) & 0xFF) in (0, 4, 8, 0xC, 0x10, 0x14):
            return (start, o - 8)                 # descriptor begins 8 bytes before its 0x20 field
    return (start, start)


def _mesh_vertexbuf_range(dram, name):
    """(start, end) of a mesh's VERTEX buffer — the counterpart to _mesh_index_range.

    Layout after the index buffer + 0x40-stride material descriptors: a footer [stride u32]
    [size u32] immediately followed by the vertex buffer, where size == vertexCount*stride and
    the vertex count is stored at hdr+0x34. Anchoring on that exact identity makes the region
    unambiguous even though the per-field vertex layout differs per mesh (glow: stride 0x14/61
    verts; flat quad: stride 0x18/14 verts) — verified: glow 61*0x14=0x4C4, team1_color
    14*0x18=0x150 both match the on-disk footer."""
    mh = _mesh_hdr(dram, name)
    if mh is None:
        return None
    count = struct.unpack_from(">I", dram, mh + 0x34)[0]
    if not (1 <= count <= 4096):
        return None
    endname = dram.find(name.encode("utf-16-be"), mh)
    if endname < 0:
        endname = min(mh + 0x4000, len(dram))
    ir = _mesh_index_range(dram, name)
    scan = ir[1] if ir else mh + 0x50
    for o in range(scan, endname - 8, 4):
        stride, size = struct.unpack_from(">II", dram, o)
        if stride in (0x14, 0x18, 0x1C, 0x20, 0x24, 0x28) and size == count * stride:
            vb = o + 8
            if vb + size <= endname:
                return (vb, vb + size)
    return None


def _mesh_backup(gdir, name):
    return Path(gdir) / f"scoreclock_meshhide_{name}.bin"


def _mesh_vb_backup(gdir, name):
    return Path(gdir) / f"scoreclock_vbhide_{name}.bin"


def mesh_vertexbuf_hidden(name, gdir):
    """True if this mesh's vertex buffer is currently all-zero (geometry collapsed)."""
    dram, _ = oe.load_dram(IFF, gdir)
    r = _mesh_vertexbuf_range(bytes(dram), name)
    if not r or r[1] <= r[0]:
        return False
    return not any(dram[r[0]:r[1]])


def _mesh_index_hidden_in_dram(dram, name):
    """No-I/O core of mesh_index_hidden: read hide-state from an already-decoded blob0 buffer."""
    r = _mesh_index_range(bytes(dram), name)
    if not r or r[1] <= r[0]:
        return False
    return not any(dram[r[0]:r[1]])


def mesh_index_hidden(name, gdir):
    """True if this mesh's index buffer is currently all-zero (mesh disabled)."""
    dram, _ = oe.load_dram(IFF, gdir)
    return _mesh_index_hidden_in_dram(bytes(dram), name)


def _set_mesh_index_hidden_in_dram(dram, name, hide, gdir, log=print):
    """No-I/O core of set_mesh_index_hidden: zero (hide) / restore a mesh's index buffer in an
    in-memory blob0 bytearray IN PLACE. Still touches the tiny byte-backup file (needed to reverse
    a hide) but never load/apply_dram. `dram` must be a mutable bytearray."""
    r = _mesh_index_range(bytes(dram), name)
    if not r or r[1] <= r[0]:
        raise ValueError(f"{name}: index buffer not found")
    s, e = r
    bpath = _mesh_backup(gdir, name)
    if hide:
        if not bpath.exists():
            bpath.write_bytes(bytes(dram[s:e]))
        dram[s:e] = b"\x00" * (e - s)
        log(f"  {name}: index buffer zeroed ({e - s} B @0x{s:X}) -> mesh hidden")
    else:
        if not bpath.exists():
            raise ValueError(f"{name}: no backup to restore (was it ever hidden?)")
        orig = bpath.read_bytes()
        dram[s:s + len(orig)] = orig
        log(f"  {name}: index buffer restored")
    return dram


def set_mesh_index_hidden(name, hide, gdir, log=print):
    """Hide a mesh by zeroing its index buffer (all triangles degenerate → invisible), or restore
    from the one-time byte backup. Blob0 (DRAM) edit via the proven apply_dram path."""
    dram, meta = oe.load_dram(IFF, gdir)
    _set_mesh_index_hidden_in_dram(dram, name, hide, gdir, log)
    return oe.apply_dram(dram, meta, IFF, gdir, log)


# ── Bottom cyan/teal bar hide = PER-VERTEX ALPHA (cracked + in-game VERIFIED 2026-07-24) ─────────
# The bottom bar is NOT the glow_cylinder_color mesh, and it isn't a named scene mesh at all — it's
# drawn from a SHARED overlay geometry pool (that's why every named-mesh index/vertex hide was a
# no-op). Its pixel shader computes final alpha = interp.w * c3.w * c4.w = vertexAlpha * 1 * 1, i.e.
# the alpha is a PER-VERTEX byte in the vertex buffer (28-byte vertex: pos xyz @+0, RGBA color
# @+12 in ARGB order, uv @+20). RenderDoc showed the bar = index-buffer indices 46..61 (16 verts;
# the "256-stride" index values were the VS 8-in-16 endian byteswap of 46,47,48…). Those 16 verts
# live at file offset 0x215690 (=vertex idx46) in the DECOMPRESSED blob0. Zeroing the alpha byte
# (vertex+12) of all 16 → interp.w=0 → the bar is fully transparent. VERIFIED in-game.
# We self-relocate by the alpha-independent RGB fingerprint + the idx46 position so the edit still
# works after other in-place blob edits (and is detectable whether or not it's already hidden).
# Full method write-up: docs/15_renderdoc_mesh_editing.md.
_BAR_VB_HINT   = 0x215690      # decompressed-blob0 offset of bar vertex idx46
_BAR_STRIDE    = 28            # 7 dwords per vertex
_BAR_NVERTS    = 16           # indices 46..61
_BAR_ALPHA_OFF = 12           # ARGB color dword @+12 → alpha is its first byte
# RGB (bytes +13..+15), alpha-independent, for verts idx46..61:
_BAR_RGB = ["000000","000000","ffffff","ffffff","ffffff","ffffff","000000","000000",
            "000000","ffffff","ffffff","000000","000000","ffffff","ffffff","000000"]
_BAR_V0_XYZ = (37.5109, -172.9844, 2.8940)   # idx46 position, for a strong (tolerant) anchor


def _bar_base(dram):
    """Offset (in decompressed blob0) of bar vertex idx46, or None. Validates by the 16-vertex RGB
    fingerprint + the idx46 position, so it stays correct if the blob shifts and works whether the
    bar is currently hidden (alpha 0) or shown."""
    def ok(base):
        if base < 0 or base + _BAR_NVERTS * _BAR_STRIDE > len(dram):
            return False
        for k, rgb in enumerate(_BAR_RGB):
            o = base + k * _BAR_STRIDE + _BAR_ALPHA_OFF + 1     # +1 → skip alpha, read RGB
            if dram[o:o + 3].hex() != rgb:
                return False
        try:
            x, y, z = struct.unpack_from(">3f", dram, base)
        except struct.error:
            return False
        return (abs(x - _BAR_V0_XYZ[0]) < 1.0 and abs(y - _BAR_V0_XYZ[1]) < 1.0
                and abs(z - _BAR_V0_XYZ[2]) < 1.0)
    if ok(_BAR_VB_HINT):
        return _BAR_VB_HINT
    for base in range(0, len(dram) - _BAR_NVERTS * _BAR_STRIDE, 4):   # relocate if the hint moved
        if ok(base):
            return base
    return None


def _bar_alpha_offsets(dram):
    b = _bar_base(dram)
    if b is None:
        return None
    return [b + k * _BAR_STRIDE + _BAR_ALPHA_OFF for k in range(_BAR_NVERTS)]


def _bar_backup(gdir):
    return Path(gdir) / "scoreclock_barvertexalpha.json"


def _teal_bar_hidden_in_dram(dram):
    """No-I/O core of teal_bar_hidden: read hide-state from an already-decoded blob0 buffer."""
    offs = _bar_alpha_offsets(bytes(dram))
    if not offs:
        return False
    return all(dram[o] == 0 for o in offs)


def teal_bar_hidden(gdir):
    """True if the bottom bar's per-vertex alpha is currently all zero (bar transparent)."""
    dram, _ = oe.load_dram(IFF, gdir)
    return _teal_bar_hidden_in_dram(bytes(dram))


def _set_teal_bar_hidden_in_dram(dram, hide, gdir, log=print):
    """No-I/O core of set_teal_bar_hidden: zero (hide) / restore the bottom bar's 16 per-vertex
    alpha bytes in an in-memory blob0 bytearray IN PLACE. Touches only the tiny JSON backup, never
    load/apply_dram. `dram` must be a mutable bytearray."""
    import json
    offs = _bar_alpha_offsets(bytes(dram))
    if not offs:
        raise ValueError("bottom bar vertices not found in overlay_static blob0")
    bpath = _bar_backup(gdir)
    if hide:
        if not bpath.exists():
            bpath.write_text(json.dumps({"stride": _BAR_STRIDE, "alpha_off": _BAR_ALPHA_OFF,
                                         "backup": [[o, dram[o]] for o in offs]}))
        for o in offs:
            dram[o] = 0
        log(f"  bottom bar: zeroed alpha on {len(offs)} vertices @0x{offs[0]:X}.. -> transparent")
    else:
        if bpath.exists():
            for o, v in json.loads(bpath.read_text())["backup"]:
                dram[o] = v
            log("  bottom bar: per-vertex alpha restored from backup")
        else:                                          # no backup → restore stock opaque alphas
            stock = [255, 0, 255, 0, 255, 0, 255, 0, 255, 255, 255, 255, 0, 0, 0, 0]
            for o, v in zip(offs, stock):
                dram[o] = v
            log("  bottom bar: per-vertex alpha restored to stock (no backup found)")
    return dram


def set_teal_bar_hidden(hide, gdir, log=print):
    """Hide (zero) or restore the bottom cyan bar's 16 per-vertex alpha bytes. Reversible via a
    one-time JSON backup of the original bytes. In-place blob0 edit via apply_dram."""
    dram, meta = oe.load_dram(IFF, gdir)
    _set_teal_bar_hidden_in_dram(dram, hide, gdir, log)
    return oe.apply_dram(dram, meta, IFF, gdir, log)


# Back-compat aliases (clearer names for the vertex-alpha mechanism).
bar_vertexalpha_hidden = teal_bar_hidden
set_bar_vertexalpha_hidden = set_teal_bar_hidden


# The scorebug "2K" logo (grey backdrop + branding) = the logo_2k_mesh geometry. Hidden the SAME
# proven way as the glows: zero its index buffer so every triangle collapses and it draws nothing
# (the old set_2k_shadow_hidden shoved a bind-pose node off-screen, which the renderer ignored).
def scorebug_logo_hidden(gdir):
    return mesh_index_hidden("logo_2k_mesh", gdir)


def set_scorebug_logo_hidden(hide, gdir, log=print):
    return set_mesh_index_hidden("logo_2k_mesh", hide, gdir, log)


def effective_rows(rows, edits):
    """Apply pending `edits` (same shape as apply_edits) to a COPY of `rows` for preview —
    no file I/O. Returns new row dicts with x/y/w/h/size/color reflecting the edits."""
    out = []
    for r in rows:
        r = dict(r)
        ed = edits.get(r["name"], {})
        r["x"] = r["x"] + float(ed.get("dx", 0))
        r["y"] = r["y"] + float(ed.get("dy", 0))
        if r["kind"] == "mesh":
            r["w"] = r.get("w", 0) * float(ed.get("sx", 1))
            r["h"] = r.get("h", 0) * float(ed.get("sy", 1))
        else:
            if "size" in ed:
                r["size"] = float(ed["size"])
            if "color" in ed:
                r["color"] = "#%02X%02X%02X" % tuple(int(c) & 0xFF for c in ed["color"])
            if "uppercase" in ed:
                r["uppercase"] = bool(ed["uppercase"])
        out.append(r)
    return out


# Elements the game re-anchors at draw time (their scene coords don't match on-screen spot).
RUNTIME_PLACED = {"quarter"}

# Representative glyph drawn for each text element in the preview (no live data at edit time).
PREVIEW_TOKEN = {
    "away_abbrev": "AWA",   # away team abbreviation (blank-hash record before team2)
    "team1": "0",           # away score (red)
    "team2": "HOM",         # home abbrev (green)
    "team2_score": "0",     # home score (cyan)
    "quarter": "2", "gameclock1": "0", "gameclock_semi": "0",   # clock "1 8 : 5 2"
    "gameclock2": ":", "gameclock3": "0",
    "team1_score": "1st",   # period text (teal), far right
    "shots_away": "0", "gameclock4": "Shots", "shots_home": "0",   # SOG row ("Shots"->caps via Uppercase toggle)
}


def preview_fill(name):
    """Schematic fill colour for a mesh element in the preview."""
    if name.startswith("logo"):
        return "#9aa0a8"
    if "colorblur" in name:
        return "#264a70"
    if name.endswith("_color"):
        return "#3a6ea5"
    if "glint" in name:
        return "#6a9ad5"
    if "separator" in name:
        return "#4a4f5a"
    return "#5a6068"


# ── Preview screen layout ─────────────────────────────────────────────────────
# The game re-anchors elements through a parent-joint hierarchy at runtime, so an element's
# raw scene X/Y does NOT equal where it lands on screen (only DELTAS transfer faithfully:
# +X right, +Y up — proven in-game). So the preview is drawn from this CALIBRATED layout,
# hand-matched to the real scoreclock (Scoreclock.JPG, left→right: SN logo, ANA + disc + 0,
# VAN + disc + 0, 17:29, 1st), and the user's pending edits are layered on top. Coordinates
# are fractions of the scoreclock strip: (cx, cy) centre, (w, h) size, 0..1.
SCREEN_LAYOUT = {
    # colour-verified 2026-07-17 (Result.JPG). NOTE: the away abbrev + period ("1st") are NOT in
    # this element set (they stayed white in the recolor test); team1_score + gameclock4 aren't
    # shown in the bar, so they're omitted from the preview.
    "away_abbrev":     (0.190, 0.46, 0.075, 0.42),   # AWAY ABBREV "ANA" (leftmost)
    "logo_away":       (0.275, 0.50, 0.070, 0.60),
    "team1":           (0.375, 0.46, 0.030, 0.42),   # AWAY SCORE (red "0")
    "team2":           (0.455, 0.46, 0.075, 0.42),   # HOME ABBREV (green "ATL")
    "logo_home":       (0.555, 0.50, 0.070, 0.60),
    "team2_score":     (0.635, 0.46, 0.030, 0.42),   # HOME SCORE (cyan "0")
    # clock "1 8 : 5 2" (right)
    "quarter":         (0.700, 0.46, 0.024, 0.40),   # clock digit 1
    "gameclock1":      (0.723, 0.46, 0.024, 0.40),   # clock digit 2
    "gameclock_semi":  (0.760, 0.46, 0.024, 0.40),   # clock digit 3
    "gameclock2":      (0.742, 0.46, 0.014, 0.40),   # colon
    "gameclock3":      (0.783, 0.46, 0.024, 0.40),   # clock digit 4
    "team1_score":     (0.835, 0.46, 0.045, 0.42),   # PERIOD "1st" (teal) — far right
    # colour bars / glints — under each team panel (mesh side convention: team2_*=away,
    # team1_*=home)
    "team2_color":     (0.290, 0.80, 0.150, 0.045),
    "team2_colorblur": (0.290, 0.85, 0.160, 0.070),
    "team2_glint1":    (0.255, 0.77, 0.075, 0.030),
    "team2_glint2":    (0.335, 0.77, 0.045, 0.030),
    "team1_color":     (0.560, 0.80, 0.150, 0.045),
    "team1_colorblur": (0.560, 0.85, 0.160, 0.070),
    "team1_glint1":    (0.525, 0.77, 0.075, 0.030),
    "team1_glint2":    (0.605, 0.77, 0.045, 0.030),
    "glint_separator": (0.290, 0.92, 0.200, 0.028),
    "glint_separator1": (0.560, 0.92, 0.120, 0.028),
    # SOG row (added 2026-07-28): away # / "Shots" label / home #, below the main strip
    "shots_away":      (0.375, 0.80, 0.030, 0.34),
    "gameclock4":      (0.455, 0.80, 0.055, 0.34),
    "shots_home":      (0.585, 0.80, 0.030, 0.34),
}

# Scene-unit -> strip-fraction, for showing a move at roughly the right magnitude.
_SCENE_TO_NORM = 1.0 / 260.0


def _norm_color(c):
    return c if isinstance(c, str) else "#%02X%02X%02X" % tuple(int(v) & 0xFF for v in c)


def preview_layout(rows, edits, factory=False):
    """Per-element preview boxes in strip fractions. The BASELINE is the current file state
    (`rows` from list_elements) — so already-applied edits are shown — unless factory=True,
    which previews the pristine default. Pending `edits` layer on top of the baseline. Returns
    {name,label,kind,token,color,cx,cy,w,h, gcx,gcy,gw,gh (stock ghost), changed}."""
    by = {r["name"]: r for r in rows}
    out = []
    for nm, (bx, byc, bw, bh) in SCREEN_LAYOUT.items():
        r = by.get(nm)
        if not r:
            continue
        sp = STOCK_POS.get(nm)
        sm = STOCK_META.get(nm, {})
        ed = edits.get(nm, {})
        # baseline: pristine default, or the element's current on-disk state
        if factory:
            cur_x, cur_y = sp if sp else (r["x"], r["y"])
            cur_size = sm.get("size", r.get("size"))
            cur_color = sm.get("color", r.get("color", "#FFFFFF"))
            cur_w, cur_h = sm.get("w", r.get("w", 0)), sm.get("h", r.get("h", 0))
        else:
            cur_x, cur_y = r["x"], r["y"]
            cur_size = r.get("size"); cur_color = r.get("color", "#FFFFFF")
            cur_w, cur_h = r.get("w", 0), r.get("h", 0)
        eff_x = cur_x + float(ed.get("dx", 0))
        eff_y = cur_y + float(ed.get("dy", 0))
        if sp:
            cx = bx + (eff_x - sp[0]) * _SCENE_TO_NORM
            cy = byc - (eff_y - sp[1]) * _SCENE_TO_NORM          # +Y up -> up on strip
        else:
            cx, cy = bx, byc
        hidden = bool(ed.get("hidden"))
        if r["kind"] == "mesh":
            rx = (cur_w / sm["w"] if sm.get("w") else 1.0) * float(ed.get("sx", 1))
            ry = (cur_h / sm["h"] if sm.get("h") else 1.0) * float(ed.get("sy", 1))
            w, h = bw * rx, bh * ry
            color = preview_fill(nm)
        else:
            base_sz = sm.get("size") or cur_size or 8
            eff_sz = float(ed.get("size", cur_size if cur_size else base_sz))
            w, h = bw, bh * (eff_sz / base_sz if base_sz else 1.0)
            color = _norm_color(ed["color"]) if "color" in ed else (cur_color or "#FFFFFF")
        changed = bool(ed) or (sp is not None
                               and (abs(eff_x - sp[0]) > 0.01 or abs(eff_y - sp[1]) > 0.01))
        token = PREVIEW_TOKEN.get(nm, "")
        up = ed["uppercase"] if "uppercase" in ed else r.get("uppercase", False)
        if up:
            token = token.upper()
        out.append({
            "name": nm, "label": r["label"], "kind": r["kind"],
            "token": token, "color": color,
            "cx": cx, "cy": cy, "w": w, "h": h,
            "gcx": bx, "gcy": byc, "gw": bw, "gh": bh,
            "changed": changed or hidden, "hidden": hidden,
        })
    return out


# overlay_static.iff textures relevant to the scoreclock, keyed by texture index (validated by
# w/h/fmt at display time). label + whether it appears on the scoreclock strip.
TEX_LABELS = {
    0:  ("Glint / stripe mask (diagonal)", 128, 128, "8", True),
    1:  ("XM Radio ad logo", 128, 64, "8888", False),
    2:  ("NHL shield logo", 64, 64, "8888", False),
    3:  ("Ice rink surface", 256, 128, "DXT1", False),
    4:  ("Detail / normal map", 256, 256, "DXN", False),
    5:  ("Glint / stripe mask (small)", 64, 64, "8", True),
    6:  ("2K Sports · NHL 2K10 brand", 512, 512, "DXT4_5", True),
    7:  ("Blue 2K parallelogram chip", 128, 64, "8888", True),
    8:  ("Glint highlight bar", 64, 64, "8", True),
    9:  ("Scoreclock base panel (silver bar)", 256, 128, "DXT4_5", True),
    10: ("Team colour bar (tinted red/green)", 256, 64, "8_8", True),
    11: ("2K Sports red logo", 256, 256, "DXT4_5", True),
    12: ("White glow blob", 256, 256, "DXT4_5", True),
    13: ("Radial glow (alpha)", 256, 256, "DXT5A", True),
    14: ("Segment / glint tint bar", 256, 32, "8_8", True),
    15: ("Checkmark icon", 128, 128, "DXT4_5", False),
    16: ("Red X icon", 32, 32, "8888", False),
    17: ("Vertical-stripe glint mask", 128, 128, "8", True),
    # extras surfaced by the full fetch-scan (formal tree lists only 18)
    18: ("Dark HUD bar / panel", 256, 64, "DXT4_5", True),
    19: ("Cyan glow bar", 256, 64, "DXT4_5", True),
    20: ("White/gold glow gradient", 256, 256, "DXT4_5", True),
}


def texture_label(index, rec):
    """(label, is_scoreclock) for an overlay_static texture record, validated by dimensions."""
    info = TEX_LABELS.get(index)
    if info:
        lbl, w, h, fmt, sc = info
        if rec.get("w") == w and rec.get("h") == h:
            return lbl, sc
    return f"Texture #{index}", False


def preview_bbox(rows):
    """(minx, miny, maxx, maxy) over element extents, ignoring RUNTIME_PLACED outliers so the
    strip fills the preview. Text points get a small pad; meshes use their bbox."""
    xs, ys = [], []
    for r in rows:
        if r["name"] in RUNTIME_PLACED:
            continue
        if r["kind"] == "mesh":
            hw, hh = r.get("w", 0) / 2, r.get("h", 0) / 2
        else:
            hw = hh = max(2.0, r.get("size", 8) / 4)
        xs += [r["x"] - hw, r["x"] + hw]
        ys += [r["y"] - hh, r["y"] + hh]
    if not xs:
        return (-180, -175, 15, -145)
    return (min(xs), min(ys), max(xs), max(ys))


def restore_stock(gdir, log=print):
    """Put every element back to pristine: position, mesh size (bbox ratio), font size
    and color."""
    edits = {}
    for r in list_elements(gdir):
        nm = r["name"]
        stock = STOCK_POS.get(nm)
        if not stock:
            continue
        ed = {}
        dx, dy = stock[0] - r["x"], stock[1] - r["y"]
        if abs(dx) > 0.005 or abs(dy) > 0.005:
            ed["dx"], ed["dy"] = dx, dy
        m = STOCK_META.get(nm, {})
        if r["kind"] == "mesh" and "w" in m:
            if r["w"] > 0.001 and abs(r["w"] - m["w"]) > 0.01:
                ed["sx"] = m["w"] / r["w"]
            if r["h"] > 0.001 and abs(r["h"] - m["h"]) > 0.01:
                ed["sy"] = m["h"] / r["h"]
        elif r["kind"] == "text":
            if "size" in m and abs(r["size"] - m["size"]) > 0.01:
                ed["size"] = m["size"]
            if abs(r.get("scale_x", 1) - 1) > 0.001 or abs(r.get("scale_y", 1) - 1) > 0.001:
                ed["sx"] = 1.0; ed["sy"] = 1.0      # reset glyph scale to stock consts
            _sfh = _STOCK_FONT.get(nm)              # reset font (+0xDC) to stock
            if _sfh is not None and r.get("font_hash", _sfh) != _sfh:
                ed["font"] = _sfh
            # NOTE: colour is intentionally NOT reset — it's the field users most often set by
            # hand, so "restore layout" leaves recolours alone. Reset colour per-element via the
            # colour picker if wanted.
        if ed:
            edits[nm] = ed
    if not edits:
        return "already at stock layout"
    return apply_edits(edits, gdir, log)


def factory_edits(gdir):
    """The edit-set that would return EVERY element to pristine default — position, mesh size,
    font size AND colour (the teal '1st' comes back). Used by the 'Reset to Default' preview
    (staged) and its Apply."""
    edits = {}
    for r in list_elements(gdir):
        nm = r["name"]
        sp = STOCK_POS.get(nm)
        if not sp:
            continue
        ed = {}
        dx, dy = sp[0] - r["x"], sp[1] - r["y"]
        if abs(dx) > 0.005 or abs(dy) > 0.005:
            ed["dx"], ed["dy"] = dx, dy
        m = STOCK_META.get(nm, {})
        if r["kind"] == "mesh" and "w" in m:
            if r["w"] > 0.001 and abs(r["w"] - m["w"]) > 0.01:
                ed["sx"] = m["w"] / r["w"]
            if r["h"] > 0.001 and abs(r["h"] - m["h"]) > 0.01:
                ed["sy"] = m["h"] / r["h"]
        elif r["kind"] == "text":
            if "size" in m and abs(r["size"] - m["size"]) > 0.01:
                ed["size"] = m["size"]
            if abs(r.get("scale_x", 1) - 1) > 0.001 or abs(r.get("scale_y", 1) - 1) > 0.001:
                ed["sx"] = 1.0; ed["sy"] = 1.0      # reset glyph scale to stock consts
            _sfh = _STOCK_FONT.get(nm)              # reset font (+0xDC) to stock
            if _sfh is not None and r.get("font_hash", _sfh) != _sfh:
                ed["font"] = _sfh
            if "color" in m and r["color"].upper() != m["color"].upper():
                c = m["color"].lstrip("#")
                ed["color"] = tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
        if ed:
            edits[nm] = ed
    return edits


def reset_to_factory(gdir, log=print):
    """Write pristine default to the file (position, size AND colour — restores the teal '1st')."""
    edits = factory_edits(gdir)
    if not edits:
        return "already at factory default"
    return apply_edits(edits, gdir, log)


if __name__ == "__main__":
    GAME = r"C:\Users\cloug\Documents\NHL 2k10 Extracted"
    for r in list_elements(GAME):
        print(f"{r['kind']:4} {r['name']:20} ({r['label']}): "
              f"x={r['x']:9.2f} y={r['y']:9.2f} verts={r['n']}")
