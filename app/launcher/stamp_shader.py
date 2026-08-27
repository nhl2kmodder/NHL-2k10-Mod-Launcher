"""stamp_shader.py -- place jersey stamps with the game's own pixel-shader math.

WHAT CHANGED AND WHY
--------------------
The preview used to place each stamp by blitting its source cell into a measured destination
rectangle. That is not what the game does, and the difference is visible: a rect blit has to
STRETCH the art to fill its rect, so glyph proportions drifted and the numbers came out far
larger than in game (the stored crest rect was 278x277 where the shader's own mask works out to
204x205 -- a 36% oversize, and it ran into the jersey's stripes).

The game never has a destination rectangle. Its jersey pixel shader samples the 2048x512 stamps
sheet TWICE, by two different rules, and lets the mesh decide where the art lands:

  LAYER 1 -- "logo" (crest, and the shoulder-patch/sponsor family). Unconditional. The sheet
  coordinate is an affine function of the BASE UV:

      sheet.x = ku * u + bu
      sheet.y = kv * v + bv
      drawn only where `window` (umin, vmin, umax, vmax) contains it -- the window MASKS, it
      never clamps, so the art appears exactly on the band of the body map the affine maps into
      the window, at exactly the scale the affine implies.

  LAYER 2 -- "decal" (numbers, letters, the pants mark). Predicated on a per-vertex selector.
  Interpolator 3 carries (decalU, decalV, slot) baked into the MESH, and each slot has its own
  scale/offset pair:

      sheet.xy = decalUV * S.xy + S.zw     drawn where mask M contains it

  The decal channel is read off the game's own vertex streams -- global.iff, the skater's
  attribute stream, snorm16 (decalU, decalV, slot) two bytes past the base V (player_model.py)
  -- and rasterised into base-UV space per garment piece. It is re-read whenever the archive
  changes; the shipped decal_atlas.npz (the same fields, off a RenderDoc pass) is the fallback.

  So a slot is a fixed quad on the garment, and the constants choose which piece of the sheet is
  pulled into it. One slot holds exactly ONE glyph, which is why a two-digit number occupies two
  slots.

Both were validated against the game's own render target: the shirt's crest and its "71", and the
back panel's "71", reproduce pixel-exactly with no destination rect anywhere. The per-slot roles
in data/stamp_shader.json were likewise read off the game's render rather than inferred -- see
tools/build_stamp_map.py for how the decal channel is baked.

THE GLYPH CONSTANT RULE
-----------------------
Digits are monospaced and the constants are derived from the art, so the launcher can synthesise
them for any glyph instead of needing a capture per digit. For a cell (cx0, cy0, cx1, cy1) whose
ink spans x [ix0, ix1] and an advance A:

    S = (A/SW,  (cy1-cy0)/SH,  (inkCentreX - A/2)/SW,  cy0/SH)
    M = (ix0/SW, cy0/SH, ix1/SW, cy1/SH)          -- tight in X, the full cell in Y

Checked against the captured constants for both '7' (ink x[1437,1508], centre 1472.5, A=79 ->
S.z*2048 = 1433.0) and '1' (ink x[1065,1111], centre 1088 -> S.z*2048 = 1048.5). Exact.

THE "LOGO LAYER" WAS A MISREADING
---------------------------------
The shield / manufacturer mark / helmet decals were thought to need a second, undecoded affine.
They do not. In ps1019 the c33-vs-c44 select that picks them is driven by `interp3.w > c9.x` --
the SAME selector the number ladder runs on. Every stamp on the model is a decal quad; only the
crest uses layer 1. So a stamp's destination is baked into the mesh and is measured directly by
rasterising interp3 into base UV, which is what tools/build_stamp_map.py does. There is no
destination rectangle and no undecoded coordinate anywhere.

That closed the last three sites: the NHL shield (collar and pants), the manufacturer wordmark, and
the helmet's team mark and number. The helmet draws from its OWN sheet -- index 2 of
uniform_<team>_<kit>.iff, 1024x256 -- not from the 2048x512 stamps sheet, so its sites carry
`"sheet": "helmet"`. Note also that the helmet parks "no decal here" at slot 0 and starts real
slots at 1, where the garment shaders park it at 6.

The team's SHOULDER PATCH is not a stamp at all: it is painted into the uniform base texture.

Piece `pants` (draw 1080) carries the NHL shield and the manufacturer wordmark -- the BAUER mark
the game renders on the thigh. It was briefly renamed "shoulders" by reading its slots' base-UV
y[209,264] as a body position; a texture coordinate does not tell you where a slot lands on the
player. The model capture's own part list names the draw `pants` and its mesh spans y[-39.2,+21.3].

WHAT IS NOT DONE HERE
---------------------
The back NAME is a third mechanism -- draw 1055 binds two 1024x256 RGBA8 textures holding a
pre-rendered name strip, placed by a switch/case affine, not by either layer above. It stays on
the old measured path in jersey_preview and is reported by `unshaded()`.

`helmet` slot 5 (a 230x527 band) is the FRONT SPONSOR badge -- the Reebok wordmark on the front of
the shell in the game's own render -- and is left unassigned on purpose. Its art is not in
uniform_<team>_<kit>.iff: the helmet draw binds a shared 256x256 BC3 sheet carrying an Easton
wordmark and an NHL shield, so no edit the Jersey Editor can make would change it, and drawing it
would only bake a fixed logo into the preview.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

ATLAS_NAME = "decal_atlas.npz"
CONFIG_NAME = "stamp_shader.json"

_atlas = None
_config = None


def _data_dir() -> Path:
    try:
        from . import resources as R
        return R.data_path(CONFIG_NAME).parent
    except Exception:
        return Path(__file__).resolve().parent / "data"


def available() -> bool:
    """The shader path can run: its config is present and there is a decal atlas to be had --
    baked live from the game's global.iff (player_model) or, failing that, the shipped npz."""
    d = _data_dir()
    if not (d / CONFIG_NAME).exists():
        return False
    try:
        from . import player_model as PM
        if PM.available():
            return True
    except Exception:
        pass
    return (d / ATLAS_NAME).exists()


