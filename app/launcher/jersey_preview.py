"""jersey_preview.py -- draw a kit on the actual player model.

The sheets a jersey editor shows are unreadable as clothing: the base is a garment atlas and the
stamps sheet is a decal LIBRARY whose layout has nothing to do with where anything lands. So this
module renders the kit the way the game assembles it, on the game's own mesh.

Two things make that possible without a GPU:

* **The mesh is the game's.** launcher/data/player_mesh.npz is baked (tools/build_player_mesh.py)
  from a RenderDoc post-VS capture of a real player draw, so the UVs ARE the shipped UVs.
* **Stamps are placed by the game's own shader math, not by a destination rectangle.** See
  `stamp_shader.py`: the jersey pixel shader samples the stamps sheet twice, once by an affine
  function of the base UV (crest and the patch family) and once through a per-vertex decal channel
  baked into the mesh (numbers and letters). Both were reproduced pixel-exactly against the game's
  own render target. Nothing is stretched to fill a rect, which is what used to make the numbers
  read far larger than they do in game.

Drawn by the shader path: crest, captaincy letter, back number, sleeve numbers (both arms) and
both front-number styles (small = upper-left chest, big = centre chest; the ROS uniform +0x18 enum
picks between them and they land nowhere near each other). The back NAME is a genuinely different
mechanism -- a pre-rendered name strip on its own textures -- and stays on the older measured-rect
path; `stamp_shader.unshaded()` lists it along with the sites whose shader constants are still
undecoded (shoulder patch, sponsor, helmet).

Rendering itself reuses the arena tab's software rasterizer (arena_preview.raster/shade): same
G-buffer, same bilinear sampler, same z-buffer.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

from . import arena_preview as AP
from . import stamp_shader as SS
from . import uniform_colors as UC

MESH_NAME = "player_mesh.npz"
PLACEMENT_NAME = "stamp_placement.json"

# Sheet ids as baked by tools/build_player_mesh.py.
SHEET_FLAT, SHEET_BASE, SHEET_HELMET = 0, 1, 2

# Flat parts are gear and skin, which no jersey edit touches. They are still drawn -- a floating
# jersey is much harder to judge than a player wearing one -- but in neutral greys so nothing
# reads as kit colour.
FLAT_GREY = 0.34

# Gear the palette paints that is a single flat colour. The bake binds no texture to these parts,
# so one slot each. Helmet trim is not measured and stays grey rather than guess.
PART_SLOT = {
    "helmet_shell": "helmet_shell",
}

# The gloves are drawn from the game's own colour ZONES, not from a sheet and not from a guess.
#
# The glove draw binds no albedo at all: the two 512s are normal maps and the 128 is an occlusion
# pass, so nothing in that set carries colour. The colour is a 32x32 palette texture the game
# rebuilds per team (capture id 16698) whose 16 filled COLUMNS are 16 flat colours, and every
# glove vertex carries a second UV whose u lands on one column. `zone` in the mesh bake is that
# index, straight off the capture — so a glove here is the same 15 regions the game draws, in the
# same places, and each one takes the palette slot with its number (see UC.ELEMENT_SLOTS).
#
# Zone 12 exists in the palette but no triangle uses it.
_ZONE_MAX = 16

# Material ids for the recoloured parts. Well clear of the sheet ids so nothing collides; one
# block per part, wide enough for every zone.
_PART_MAT0 = 64
_PART_MAT_STRIDE = 32

# The rasterizer has no mipmaps, so a 1024^2 sheet minified onto a ~350px body shatters thin
# stripes into slashes. Prefiltering the sheet with a box filter is exactly what a mip level is,
# and one fixed level is enough because the framing is fixed too.
TEX_MAX = 512

# The capture is in the game's eye space, where the camera looks down -Z, while the rasterizer
# treats a smaller Z as nearer. Negating Z on load reconciles the two: yaw 0 then puts the chest
# toward the viewer and leaves screen X un-mirrored (turning the camera around instead would show
# the crest, but as a mirror image).
FLIP_Z = True

_mesh = None
_placement = None


def _data_dir() -> Path:
    try:
        from . import resources as R
        return R.data_path(PLACEMENT_NAME).parent
    except Exception:
        return Path(__file__).resolve().parent / "data"


def available() -> bool:
    return (_data_dir() / MESH_NAME).exists()


def load_mesh() -> dict | None:
    global _mesh
    if _mesh is None:
        p = _data_dir() / MESH_NAME
        if not p.exists():
            return None
        with np.load(p, allow_pickle=False) as z:
            _mesh = {k: z[k] for k in z.files}
        if FLIP_Z:
            _mesh["pos"] = _mesh["pos"].copy()
            _mesh["nrm"] = _mesh["nrm"].copy()
            _mesh["pos"][:, 2] *= -1.0
            _mesh["nrm"][:, 2] *= -1.0
    return _mesh


def placement() -> dict:
    global _placement
    if _placement is None:
        _placement = json.loads((_data_dir() / PLACEMENT_NAME).read_text(encoding="utf-8"))
    return _placement


def unplaced() -> list[str]:
    """Stamps the shader path cannot draw, with the reason -- see stamp_shader.unshaded()."""
    return SS.unshaded()


def estimated() -> list[str]:
    """Decals that ARE drawn, but at an estimate rather than a measurement."""
    return [k for k, d in placement().get("decals", {}).items()
            if isinstance(d, dict) and d.get("confidence") == "estimated"]


# ── compositing the kit into one UV-space texture ─────────────────────────────────────────────

def _key_recolour(cell: np.ndarray, fill, mid, outer) -> np.ndarray:
    """Glyph art -> the kit's own colours. R/G/B are three DISJOINT layer masks, weighted.

    Measured on the shipped San Jose sheets: in the '0' digit cell R=2776, G=3230, B=8684 px and
    A=14008, so the three channels sum to the alpha instead of nesting inside it, and none is a
    subset of another. Walking a scanline across the digit reads

        R . . . B B B B . . R . [hole] . R . . B B B (G) R

    — B is the body of the stroke, R hugs the alpha edge on both the outside and the hole, and G
    sits between B and R. So each channel is one layer of the glyph, and the trio maps straight
    onto the palette triplet the game gives every glyph decal:

        B  fill    (slot 50 / 53 / 56 / 59)
        G  mid     (slot 49 / 52 / 55 / 58)
        R  outer   (slot 48 / 51 / 54 / 57)

    Stock San Jose corroborates it: those triplets read f5f5f5 / ce7b1a / 141414 — the white,
    orange and black on the real sweater. Letters carry no G at all (G=0 across the whole sheet),
    which is simply a two-layer nameplate; the mid slot then goes unused.

    The classification was already right. What made the preview's lettering coarse next to the
    game's was doing it with `argmax`: every edge pixel is a BLEND of two layers ([131,0,123] at a
    fill/outline boundary, [0,170,102] at a mid/fill one), and argmax snaps each to whichever layer
    happens to lead, throwing away all of the artist's antialiasing and leaving stair-stepped
    outlines. A trailing "near-grey pixels keep their raw sheet colour" clause then punched the
    sheet's own colours back through wherever the three channels landed close together, which is
    exactly the ambiguous edge pixels.

    Weighting instead of choosing keeps those blends: a pixel half R and half B comes out half
    outer and half fill, which is what the edge looked like before it was quantised.
    """
    w = cell[:, :, :3].astype(np.float32)
    tot = w.sum(axis=2, keepdims=True)
    w = w / np.where(tot > 1e-3, tot, 1.0)
    cols = np.stack([np.asarray(c, np.float32) for c in (outer, mid, fill)])   # R, G, B order
    out = w @ cols
    return np.concatenate([out, cell[:, :, 3:4]], axis=2)


def _blit(dst: np.ndarray, cell: np.ndarray, rect) -> None:
    x0, y0, x1, y1 = rect
    w, h = max(1, int(round(x1 - x0))), max(1, int(round(y1 - y0)))
    ix, iy = int(round(x0)), int(round(y0))
    im = Image.fromarray(np.clip(cell, 0, 255).astype(np.uint8), "RGBA").resize(
        (w, h), Image.LANCZOS)
    src = np.asarray(im, np.float32)
    H, W = dst.shape[:2]
    sx0, sy0 = max(0, -ix), max(0, -iy)
    ex, ey = min(w, W - ix), min(h, H - iy)
    if ex <= sx0 or ey <= sy0:
        return
    src = src[sy0:ey, sx0:ex]
    view = dst[iy + sy0:iy + ey, ix + sx0:ix + ex]
    a = src[:, :, 3:4] / 255.0
    view[:, :, :3] = src[:, :, :3] * a + view[:, :, :3] * (1 - a)
    view[:, :, 3:4] = np.maximum(view[:, :, 3:4], src[:, :, 3:4])


# ── back name / number ────────────────────────────────────────────────────────────────────────

# Letters sheet: 31 cells of 128x256 across 3968px (see jersey_convert_profile.json "letters").
# A-Z start at cell 4; the two punctuation cells sit at the front.
_LETTER_CELL = (128, 256)
_STAMP_CELL = (128, 256)


def _letter_slot(ch: str) -> int | None:
    if "A" <= ch <= "Z":
        return 4 + ord(ch) - ord("A")
    return {"'": 0, ".": 1}.get(ch)


def _digit_cell(d: int) -> tuple[int, int, int, int]:
    """Digit cells run in two rows: row 0 holds 0 2 4 6 8, row 1 holds 1 3 5 7 9.

    This is the NOMINAL grid the converter paints into. The game masks each digit to a tighter
    per-kit ink rect instead -- see SS.kit_metrics -- so this is only the fallback for when the
    kit's own table cannot be read.
    """
    w, h = _STAMP_CELL
    return 1024 + (d // 2) * w, (d % 2) * h, 1024 + (d // 2 + 1) * w, (d % 2 + 1) * h


def _glyph_strip(sheet: np.ndarray, cells, key, gap_frac=0.10) -> np.ndarray | None:
    """Lay out nameplate letters side by side.

    Only the back NAME comes through here now -- every other stamp is placed by the shader path,
    which needs no strip because it samples one glyph per decal slot.

    The nameplate font is proportional and the game's own metrics live in an ASCII-indexed u16
    table in the uniform's DRAM (~0x8b2) that we cannot yet read as widths, so each glyph is
    trimmed to its ink and separated by a constant gap. That is what a nameplate looks like; it is
    not claimed to be the game's exact metrics.
    """
    cw, ch = _LETTER_CELL
    inks, gap = [], int(round(gap_frac * ch))
    for c in cells:
        if c is None:                                   # space
            inks.append(None)
            continue
        x0, y0, x1, y1 = c
        cell = sheet[y0:y1, x0:x1]
        if not cell.size:
            return None
        cell = _key_recolour(cell, *key) if key else cell
        ys, xs = np.nonzero(cell[:, :, 3] > 8)
        if not len(xs):
            inks.append(None)
            continue
        inks.append((cell, int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1))
    solid = [i for i in inks if i]
    if not solid:
        return None
    # One shared baseline: every glyph keeps its height relative to the tallest, so 'O' still
    # overshoots 'H' exactly as the artist drew it.
    top = min(i[3] for i in solid)
    bot = max(i[4] for i in solid)

    width = sum((i[2] - i[1]) if i else int(0.42 * cw) for i in inks) + gap * (len(inks) - 1)
    out = np.zeros((bot - top, max(1, width), 4), np.float32)
    x = 0
    for i in inks:
        if i is None:
            x += int(0.42 * cw) + gap
            continue
        cell, ix0, ix1, _, _ = i
        out[:, x:x + ix1 - ix0] = cell[top:bot, ix0:ix1]
        x += ix1 - ix0 + gap

    # Trim the assembled run back to its own ink, so the destination rect -- which was measured
    # from the game's rendered ink, not from its advance boxes -- still lines up.
    xs = np.nonzero((out[:, :, 3] > 8).any(0))[0]
    return out[:, xs.min():xs.max() + 1] if len(xs) else None


def _fit(rect, measured_glyphs: int, n: int) -> tuple[float, float, float, float]:
    """Destination rect for `n` glyphs, from a rect measured with `measured_glyphs` of them.

    Nameplate only -- everything else goes through the shader path and has no rect. Height is
    fixed and width scales with the glyph count about the rect's centre, which is what a centred
    layout does. The strip is then stretched to fill, which is an approximation; the nameplate's
    real placement is the undecoded switch/case affine on the pre-rendered name strip.
    """
    x0, y0, x1, y1 = rect
    cx = (x0 + x1) / 2.0
    w = (x1 - x0) * (float(n) / max(1, measured_glyphs))
    return cx - w / 2, y0, cx + w / 2, y1


# Which palette triplet paints which decal. The name, the number, the sleeve number and the helmet
# number each own three consecutive slots (fill / mid / outer) — measured in-game, see
# uniform_colors.GLYPH_TRIPLETS. Front and back numbers SHARE 51/52/53, so they cannot be given
# different colours; that is the game's own limit, not the preview's.
DECAL_GLYPH = {
    "back_name": "name",
    "letter": "name",                  # UNVERIFIED — no probe pass isolated the captaincy patch
    "back_number": "number",
    "front_number_small": "number",
    "front_number_big": "number",
    "sleeve_number": "sleeve_number",
    "sleeve_number_right": "sleeve_number",
    "helmet_number": "helmet_number",  # slots 57-59, and the only thing that reads them
}


def _rgb(hx) -> np.ndarray:
    h = str(hx).lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32)


def _decal_key(decal: str, palette, fallback):
    """(fill, mid, outer) for one decal — from the uniform's palette when we have one."""
    glyph = DECAL_GLYPH.get(decal)
    if palette and glyph:
        try:
            return [_rgb(palette[s]) for s in UC.GLYPH_TRIPLETS[glyph]]
        except (IndexError, KeyError, ValueError):
            pass
    return fallback


