"""nhl26_sources.py -- identify the textures in an NHL 26 jersey dump by CONTENT.

The NHL 23 detector in `jersey_convert.find_sources` reads a map's role off the last underscore
token (`_color` = albedo, `_coeff` = packed PBR, `_normal` = normal). That contract does not hold
for the NHL 26 dumps: the exporter names each file after whatever shader sampler it landed on, so
one role turns up under `_color`, `_coeff`, `_normal`, `_player_team_brt`, `_blueprint`, `_hq_asp`
and `_array` from folder to folder, and when a name is already taken it appends `_2` .. `_7`.
Measured across the whole set: 424 kit folders, 14,344 files, only 10,298 distinct textures --
about 28% are byte-identical copies filed under a second sampler name.

Consequence: in anaheim/adidas/eleven, `jersey_..._base_color.dds` is the NORMAL MAP and
`jersey_..._base_normal.dds` is the albedo. The suffix is noise; only the pixels are not.

Role is therefore decided from the DDS header plus a sample of the compressed blocks:

    BC5 / ATI2                -> normal map, always (two-channel tangent space, no blue lane)
    RGB mean ~= (128,128,255) -> normal map stored in DXT1/DXT5/BC7 (the "purple base")
    blue lane empty and flat  -> coefficient / packed mask
    desaturated and opaque    -> unused placeholder slot
    otherwise                 -> colour / albedo

Blocks are read rather than decoded: a 2048x2048 BC sheet costs about 4 ms this way against
roughly a second for a full Pillow decode, which matters across a 23 GB dump.
"""
from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path

import numpy as np

__all__ = ["stats", "classify", "scan", "resolve", "resolve_folder",
           "find_sources", "decal_roles", "league_mark", "ROLES"]

# Families the exporter emits, as the leading token before `_adidas_`.
_FAMILY = re.compile(
    r'^(branding_jersey|branding_pant|stamp_jersey|helmetlogo|jersey|pant|sock|font)'
    r'_adidas_(?P<rest>.*)\.dds$', re.I)

# DXGI format -> block layout we need to sample. Only the ones the dump actually uses.
_DXGI = {71: 'BC1', 72: 'BC1', 74: 'BC2', 77: 'BC3', 78: 'BC3',
         80: 'BC4', 83: 'BC5', 98: 'BC7', 99: 'BC7'}

_TWO_CHANNEL = ('ATI2', 'BC5U', 'A2XY', 'BC5', 'BC4')

ROLES = ("base_color", "base_normal", "base_coeff", "pant_color", "pant_normal",
         "sock_color", "sock_normal", "font_color", "font_normal", "helmet_color")

# Decal slot 0 is the primary crest wherever the exporter numbered its branding files. The
# remaining slots differ team by team, so they are reported by number, not by meaning.
CREST_SLOT = 0

# Every branding decal in the dump is 512x512. Anything larger under a branding name is a garment
# sheet the exporter aliased there, not a patch.
_DECAL_MAX_PX = 1024


# -- header + block sampling ---------------------------------------------------------------

def _parse(path):
    raw = Path(path).read_bytes()
    if raw[:4] != b'DDS ':
        raise ValueError("%s: not a DDS" % Path(path).name)
    h, w = struct.unpack_from('<II', raw, 12)
    fcc = raw[84:88].decode('latin1').rstrip('\x00')
    off = 128
    if fcc == 'DX10':                              # DDS_HEADER_DXT10 follows the base header
        fcc = _DXGI.get(struct.unpack_from('<I', raw, 128)[0], 'DX10')
        off = 148
    return raw, off, w, h, fcc