def config() -> dict:
    global _config
    if _config is None:
        p = _data_dir() / CONFIG_NAME
        _config = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _config


_atlas_key = None       # player_model.atlas_key() the cached atlas was built for ("" = the npz)


def _shift_sleeves(a: dict) -> dict:
    if "sleeves_uv" in a:
        # The arm quads' baked decalV runs [-0.5,+1.5], which centres the digit band in the quad
        # and parks its ink 9px off the sleeve stripe. The live game draws it half a cell up the
        # arm: on the 2026-08-18 SEA-away screenshot the ink-to-stripe gap measures 48±8 texels
        # against the stripe bands' known UV widths, i.e. the ink sits flush to the quad's
        # shoulder edge -- exactly decalV+0.5. The mesh really does carry the centred values (the
        # on-disk stream and the captured pass agree), so whatever adds that half cell (a VS
        # constant, most likely) is not in the vertex data; the render is the authority, so the
        # shift is applied here, at load, for every consumer.
        uv = a["sleeves_uv"].astype(np.float32)
        uv[..., 1] += 0.5
        a["sleeves_uv"] = uv.astype(a["sleeves_uv"].dtype)
    return a


def atlas() -> dict:
    """The decal channel: per piece, `decalUV`, the slot id and coverage, in 1024^2 UV.

    Rasterised from the game's own vertex streams (player_model.decal_atlas, off global.iff in
    the configured game folder) and re-read whenever that archive changes; the shipped
    decal_atlas.npz -- the same fields, baked from a RenderDoc pass -- is the fallback for when
    no game folder is set.
    """
    global _atlas, _atlas_key
    key = None
    try:
        from . import player_model as PM
        key = PM.atlas_key()
    except Exception:
        key = None
    if _atlas is not None and _atlas_key == (key or ""):
        return _atlas
    got = None
    if key:
        try:
            got = PM.decal_atlas()
        except Exception:
            got = None
    if got:
        _atlas, _atlas_key = _shift_sleeves(dict(got)), key
    else:
        p = _data_dir() / ATLAS_NAME
        if not p.exists():
            _atlas = {}
        else:
            with np.load(p, allow_pickle=False) as z:
                _atlas = _shift_sleeves({k: z[k] for k in z.files})
        _atlas_key = ""
    _BBOX.clear()
    global _collar_applied
    _collar_applied = None
    _apply_collar()
    return _atlas


def atlas_source() -> str:
    """'game' when the atlas came off global.iff, 'npz' for the shipped bake, '' for none."""
    atlas()
    return "game" if _atlas_key else ("npz" if _atlas else "")


_collar = None          # the collar style (0..4) whose "shirt2@c<n>" fields are aliased to shirt2
_collar_applied = None


def select_collar(style) -> None:
    """Point the collar piece ("shirt2") at the collar the figure is wearing.

    The skater carries five collars and each has its NHL-shield quad in its own place on the
    base sheet, so player_model rasterises one "shirt2@c<style>" field set per collar. This
    aliases the chosen one onto "shirt2", which is the name every site in stamp_shader.json
    uses. None = leave whatever the atlas came with (the capture bake's collar, style 2)."""
    global _collar
    _collar = None if style is None else int(style)
    if _atlas:
        _apply_collar()


def _apply_collar() -> None:
    global _collar_applied
    if _collar_applied == _collar:
        return
    if _collar is not None and f"shirt2@c{_collar}_cov" in _atlas:
        for f in ("uv", "slot", "cov"):
            _atlas[f"shirt2_{f}"] = _atlas[f"shirt2@c{_collar}_{f}"]
        _BBOX.clear()
    _collar_applied = _collar


def atlas_pieces() -> set[str]:
    """Which garment pieces the baked atlas actually carries (the per-collar variants of the
    collar piece are not pieces of their own -- select_collar aliases one onto "shirt2")."""
    return {k[:-4] for k in atlas() if k.endswith("_cov") and "@" not in k}


def unshaded() -> list[str]:
    """Stamps still drawn the old way, or not at all, with the reason."""
    return list(config().get("not_shaded", {}).keys())