def program(stamps: Image.Image, letter: str | None = None,
            colors=("#FFFFFF", "#C6A96A", "#101010"), number: str | None = None,
            front_style: str = "small", palette=None, keyed: bool = True,
            metrics: dict | None = None) -> dict:
    """The stamp draws for one kit, resolved but not yet executed.

    Both layers reduce to `(piece, sheet, constants)`, and nothing in that triple depends on where
    the result is written. Splitting it out is what lets `composite` bake the draws into a texture
    while the 3-D preview runs the SAME draws per fragment -- see `stamp_shader`'s fragment path
    for why the baked copy is not good enough on screen.

    -> {"logo": [(piece, sheet, site)], "decal": [(piece, sheet, slot_id, S, M)]}
    """
    pl = placement()
    cfg = SS.config()
    sheet = np.asarray(stamps.convert("RGBA"), np.float32)
    sw, sh = pl["sheet_size"]
    if sheet.shape[1] != sw or sheet.shape[0] != sh:       # tolerate a resized sheet
        sheet = np.asarray(stamps.convert("RGBA").resize((sw, sh), Image.LANCZOS), np.float32)
    key = [_rgb(c) for c in colors]
    tinted: dict = {}

    def src_for(site_name: str, site: dict) -> np.ndarray:
        """The sheet this site samples -- recoloured to the kit's palette when the site is keyed.

        The recolour is done once over the WHOLE sheet rather than per cell, because the shader
        samples the sheet directly; caching by triplet keeps that to one pass per distinct colour
        set even though several sites share a triplet.
        """
        if not (keyed and site.get("keyed")):
            return sheet
        trip = _decal_key(site_name, palette, key)
        k = tuple(tuple(np.asarray(c, np.float32).round(3).tolist()) for c in trip)
        if k not in tinted:
            tinted[k] = _key_recolour(sheet, *trip)
        return tinted[k]

    logo = [(site["piece"], src_for(nm, site), site)
            for nm, site in cfg.get("logo_sites", {}).items()]

    # A slot holds exactly one glyph, so a two-digit number fills two slots. `single_digit`
    # decides which slot a one-digit number takes -- it cannot be centred between them.
    digits = [int(c) for c in str(number or "") if c.isdigit()][:2]
    decal = []
    for nm, site in cfg.get("decal_sites", {}).items():
        if site.get("sheet", "stamps") != "stamps":     # the helmet has its own sheet and UV space
            continue
        slots = list(site["slots"])
        role = site.get("role")
        glyphs: list[int] = []
        if role in ("number", "sleeve_number"):
            if not digits:
                continue
            if nm.startswith("front_number_") and nm != "front_number_" + (front_style or ""):
                continue
            cells = [_digit_cell(d) for d in digits]
            glyphs = list(digits)
            if len(cells) < len(slots):
                slots = slots[-len(cells):] if cfg.get("single_digit") == "right" \
                    else slots[:len(cells)]
        elif role == "letter":
            d = pl["decals"].get("letter")
            if not letter or not d or "src_cell" not in d:
                continue
            x0, y0, x1, y1 = d["src_cell"]
            if letter.upper() == "C":      # the 'C' cell is the neighbouring 128px column
                x0, x1 = x1, x1 + (x1 - x0)
            cells = [(x0, y0, x1, y1)]
        elif "cell" in site:
            # A logo drawn through the decal path -- the NHL shield, the manufacturer wordmark.
            # Every slot listed pulls the SAME cell; mirroring lives in the mesh's decal UVs.
            cells = [tuple(site["cell"])] * len(slots)
        else:                              # a logo in a decal slot, with no known source cell
            continue

        src = src_for(nm, site)
        adv = site.get("advance_px") if site.get("glyph") == "digit" else None
        fit = site.get("fit", "cell")
        # The kit's own table wins over the site's captured advance whenever we could read it.
        use_kit = bool(metrics) and site.get("glyph") == "digit" and len(glyphs) == len(cells)
        for i, (slot_id, cell) in enumerate(zip(slots, cells)):
            sm = (SS.digit_constants(metrics, glyphs[i], (sw, sh)) if use_kit
                  else SS.glyph_constants(src, cell, adv, fit))
            if sm:
                decal.append((site["piece"], src, slot_id, sm[0], sm[1]))
    return {"logo": logo, "decal": decal}