def stats(path, max_blocks=20000):
    """Header facts plus block-endpoint statistics, and the role they imply."""
    raw, off, w, h, fcc = _parse(path)
    if fcc in _TWO_CHANNEL:
        return dict(w=w, h=h, px=w * h, fcc=fcc, kind='normal', mR=0.0, mG=0.0, mB=0.0,
                    sdB=0.0, mA=255.0, aOpaque=1.0, aZero=0.0, sat=0.0, gray=0.0)

    bs = 8 if fcc in ('DXT1', 'BC1') else 16
    nblk = min(((w + 3) // 4) * ((h + 3) // 4), (len(raw) - off) // bs)
    step = max(1, nblk // max_blocks)
    idx = np.arange(0, max(nblk, 1), step, dtype=np.int64)
    base = off + idx * bs
    b = np.frombuffer(raw, dtype=np.uint8)
    coff = 0 if bs == 8 else 8                     # DXT5 puts the colour block second

    def u16(o):
        return b[base + o].astype(np.uint32) | (b[base + o + 1].astype(np.uint32) << 8)

    def rgb565(c):
        return (((c >> 11) & 31) * (255 / 31), ((c >> 5) & 63) * (255 / 63), (c & 31) * (255 / 31))

    r0, g0, b0 = rgb565(u16(coff))
    r1, g1, b1 = rgb565(u16(coff + 2))
    R = np.concatenate([r0, r1])
    G = np.concatenate([g0, g1])
    B = np.concatenate([b0, b1])

    if bs == 16 and fcc in ('DXT5', 'BC3', 'DXT4'):
        # BC3 alpha: two endpoints then 16 x 3-bit indices packed into 6 bytes. When a0 <= a1 the
        # palette is 4 interpolants plus the literals 0 and 255 -- the encoding a hard cutout
        # decal lands in, so reading the endpoints alone reports every such file as empty.
        a0 = b[base].astype(np.float32)
        a1 = b[base + 1].astype(np.float32)
        bits = np.zeros(len(base), dtype=np.uint64)
        for k in range(6):
            bits |= b[base + 2 + k].astype(np.uint64) << np.uint64(8 * k)
        sel = np.stack([((bits >> np.uint64(3 * t)) & np.uint64(7)).astype(np.int64)
                        for t in range(16)], axis=1)
        six = a0 <= a1
        pal = np.empty((len(base), 8), dtype=np.float32)
        pal[:, 0], pal[:, 1] = a0, a1
        for t in range(1, 6):
            pal[:, t + 1] = np.where(six, a0 + (a1 - a0) * (t / 5.0), a0 + (a1 - a0) * (t / 7.0))
        pal[six, 6], pal[six, 7] = 0.0, 255.0
        av = np.take_along_axis(pal, sel, axis=1)
        aOpaque, aZero, mA = float((av > 247).mean()), float((av < 8).mean()), float(av.mean())
    elif fcc in ('DXT1', 'BC1'):
        aOpaque, aZero, mA = float((u16(0) > u16(2)).mean()), 0.0, 255.0
    else:
        aOpaque, aZero, mA = 1.0, 0.0, 255.0

    mx = np.maximum(np.maximum(R, G), B)
    mn = np.minimum(np.minimum(R, G), B)
    s = dict(w=w, h=h, px=w * h, fcc=fcc,
             mR=float(R.mean()), mG=float(G.mean()), mB=float(B.mean()), sdB=float(B.std()),
             mA=mA, aOpaque=aOpaque, aZero=aZero,
             sat=float(((mx - mn) / np.maximum(mx, 1)).mean()),
             gray=float((np.abs(R - G) < 8).mean()))
    s['kind'] = classify(s)
    return s


def classify(s):
    """'normal' | 'coeff' | 'flat' | 'color' -- from pixels, never from the filename."""
    if s['fcc'] in _TWO_CHANNEL:
        return 'normal'
    if s['mB'] > 180 and 96 < s['mR'] < 168 and 96 < s['mG'] < 168 and s['sdB'] < 45:
        return 'normal'
    # A packed map leaves the blue lane UNUSED, which means flat, not merely dark. `sdB < 22` was
    # too loose: a saturated warm kit -- Columbus's red pants (152,19,14), Boston's and
    # Nashville's gold -- has a near-zero blue MEAN too, and got thrown away as a coefficient
    # map, leaving columbus/ten with a 512x512 decal in its pant slot. Measured over the
    # ten/eleven set the two groups do not overlap: every genuine coeff map sits at sdB 0-2 and
    # every misfire at 4 or above.
    if s['mB'] < 20 and s['sdB'] < 3:
        return 'coeff'
    # Desaturated is not enough on its own to call a sheet a placeholder: Vegas's pants are a
    # near-monochrome dark grey and would be thrown away, leaving a blueprint proof to win the
    # slot. A real placeholder is a UNIFORM fill, so require the variance to be gone too.
    if s['sat'] < 0.06 and s['sdB'] < 2.0 and s['aOpaque'] > 0.95:
        return 'flat'                       # placeholder / unused slot
    return 'color'


# -- folder scan ---------------------------------------------------------------------------

def _family(name, variation):
    m = _FAMILY.match(name)
    if not m:
        return None, None
    fam, rest = m.group(1).lower(), m.group('rest').lower()
    slot = None
    if fam in ('branding_jersey', 'branding_pant', 'helmetlogo'):
        # `..._<variation>_3_color` numbers the decal slot. A trailing `_2` .. `_7` does NOT: that
        # is the exporter's collision counter, appended when the name it wanted was already used,
        # which is how one slot's colour and normal end up as `_0_color` and `_0_color_2`. The
        # same counter rides on `_array` names (`..._color_array_4`), so those carry no slot at
        # all -- folders that dump their branding as `_array` have lost the slot order.
        mm = re.search(re.escape(variation) + r'_(\d+)', rest)
        slot = int(mm.group(1)) if mm else None
    if fam in ('jersey', 'pant', 'sock') and re.search(re.escape(variation) + r'_base', rest):
        fam += '_base'
    return fam, slot


# Below this, hash the whole file; above it, sample. Every 512x512 decal is well under it.
_FULL_HASH_MAX = 2 << 20


def _quickhash(path):
    """Identity key for de-aliasing. Whole file when small, sampled when not (the dump is 23 GB).

    Sampling must include the MIDDLE. DDS blocks are row-major, so a head+tail digest reads only
    the top and bottom bands of the image -- and a decal sheet is a mark centred on empty, so two
    entirely different wordmarks share blank top and bottom bands and collide. That is why the
    512x512 decals are hashed in full rather than sampled.
    """
    size = os.path.getsize(path)
    h = hashlib.blake2b(str(size).encode(), digest_size=16)
    with open(path, 'rb') as fh:
        if size <= _FULL_HASH_MAX:
            for chunk in iter(lambda: fh.read(1 << 20), b''):
                h.update(chunk)
        else:
            for pos in (0, size // 3, 2 * size // 3, max(0, size - 65536)):
                fh.seek(pos)
                h.update(fh.read(65536))
    return h.hexdigest()


def scan(folder, variation=None):
    """-> [record], one per DISTINCT texture. Every name it was filed under is in `aliases`."""
    folder = Path(folder)
    variation = (variation or folder.name).lower()
    seen, out = {}, []
    for p in sorted(folder.glob('*.dds')):
        fam, slot = _family(p.name, variation)
        key = _quickhash(p)
        if key in seen:                            # same pixels, another sampler name
            rec = seen[key]
            rec['aliases'].append(p.name)
            if fam:
                rec['fams'].add(fam)
            if slot is not None:
                rec['slots'].add(slot)
            continue
        try:
            s = stats(p)
        except Exception:
            continue
        rec = dict(name=p.name, path=p, key=key[:8], aliases=[],
                   fams={fam} if fam else set(), slots={slot} if slot is not None else set(), **s)
        seen[key] = rec
        out.append(rec)
    return out


def resolve(folder, variation=None):
    """-> ([record], {role: record | None}). Roles are ROLES plus `decal<N>_color/_normal`."""
    folder = Path(folder)
    variation = (variation or folder.name).lower()
    recs = scan(folder, variation)

    def cands(fams, kind, slot=None):
        return [r for r in recs if (r['fams'] & set(fams)) and r['kind'] == kind
                and (slot is None or slot in r['slots'])]

    def garment(fams, kind):
        # The sheet to convert is the FULLY OPAQUE copy. The same art is often also dumped as a
        # DXT5 whose alpha lane carries an unrelated mask; converting that copy loses the art
        # wherever the mask happens to be zero.
        c = cands(fams, kind)
        return max(c, key=lambda r: (round(r['aOpaque'], 2), r['px'])) if c else None

    def atlas(kind):
        # The glyph sheet is glyphs on empty, so it is the transparent one. Some folders alias
        # the jersey sheet under a `font_` name too; the transparency test separates them.
        c = cands(('font',), kind)
        if not c:
            return None
        return max([r for r in c if r['aZero'] > 0.25] or c, key=lambda r: r['px'])

    JERSEY = ('jersey', 'jersey_base', 'stamp_jersey')
    out = {
        'base_color': garment(JERSEY, 'color'),
        'base_normal': garment(JERSEY, 'normal'),
        'base_coeff': garment(JERSEY, 'coeff'),
        'pant_color': garment(('pant', 'pant_base'), 'color'),
        'pant_normal': garment(('pant', 'pant_base'), 'normal'),
        'sock_color': garment(('sock', 'sock_base'), 'color'),
        'sock_normal': garment(('sock', 'sock_base'), 'normal'),
        'font_color': atlas('color'),
        'font_normal': atlas('normal'),
        'helmet_color': garment(('helmetlogo',), 'color'),
    }
    # Decal slots. Slot 0 is the primary crest in every folder that numbers its branding files;
    # slots 1..5 hold the shoulder patches, the NHL shield, the adidas mark, the league
    # sustainability mark and the kit sponsor, in an order that VARIES by team.
    for sl in sorted({s for r in recs if 'branding_jersey' in r['fams'] for s in r['slots']}):
        for kind in ('color', 'normal'):
            # Alpha coverage breaks the tie: decals are all 512x512, so pixel count alone leaves
            # a slot's two candidates level, and the loser is a washed-out proof of the same mark.
            c = cands(('branding_jersey',), kind, sl)
            out['decal%d_%s' % (sl, kind)] = (
                max(c, key=lambda r: (r['px'], r['aOpaque'])) if c else None)
    # Branding art whose slot the filename never recorded (the `_array` folders). It is real crest
    # and patch art, just unordered, so it is offered as a list for the operator to pick from
    # rather than guessed into a slot. Garment sheets aliased under a branding name are excluded.
    # The size gate matters: every real decal in the dump is 512x512, while a garment sheet that
    # happens to be aliased under a `branding_jersey_..._array` name is 2048x2048 and would
    # otherwise be offered as a patch.
    claimed = {id(v) for k, v in out.items() if v is not None}
    out['decals_unassigned'] = [
        r for r in recs
        if 'branding_jersey' in r['fams'] and not r['slots']
        and r['kind'] == 'color' and r['w'] <= _DECAL_MAX_PX and id(r) not in claimed]
    return recs, out


def _sources_from(roles):
    """Garment/atlas slots of a resolved folder, in `jersey_convert.find_sources` shape."""
    out = {}
    for key, role in (("base", "base_color"), ("pant", "pant_color"), ("sock", "sock_color"),
                      ("font", "font_color"), ("font_normal", "font_normal"),
                      ("helmet_logo", "helmet_color")):
        rec = roles.get(role)
        if rec is not None:
            out[key] = rec['path']
    return out


def find_sources(folder, variation=None):
    """Garment sources for an NHL 26 kit folder -> {kind: Path}.

    Keys: `base`, `pant`, `sock`, `font`, `font_normal`, `helmet_logo`. The crest and patch art
    is a separate question -- ask `decal_roles`, or `resolve_folder` for both off one scan.
    """
    _, roles = resolve(folder, variation)
    return _sources_from(roles)


def resolve_folder(folder, variation=None):
    """-> (sources, decals). Both halves of a kit folder off a SINGLE scan.

    Scanning samples every DDS in the folder, so the tab asks for both at once rather than
    paying for the walk twice.
    """
    _, roles = resolve(folder, variation)
    return _sources_from(roles), _decals_from(roles)


# -- decal identity -------------------------------------------------------------------------
#
# A decal's SLOT NUMBER does not carry its meaning. Anaheim's slot 1 is the wing shoulder patch,
# Chicago's and Detroit's is the NHL shield, Toronto's is the Milk sponsor. What does separate
# them is that the league marks are ONE asset reused league-wide: measured across all 424 kits,
# 658 of 739 distinct colour decals appear in exactly one team, while a handful appear in 21-33.
# Those few are the NHL/LNH shield, the adidas three-stripe mark and the End Plastic Waste mark.
#
# They are matched here by a perceptual hash of the alpha shape rather than by file hash, so a
# re-dump at different compression settings still matches. `_LEAGUE_MARKS` is generated from the
# corpus by tools/nhl26_league_marks.py.

def phash(path, size=8):
    """64-bit difference hash of the decal's ALPHA coverage -- the mark's silhouette.

    Alpha rather than colour because the same shield ships in black, white and team-tinted
    variants; the cut-out shape is what stays constant.
    """
    from PIL import Image
    im = Image.open(path).convert("RGBA")
    a = im.split()[3].resize((size + 1, size), Image.BILINEAR)
    px = a.load()
    bits = 0
    for y in range(size):
        for x in range(size):
            bits = (bits << 1) | (1 if px[x, y] > px[x + 1, y] else 0)
    return bits


def _hamming(a, b):
    return bin(a ^ b).count('1')


# Alpha-silhouette hashes of the league-wide marks, with the team count each was seen in. The
# NHL shield ships in black, white and an orange Winter-Classic tint, but every variant cuts the
# same shield outline, so one hash covers them all. adidas appears in two lockups (bare
# three-stripe and the full wordmark), so both are listed under the same label.
#
# The Fanatics flag is why this matches on silhouette rather than file hash: it is TINTED PER TEAM
# (Anaheim orange, Toronto navy, Detroit red, Seattle teal, Vegas black), so no two teams share a
# byte-identical copy and the cross-team file-hash count saw only 5 of them. Every kit cuts the
# same flag, so one phash entry covers the league.
_LEAGUE_MARKS = {
    0x2a0f070707070f0c: 'nhl_shield',          # 30, 30, 23, 21, 10, 9, 5 teams
    0x0101030301013f7f: 'end_plastic_waste',   # 33, 15, 7, 7 teams
    0x0006060713496900: 'adidas_mark',         # 14, 8 teams  (three-stripe)
    0x0406070707030101: 'adidas_mark',         # 5 teams      (wordmark lockup)
    0x434303071f0e0c0c: 'fanatics_mark',       # the Fanatics flag, on every ten/eleven kit
}

# Nearest distance between two different league marks is 14 (measured), and the nearest team
# crest sits 10 away from the shield, so 6 leaves room on both sides.
_MARK_MAX_DIST = 6


def league_mark(path):
    """-> 'nhl_shield' | 'adidas_mark' | 'end_plastic_waste' | None."""
    try:
        ph = phash(path)
    except Exception:
        return None
    best, name = _MARK_MAX_DIST + 1, None
    for ref, label in _LEAGUE_MARKS.items():
        d = _hamming(ph, ref)
        if d < best:
            best, name = d, label
    return name


def decal_roles(folder, variation=None):
    """-> {role: Path}. Decals named by WHAT THEY ARE, not by the slot number they landed in.

    Roles: `crest`, `nhl_shield`, `adidas_mark`, `fanatics_mark`, `end_plastic_waste`, and
    `patch0` .. `patchN` for the team-specific art left over (shoulder patches, kit sponsors,
    anniversary marks), ordered by slot. A folder that dumped its branding without slot numbers
    has no `crest`; its art still appears as patches.
    """
    _, roles = resolve(folder, variation)
    return _decals_from(roles)


def _decals_from(roles):
    out, patches = {}, []
    seen = set()

    # Slot 0 is claimed FIRST and never mark-matched. Several crests are shield-shaped -- Vegas's
    # most of all -- and the silhouette hash cannot tell a gold Golden Knights shield from the
    # NHL one. The slot number can, and slot 0 held the crest in every numbered folder checked.
    crest = roles.get('decal%d_color' % CREST_SLOT)
    if crest is not None:
        out['crest'] = crest['path']
        seen.add(id(crest))

    for key in sorted(k for k in roles if k.startswith('decal') and k.endswith('_color')):
        rec = roles[key]
        if rec is None or id(rec) in seen:
            continue
        seen.add(id(rec))
        mark = league_mark(rec['path'])
        if mark is not None:
            out.setdefault(mark, rec['path'])
        else:
            patches.append(rec['path'])
    for rec in roles.get('decals_unassigned', ()):
        if id(rec) in seen:
            continue
        seen.add(id(rec))
        mark = league_mark(rec['path'])
        if mark is not None:
            out.setdefault(mark, rec['path'])
        else:
            patches.append(rec['path'])
    # The same file can arrive under two slot numbers (anaheim/eleven numbers the shield 2 AND 3),
    # which `seen` misses because those are two records. Dedupe by path so a doubled slot does not
    # produce two identical patches.
    byname, ordered = set(), []
    for pth in patches:
        if str(pth).lower() in byname:
            continue
        byname.add(str(pth).lower())
        ordered.append(pth)
    for i, pth in enumerate(ordered):
        out['patch%d' % i] = pth
    return out