# ── per-kit glyph metrics ─────────────────────────────────────────────────────────────────────
#
# The digit advance and the per-digit ink rects are NOT a property of the site, and they are not
# derivable from the art. They live in the uniform's own DRAM blob and differ from kit to kit:
#
#     0x540   u32 advance in sheet pixels, u32 128 (the row height, constant)
#     0x550   27 x (u32 x, y, w, h) -- the kit's slot table; entry 8 + d is digit d's INK rect
#
# Measured: cgy_home 92, ana_away 104, chi_home 79. Those are exactly the advances the captures'
# own pixel-shader constant buffers carry for every garment draw in the matching capture, and the
# rects match the captured masks byte for byte (ana '7' -> entry 15 = (1419,256,104,256), captured
# mask x[1419,1523]; ana '4' -> entry 12 = (1298,0,91,256), S.z*2048 = 1298+45.5-52 = 1291.5).
#
# stamp_shader.json used to hardcode 79 for every garment number site, so a Calgary kit drew every
# digit 92/79 = 1.16x too large and the big front numbers 92/67 = 1.37x -- the "numbers look oddly
# big" report. A SMALLER advance stretches the same ink across more of the slot quad.
_METRICS: dict[str, dict | None] = {}
_TABLES: dict[str, dict | None] = {}

DIGIT_SLOT0 = 8       # slot-table index of digit 0; digit d is at DIGIT_SLOT0 + d
ADVANCE_OFF = 0x540   # u32 digit advance, u32 row height (128)
TABLE_OFF = 0x550     # 27 x (u32 x, y, w, h), big-endian
TABLE_ENTRIES = 27
TABLE_END = TABLE_OFF + TABLE_ENTRIES * 16          # 0x700

# Which table entry each editor slot is. This is the order Uniform_SetStampShaderParams
# (0x8408EAE8) indexes the table in -- NOT the order the profile lists the slots, which was
# eyeballed off the sheet left to right. Entry N lives at DRAM 0x550 + N*16.
DRAM_ENTRY = {
    "crest": 0, "pants_patch": 1, "shoulder_patch": 2, "shoulder_right": 3,
    "cup_patch": 4, "league_patch": 5, "pants_sponsor": 6, "jersey_sponsor": 7,
    **{f"digit_{d}": DIGIT_SLOT0 + d for d in range(10)},
    "capt_a": 18, "capt_c": 19, "sponsor_3": 20,
    **{f"small_num_{k}": 21 + k for k in range(6)},
}
ENTRY_NAME = {v: k for k, v in DRAM_ENTRY.items()}

# The ROS uniform +0x18 pickers: a 3-bit field each, whose VALUE is the table entry the site
# draws (1 pants_patch, 2 shoulder_patch, 3 shoulder_right) and 4 means "nothing here".
PICK_NONE = 4


def clear_cache(iff: str | None = None) -> None:
    """Forget cached tables/metrics -- after a write, or when the archives change under us."""
    if iff is None:
        _METRICS.clear()
        _TABLES.clear()
        _LOGO.clear()
    else:
        _METRICS.pop(iff, None)
        _TABLES.pop(iff, None)
        for k in [k for k in _LOGO if k.split("|")[0] in (iff, base_iff(iff))]:
            _LOGO.pop(k, None)


def _dram_blob(iff: str, live: bool = True):
    """(bytes, blob dict) of the uniform asset's DRAM blob (the one carrying the table)."""
    from . import archive_textures as A
    _loc, data, size = A._read_asset(iff, None, live)
    for b in A._walk_blobs(data, size):
        if b.get("dec") and len(b["dec"]) > TABLE_END:
            return b["dec"], b
    return None, None


def parse_table(d: bytes) -> dict | None:
    """The slot table out of a decoded DRAM blob -> {'advance', 'row_h', 'rects': [[x,y,w,h]]*27}."""
    import struct
    if d is None or len(d) < TABLE_END:
        return None
    adv, row_h = struct.unpack_from(">2I", d, ADVANCE_OFF)
    rects = [list(struct.unpack_from(">4I", d, TABLE_OFF + i * 16)) for i in range(TABLE_ENTRIES)]
    if not (8 <= adv <= 512):
        return None
    return {"advance": int(adv), "row_h": int(row_h), "rects": rects}


def pack_table(d: bytes, table: dict) -> bytes:
    """`d` with the table's 0x540..0x700 span replaced. Size-preserving by construction."""
    import struct
    out = bytearray(d)
    struct.pack_into(">2I", out, ADVANCE_OFF, int(table["advance"]), int(table.get("row_h", 128)))
    for i, r in enumerate(table["rects"][:TABLE_ENTRIES]):
        struct.pack_into(">4I", out, TABLE_OFF + i * 16, *(max(0, int(v)) for v in r))
    return bytes(out)


def kit_table(iff: str, live: bool = True) -> dict | None:
    """The kit's own slot table, read from the game files as they are NOW (live=True).

    None if the asset or its DRAM blob cannot be read. Cached per asset; `clear_cache` after a
    write. `live=False` reads the pristine `.orig` copy instead.
    """
    key = iff if live else iff + "|orig"
    if key in _TABLES:
        return _TABLES[key]
    out = None
    try:
        d, _b = _dram_blob(iff, live)
        out = parse_table(d)
    except Exception:
        out = None
    _TABLES[key] = out
    return out