def composite(base: Image.Image, stamps: Image.Image | None, letter: str | None = None,
              colors=("#FFFFFF", "#C6A96A", "#101010"), name: str | None = None,
              number: str | None = None, letters: Image.Image | None = None,
              front_style: str = "small", palette=None, keyed: bool = True,
              metrics: dict | None = None, stamped: bool = True) -> Image.Image:
    """base + the decals the game stamps onto it, in the base's own UV space.

    This is what the body is textured with. `letter` draws a captaincy patch ('C'/'A'); the two
    cells land in the same destination, which is why one rect serves both.

    `palette` is the 60-entry slot list from `uniform_colors.get_colors` for the kit's own row.
    Given one, each glyph decal takes its own measured triplet, so the nameplate and the numbers
    can differ exactly as they do on the real jersey. Without one, `colors` paints them all.

    `keyed=False` copies every cell verbatim, skipping the R/G/B-are-layer-masks recolour. That is
    for running this same placement machinery over the NORMAL sheets: their channels are XYZ, not
    layer masks, and recolouring one would turn a normal into a colour.

    `metrics` is `SS.kit_metrics(uniform_<team>_<kit>.iff)` -- the KIT's own digit advance and ink
    rects. The number sites' `advance_px` in the config is a single captured kit's value and is
    wrong for every other kit; given metrics, the digits are placed from the kit's table instead,
    which is exactly what the game's constants do.

    `stamped=False` leaves both shader layers off and returns the base alone (the nameplate still
    draws -- it is a rect blit, not a shader layer). That is for the 3-D preview, which runs the
    two layers per fragment instead; see `fragment_stamps`.
    """
    out = np.asarray(base.convert("RGBA"), np.float32).copy()
    if stamps is None:
        return Image.fromarray(out.astype(np.uint8), "RGBA")
    pl = placement()
    shape = out.shape[:2]
    pieces: dict = {}

    def piece_for(nm: str):
        if nm not in pieces:
            pieces[nm] = SS.piece(nm, shape)
        return pieces[nm]

    if stamped:
        prog = program(stamps, letter, colors, number, front_style, palette, keyed, metrics)
        # ── layer 1: logos, straight off the base UV ──────────────────────────────────────────
        for pname, src, site in prog["logo"]:
            p = piece_for(pname)
            if p is not None:
                SS.logo_layer(out, src, site, p[2])
        # ── layer 2: decals, through the mesh's own decal quads ───────────────────────────────
        for pname, src, slot_id, S, M in prog["decal"]:
            p = piece_for(pname)
            if p is not None:
                SS.decal_layer(out, src, p[0], p[1], p[2], slot_id, S, M)

    # ── the back nameplate: a third mechanism, still on its measured rect ─────────────────────
    # Draw 1055 binds a pre-rendered 1024x256 name strip placed by its own affine, so neither
    # stamp layer applies. The shirt is unwrapped across the shoulder seam, which is why the
    # back-panel art has to be turned 180 degrees to read the right way round.
    d = pl["decals"].get("back_name")
    if name and letters is not None and d and "dst_cell" in d:
        uw, uh = pl["uv_size"]
        fx, fy = out.shape[1] / uw, out.shape[0] / uh
        cw, ch = _LETTER_CELL
        cells = []
        for c in name.upper():
            s = _letter_slot(c)                    # unknown glyphs fall back to a space
            cells.append(None if s is None else (s * cw, 0, (s + 1) * cw, ch))
        strip = _glyph_strip(np.asarray(letters.convert("RGBA"), np.float32), cells,
                             _decal_key("back_name", palette, [_rgb(c) for c in colors])
                             if (keyed and d.get("keyed")) else None)
        if strip is not None:
            if d.get("flip180"):
                strip = strip[::-1, ::-1]
            r = _fit(d["dst_cell"], int(d.get("glyphs", len(cells))), len(cells))
            _blit(out, strip, (r[0] * fx, r[1] * fy, r[2] * fx, r[3] * fy))
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