def table_metrics(table: dict | None, flags: dict | None = None,
                  logo: dict | None = None) -> dict | None:
    """The `metrics` dict the preview takes, from a table (+ the uniform's ROS flags).

    {'advance', 'digit_rects': [(x0,y0,x1,y1)]*10, 'rects': the 27 [x,y,w,h], 'flags': {...},
     'logo': {...}}.
    `flags` is `uniform_colors.stamp_flags()` -- front-number style, the two sleeve enables and
    the four patch pickers -- or None when no roster is loaded (everything then draws). `logo`
    is `kit_logo_params()` -- the crest/shoulder placement -- or None for the material defaults.
    """
    if not table:
        return None
    rects = [list(r) for r in table["rects"]]
    digit_rects = []
    for dg in range(10):
        x, y, w, h = rects[DIGIT_SLOT0 + dg]
        digit_rects.append((x, y, x + w, y + h))
    return {"advance": int(table["advance"]), "digit_rects": digit_rects, "rects": rects,
            "flags": dict(flags or {}), "logo": logo}


def kit_metrics(iff: str) -> dict | None:
    """{'advance': px, 'digit_rects': [(x0,y0,x1,y1)] * 10, 'rects', 'flags'} for one kit.

    Returns None if the asset or its DRAM blob cannot be read, in which case callers fall back to
    the per-site `advance_px` and the nominal 128x256 cell grid.
    """
    if iff in _METRICS:
        return _METRICS[iff]
    out = table_metrics(kit_table(iff), logo=kit_logo_params(iff))
    if out and not all(r[2] > r[0] and r[3] > r[1] for r in out["digit_rects"]):
        out = None
    _METRICS[iff] = out
    return out


# ── per-kit logo placement: the uniform_base MATERIAL parameters ──────────────────────────────
#
# Where the crest and the two shoulder patches land on the garment is not a shader literal and
# not in the slot table: it is four material parameters per logo, per KIT, in
# uniform_base_<team>_<kit>.iff's DRAM section (0xBB05A9C1), pushed to the pixel shader by
# Function_8408F840 -> Material_SetParamByHash. Records are 0x50 bytes -- +4 the CRC32 of the
# parameter's name, +0x20 its f32 value -- and every material's set is the same 165 records, so
# each name occurs once per material, in material order: #0 back, #1 FRONT, #2 ARMS, #3 pants.
#
# The shader then maps the base UV (u, v) to a pre-cell sheet position:
#     crest          U =  HS*u + 0.5 + (HOff - HS)/2      V =  VS*v + 0.5 + (VOff - VS)/2
#     left shoulder  U = -HS*v + 0.5 + (HOff + HS)/2      V =  VS*u + 0.5 + (VOff - VS)/2
#     right shoulder U =  HS*v + 0.5 + (HOff - HS)/2      V = -VS*u + 0.5 + (VOff + VS)/2
# (the arms are unwrapped sideways, so the shoulders read the axes swapped and one mirrored),
# and the cell's S/M (Stamp_SetCellParams on the slot-table entry -- 0 for the crest, the ROS
# left/right picker's entry for the shoulders) finish it: sheet = pre * S.xy + S.zw. Checked on
# the ana_away capture: its front set (0, 3.7, -2.136, 3.7) gives 3.7u-1.35 / 3.7v-2.418, and the
# crest measured off that render was 3.684u-1.341 / 3.694v-2.413.
LOGO_KINDS = {"front": "FrntLogo", "left": "SlvLftLogo", "right": "SlvRgtLogo"}
LOGO_PARAM_HASH = {
    "FrntLogoHOffset": 0x63693738, "FrntLogoHScale": 0x3AEA03E5,
    "FrntLogoVOffset": 0x5488D0AB, "FrntLogoVScale": 0x0336600E,
    "SlvLftLogoHOffset": 0xA7FCF262, "SlvLftLogoHScale": 0xCE65E11C,
    "SlvLftLogoVOffset": 0x901D15F1, "SlvLftLogoVScale": 0xF7B982F7,
    "SlvRgtLogoHOffset": 0x77A7A76D, "SlvRgtLogoHScale": 0xF52619F9,
    "SlvRgtLogoVOffset": 0x404640FE, "SlvRgtLogoVScale": 0xCCFA7A12,
}
LOGO_SET = {"front": 1, "left": 2, "right": 2}          # which material's set holds each logo
# The material's own defaults (the record's value in a kit that never overrode it).
LOGO_DEFAULTS = {"front": (0.0, 3.0, -1.5, 3.0), "left": (0.0, 9.0, -2.0, 9.0),
                 "right": (0.0, 9.0, -2.0, 9.0)}
_LOGO: dict[str, dict | None] = {}


def base_iff(iff: str) -> str:
    """uniform_<t>_<k>.iff -> uniform_base_<t>_<k>.iff (a base name passes through)."""
    import re as _re
    m = _re.match(r"^uniform_(?:base_)?([a-z0-9]{2,4}_(?:home|away|alt))\.iff$", iff)
    return f"uniform_base_{m.group(1)}.iff" if m else iff


def _logo_records(d: bytes) -> dict | None:
    """{'front'|'left'|'right': (HOffset, HScale, VOffset, VScale)} out of one decoded blob, or
    None when it is not the material-parameter blob (every name must occur >= 3 times)."""
    import struct
    vals = {}
    for name, h in LOGO_PARAM_HASH.items():
        pat = struct.pack(">I", h)
        hits = []
        i = d.find(pat)
        while i != -1:
            if i % 4 == 0 and i >= 4 and i + 0x24 <= len(d):
                hits.append(struct.unpack_from(">f", d, i - 4 + 0x20)[0])
            i = d.find(pat, i + 1)
        if len(hits) < 3:
            return None
        vals[name] = hits
    out = {}
    for kind, pre in LOGO_KINDS.items():
        j = LOGO_SET[kind]
        out[kind] = tuple(float(vals[pre + f][j]) for f in ("HOffset", "HScale", "VOffset",
                                                            "VScale"))
    return out


def kit_logo_params(iff: str, live: bool = True) -> dict | None:
    """The kit's crest / shoulder placement parameters, read off its uniform_base asset as the
    game files are NOW: {'front'|'left'|'right': (HOffset, HScale, VOffset, VScale)}. None when
    the base asset (or its material blob) cannot be read -- callers then use LOGO_DEFAULTS.
    """
    name = base_iff(iff)
    key = name if live else name + "|orig"
    if key in _LOGO:
        return _LOGO[key]
    out = None
    try:
        from . import archive_textures as A
        _loc, data, size = A._read_asset(name, None, live)
        if data is not None:
            for b in A._walk_blobs(data, size):
                dec = b.get("dec")
                if dec and len(dec) > 0x3000:
                    out = _logo_records(dec)
                    if out:
                        break
    except Exception:
        out = None
    _LOGO[key] = out
    return out


def logo_affine(kind: str, params) -> dict:
    """The pre-cell affine for one logo: sheet.xy = (ku*u + kuv*v + bu, kvu*u + kv*v + bv)."""
    ho, hs, vo, vs = (float(x) for x in params)
    if kind == "front":
        return dict(ku=hs, kuv=0.0, bu=0.5 + (ho - hs) / 2, kvu=0.0, kv=vs, bv=0.5 + (vo - vs) / 2)
    if kind == "left":
        return dict(ku=0.0, kuv=-hs, bu=0.5 + (ho + hs) / 2, kvu=vs, kv=0.0, bv=0.5 + (vo - vs) / 2)
    if kind == "right":
        return dict(ku=0.0, kuv=hs, bu=0.5 + (ho - hs) / 2, kvu=-vs, kv=0.0, bv=0.5 + (vo + vs) / 2)
    raise ValueError(kind)


def logo_site(site: dict, kind: str, params, rect, sheet_size=(2048, 512)) -> dict | None:
    """A drawable logo site: the kit's affine for `kind` composed with the cell (x0,y0,x1,y1)."""
    sm = cell_constants(rect, sheet_size)
    if sm is None:
        return None
    S, M = sm
    a = logo_affine(kind, params)
    out = dict(site)
    out.update(ku=a["ku"] * S[0], kuv=a["kuv"] * S[0], bu=a["bu"] * S[0] + S[2],
               kvu=a["kvu"] * S[1], kv=a["kv"] * S[1], bv=a["bv"] * S[1] + S[3],
               window=list(M))
    return out


def _site_uv(site: dict, u, v):
    """sheet.xy at base UV (u, v) for a logo site; `kuv`/`kvu` are the transposed shoulders'
    cross terms and default to 0 (the crest's measured site has none)."""
    su = u * site["ku"] + v * site.get("kuv", 0.0) + site["bu"]
    sv = u * site.get("kvu", 0.0) + v * site["kv"] + site["bv"]
    return su, sv


def entry_rect(metrics: dict | None, entry: int | None):
    """The table entry as an (x0, y0, x1, y1) cell, or None when absent/empty (w or h == 0)."""
    if not metrics or entry is None or not metrics.get("rects"):
        return None
    try:
        x, y, w, h = metrics["rects"][int(entry)]
    except (IndexError, ValueError, TypeError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, x + w, y + h)


def cell_constants(rect, sheet_size=(2048, 512)):
    """(S, M) for a whole-cell stamp -- Stamp_SetCellParams (0x840DFD88) to the letter:
    S = (w/W, h/H, x/W, y/H), M = the cell rect. rect is (x0, y0, x1, y1)."""
    SW, SH = sheet_size
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return None
    S = ((x1 - x0) / float(SW), (y1 - y0) / float(SH), x0 / float(SW), y0 / float(SH))
    return S, (x0 / float(SW), y0 / float(SH), x1 / float(SW), y1 / float(SH))


def digit_constants(metrics: dict, digit: int, sheet_size=(2048, 512), f1: float = 0.0):
    """(S, M) for one digit, straight off the kit's own table -- no art inspection at all.

    Stamp_SetDigitParams (0x840DFB60): S = (adv/W, h/H, (x + w/2 - adv/2 + adv*f1)/W, y/H),
    M = the ink rect. `f1` shifts the sample window by whole advances: 0 is the normal two-digit
    case; a ONE-digit number puts the same digit in both slots with f1 = -0.5 on the tens quad
    and +0.5 on the ones quad, which centres the glyph on the seam between them (each quad shows
    its half through the mask); the game's "off" is f1 = 8.0, i.e. eight advances off the sheet.
    """
    SW, SH = sheet_size
    x0, y0, x1, y1 = metrics["digit_rects"][digit]
    A = float(metrics["advance"])
    centre = (x0 + x1) / 2.0
    S = (A / SW, (y1 - y0) / float(SH), (centre - A / 2.0 + A * f1) / SW, y0 / float(SH))
    return S, (x0 / float(SW), y0 / float(SH), x1 / float(SW), y1 / float(SH))