HELMET_TEX = 512      # the game's helmet base is 256x256; render at 2x so the digits stay crisp


def composite_helmet(helmet: Image.Image | None, number: str | None = None,
                     shell=(0.5, 0.5, 0.5), palette=None, colors=None,
                     keyed: bool = True, size: int = HELMET_TEX):
    """The helmet skin: the shell colour, plus the decals the game stamps onto it.

    The helmet is its own material with its own sheet and its own UV space -- index 2 of
    uniform_<team>_<kit>.iff, 1024x256, holding a 256x256 team mark and a digit grid. It does NOT
    read the garment stamps sheet, which is why the number and the team mark never appeared on the
    body composite. Placement is the same decal algebra as everywhere else, over the `helmet` piece
    baked from the game's own helmet draw.

    `shell` is the base colour the decals sit on (the palette's helmet slot, when there is one).
    Returns an HxWx3 uint8 array, or None if there is nothing to draw.
    """
    if helmet is None:
        return None
    p = SS.piece("helmet", (size, size))
    if p is None:
        return None
    cfg = SS.config()
    sh = cfg.get("sheets", {}).get("helmet", {})
    sw, shh = sh.get("size", [1024, 256])
    sheet = helmet.convert("RGBA")
    if sheet.size != (sw, shh):
        sheet = sheet.resize((sw, shh), Image.LANCZOS)
    sheet = np.asarray(sheet, np.float32)

    out = np.empty((size, size, 4), np.float32)
    out[..., :3] = np.asarray(shell, np.float32) * 255.0
    out[..., 3] = 255.0

    ox, oy = sh.get("digit_origin", [256, 0])
    cw, ch = sh.get("digit_cell", [64, 128])
    digits = [int(c) for c in str(number or "") if c.isdigit()][:2]
    key = [_rgb(c) for c in (colors or ("#FFFFFF", "#C6A96A", "#101010"))]
    tinted: dict = {}

    duv, slotmap, cov = p
    for nm, site in cfg.get("decal_sites", {}).items():
        if site.get("sheet") != "helmet":
            continue
        slots = list(site["slots"])
        if site.get("role") == "number":
            if not digits:
                continue
            # Same two-row grid as the garment sheet: evens on the top row, odds on the bottom.
            cells = [(ox + (d // 2) * cw, (d % 2) * ch,
                      ox + (d // 2 + 1) * cw, (d % 2 + 1) * ch) for d in digits]
            if len(cells) < len(slots):
                slots = slots[-len(cells):] if cfg.get("single_digit") == "right" \
                    else slots[:len(cells)]
        elif "cell" in site:
            cells = [tuple(site["cell"])] * len(slots)
        else:
            continue

        src = sheet
        if keyed and site.get("keyed"):
            trip = _decal_key(nm, palette, key)
            k = tuple(tuple(np.asarray(c, np.float32).round(3).tolist()) for c in trip)
            if k not in tinted:
                tinted[k] = _key_recolour(sheet, *trip)
            src = tinted[k]
        adv = site.get("advance_px") if site.get("glyph") == "digit" else None
        fit = site.get("fit", "cell")
        for slot_id, cell in zip(slots, cells):
            sm = SS.glyph_constants(src, cell, adv, fit)
            if sm:
                SS.decal_layer(out, src, duv, slotmap, cov, slot_id, *sm)
    return np.clip(out[..., :3], 0, 255).astype(np.uint8)


# ── normal maps ───────────────────────────────────────────────────────────────────────────────

# Where the light sits in TANGENT space when the normal maps are lit. Roughly over the viewer's
# left shoulder, matching the key light the vertex shading uses, so the relief and the body agree
# about which way is up. z is kept large: the relief is a detail pass, not the main light.
RELIEF_LIGHT = (-0.42, -0.42, 0.82)
RELIEF_GAIN = 1.35      # how far the stitching is allowed to darken/brighten a texel
RELIEF_LOD  = 1.2       # px - band limit on the normal sheet the per-fragment relief reads (there
                        #   is no mip chain on that fetch; see fragment_stamps)


def _with_alpha(normal: Image.Image, colour: Image.Image | None) -> Image.Image:
    """A decal normal sheet wearing its COLOUR sheet's alpha.

    A DXN normal decodes opaque, so blitting one through the placement path would stamp the cell's
    whole rectangle — flat surround included — over the base's own relief, wiping the wrinkles
    around every crest. Coverage belongs to the art, and the art's coverage is its colour alpha.
    """
    n = normal.convert("RGB")
    if colour is None:
        return n.convert("RGBA")
    a = colour.convert("RGBA").split()[3]
    if a.size != n.size:
        a = a.resize(n.size, Image.LANCZOS)
    n = n.convert("RGBA")
    n.putalpha(a)
    return n


def relief(atlas: Image.Image, gain: float = RELIEF_GAIN) -> np.ndarray:
    """A tangent-space normal atlas -> a per-texel brightness multiplier around 1.0.

    The rasterizer has no tangent frames, so this cannot be a true normal-mapped light. What it can
    do — and what the sewn look actually IS — is shade the relief against a FIXED tangent-space
    light and fold the result into the albedo. A flat texel (128,128,255) comes out exactly 1.0, so
    an unstitched jersey renders identically to before; only where the map bends does anything
    change. That is a detail-lighting approximation, not the game's shading, and it is the honest
    ceiling for a software rasterizer with no per-vertex tangents.
    """
    return relief_mul(np.asarray(atlas.convert("RGB"), np.float32), gain)


def relief_mul(rgb: np.ndarray, gain: float = RELIEF_GAIN) -> np.ndarray:
    """The same brightness multiplier, for a bare 0..255 RGB array of any shape."""
    n = rgb / 127.5 - 1.0
    nz = np.sqrt(np.clip(1.0 - n[..., 0] ** 2 - n[..., 1] ** 2, 0.0, 1.0))
    L = np.asarray(RELIEF_LIGHT, np.float32)
    L /= np.linalg.norm(L)
    lam = n[..., 0] * L[0] + n[..., 1] * L[1] + nz * L[2]
    # Normalised by what a FLAT texel would give, so "no relief" is exactly no change.
    return np.clip(1.0 + (lam / max(L[2], 1e-3) - 1.0) * gain, 0.25, 1.75)


def _prefilter(img: Image.Image) -> np.ndarray:
    """Sheet -> RGB texture, box-filtered down to TEX_MAX so minification doesn't alias."""
    w, h = img.size
    k = max(w, h) / float(TEX_MAX)
    if k > 1.0:
        img = img.resize((max(1, int(round(w / k))), max(1, int(round(h / k)))), Image.BOX)
    return np.ascontiguousarray(np.asarray(img.convert("RGB"), np.uint8))


def _normal_sheets(sheets: dict) -> tuple:
    """(stamp normals, letter normals) wearing their colour sheets' alpha, or (None, None)."""
    return (_with_alpha(sheets["normal"], sheets.get("stamps"))
            if sheets.get("normal") is not None else None,
            _with_alpha(sheets["letters_normal"], sheets.get("letters"))
            if sheets.get("letters_normal") is not None else None)


def _light_relief(body: Image.Image, sheets: dict, letter, name, number,
                  front_style, metrics=None, stamped: bool = True) -> Image.Image:
    """Multiply the kit's stitching relief into the finished body texture.

    The normal atlas is assembled by running `composite` a second time over the normal sheets, so
    the crest's relief lands exactly where the crest does and the nameplate's lands under the
    nameplate -- no second placement table, and no chance of the two drifting apart. `keyed=False`
    because a normal's channels are XYZ, not the R/G/B layer masks the glyph recolour assumes.

    `stamped=False` bakes only the BASE weave's relief. The 3-D preview uses that, because there
    the stamps -- and so their stitching -- are drawn per fragment instead.
    """
    n_base = sheets["base_normal"]
    if n_base.size != body.size:
        n_base = n_base.resize(body.size, Image.LANCZOS)
    n_stamp, n_letters = _normal_sheets(sheets)
    atlas = composite(n_base.convert("RGBA"), n_stamp,
                      letter, name=name, number=number, letters=n_letters,
                      front_style=front_style, keyed=False, metrics=metrics,
                      stamped=stamped)
    mul = relief(atlas)[..., None]
    out = np.asarray(body.convert("RGBA"), np.float32)
    out[..., :3] = np.clip(out[..., :3] * mul, 0, 255)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def fragment_stamps(colour: dict, normal: dict | None, material: int):
    """A per-FRAGMENT stamp pass for `arena_preview.shade`.

    The baked path writes the stamps into the base texture, so a stamp is only ever as sharp as
    the slice of the base map its quad owns. That slice is tiny for the small badges -- the
    collar's NHL shield gets 94x10 texels of the 1024^2 atlas and 47x5 of the 512^2 render
    texture, which is why it arrived as a smear rather than a shield. The game has no such step:
    it evaluates the same two layers per fragment, off the full 2048x512 art. So does this.

    `colour` and `normal` are `program()` outputs over the colour and normal sheets. The normal
    draw supplies the stitching relief for the stamp, which the baked atlas would otherwise have
    lost to exactly the same squeeze.

    Returns f(samp, mat, uv) mutating `samp` (0..1 RGB) in place for fragments of `material`.
    """
    # The normal program is the same code over a different sheet, so its draws line up one for one
    # with the colour's. Pairing them positionally needs no keys -- but a sheet whose alpha differs
    # could drop a draw, so a length mismatch just turns the relief off rather than mispairing it.
    nl = nd = None
    if normal and len(normal["logo"]) == len(colour["logo"]) \
            and len(normal["decal"]) == len(colour["decal"]):
        # The sheet fetch has no mip chain (see stamp_shader._sample), and a stamp's relief is the
        # one thing on these sheets with detail at the texel: the digit cells are filled with a
        # 3-texel diagonal twill. Minified, bilinear turned that into the moire of dots that showed
        # up across the back number -- with the game's OWN art, so a launcher fault, not a
        # conversion one. Relief is a detail-lighting term, so band-limiting the sheet it is read
        # from is the honest fix; the crest's border bead is 4-6 texels and survives it.
        blur = {}
        def _lp(a):
            k = id(a)
            if k not in blur:
                b = a.astype(np.float32).copy()
                b[..., :3] = gaussian_filter(b[..., :3], (RELIEF_LOD, RELIEF_LOD, 0))
                blur[k] = b.astype(a.dtype)
            return blur[k]
        nl = [_lp(d[1]) for d in normal["logo"]]
        nd = [_lp(d[1]) for d in normal["decal"]]

    # Grouped by piece: a piece owns a few percent of the map, so its draws can share one UV
    # bounding-box reject and one field fetch instead of touching every body fragment each time.
    by_piece: dict = {}
    for i, d in enumerate(colour["logo"]):
        by_piece.setdefault(d[0], []).append(("logo", i, d))
    for i, d in enumerate(colour["decal"]):
        by_piece.setdefault(d[0], []).append(("decal", i, d))

    def run(samp: np.ndarray, mat: np.ndarray, uv: np.ndarray) -> None:
        sel = np.nonzero(mat == material)[0]
        if not len(sel):
            return
        suv = np.mod(uv[sel].astype(np.float32), 1.0)      # shade() samples wrapped; match it
        for pname, draws in by_piece.items():
            bb = SS.piece_bbox(pname)
            if bb is None:
                continue
            sub = np.nonzero((suv[:, 0] >= bb[0]) & (suv[:, 0] < bb[2])
                             & (suv[:, 1] >= bb[1]) & (suv[:, 1] < bb[3]))[0]
            if not len(sub):
                continue
            puv = suv[sub]
            f = SS.sample_piece(pname, puv)
            if f is None or not f[2].any():
                continue
            dst = samp[sel[sub]]

            def relief(vis, nsrc, **kw):
                """Multiply the stamp's own stitching into the texels it just covered."""
                if nsrc is None or vis is None or not vis.any():
                    return
                npx = SS.sample_at(nsrc, puv[vis], **kw)
                lit = npx[:, 3] > 8
                if lit.any():
                    # Weighted by the SAME alpha the colour draw blends with. Multiplying at full
                    # strength wherever alpha merely cleared 8 lit the transparent surround of each
                    # cell as brightly as the glyph, which is what drew the ghost rectangle of
                    # stitching around the back number.
                    a = (npx[lit, 3:4] / 255.0)
                    dst[np.nonzero(vis)[0][lit]] *= 1.0 + (relief_mul(npx[lit, :3])[:, None] - 1.0) * a

            for kind, i, d in draws:
                if kind == "logo":
                    relief(SS.logo_points(dst, d[1], d[2], puv, f[2]), nl and nl[i], site=d[2])
                else:
                    relief(SS.decal_points(dst, d[1], f[0], f[1], f[2], d[2], d[3], d[4]),
                           nd and nd[i], S=d[3], M=d[4])
            samp[sel[sub]] = np.clip(dst, 0, 1)

    return run


# ── scene ─────────────────────────────────────────────────────────────────────────────────────

def build_scene(sheets: dict, letter: str | None = None, colors=None,
                name: str | None = None, number: str | None = None,
                front_style: str = "small", palette=None,
                normals: bool = True, metrics: dict | None = None) -> dict | None:
    """Scene for arena_preview.raster/shade from the editor's sheets.

    `sheets` is the tab's working set: 'base', 'stamps', 'letters', 'helmet'. Anything missing
    just leaves those parts flat. The 'helmet' sheet is the helmet's OWN 1024x256 decal sheet in
    its OWN UV space, so it is never part of this composite -- see `composite_helmet`. `name`/`number` draw the nameplate and the back, sleeve and
    front numbers. `front_style` picks which front-number placement the player wears -- 'small'
    (upper-left chest) or 'big' (centre chest); that is the ROS uniform +0x18 enum, and the two
    styles land in completely different places, so it is a real choice, not a size tweak.

    `palette` is the kit's own 60-slot colour list (`uniform_colors.get_colors`). It colours the
    glyph decals and the gear the palette owns -- see PART_SLOT.

    `metrics` is the target kit's own glyph table (`stamp_shader.kit_metrics`). Pass it and the
    numbers come out the size the game draws them; leave it out and they fall back to a single
    captured kit's advance, which is only right for that one kit.

    `normals` lights the kit's normal maps into the body texture when the working set carries them
    ('base_normal', 'normal', 'letters_normal'). They go through the SAME placement machinery as
    the colour, so a crest's stitching lands on the crest -- see `relief`.
    """
    m = load_mesh()
    if m is None:
        return None
    base, stamps = sheets.get("base"), sheets.get("stamps")
    tex = {}
    frag = None
    if base is not None:
        cols = colors or ("#FFFFFF", "#C6A96A", "#101010")
        # The stamps are NOT baked into the render texture. `_prefilter` caps it at TEX_MAX (512),
        # and a badge that owns a 94x10 sliver of the 1024^2 map owns 47x5 of that -- the collar
        # shield came out a smear. They are run per fragment instead, which is what the game does.
        # Without a baked decal atlas there is no fragment path, so the stamps stay in the texture.
        per_frag = stamps is not None and bool(SS.atlas_pieces())
        body = composite(base, stamps, letter, cols, name=name, number=number,
                         letters=sheets.get("letters"), front_style=front_style,
                         palette=palette, metrics=metrics, stamped=not per_frag)
        if normals and sheets.get("base_normal") is not None:
            body = _light_relief(body, sheets, letter, name, number, front_style, metrics,
                                 stamped=not per_frag)
        tex[SHEET_BASE] = _prefilter(body)
        if per_frag:
            n_stamp = _normal_sheets(sheets)[0] if normals else None
            frag = fragment_stamps(
                program(stamps, letter, cols, number, front_style, palette, True, metrics),
                program(n_stamp, letter, cols, number, front_style, palette, False, metrics)
                if n_stamp is not None else None,
                SHEET_BASE)

    # No light bake exists for the player (the arena's vertex colours come from the level file),
    # so shading is a two-light lambert off the captured normals: a key light over the viewer's
    # shoulder and a dim fill from below, which is enough to read folds and the shoulder curve
    # without inventing a look.
    n = m["nrm"].astype(np.float32)
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = n / np.where(ln > 1e-6, ln, 1.0)
    # -Z is toward the viewer once load_mesh has flipped the capture's eye space.
    key = np.array([0.35, 0.45, -0.82], np.float32)
    key /= np.linalg.norm(key)
    fill = np.array([-0.45, -0.5, -0.74], np.float32)
    fill /= np.linalg.norm(fill)
    lam = 0.62 + 0.42 * np.clip(n @ key, 0, 1) + 0.16 * np.clip(n @ fill, 0, 1)
    vcol = np.clip(lam, 0, 1.35)[:, None].repeat(3, 1).astype(np.float32)

    sheet = m["sheet"]
    flat = sheet == SHEET_FLAT
    # Flat parts get their grey through the texture path (a constant 1x1 texture) rather than by
    # dimming vertex colours, so their shading matches the textured parts exactly.
    tex[SHEET_FLAT] = np.full((1, 1, 3), int(FLAT_GREY * 255), np.uint8)
    part = m["part"].astype(np.int32)
    mat = sheet.astype(np.int32)

    # Gear the palette paints gets lifted off the shared grey onto its own 1x1 texture. Same path
    # as the grey, so it takes the same lighting; the only difference is the colour in the texel.
    # Names in the bake carry the capture's draw id ("glove_left_1238"), so match on the stem.
    names = [str(x) for x in m["names"]]

    uv = m["uv"].astype(np.float32)

    def solid(sel, pid, element):
        """Give the selected triangles their own material with a 1x1 palette-coloured texture."""
        hx = palette[UC.ELEMENT_SLOTS[element]]
        if UC.is_sentinel(hx) or not sel.any():   # sentinel = "unused, use the default"
            return
        mid = _PART_MAT0 + pid * _PART_MAT_STRIDE
        mat[sel] = mid
        tex[mid] = _rgb(hx).astype(np.uint8).reshape(1, 1, 3)

    if palette:
        tri = m["tri"]
        zone = m["zone"] if "zone" in m else None
        # A zone is per-vertex and its regions are contiguous, so a triangle's zone is its first
        # vertex's. Zones with no colour (sentinel) keep the default grey.
        tzone = zone[tri[:, 0]] if zone is not None else None
        for pid, nm in enumerate(names):
            stem = nm.rsplit("_", 1)[0]
            here = part == pid
            if stem in PART_SLOT:
                solid(here, pid, PART_SLOT[stem])
            elif tzone is not None and (here & (tzone >= 0)).any():
                base = _PART_MAT0 + pid * _PART_MAT_STRIDE
                for z in range(_ZONE_MAX):
                    sel = here & (tzone == z)
                    if z not in UC.SLOT_ELEMENT or not sel.any():
                        continue
                    hx = palette[z]                    # zone id IS the palette slot
                    if UC.is_sentinel(hx):
                        continue
                    mat[sel] = base + z
                    tex[base + z] = _rgb(hx).astype(np.uint8).reshape(1, 1, 3)

    # ── the helmet ───────────────────────────────────────────────────────────────────────────
    # Its own material, its own sheet, its own UV space -- so it is skinned separately and then
    # bound to the shell's triangles by the mesh's own UVs. This runs AFTER the palette pass on
    # purpose: PART_SLOT paints the shell one flat colour, and the skin is that colour plus the
    # decals, so it has to win.
    #
    # Skin and mesh are the same unwrap by construction, not by resemblance: the `helmet` piece of
    # decal_atlas.npz is baked from draw 1324 of jersey_model_capture, and player_mesh.npz's
    # helmet_shell was exported from that same draw. Their UV coverage is the identical set --
    # IoU 1.000 once the OBJ's V flip is undone -- so there is no fitting step here.
    #
    # It also lands where the game puts it. Logo slot 3 sits at x[+2.5,+7.0] and slot 4 at
    # x[-8.9,-1.9] on a shell spanning x[-10.5,+10.4]: a mirrored pair high on each side, each
    # about 22% of the shell's length, which is what the game's own side render measures.
    helm = sheets.get("helmet")
    if helm is not None:
        shell = (FLAT_GREY, FLAT_GREY, FLAT_GREY)
        if palette:
            hx = palette[UC.ELEMENT_SLOTS["helmet_shell"]]
            if not UC.is_sentinel(hx):
                shell = tuple(_rgb(hx) / 255.0)
        try:
            skin = composite_helmet(helm, number=number, shell=shell, palette=palette)
        except Exception:
            skin = None
        if skin is not None:
            sel = np.zeros(len(part), bool)
            for pid, nm in enumerate(names):
                if nm.rsplit("_", 1)[0] == "helmet_shell":
                    sel |= part == pid
            if sel.any():
                mat[sel] = SHEET_HELMET
                tex[SHEET_HELMET] = _prefilter(Image.fromarray(skin, "RGB"))
                flat = flat & ~sel

    pbox = {}
    for pid in np.unique(part):
        v = np.unique(m["tri"][part == pid])
        pbox[int(pid)] = (m["pos"][v].min(0), m["pos"][v].max(0))
    return dict(pos=m["pos"].astype(np.float32), uv=uv, vcol=vcol,
                tri=m["tri"].astype(np.int32), mat=mat, part=part,
                tex=tex, pbox=pbox, ref=[], flat_mask=flat, names=names,
                fragment_pass=frag)


def render(scene, W=520, H=680, yaw=0.0, pitch=0.0, zoom=1.0, pan=(0.0, 0.0), ss=2,
           exposure=1.0):
    """One frame, yaw 0 = facing the viewer. Framing is pinned to the mesh's own bbox so
    rotating doesn't make it swim."""
    if not scene:
        return None
    pos = scene["pos"]
    ctr = (pos.min(0) + pos.max(0)) / 2.0
    span = float(np.abs(pos - ctr).max()) * 2.25
    # `player_mesh.npz` is in the capture's CAMERA space, so the eye sits at the origin and the
    # distance to the figure is simply -ctr.z. Handing that to the rasterizer gives the preview
    # the game's real perspective instead of an orthographic one -- the near arm and the near
    # half of the torso diverge from the far side by the same amount they do on screen.
    g = AP.raster(scene, W, H, yaw=yaw, pitch=pitch, zoom=zoom, pan=pan, ss=ss,
                  ctr=ctr, span=span, pixel_depth=True, cam_dist=abs(float(ctr[2])) or None)
    return AP.shade(scene, g, exposure=exposure)