# The crest is the one stamp drawn off the BASE UV (FrontLogo, via Stamp_SetCellParams on entry
# 0), and the site in stamp_shader.json was measured with the shipped entry-0 cell (0,0,512,512),
# i.e. S = (0.25, 1, 0, 0). The mesh-side affine base-UV -> decalUV is fixed, so any other cell is
# that same affine composed with the new S/M.
CREST_REF_S = (0.25, 1.0, 0.0, 0.0)


def crest_site(rect, site: dict, sheet_size=(2048, 512)) -> dict | None:
    """The crest logo site for an arbitrary entry-0 cell (x0, y0, x1, y1), from the measured one."""
    sm = cell_constants(rect, sheet_size)
    if sm is None:
        return None
    S, M = sm
    rx, ry = CREST_REF_S[0], CREST_REF_S[1]
    out = dict(site)
    out["ku"] = site["ku"] * (S[0] / rx)
    out["kuv"] = site.get("kuv", 0.0) * (S[0] / rx)
    out["bu"] = site["bu"] * (S[0] / rx) + S[2]
    out["kvu"] = site.get("kvu", 0.0) * (S[1] / ry)
    out["kv"] = site["kv"] * (S[1] / ry)
    out["bv"] = site["bv"] * (S[1] / ry) + S[3]
    out["window"] = list(M)
    return out


def write_kit_table(iff: str, table: dict, game_dir, log=print) -> str:
    """Write a slot table into the uniform's DRAM blob, in place and size-preserving.

    Same recipe as arena_model.write_dram: re-encode blob 0 with its own codec/window, pad the
    packed bytes back to the original slot so nothing after it moves and the TOC stays put, verify
    the round trip, back the archive up once, write. The table is 0x1C0 bytes of a 28 KB blob and
    compresses about as well as before, so the slot is never a problem in practice; if it were,
    nothing is written and the reason is raised.
    """
    import struct
    from pathlib import Path
    from . import archive_textures as A
    from . import decode_e4837_fixed as DF
    from . import encode_e4837_lazy as EE
    game_dir = Path(game_dir)
    loc = A.resolve(iff, game_dir)
    if not loc:
        raise ValueError(f"{iff}: not found in game TOC")
    arc, off, size, _idx, _f3 = loc
    with open(game_dir / arc, "rb") as f:
        f.seek(off)
        res = bytearray(f.read(size))
    b = next((b for b in A._walk_blobs(res, size) if b.get("dec") and len(b["dec"]) > TABLE_END),
             None)
    if b is None:
        raise ValueError(f"{iff}: no DRAM blob")
    new = pack_table(b["dec"], table)
    if new == bytes(b["dec"]):
        return f"{iff}: slot table unchanged"
    blob = EE.encode_payload(new, wparam=b["wp"], codec=b["codec"])
    if len(blob) > b["tot"]:
        raise ValueError(f"{iff}: the edited table re-compresses to {len(blob)} bytes but its "
                         f"slot is {b['tot']} -- nothing written")
    padded = blob[:8] + struct.pack(">I", b["tot"]) + blob[12:] + b"\x00" * (b["tot"] - len(blob))
    chk = DF.decompress_codec(padded[20:b["tot"]], b["dec_sz"], (1 << b["wp"]) - 1, b["wp"])
    if chk != new:
        raise ValueError(f"{iff}: re-encoded DRAM did not round-trip -- nothing written")
    res[b["off"]:b["off"] + b["tot"]] = padded
    A._backup_once(game_dir / arc, log)
    with open(game_dir / arc, "r+b") as f:
        f.seek(off)
        f.write(res)
    clear_cache(iff)
    try:
        from . import volume_store as _vs
        _vs.sync_after_op(game_dir, (iff,))
    except Exception:
        pass
    return (f"IN-PLACE: {iff} slot table (0x540..0x700) -> {arc}:0x{off:X} "
            f"({len(blob)}/{b['tot']} bytes of the blob slot)")


# ── sampling ──────────────────────────────────────────────────────────────────────────────────

def _sample(sheet: np.ndarray, su: np.ndarray, sv: np.ndarray) -> np.ndarray:
    """Bilinear fetch, matching the sampler the draws use (linear, no mip on this sheet)."""
    h, w = sheet.shape[:2]
    x = np.clip(su * w - 0.5, 0, w - 1)
    y = np.clip(sv * h - 0.5, 0, h - 1)
    x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    return ((sheet[y0, x0] * (1 - fx) + sheet[y0, x1] * fx) * (1 - fy)
            + (sheet[y1, x0] * (1 - fx) + sheet[y1, x1] * fx) * fy)


def _blend(out: np.ndarray, vis: np.ndarray, px: np.ndarray) -> None:
    a = px[:, 3:4] / 255.0
    view = out[vis]
    view[:, :3] = px[:, :3] * a + view[:, :3] * (1 - a)
    view[:, 3:4] = np.maximum(view[:, 3:4], px[:, 3:4])
    out[vis] = view


def _resize_nearest(a: np.ndarray, shape) -> np.ndarray:
    """Point-sample the 1024^2 atlas up or down to the working texture's size."""
    H, W = shape
    if a.shape[0] == H and a.shape[1] == W:
        return a
    yi = (np.arange(H) * (a.shape[0] / float(H))).astype(np.int32).clip(0, a.shape[0] - 1)
    xi = (np.arange(W) * (a.shape[1] / float(W))).astype(np.int32).clip(0, a.shape[1] - 1)
    return a[yi][:, xi]


# ── layer 1: logos ────────────────────────────────────────────────────────────────────────────

def logo_layer(out: np.ndarray, sheet: np.ndarray, site: dict, cov: np.ndarray) -> bool:
    """sheet.xy = (ku*u + kuv*v + bu, kvu*u + kv*v + bv), masked by `window`. Returns whether
    anything drew."""
    H, W = out.shape[:2]
    u = ((np.arange(W) + 0.5) / W)[None, :]
    v = ((np.arange(H) + 0.5) / H)[:, None]
    su, sv = _site_uv(site, u, v)
    su = np.broadcast_to(su, (H, W))
    sv = np.broadcast_to(sv, (H, W))
    m = site["window"]
    vis = cov & (su > m[0]) & (sv > m[1]) & (m[2] >= su) & (m[3] >= sv)
    if not vis.any():
        return False
    px = _sample(sheet, su[vis], sv[vis])
    if not (px[:, 3] > 8).any():
        return False
    _blend(out, vis, px)
    return True


# ── layer 2: decals ───────────────────────────────────────────────────────────────────────────

def glyph_constants(sheet: np.ndarray, cell, advance_px: float | None, fit: str = "cell"):
    """(S, M) for one source cell -- the game's own per-slot constants, derived from the art.

    With `advance_px` the cell is treated as a monospaced glyph: the slot spans one advance and
    the ink is centred in it, which is the rule the captured digit constants follow exactly.

    Without one, `fit` picks the rule:

      "cell" (default, and what the GAME does) -- the whole cell is mapped across the slot:
          S = (cellW/SW, cellH/SH, cellX0/SW, cellY0/SH),  M = the cell rect
      Read byte-exact off three independent captured sites: the NHL shield, the manufacturer
      wordmark and the helmet team mark. The mark's size on the garment therefore comes from the
      slot quad in the mesh, not from how much ink the cell happens to hold.

      "ink" -- fit the slot to the ink bbox instead. Only for sites whose source cell is supplied
      by the launcher rather than measured off the game's own atlas, where a generously padded
      cell would otherwise blow the art up to fill the quad.
    """
    SH, SW = sheet.shape[:2]
    cx0, cy0, cx1, cy1 = (int(round(t)) for t in cell)
    cx0, cy0 = max(0, cx0), max(0, cy0)
    cx1, cy1 = min(SW, cx1), min(SH, cy1)
    if cx1 <= cx0 or cy1 <= cy0:
        return None
    a = sheet[cy0:cy1, cx0:cx1, 3]
    ys, xs = np.nonzero(a > 8)
    if not len(xs):
        return None
    ix0, ix1 = cx0 + int(xs.min()), cx0 + int(xs.max()) + 1
    if advance_px:
        A = float(advance_px)
        centre = (ix0 + ix1) / 2.0
        S = (A / SW, (cy1 - cy0) / float(SH), (centre - A / 2.0) / SW, cy0 / float(SH))
        return S, (ix0 / float(SW), cy0 / float(SH), ix1 / float(SW), cy1 / float(SH))
    if fit == "ink":
        iy0, iy1 = cy0 + int(ys.min()), cy0 + int(ys.max()) + 1
        S = ((ix1 - ix0) / float(SW), (iy1 - iy0) / float(SH), ix0 / float(SW), iy0 / float(SH))
        return S, (ix0 / float(SW), iy0 / float(SH), ix1 / float(SW), iy1 / float(SH))
    S = ((cx1 - cx0) / float(SW), (cy1 - cy0) / float(SH), cx0 / float(SW), cy0 / float(SH))
    return S, (cx0 / float(SW), cy0 / float(SH), cx1 / float(SW), cy1 / float(SH))


def decal_layer(out: np.ndarray, sheet: np.ndarray, duv: np.ndarray, slot: np.ndarray,
                cov: np.ndarray, slot_id: int, S, M) -> bool:
    """sheet.xy = decalUV * S.xy + S.zw, masked by M, on the texels carrying `slot_id`."""
    su = duv[..., 0] * S[0] + S[2]
    sv = duv[..., 1] * S[1] + S[3]
    vis = cov & (slot == slot_id) & (su > M[0]) & (sv > M[1]) & (M[2] >= su) & (M[3] >= sv)
    if not vis.any():
        return False
    px = _sample(sheet, su[vis], sv[vis])
    if not (px[:, 3] > 8).any():
        return False
    _blend(out, vis, px)
    return True


def piece(name: str, shape):
    """(decalUV, slot, coverage) for one garment piece, at the working texture's resolution."""
    A = atlas()
    if name + "_cov" not in A:
        return None
    duv = _resize_nearest(A[name + "_uv"], shape).astype(np.float32)
    return duv, _resize_nearest(A[name + "_slot"], shape), _resize_nearest(A[name + "_cov"], shape)


# ── per-FRAGMENT evaluation ───────────────────────────────────────────────────────────────────
#
# Everything above bakes the two layers into a texture in base-UV space. That is right for the
# files the game is given -- the game does its own stamping and never sees a baked copy -- but it
# is wrong for the 3-D preview, because a stamp's resolution is then capped by how much of the
# base map its quad happens to own. The collar badge owns a 94x10 sliver of the 1024^2 atlas and
# 47x5 of the 512^2 render texture, so it arrives as a smear where the game draws a crisp shield.
#
# The game has no such cap: it evaluates the shader per FRAGMENT, at screen resolution. So does
# this. `decalUV` is smooth inside a slot quad, so resampling it out of the baked 1024^2 field
# reconstructs it essentially exactly; it is the SHEET fetch that must not be pre-baked, and here
# it happens at the fragment, straight off the 2048x512 art.

_BBOX: dict[str, tuple] = {}


def piece_bbox(name: str):
    """The piece's UV bounding box, so a fragment pass can skip the fragments it cannot touch.

    A garment piece owns a few percent of the map; testing a fragment against four floats first
    is what keeps per-fragment evaluation affordable on a preview that redraws while you drag.
    """
    if name not in _BBOX:
        A = atlas()
        C = A.get(name + "_cov")
        if C is None:
            _BBOX[name] = None
        else:
            ys, xs = np.nonzero(C)
            h, w = C.shape
            _BBOX[name] = (None if not len(xs) else
                           (xs.min() / w, ys.min() / h, (xs.max() + 1) / w, (ys.max() + 1) / h))
    return _BBOX[name]


def sample_piece(name: str, uv: np.ndarray):
    """(decalUV, slot, coverage) sampled at arbitrary UV points rather than on a texel grid.

    `decalUV` is interpolated bilinearly among only those neighbours that share the centre
    texel's slot: it is smooth within a quad and jumps between quads, so an unguarded tap on a
    slot boundary would bleed one digit's coordinates into the next.
    """
    A = atlas()
    if name + "_cov" not in A:
        return None
    D, SL, C = A[name + "_uv"].astype(np.float32), A[name + "_slot"], A[name + "_cov"]
    h, w = C.shape
    x = np.clip(uv[:, 0] * w - 0.5, 0, w - 1)
    y = np.clip(uv[:, 1] * h - 0.5, 0, h - 1)
    x0, y0 = np.floor(x).astype(np.int32), np.floor(y).astype(np.int32)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    xi, yi = np.rint(x).astype(np.int32).clip(0, w - 1), np.rint(y).astype(np.int32).clip(0, h - 1)
    slot, cov = SL[yi, xi], C[yi, xi]

    acc = np.zeros((len(x), 2), np.float32)
    wsum = np.zeros((len(x), 1), np.float32)
    for yy, xx, ww in ((y0, x0, (1 - fx) * (1 - fy)), (y0, x1, fx * (1 - fy)),
                       (y1, x0, (1 - fx) * fy), (y1, x1, fx * fy)):
        ok = ((SL[yy, xx] == slot) & C[yy, xx])[:, None] * ww
        acc += D[yy, xx] * ok
        wsum += ok
    duv = np.where(wsum > 1e-6, acc / np.maximum(wsum, 1e-6), D[yi, xi])
    return duv, slot, cov


def _blend01(dst: np.ndarray, vis: np.ndarray, px: np.ndarray) -> None:
    """Alpha-over one sheet fetch into a 0..1 RGB fragment buffer."""
    a = (px[:, 3:4] / 255.0).astype(np.float32)
    dst[vis] = px[:, :3] * (a / 255.0) + dst[vis] * (1.0 - a)


def logo_points(dst: np.ndarray, sheet: np.ndarray, site: dict, uv: np.ndarray,
                cov: np.ndarray) -> np.ndarray | None:
    """`logo_layer` at fragments. Returns the visibility mask, or None if nothing drew."""
    su, sv = _site_uv(site, uv[:, 0], uv[:, 1])
    m = site["window"]
    vis = cov & (su > m[0]) & (sv > m[1]) & (m[2] >= su) & (m[3] >= sv)
    if not vis.any():
        return None
    px = _sample(sheet, su[vis], sv[vis])
    _blend01(dst, vis, px)
    return vis


def decal_points(dst: np.ndarray, sheet: np.ndarray, duv: np.ndarray, slot: np.ndarray,
                 cov: np.ndarray, slot_id: int, S, M) -> np.ndarray | None:
    """`decal_layer` at fragments. Returns the visibility mask, or None if nothing drew."""
    su = duv[:, 0] * S[0] + S[2]
    sv = duv[:, 1] * S[1] + S[3]
    vis = cov & (slot == slot_id) & (su > M[0]) & (sv > M[1]) & (M[2] >= su) & (M[3] >= sv)
    if not vis.any():
        return None
    px = _sample(sheet, su[vis], sv[vis])
    _blend01(dst, vis, px)
    return vis


def sample_at(sheet: np.ndarray, uv: np.ndarray, site: dict = None, S=None, M=None):
    """The raw sheet fetch a logo/decal draw makes at the given fragments -- used by the relief
    pass, which needs the NORMAL sheet's value at exactly the texels the colour draw covered."""
    if site is not None:
        return _sample(sheet, *_site_uv(site, uv[:, 0], uv[:, 1]))
    return _sample(sheet, uv[:, 0] * S[0] + S[2], uv[:, 1] * S[1] + S[3])
