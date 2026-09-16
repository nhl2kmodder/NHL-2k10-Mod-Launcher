"""
modpack.py — share & merge NHL 2k10 mod work between people.

Two artefacts, one merge engine (everything is keyed by STABLE asset identity, 
never by a user-chosen name, so two people's naming differences never break the merge):
  * Audio Names file (.json)  — just the naming work: per-stream {name, category, sample_rate}.
  * Mod Pack (.n2kpack = zip) — everything: audio names + replacement WAVs + replacement textures, compressed (lossless).

A third section — ROSTER — is different in kind: it is not a replacement FILE but a set of
FIELD VALUES read live out of a Roster.ROS save and re-applied (in place, over the top) onto
whatever ROS the recipient uses, so you can share e.g. "all 30 teams' colours" without shipping
your players/ratings.

A fourth section — SCORECLOCK — follows the same "captured state, not files" model: ONE
scoreclock.json holding the full scoreclock mod = {snapshot (lossless per-element layout from
scorebug_layout), shadow_hidden, teal_hidden, sog (Shots-on-Goal present), anchor ([x,y] XEX
screen-anchor pair)}. Applying replays it onto the recipient's game files: one DRAM rebuild
(rebuild_overlay_from_snapshot re-adds SOG records + all element edits + both hides in a single
decode/encode) plus the two idempotent, sentinel-checked XEX patches (SOG bind rows + anchor).
The scoreclock's overlay_static TEXTURES ride in the normal textures section — staged into
Extracted and applied by the usual Apply All pass, which preserves this layout.

A fifth section — PORTRAITS — ships RAW GAME BYTES: the whole disc_b9610aac.iff player-portrait
pixel pack (~65MB, all 1478 faces), installed over the recipient's outright. All-or-nothing, and
it carries no player->portrait assignments (those live in Roster.ROS, and portrait keys only mean
anything against the roster they were authored for) — see the section header for why.

A sixth section — EXPANSION TEAMS — is the only one that ADDS a club rather than editing one.
It ships a RECIPE per club (donor + the field diff against that donor), because both sides build
it the same way: `expansion.create_expansion_team` clones a shipping club into a spare record and
`extra_teams.ensure_team_assets` clones the donor's whole art family onto the new asset key. The
club's own textures ride in the normal textures section (auto-included on export). Applied BEFORE
the roster groups, since those address teams by code. See that section's header for the details.

A seventh section — PLAYER HEADS — ships a custom FACE, and it is the only section that carries
model geometry. Per head: blob 0 of player_head_id_NNNN.iff (the reshaped mesh), whichever of the
three 512x512 surfaces differ from the artist's, and the roster binding (which row wears it, plus
that row's eye colour). Which heads are "edited" is read off the files — live archive bytes vs the
pristine `.orig` bytes — so it needs no launcher config, and every edited head is a checkbox: tick
one, tick twenty, or leave them all ticked and the pack carries every head you have made. Applied
LAST, because expansion clubs add player records and a head's identity is a row number. See that
section's header for the id-collision rule and the identity chain.

Whole-league groups, each one selectable checkbox:
  * team_colors  — {CODE: {primary, secondary}}      (chunk 0x8489FAF3, via team_colors.py)
  * arena_names  — {CODE: arena}                      (string pool, via roster_editor.py)
  * arena_fields — {CODE: {dasher, dasher2, capacity}} (chunk 0xE35B988E, via arena_colors.py)
  * team_names   — {CODE: {city, state, team, name}}  (string pool, via roster_editor.py)
  * goalie_masks — {row: {shell, pattern, colors, cage, ...}} (player records, via player_assign.py)

arena_fields is keyed by team code like the rest, but the record it writes belongs to a BUILDING:
teams that share an arena (an AHL affiliate rides its parent club's) resolve to the same record, so
applying it is idempotent-by-repetition and the count reported is arenas touched, not teams.

goalie_masks is the one roster group that is PER-PLAYER rather than per-team, so it carries its
own identity plumbing (see that section's header). Selecting it also force-includes the mask
TEXTURE files it names, so the pack is self-contained the way the scoreclock section is.

Applying names is best-effort: a name longer than its fixed slot in the target save is skipped
(the same in-place constraint the Teams tab enforces), never grown.

Identity keys:
  audio stream  ->  "<fid>:0x<OFFSET8>"   (resolved through the extract manifest, robust to renames)
  texture       ->  its relative path under Textures/Extracted/  (deterministic filename)
  roster group  ->  the group name ("team_colors" | "arena_names" | "team_names" | "goalie_masks")
  scoreclock    ->  the single key "scoreclock" (whole-mod, all-or-nothing)
  expansion team->  its ASSET KEY ("SEA") — the thing that names all of its art
  player head   ->  its HEAD ASSET ID ("3040") — the same slot on every install

Merge model per item:
  NEW (recipient lacks it) -> add;
  SAME (identical) -> skip;
  CONFLICT (present but different) -> caller decides keep-mine / keep-theirs.
"""

import json
import os
import zipfile
import hashlib
import shutil
import struct
import time
from pathlib import Path

try:
    from . import archive_textures as AT
    from . import audio_store as AS
except ImportError:
    import archive_textures as AT
    import audio_store as AS

try:
    from . import team_colors as TC
    from . import roster_editor as RE
    from . import player_assign as PA
    from . import team_order as TO
    from . import arena_colors as AC
except ImportError:
    import team_colors as TC
    import roster_editor as RE
    import player_assign as PA
    import team_order as TO
    import arena_colors as AC

FORMAT = "nhl2k10-modpack"
# 2 = carries audio_cue.json, the portable (bank, cue) key beside each WAV's physical akey path.
# Purely additive: a VERSION 1 pack still imports (no sidecar -> akeys used verbatim, as before),
# and a VERSION 2 pack read by older code just ignores a member it does not know.
VERSION = 2
# (no FILE_IDS here — this module never enumerates containers. Every audio path below is driven off
# `AS.load_manifest`, whose keys are "<fid>:0x<off>", so a fifth archive needs no change here. The
# hardcoded four that used to sit on this line was dead code, and worse, it read as a promise that
# packs were limited to them.)
PACK_EXT = ".n2kpack"
NAMES_EXT = ".n2knames.json"

ROSTER_GROUP_ORDER = ["team_order", "team_colors", "arena_names", "arena_fields",
                      "team_names", "goalie_masks"]
ROSTER_GROUPS = {
    "team_order":   "Team Order — the order teams are listed in every menu",
    "team_colors":  "Team Colours — primary + secondary (all teams)",
    "arena_names":  "Arena Names (all teams)",
    "arena_fields": "Arena Boards & Seats — dasher colours + seating capacity (all teams)",
    "team_names":   "Team Names — city / state / team / name (all teams)",
    "goalie_masks": "Goalie Masks — mask + pattern colours + cage (every custom-mask goalie)",
}


# ── small helpers ─────────────────────────────────────────────────────────────

akey       = AS.akey
parse_akey = AS.parse_akey


# ── portable audio keys ───────────────────────────────────────────────────────
# An akey is a PHYSICAL address ("1A:0x4B8EC000"), which is only meaningful on the install that
# produced it. Grow a wave bank and the live TOC RELOCATES it (project_relocated_bank_keys), so a
# pack built here names bytes that on the recipient's copy are a different clip or nothing at all
# — silently, because the address still parses.
#
# (bank, cue index) does not move. `speech_line_tables.json` is SHIPPED data, identical for sender
# and recipient, and `speech_lines.physical()` resolves a cue to whatever address it occupies on
# THIS install (consulting the live TOC via wave_banks.moved()). So the pack carries a sidecar
# `audio_cue.json` = {akey: "bank#cue"} beside the WAVs, and import prefers the cue key when it
# resolves. The WAVs stay under their akey path so a VERSION 1 reader is unaffected.
#
# Not every stream has one: 10 of 19 banks have no decoded line table, and a cue ADDED past the
# shipped table (the 440 new PxP takes) has no shipped index. Those fall back to the akey, which
# is exactly as portable as it was before — no worse, and now the difference is visible.
CUEMAP_ARC = "audio_cue.json"

try:
    from . import speech_lines as SL
except ImportError:
    try:
        import speech_lines as SL
    except Exception:
        SL = None
except Exception:
    SL = None


def cue_key(key):
    """akey -> "bank#cue" for a stream the shipped line tables cover, else None."""
    if SL is None:
        return None
    try:
        fid, off = parse_akey(key)
        bank, cue = SL.cue_index(fid, off)
        return f"{bank}#{cue}" if bank and cue >= 0 else None
    except Exception:
        return None


def cue_key_akey(ck):
    """"bank#cue" -> this install's akey for that cue, or None if it cannot be placed."""
    if SL is None or not ck or "#" not in ck:
        return None
    try:
        bank, cue = ck.rsplit("#", 1)
        rel = SL.cue_offset(bank, int(cue))
        if rel is None:
            return None
        fid, off = SL.physical(bank, rel)
        return akey(fid, off) if fid else None
    except Exception:
        return None


def rekey_audio(key, cuemap):
    """The akey to use locally for a packed stream: (key, note).

    `note` is non-empty only when the cue key moved the answer — that is the case worth saying out
    loud, because it means the sender's addresses were NOT this install's.
    """
    local = cue_key_akey((cuemap or {}).get(key))
    if local and local != key:
        return local, f"re-keyed {key} -> {local} via cue {cuemap[key]}"
    return key, ""

def _load_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}

def _sha(b):
    return hashlib.sha1(b).hexdigest()

def _sha_file(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()

def _is_safe_path(base_dir: Path, target_path: Path) -> bool:
    """Guard against Zip Slip / path traversal vulnerabilities."""
    try:
        target_path.resolve().relative_to(base_dir.resolve())
        return True
    except ValueError:
        return False


# ── local-state scanners ──────────────────────────────────────────────────────

def load_audio_meta(root):
    """Shareable per-stream metadata (name / category / sample rate) from Audio_Names.json."""
    meta = {}
    entries, _ = AS.load_names(root)
    for k, v in entries.items():
        try:
            AS.parse_akey(k)
        except Exception:
            continue
        e = {kk: vv for kk, vv in v.items() if kk != "stem" and not kk.startswith("_")}
        if e:
            meta[k] = e
    return meta

def _catalog_index(root, manifest=None):
    """(name -> (fid, off), akey -> entry) from the extract manifest.

    `friendly_name`/`_fid` are kept as aliases because pack files written before the layout
    change carry those field names and still have to import.
    """
    name2off, off2entry = {}, {}
    for key, e in (manifest if manifest is not None else AS.load_manifest(root)).items():
        try:
            fid, off = AS.parse_akey(key)
        except Exception:
            continue
        name = e.get("name") or AS.stem_of(e)
        off2entry[key] = {**e, "_fid": fid, "friendly_name": name,
                          "stem": AS.stem_of(e)}
        name2off[name] = (fid, off)
        name2off[AS.stem_of(e)] = (fid, off)
    return name2off, off2entry

_MTIME_EPS = 2.0    # FAT mtime granularity + copy2 rounding — same slack as the Audio tab


def _wav_stat_sweep(root):
    """normcase'd wav path -> (size, mtime) from ONE directory walk of Audio/Extracted.

    scandir hands both back from the directory entry the walk already read, so this is ~0.2 s
    for 80k files where per-file stat() calls would take seconds."""
    stats = {}
    base = AS.extracted_root(root)
    for dirpath, _dirs, _files in os.walk(base):
        with os.scandir(dirpath) as it:
            for de in it:
                if de.name.endswith(".wav") and de.is_file():
                    try:
                        st = de.stat()
                        stats[os.path.normcase(de.path)] = (st.st_size, st.st_mtime)
                    except OSError:
                        pass
    return stats


def load_audio_wavs(root, fast=False, manifest=None):
    """akey -> WAV path, for streams the user has actually edited.

    Editing is in place now, so "edited" is the manifest sha1 comparison rather than
    "a file exists in Modified/Audio/".

    `fast` swaps the SHA-1 comparison (a ~10 min sweep over a fully-extracted 24 GB tree) for
    the same dirty -> size -> mtime test the Audio tab's ✓ Modified column uses: one stat walk,
    no file reads. That is the right answer for pickers and diff listings; anything that must
    PROVE bytes differ (Patch Game, Rescan Edits) keeps the hash."""
    out = {}
    man = manifest if manifest is not None else AS.load_manifest(root)
    if not fast:
        for key, e in man.items():
            p = AS.wav_path(root, e)
            if AS.is_edited(root, e, p):
                out[key] = p
        return out
    stats = _wav_stat_sweep(root)
    for key, e in man.items():
        p = AS.wav_path(root, e)
        st = stats.get(os.path.normcase(str(p)))
        if st is None:                                # no WAV on disk -> nothing to ship
            continue
        cur_size, cur_mtime = st
        if e.get("dirty"):
            out[key] = p; continue
        base_size = e.get("size")
        if base_size is None or cur_size != base_size:
            out[key] = p; continue
        base_mtime = e.get("mtime")
        # No recorded mtime (pre-tracking extract) -> size-only, same fallback as the tab list;
        # Rescan Edits on the Audio tab is how those entries get settled into `dirty`.
        if base_mtime is not None and abs(cur_mtime - base_mtime) > _MTIME_EPS:
            out[key] = p
    return out

def load_textures(root):
    out = {}
    base = AT.extracted_root(root)
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file():
                out[p.relative_to(base).as_posix()] = p
    return out


# ── roster field-groups (read live from a Roster.ROS, applied over the top) ────

def _rgbhex(rgb):
    return "#%02X%02X%02X" % (rgb[0] & 0xFF, rgb[1] & 0xFF, rgb[2] & 0xFF)


# ── goalie masks (a PER-PLAYER roster group) ──────────────────────────────────
#
# The unit shipped per goalie is the whole look: which mask (shell + pattern), the three
# pattern/recolour colour slots and the cage colour — the four things the Creation-Zone mask
# editor exposes, all of them plain fields in the player record (+0xB4/+0xB8/+0x158../+0x168).
#
# WHICH goalies: the three recolour slots set to the RGB identity (FF0000/00FF00/0000FF) is the
# file-visible signature of a repainted custom mask — it neutralises the team-colour substitution
# so an authored true-colour texture renders as painted. Nobody sets that by accident, and it needs
# no launcher config to detect, so it is the export filter.
#
# IDENTITY across rosters is the hard part: names are NOT stored inline in the player record (the
# loader resolves them through a pointer), so a goalie can only be pinned by numbers. Each entry
# therefore carries three, checked in descending order of confidence at apply time:
#   row      — the record index; exact on the same roster lineage, which is the normal case since
#              a portrait/mask pack already tells recipients to take the author's Roster.ROS.
#   portrait — the row's portrait key (+0x1C). Verifies `row` still points at the same player, and
#              is the search key when it doesn't. Not unique (backup/duplicate rows share keys).
#   ordinal  — nth goalie in file order. Last resort, only trusted when the goalie COUNT matches.
# Anything that resolves to a non-goalie row, or doesn't resolve at all, is skipped and reported —
# a mask written onto a skater would be silent corruption.

GOALIE_MASKS_KEY = "goalie_masks"

def _hex6(v):
    return "#%06X" % (int(v) & 0xFFFFFF)

def _unhex6(s):
    if isinstance(s, int):
        return s & 0xFFFFFF
    return int(str(s).lstrip("#"), 16) & 0xFFFFFF

def _goalie_name_map():
    """portrait key -> "First Last", best-effort, for readable labels and logs ONLY.

    Read out of the launcher's own config (the Portraits tab's name->key assignments); a pack that
    lands on someone else's machine simply carries whatever names the author's config resolved.
    Never used for matching — see the section header for why identity is numeric.
    """
    import os
    try:
        p = Path(os.environ["APPDATA"]) / "NHL2K10 Mod Launcher" / "nhl2k10_launcher_config.json"
        cfg = json.loads(p.read_text(encoding="utf-8"))
        out = {}
        for nm, key in (cfg.get("player_portraits") or {}).items():
            out.setdefault(int(key), str(nm).replace("|", " "))
        return out
    except Exception:
        return {}

def mask_asset_name(shell, pattern):
    """The mask texture asset for a (shell, pattern) pair.

    The texture filename's shell number is one HIGHER than the value in the player record — the
    record's 0 is helmet_g01 — which is the same off-by-one the Goalie tab applies in reverse.
    """
    return "helmet_g%02d_pattern_%02d.iff" % (int(shell) + 1, int(pattern))

def load_goalie_masks(ros_path):
    """{"<row>": {ordinal, portrait, name, shell, pattern, colors[3], cage}} for custom-mask goalies."""
    ros_path = Path(ros_path)
    if not ros_path.is_file():
        return {}
    ident = tuple(c & 0xFFFFFF for c in PA.IDENTITY_COLORS)
    names = _goalie_name_map()
    out = {}
    try:
        t = PA.PlayerTable(ros_path)
        goalies = t.goalie_rows()
        for ordinal, row in enumerate(goalies):
            look = t.goalie_look(row)
            if tuple(look["colors"]) != ident:
                continue
            pk = t.portrait(row)
            out[str(row)] = {
                "ordinal": ordinal,
                "portrait": pk,
                "name": names.get(pk, ""),
                "shell": look["shell"],
                "pattern": look["pattern"],
                "colors": [_hex6(c) for c in look["colors"]],
                "cage": _hex6(look["cage"]),
            }
    except Exception:
        return {}
    return out

def goalie_mask_assets(data):
    """The mask texture assets a goalie_masks group refers to — the art that must ride along."""
    out = set()
    for e in (data or {}).values():
        try:
            out.add(mask_asset_name(e["shell"], e["pattern"]))
        except Exception:
            continue
    return out

def goalie_mask_tex_folders(data):
    """Those assets' folders under Textures/Extracted/, lowercased, as posix prefixes.

    Via AT.asset_iff rather than the asset name: the extract tree is a GROUPED layout, so a folder
    can be nested (Uniform/ANA/AWAY) or shared (Logos). Masks currently sit in a flat
    helmet_gNN_pattern_NN.iff/ folder, but going through the same mapping the IFF tab extracts with
    means this keeps working if that ever changes.
    """
    return {AT.asset_iff(a).lower().replace("\\", "/").strip("/") for a in goalie_mask_assets(data)}

def _resolve_goalie_row(t, goalies, want_row, portrait, ordinal):
    """(row, note) — the recipient row this entry belongs to, or (None, why-not). See header."""
    gset = set(goalies)
    if want_row in gset and (portrait is None or t.portrait(want_row) == portrait):
        return want_row, ""
    if portrait is not None:
        hits = [r for r in goalies if t.portrait(r) == portrait]
        if len(hits) == 1:
            return hits[0], f"row {want_row} moved -> {hits[0]} (matched by portrait {portrait})"
        if len(hits) > 1:
            if want_row in hits:
                return want_row, ""
            return hits[0], (f"row {want_row} moved -> {hits[0]} (portrait {portrait} is on "
                             f"{len(hits)} goalies — took the first)")
    if want_row in gset:
        return want_row, f"row {want_row}: portrait differs from the pack — applied by row anyway"
    if ordinal is not None and 0 <= ordinal < len(goalies):
        return goalies[ordinal], (f"row {want_row} is not a goalie here — fell back to goalie "
                                  f"#{ordinal} (row {goalies[ordinal]})")
    return None, f"row {want_row} (portrait {portrait}) has no goalie to match in this roster"

def apply_goalie_masks(ros_path, data, log=print):
    """Write each shipped goalie look onto the matching row. Returns the number applied."""
    ros_path = Path(ros_path)
    t = PA.PlayerTable(ros_path)
    goalies = t.goalie_rows()
    n, skipped = 0, 0
    for rk, e in sorted(data.items(), key=lambda kv: int(kv[0])):
        who = e.get("name") or f"portrait {e.get('portrait')}"
        row, note = _resolve_goalie_row(t, goalies, int(rk), e.get("portrait"), e.get("ordinal"))
        if row is None:
            log(f"  goalie_masks: {who} skipped — {note}")
            skipped += 1
            continue
        if note:
            log(f"  goalie_masks: {who} — {note}")
        t.set_goalie_look(row, {
            "shell":   e["shell"],
            "pattern": e["pattern"],
            "colors":  [_unhex6(c) for c in e["colors"]],
            "cage":    _unhex6(e["cage"]),
        })
        n += 1
    if n:
        t.save()
    if skipped:
        log(f"  goalie_masks: {skipped} goalie(s) could not be matched in this roster")
    return n


def load_roster(ros_path):
    out = {}
    ros_path = Path(ros_path)
    if not ros_path.is_file():
        return out
    try:
        # a list of the 30 display codes, in menu order — see team_order.py
        codes = TO.order_codes(ros_path)
        if len(codes) == TO.NHL and all(codes):
            out["team_order"] = codes
    except Exception:
        pass
    try:
        cols = TC.load(ros_path)
        tc = {code: {"primary": _rgbhex(v["primary"]), "secondary": _rgbhex(v["secondary"])} for code, v in cols.items()}
        if tc:
            out["team_colors"] = tc
    except Exception:
        pass
    try:
        ed = RE.RosterEditor(str(ros_path))
        arenas, names = {}, {}
        for t in ed.teams:
            if t.arena:
                arenas[t.code] = t.arena.text
            rec = {}
            if t.city:     rec["city"]  = t.city.text
            if t.state:    rec["state"] = t.state.text
            if t.team:     rec["team"]  = t.team.text
            if t.nickname: rec["name"]  = t.nickname.text
            if rec:
                names[t.code] = rec
        if arenas:
            out["arena_names"] = arenas
        if names:
            out["team_names"]  = names
    except Exception:
        pass
    try:
        # The ARENA record, not the team record — dasher boards and seating capacity. Keyed by team
        # code like every other group, but several teams share a building (AHL affiliates ride their
        # parent club's arena), so applying it is idempotent-by-repetition rather than one-per-record.
        ar = {}
        for code, v in AC.load(ros_path).items():
            ar[code] = {"dasher": v["dasher"], "dasher2": v["dasher2"], "capacity": v["capacity"]}
        if ar:
            out["arena_fields"] = ar
    except Exception:
        pass
    gm = load_goalie_masks(ros_path)
    if gm:
        out["goalie_masks"] = gm
    return out

def _roster_group_label(group, data=None):
    label = ROSTER_GROUPS.get(group, group.replace("_", " ").title())
    if group == GOALIE_MASKS_KEY and data:
        label += f"   [{len(data)} goalies — mask textures ride along]"
    if group == "team_order" and data:
        label += f"   [{', '.join(list(data)[:6])}…]"
    return label

def _roster_item(group, data):
    return {"section": "roster", "key": group, "label": _roster_group_label(group, data),
            "team": "", "category": "Roster", "count": len(data)}

def local_roster_items(ros_path):
    data = load_roster(ros_path)
    return [_roster_item(g, data[g]) for g in ROSTER_GROUP_ORDER if g in data]

def diff_roster(in_roster, ros_path):
    local = load_roster(ros_path) if ros_path else {}
    items = []
    for g in ROSTER_GROUP_ORDER:
        if g not in in_roster:
            continue
        inc, loc = in_roster[g], local.get(g)
        status = "new" if loc is None else ("same" if loc == inc else "conflict")
        items.append({"section": "roster", "key": g, "status": status, "incoming": inc, "local": loc,
                      "label": _roster_group_label(g, inc), "team": "", "category": "Roster"})
    return items

def _add_name_edit(edits, skipped, slot, new):
    if slot is None or not new or new == slot.text:
        return
    if len(new) > slot.cap_chars:
        skipped.append((slot.text, new, slot.cap_chars))
        return
    edits.append((slot, new))

def apply_roster(ros_path, groups, log=print):
    ros_path = Path(ros_path)
    applied = {}
    # team_order FIRST: it physically permutes the team records, and every other group
    # addresses teams by code, so ordering it first keeps the rest addressing the same
    # teams either way.
    if "team_order" in groups:
        try:
            TO.apply_order(ros_path, groups["team_order"], log=log)
            applied["team_order"] = len(groups["team_order"])
        except Exception as e:
            log(f"  team_order failed: {e}")
            applied["team_order"] = 0

    if "team_colors" in groups:
        n = 0
        for code, v in groups["team_colors"].items():
            try:
                TC.set_color(ros_path, code, primary=v.get("primary"), secondary=v.get("secondary"), log=log)
                n += 1
            except KeyError:
                log(f"  team_colors: '{code}' not in this roster — skipped")
            except Exception as e:
                log(f"  team_colors: '{code}' failed: {e}")
        applied["team_colors"] = n

    if "arena_fields" in groups:
        n, seen = 0, set()
        for code, v in groups["arena_fields"].items():
            try:
                ar = AC.set_arena(ros_path, code, dasher=v.get("dasher"), dasher2=v.get("dasher2"),
                                  capacity=v.get("capacity"), log=log)
                # Shared buildings would otherwise be counted once per tenant.
                if ar not in seen:
                    seen.add(ar); n += 1
            except KeyError:
                log(f"  arena_fields: '{code}' has no arena in this roster — skipped")
            except Exception as e:
                log(f"  arena_fields: '{code}' failed: {e}")
        applied["arena_fields"] = n

    if "goalie_masks" in groups:
        try:
            applied["goalie_masks"] = apply_goalie_masks(ros_path, groups["goalie_masks"], log)
        except Exception as e:
            log(f"  goalie_masks failed: {e}")
            applied["goalie_masks"] = 0

    name_groups = [g for g in ("arena_names", "team_names") if g in groups]
    if name_groups:
        ed = RE.RosterEditor(str(ros_path))
        by = {t.code: t for t in ed.teams}
        edits, skipped, touched = [], [], {g: 0 for g in name_groups}

        if "arena_names" in groups:
            for code, arena in groups["arena_names"].items():
                t = by.get(code)
                if t:
                    before = len(edits)
                    _add_name_edit(edits, skipped, t.arena, arena)
                    touched["arena_names"] += len(edits) - before

        if "team_names" in groups:
            for code, rec in groups["team_names"].items():
                t = by.get(code)
                if not t:
                    continue
                before = len(edits)
                _add_name_edit(edits, skipped, t.city,  rec.get("city"))
                _add_name_edit(edits, skipped, t.state, rec.get("state"))
                _add_name_edit(edits, skipped, t.team,  rec.get("team"))

                nm = rec.get("name")
                if t.nickname and nm and nm != t.nickname.text:
                    low = nm.lower().replace(" ", "")
                    # Ensure BOTH nickname and internal lower alias fit before appending
                    nick_fits = len(nm) <= t.nickname.cap_chars
                    lower_fits = t.nick_lower is None or len(low) <= t.nick_lower.cap_chars

                    if nick_fits and lower_fits:
                        edits.append((t.nickname, nm))
                        if t.nick_lower:
                            edits.append((t.nick_lower, low))
                    else:
                        cap = t.nickname.cap_chars if not nick_fits else t.nick_lower.cap_chars
                        skipped.append((t.nickname.text, nm, cap))

                touched["team_names"] += len(edits) - before

        if edits:
            ed.apply_edits(edits)
            ed.save()

        for old, new, cap in skipped:
            log(f"  roster name '{new}' too long for its slot ({cap} chars) — skipped")

        for g in name_groups:
            applied[g] = touched[g]

    return applied


# ── expansion teams (a club that does not exist in the recipient's files at all) ──
#
# Every other section edits something the recipient already has.  This one ADDS a club: a team
# record built in a spare created-team slot (`expansion.py`) plus the whole family of art assets
# its asset key names (`extra_teams.ensure_team_assets`).  Without it a pack carrying Seattle's
# jersey lands on a roster with no Seattle and an archive with no `uniform_sea_home.iff`, and
# every one of those textures is silently dropped.
#
# What ships is a RECIPE, not bytes.  Both sides build the club the same way — byte-clone a
# SHIPPING donor club — so the pack only has to name the donor and carry what the author changed
# on top of it: `fields` is the 4-byte-field diff of the author's record against that donor
# (colours, record book, division, anything else edited), which is a handful of ints instead of
# a 412-byte blob that would need every pointer re-aimed on arrival.
#
# The donor is read off the ROSTER, not the art: a cloned record shares the donor's arena (+0xCC)
# and head-coach (+0xD8) pointers, and the one sharer that is a CATALOGUED club is the donor.
# Content-matching the uniforms (`extra_teams.infer_donor`) stops working the moment the author
# repaints them, which is the normal case for a club worth sharing.
#
# The club's PLAYERS are the donor's, on both sides, because that is what `create_expansion_team`
# writes — player names are pool-allocated strings, not inline record data, so they cannot be
# carried as values.  A recipient who wants the author's actual roster takes their Roster.ROS.
#
# Ordering matters and is enforced in `apply_items`: expansion runs BEFORE the roster groups
# (team_order permutes records, and every other group addresses teams by CODE, so the club has to
# exist first) and before the recipient's next Apply All (which needs the TOC entries to exist
# before it can write the pack's textures into them).

EXPANSION_KEY = "expansion"

# Identity that must be allocated locally, never copied: the team id and the uniform-owner slot
# are "one past whatever this file already uses", so the author's values would collide.
_EXP_LOCAL_FIELDS = {TO.OFF_ID, 0xF0}


def _expansion_modules():
    """(expansion, extra_teams) — imported lazily; extra_teams pulls in the whole archive stack,
    which a roster-only pack never needs."""
    try:
        from . import expansion as EX
        from . import extra_teams as ET
    except ImportError:
        import expansion as EX
        import extra_teams as ET
    return EX, ET


def _team_offsets(d):
    """{CODE: record offset} for every team record in the file."""
    base, count = TO.find_table(d)
    out = {}
    for i in range(count):
        o = base + i * TO.STRIDE
        code = TO._text(d, o + TO.OFF_CODE).strip().upper()
        if code:
            out.setdefault(code, o)
    return out


def _catalogued_codes(d):
    """Display codes of the clubs that SHIPPED — i.e. the ones a recipient is guaranteed to have,
    so the ones an expansion club may be cloned from."""
    _EX, ET = _expansion_modules()
    known = ET._catalogued_keys()
    base, count = TO.find_table(d)
    out = {}
    for i in TO.league0_slots(d, base, count):
        o = base + i * TO.STRIDE
        akey = TO._text(d, o + TO.OFF_AKEY).strip()
        if akey.lower() in known:
            out[TO._text(d, o + TO.OFF_CODE).strip().upper()] = akey.upper()
    return out


def _donor_of(d, o):
    """(donor code, donor asset key) for the expansion record at `o`, or (None, None).

    A cloned record still points at the donor's arena and head coach — those two pointers
    together single out the club it was built from.
    """
    arena, coach = TO._target(d, o + 0xCC), TO._target(d, o + 0xD8)
    base, count = TO.find_table(d)
    shipped = _catalogued_codes(d)
    hits = []
    for i in TO.league0_slots(d, base, count):
        p = base + i * TO.STRIDE
        if p == o:
            continue
        code = TO._text(d, p + TO.OFF_CODE).strip().upper()
        if code in shipped and TO._target(d, p + 0xCC) == arena and TO._target(d, p + 0xD8) == coach:
            hits.append(code)
    if not hits:
        return None, None
    return hits[0], shipped[hits[0]]


def _record_diff(d, o, donor_o):
    """{"0xNNN": value} for every plain (non-pointer, non-locally-allocated) field where the
    author's record differs from the donor it was cloned from."""
    ptr = set(TO.PTR_FIELDS)
    out = {}
    for f in range(0, TO.STRIDE, 4):
        if f in ptr or f in _EXP_LOCAL_FIELDS:
            continue
        v, dv = TO._u32(d, o + f), TO._u32(d, donor_o + f)
        if v != dv:
            out["0x%03X" % f] = v
    return out


def load_expansion(ros_path):
    """{AKEY: recipe} for every expansion club in this roster — the clubs whose asset key the
    shipped catalog doesn't know (`extra_teams.extra_teams`), i.e. the ones a recipient lacks."""
    out = {}
    ros_path = Path(ros_path) if ros_path else None
    if not ros_path or not ros_path.is_file():
        return out
    _EX, ET = _expansion_modules()
    d = ros_path.read_bytes()
    offs = _team_offsets(d)
    for t in ET.extra_teams(ros_path):
        o = offs.get(t["code"].upper())
        if o is None:
            continue
        donor_code, donor_akey = _donor_of(d, o)
        if not donor_code:
            continue                      # can't name a donor -> can't be rebuilt; don't ship it
        entry = {
            "code":  t["code"], "city": t["city"], "nick": t["nick"],
            "akey":  t["akey"].upper(),
            "lower": TO._text(d, o + TO.OFF_LOWER),
            "donor": donor_code, "donor_akey": donor_akey,
            "fields": _record_diff(d, o, offs[donor_code]),
        }
        out[entry["akey"]] = entry
    return out


def _expansion_family_folders(exp):
    """(folder prefixes, shared-folder file STEMS) for every club in `exp`.

    A staged texture lives under its asset's FOLDER (`Uniform/VGK/AWAY`), not under the .iff
    name, and one folder can host many assets — `Logos/` holds all 30+ team logos. So a club's
    own folders are matched by prefix, and shared folders by the asset's own primary file, the
    same split `revert_iffs_for_pack` makes in the other direction. By STEM, not full filename:
    an edited logo is normally `vgk.png` sitting next to (and outranking) the extracted `vgk.dds`.
    """
    _EX, ET = _expansion_modules()
    folders, files = set(), set()
    by_folder = {}
    try:
        for row in AT.load_catalog():
            by_folder.setdefault(AT.asset_iff(row["iff"]), set()).add(row["iff"])
    except Exception:
        pass
    for e in exp.values():
        for _cat, iff in ET.family_names((e.get("akey") or "").lower()):
            try:
                folder = AT.asset_iff(iff)
            except Exception:
                continue
            if len(by_folder.get(folder, ())) > 1:        # shared (Logos/) -> match by stem
                try:
                    files.add(Path(AT.texture_filename(iff, None).lower()).stem)
                except Exception:
                    pass
            else:
                folders.add(folder.lower() + "/")
    return folders, files


def expansion_texture_files(root, exp):
    """{rel: path} — every staged texture file belonging to these clubs' asset families."""
    folders, files = _expansion_family_folders(exp)
    pre = tuple(sorted(folders))
    out = {}
    for rel, p in load_textures(root).items():
        r = rel.lower().replace("\\", "/")
        if (pre and r.startswith(pre)) or Path(r).stem in files:
            out[rel] = p
    return out


def _expansion_label(akey, e):
    e = e or {}
    name = " ".join(x for x in (e.get("city"), e.get("nick")) if x) or akey
    n = len(e.get("fields") or {})
    return (f"{name} ({e.get('code', akey)}) — new club cloned from {e.get('donor', '?')}"
            f", plus its art assets" + (f"   [{n} edited field(s)]" if n else ""))


def _expansion_item(akey, e):
    return {"section": EXPANSION_KEY, "key": akey, "label": _expansion_label(akey, e),
            "team": e.get("code", akey), "category": "Expansion team"}


def local_expansion_items(ros_path):
    data = load_expansion(ros_path)
    return [_expansion_item(k, data[k]) for k in sorted(data)]


def diff_expansion(in_exp, ros_path):
    local = load_expansion(ros_path) if ros_path else {}
    items = []
    for k in sorted(in_exp):
        inc, loc = in_exp[k], local.get(k)
        status = "new" if loc is None else ("same" if loc == inc else "conflict")
        items.append({"section": EXPANSION_KEY, "key": k, "status": status,
                      "incoming": inc, "local": loc, "label": _expansion_label(k, inc),
                      "team": inc.get("code", k), "category": "Expansion team"})
    return items


def grow_audio_slots(game_dir, log=print):
    """Add the expansion speech-DB / cue slots. Idempotent — it no-ops once the ids are there.

    Split out of `apply_expansion` because of ORDER: the audio ids an expansion club hands out only
    exist on an install whose speech DB has been grown, and growing a wave bank RELOCATES it, which
    moves every physical address in it. Audio items are applied in the item loop, well before
    `apply_expansion` runs, so the growth has to happen first or the WAVs are keyed and written
    against a layout the import is about to invalidate.
    """
    try:
        from . import expansion_audio as EA
    except ImportError:
        import expansion_audio as EA
    try:
        log("  " + EA.apply(game_dir, log=log))
        return True
    except Exception as ex:
        log(f"  audio slots NOT added — {ex}")
        log("  the club will still work, but it will borrow another team's name call")
        return False


def apply_expansion(ros_path, game_dir, teams, log=print):
    """Build each incoming club in the recipient's files: roster record (+ players) first, then
    the asset family its key names.  Returns {'teams': n, 'assets': n}.

    A club whose code already exists is NOT rebuilt — only the author's field edits are written
    over it, so re-importing a pack, or importing two packs that share a club, is idempotent.
    """
    EX, ET = _expansion_modules()
    ros_path = Path(ros_path)

    # The audio ids `create_expansion_team` is about to hand out only exist on an install whose
    # speech DB has been grown, so grow the recipient's first.  It is a data edit through the
    # normal TOC path and it no-ops once the ids are there, so importing twice is harmless.
    if teams and game_dir and Path(game_dir).is_dir():
        grow_audio_slots(game_dir, log)      # no-ops if apply_items already ran it

    made = 0
    for akey in sorted(teams):
        e = teams[akey]
        code = (e.get("code") or akey).upper()
        d = ros_path.read_bytes()
        offs = _team_offsets(d)
        if code in offs:
            log(f"  {code}: already in this roster — updating its fields only")
        else:
            donor = (e.get("donor") or "").upper()
            if donor not in offs:
                log(f"  {code}: donor {donor or '?'} is not in this roster — skipped")
                continue
            cap = EX.capacity(ros_path)
            if not cap["records"]:
                log(f"  {code}: no spare created-team record left in this roster — skipped")
                continue
            try:
                EX.create_expansion_team(ros_path, code, e.get("city") or code,
                                         e.get("nick") or code, akey=e.get("akey") or code,
                                         lower=e.get("lower") or None, donor=donor, log=log)
            except Exception as ex:
                log(f"  {code}: could not be built — {ex}")
                continue
        # the author's edits on top of the clone (colours, record book, division, …)
        fields = e.get("fields") or {}
        if fields:
            buf = bytearray(ros_path.read_bytes())
            o = _team_offsets(buf).get(code)
            if o is not None:
                for f, v in fields.items():
                    struct.pack_into(">I", buf, o + int(f, 16), int(v) & 0xFFFFFFFF)
                ros_path.write_bytes(bytes(buf))
                log(f"  {code}: {len(fields)} edited field(s) written")
        made += 1

    assets = 0
    if made and game_dir and Path(game_dir).is_dir():
        for akey in sorted(teams):
            e = teams[akey]
            try:
                log(f"  {akey}: preparing art assets…")
                log("  " + ET.ensure_team_assets(game_dir, ros_path,
                                                 akey=(e.get("akey") or akey).lower(),
                                                 donor=(e.get("donor_akey") or "").lower() or None,
                                                 log=log))
                assets += 1
            except Exception as ex:
                log(f"  {akey}: asset preparation FAILED — {ex}")
    elif made:
        log("  expansion assets skipped: no game files folder — the club exists in the roster but "
            "owns no art yet (Roster Editor tab → Prepare expansion team assets…)")
    if made:
        # A freshly built club is its own farm club (+0xD4 points at itself) until the AHL table
        # is rebuilt — the one thing a recipe can't carry, since the affiliate is its own record.
        log("  note: run Roster Editor tab → rebuild AHL affiliates to give the new club(s) a farm team")
    return {"teams": made, "assets": assets}


# ── scoreclock (captured whole-mod state, re-applied onto the recipient's files) ──

SCORECLOCK_KEY = "scoreclock"
SCORECLOCK_LABEL = "Scoreclock — layout + Shots-on-Goal + hides + screen anchor"


def _scoreclock_modules():
    """(scorebug_layout, scorebug_anchors, scorebug_xex_rows) — imported lazily: they pull in the
    whole overlay/XEX stack, which most modpack operations never need."""
    try:
        from . import scorebug_layout as sbl
        from . import scorebug_anchors as sba
        from . import scorebug_xex_rows as sxr
    except ImportError:
        import scorebug_layout as sbl
        import scorebug_anchors as sba
        import scorebug_xex_rows as sxr
    return sbl, sba, sxr


def load_scoreclock(game_dir, log=print):
    """Capture the FULL scoreclock state from the game files as the pack's scoreclock.json dict.
    SLOW (~2 min: one DRAM decode via capture_overlay_state) — call from a worker thread.
    Returns {} when there is nothing to capture (missing/undecodable overlay_static.iff)."""
    sbl, sba, _sxr = _scoreclock_modules()
    game_dir = Path(game_dir)
    # overlay_static.iff is not a loose file — it lives inside the game archives, so existence
    # is a TOC lookup, not a filesystem check.
    try:
        if not AT.resolve(sbl.IFF, game_dir):
            return {}
    except Exception:
        return {}
    snap, shadow, teal = sbl.capture_overlay_state(str(game_dir))
    if not snap:
        return {}
    doc = {
        "snapshot": snap,                      # scorebug_layout format (color "#RRGGBB")
        "shadow_hidden": bool(shadow),
        "teal_hidden": bool(teal),
        "sog": sbl.snapshot_has_sog(snap),
    }
    try:
        x, y = sba.get_scorebug_anchor(str(game_dir / "default.xex"))
        doc["anchor"] = [int(x), int(y)]
    except Exception as e:                     # non-flat / TU'd XEX — ship the pack without it
        log(f"  scoreclock: anchor not captured ({e})")
    return doc


def scoreclock_export_item():
    """Picker row offered at export time. Deliberately does NO file reads (capture is a ~2min
    DRAM decode) — the state is captured later, inside the export worker, only if selected."""
    return {"section": "scoreclock", "key": SCORECLOCK_KEY, "label": SCORECLOCK_LABEL,
            "team": "", "category": "Scoreclock"}


def diff_scoreclock_item(in_sc):
    """Incoming-pack row for the import picker. Always status 'new': deciding same/conflict
    would cost a ~2min DRAM decode of the recipient's overlay during 'Reading Mod Pack…', so we
    present it as an opt-in overwrite instead (the label says so)."""
    n = len(in_sc.get("snapshot") or {})
    return {"section": "scoreclock", "key": SCORECLOCK_KEY, "status": "new", "incoming": in_sc,
            "local": None, "label": f"{SCORECLOCK_LABEL}  ({n} elements — replaces yours)",
            "team": "", "category": "Scoreclock"}


def scoreclock_preset_snapshot(sc):
    """Convert a pack snapshot (scorebug_layout format: color '#RRGGBB', no hidden flag) into
    the Scoreclock tab's stored-preset format ({'__abs__': {... color:[r,g,b], hidden}}), so the
    imported look can be saved as a named preset the recipient can reload/tweak."""
    out = {}
    for nm, a in (sc.get("snapshot") or {}).items():
        e = dict(a)
        if e.get("kind") == "text":
            c = str(e.get("color") or "#FFFFFF").lstrip("#")
            e["color"] = ([int(c[i:i + 2], 16) for i in (0, 2, 4)] if len(c) >= 6
                          else [255, 255, 255])
        e.setdefault("hidden", False)
        out[nm] = e
    return {"__abs__": out}


def apply_scoreclock(game_dir, sc, log=print):
    """Replay an incoming scoreclock section onto the recipient's game files. SLOW (~2-4 min:
    one DRAM decode + one encode) — call from a worker thread. The overlay rebuild re-adds the
    SOG records itself when the snapshot has them; the XEX side (SOG bind rows + anchor) is
    idempotent and sentinel-checked, so a wrong-version default.xex fails loudly per-patch while
    the overlay work still lands. Returns a one-line status; XEX failures are folded into it."""
    sbl, sba, sxr = _scoreclock_modules()
    game_dir = str(game_dir)
    snap = sc.get("snapshot") or {}
    if not snap:
        return "scoreclock: empty section — nothing applied"
    parts = [sbl.rebuild_overlay_from_snapshot(
        snap, bool(sc.get("shadow_hidden")), bool(sc.get("teal_hidden")), game_dir, log)]
    xex = str(Path(game_dir) / "default.xex")
    # A recipient's stock default.xex is usually LZX-compressed; every patcher below needs the
    # flat form. Auto-convert once (XexTool bundled in tools\) so end users do nothing.
    try:
        from . import xex_patch as _xp
    except ImportError:
        import xex_patch as _xp
    try:
        st = _xp.ensure_flat(xex, game_dir, log)
        if st != "already flat":
            parts.append(st)
    except Exception as e:
        parts.append(f"XEX flatten FAILED ({e})")
    if sc.get("sog"):
        try:
            parts.append("SOG XEX rows: " + sxr.apply(xex))
        except Exception as e:
            parts.append(f"SOG XEX rows FAILED ({e}) — is default.xex the stock v1.0 flat file?")
    anchor = sc.get("anchor")
    if anchor:
        try:
            n = sba.set_scorebug_anchor(xex, int(anchor[0]), int(anchor[1]), log)
            parts.append("anchor set (moves on next launch)" if n else "anchor already matches")
        except Exception as e:
            parts.append(f"anchor FAILED ({e})")
    return "; ".join(parts)


# ── portraits (the whole player-portrait pixel pack, shipped verbatim) ────────
#
# Unlike every other section this one ships RAW GAME BYTES: the complete disc_b9610aac.iff pixel
# pack (~65MB, all 1478 portraits). It can't ride in the textures section — portraits aren't files
# under Textures/Extracted, they are 0E4837 blobs inside the archive — and it deliberately doesn't
# ship 1478 PNGs to be re-encoded on the recipient's machine: re-encoding would rebuild every mip
# chain locally, so the result would only be approximately the author's. Shipping the finished
# asset makes an imported pack byte-identical to the author's, and the install is one relocate.
#
# All-or-nothing on purpose. There is no per-portrait row and no player→portrait ASSIGNMENT in the
# pack: assignments live in Roster.ROS (player record +0x1C), and portrait KEYS are only meaningful
# against the roster they were authored for. Recipients are expected to take the author's
# Roster.ROS alongside the portrait pack; a pack applied over an unrelated roster gives correct
# images at the wrong players, which is a roster problem, not a pack problem.

PORTRAITS_KEY = "portraits"
PORTRAITS_LABEL = "Player Portraits — the whole portrait pack (all 1478 faces)"
PORTRAITS_ARC = "portraits/" + AT.PORTRAIT_PACK_NAME


def _sha_stream(fh, chunk=1 << 20):
    h = hashlib.sha1()
    for c in iter(lambda: fh.read(chunk), b""):
        h.update(c)
    return h.hexdigest()


def portraits_export_item(game_dir=None):
    """Picker row offered at export time, or None when there is no portrait pack to export.
    Cheap: a TOC lookup, no 65MB read (that happens in the export worker if selected)."""
    if not game_dir:
        return None
    try:
        if not AT.resolve(AT.PORTRAIT_PACK_NAME, Path(game_dir)):
            return None
    except Exception:
        return None
    return {"section": "portraits", "key": PORTRAITS_KEY, "label": PORTRAITS_LABEL,
            "team": "", "category": "Portraits"}


def load_portraits(game_dir, log=print):
    """The current portrait pack's bytes, or b"" when it can't be read. ~65MB — worker thread."""
    try:
        data = AT.export_portrait_pack(Path(game_dir))
    except Exception as e:
        log(f"  portraits: not captured ({e})")
        return b""
    n = len(AT._portrait_pairs(data))
    if n != AT.PORTRAIT_COUNT:
        log(f"  portraits: pack walks to {n} portraits, expected {AT.PORTRAIT_COUNT} — section skipped")
        return b""
    log(f"  portraits: captured {n} portraits ({len(data)} bytes)")
    return data


def diff_portraits_item(z, game_dir=None):
    """Incoming-pack row for the import picker. 'same' when the recipient's pack is already
    byte-identical (re-importing is then a no-op), otherwise 'conflict' so it stays opt-in —
    this replaces ALL of their portraits, which is never something to do silently."""
    with z.open(PORTRAITS_ARC) as f:
        inc_h = _sha_stream(f)
    size = z.getinfo(PORTRAITS_ARC).file_size
    loc_h = None
    if game_dir:
        try:
            loc_h = _sha(AT.export_portrait_pack(Path(game_dir)))
        except Exception:
            loc_h = None
    status = "same" if (loc_h and loc_h == inc_h) else ("new" if loc_h is None else "conflict")
    note = ("identical to yours" if status == "same" else
            "REPLACES every portrait you have")
    return {"section": "portraits", "key": PORTRAITS_KEY, "status": status,
            "arc": PORTRAITS_ARC, "local": loc_h, "incoming": inc_h,
            "label": f"{PORTRAITS_LABEL}  ({size // (1 << 20)} MB — {note})",
            "team": "", "category": "Portraits"}


def apply_portraits(z, arc, game_dir, log=print):
    """Install the pack's portrait bytes over the recipient's. Validated + index-synced inside
    archive_textures.install_portrait_pack; nothing is written if the pack doesn't validate."""
    with z.open(arc) as f:
        data = f.read()
    return AT.install_portrait_pack(data, Path(game_dir), log)


def install_portraits_from_pack(pack_path, game_dir, log=print):
    """apply_portraits for a caller that has a pack PATH rather than an open zip (the GUI defers
    the install to its background finalize, after the texture pass's compact_1b)."""
    with zipfile.ZipFile(pack_path, "r") as z:
        return apply_portraits(z, PORTRAITS_ARC, game_dir, log)


def revert_portraits(game_dir, log=print):
    """Put the stock portraits back: reset the pixel pack AND portrait.iff (which holds the
    per-portrait read table — a stock pack under a modded table reads garbage)."""
    game_dir = Path(game_dir)
    n = 0
    for iff in (AT.PORTRAIT_PACK_NAME, "portrait.iff"):
        try:
            if AT.ensure_clean(iff, game_dir, log):
                n += 1
        except Exception as e:
            log(f"  ERROR reverting {iff}: {e}")
    return n


# ── player heads (a custom face: geometry + maps + the roster slot it is bound to) ────
#
# The unit shipped per head is the whole face the Head Editor authored:
#   * the GEOMETRY  — blob 0 of player_head_id_NNNN.iff, the reshaped vertex positions;
#   * the MAPS      — whichever of colour / normal / occlusion differ from the artist's;
#   * the BINDING   — which roster row wears it (+0xB2) and that row's eye colour.
# All three or it isn't a face: maps without geometry are Makar's skin on a stranger's skull, and
# geometry without the binding is a head nobody in the recipient's league is wearing.
#
# WHICH heads: exactly the ones whose bytes in the live archives differ from the pristine `.orig`
# bytes. That is the same "file-visible signature, no launcher config" rule the goalie-mask section
# uses, and it is honest in a way a registry of "heads I installed" could never be — it reads the
# game as it actually is, so a head installed by an older build, by a script, or by hand still
# exports, and one that was restored to stock stops exporting the moment it is. The scan is a
# chunked compare with an early exit over 447 assets (~440 MB, sequential, sorted by archive
# offset); measured at well under a second warm, and it is only ever run when a picker is opened.
#
# WHAT THE ID MEANS: a head id is an ASSET slot, identical on every install, so the pack keeps it.
# It is not portable — the geometry is a vertex-for-vertex replacement of THAT asset's mesh and
# `write_dram` is size-preserving, so the same bytes simply do not fit any other slot. Which makes
# a collision (the recipient has some other player wearing that id) a roster problem, not an asset
# problem, and it is solved on the roster: the squatters are moved onto free head slots so the
# imported face still belongs to exactly one player — the rule the whole head feature is built on.
#
# IDENTITY across rosters is the same descending-confidence chain as goalie masks, with one extra
# and much stronger link: player NAMES are readable off disk (the UTF-16BE name pool), so a head
# does not have to fall back to an ordinal the way a mask does.
#   row  ->  portrait (+0x1C) verifies it, and finds it when the row moved  ->  first+last name.

HEADS_KEY = "heads"
# Colour and normal are the two the head pipeline authors, and deliberately the only two shipped.
# The third surface, occlusion, DOES come back different from stock on an installed head — measured
# on head 3040: 56% of its texels move, up to 143 levels — but that is not authorship, it is the
# install re-encoding the whole VRAM blob and DXT1 not landing on the same block endpoints twice.
# (Control: an untouched head's occlusion decodes byte-identical live vs pristine, so the drift is
# the write, not the reader.) Shipping it would carry a generation of that loss into the recipient's
# file for no content — and their own install re-encodes it anyway. If a head tool ever really
# paints occlusion, add it here; until then the artist's AO stays the artist's on both sides.
HEAD_MAPS = ("color", "normal")


def _head_mods():
    """char_model / face_builder / face_shape, imported late.

    They pull in numpy and (through face_builder) the head-building stack, which is a lot of import
    for a module whose other five sections never touch it — and a launcher with no head work to
    share should not fail to open the Mod Pack tab because of it.
    """
    try:
        from . import char_model as C, face_builder as FB, face_shape as FS
    except ImportError:
        import char_model as C, face_builder as FB, face_shape as FS
    return C, FB, FS


def head_asset(head_id) -> str:
    return "player_head_id_%04d.iff" % int(head_id)


def edited_head_ids(game_dir=None, log=print):
    """Head ids whose live archive bytes differ from the pristine ones. See the section header.

    Compared in 256 KB chunks with an early exit, and the reads are issued in archive order so the
    two files are walked forwards rather than seeked at random.
    """
    C, _FB, _FS = _head_mods()
    try:
        d = AT._dir(game_dir)
        ids = C.head_ids(game_dir)
    except Exception as e:
        log(f"  heads: cannot scan ({e})")
        return []
    plan = []
    for i in ids:
        nm = head_asset(i)
        try:
            live = AT.resolve(nm, d, clean=False)
            pristine, from_clean = AT.resolve_clean(nm, d)
        except Exception:
            continue
        if live and pristine:
            plan.append((i, live, pristine, from_clean))
    plan.sort(key=lambda x: (x[1][0], x[1][1]))

    out, handles = [], {}
    try:
        for i, live, pristine, from_clean in plan:
            if live[2] != pristine[2]:                     # size changed -> certainly edited
                out.append(i)
                continue
            fl = handles.setdefault((live[0], False),
                                    open(AT._arc_file(d, live[0], clean=False), "rb"))
            fp = handles.setdefault((pristine[0], from_clean),
                                    open(AT._arc_file(d, pristine[0], clean=from_clean), "rb"))
            fl.seek(live[1])
            fp.seek(pristine[1])
            left, same = live[2], True
            while left > 0:
                n = min(left, 1 << 18)
                if fl.read(n) != fp.read(n):
                    same = False
                    break
                left -= n
            if not same:
                out.append(i)
    finally:
        for f in handles.values():
            try:
                f.close()
            except Exception:
                pass
    return sorted(out)


def _head_binding(t, head_id):
    """(row, entry-fields) for the roster row wearing `head_id`, or (None, {}) — see the header.

    A head the Head Editor authored is worn by exactly one row (it refuses to install onto a shared
    slot). Should some other tool have shared it anyway, the LOWEST row wins and the rest are named
    in `shared_with` so the label can say so rather than silently picking one.
    """
    if t is None:
        return None, {}
    rows = sorted(t.head_usage().get(int(head_id), []))
    if not rows:
        return None, {}
    row = rows[0]
    first, last = t.name(row)
    return row, {
        "row": row,
        "portrait": t.portrait(row),
        "first": first,
        "last": last,
        "eye": t.eye_color(row),
        "shared_with": rows[1:],
    }


def head_payload(head_id, game_dir, light=False):
    """({'geometry': bytes|None, 'maps': {label: png_bytes}}, {'geometry': sha|None, label: sha})

    `light` answers only "WHICH parts differ" — map values in the sha dict become placeholders and
    no PNG is encoded. That is all the export picker needs, and the PNG optimize pass is by far
    the most expensive step here, so the picker must not pay for it. Anything that ships or
    compares real bytes (diff on import, _collect_heads at pack-write time) uses the full mode.

    Only the parts that DIFFER from the artist's asset are carried. The Head Editor writes colour
    and normal and leaves occlusion alone, so a typical head ships two maps — but this asks the
    files rather than assuming, so a head built by some other route ships whatever it changed.
    """
    import io
    C, _FB, _FS = _head_mods()
    nm, blobs, shas = head_asset(head_id), {"geometry": None, "maps": {}}, {}

    try:
        live_dram = C.blob(True, game_dir, nm)
        stock_dram = C.blob(False, game_dir, nm)
    except Exception:
        live_dram = stock_dram = None
    if live_dram is not None and live_dram != stock_dram:
        blobs["geometry"] = live_dram
        shas["geometry"] = _sha(live_dram)

    try:
        recs = {r["label"]: r for r in AT.list_textures(nm, game_dir)}
    except Exception:
        recs = {}
    for label in HEAD_MAPS:
        rec = recs.get(label)
        if rec is None:
            continue
        try:
            now = AT.decode_record(nm, rec, game_dir, live=True)
            was = AT.decode_record(nm, rec, game_dir, live=False)
        except Exception:
            continue
        if now is None or was is None or now.tobytes() == was.tobytes():
            continue
        if light:
            shas[label] = "edited"                    # truthy placeholder — presence is the answer
            continue
        buf = io.BytesIO()
        now.convert("RGB").save(buf, "PNG", optimize=True)
        blobs["maps"][label] = buf.getvalue()
        shas[label] = _sha(blobs["maps"][label])
    return blobs, shas


def load_heads(game_dir, ros_path=None, ids=None, log=print):
    """{"<head_id>": entry} for every edited head — metadata plus the shas the diff compares on.

    `ids` limits the scan to a known set (the export path already knows which rows were ticked, so
    it never re-scans 447 assets). The payload bytes are NOT read here: this feeds a picker.
    """
    if not game_dir:
        return {}
    t = None
    if ros_path and Path(ros_path).is_file():
        try:
            t = PA.PlayerTable(Path(ros_path))
        except Exception as e:
            log(f"  heads: roster not readable ({e}) — heads will ship without a player binding")
    out = {}
    for hid in (sorted(int(i) for i in ids) if ids is not None
                else edited_head_ids(game_dir, log)):
        _row, bind = _head_binding(t, hid)
        # light: which parts differ is all a picker needs — _collect_heads re-reads the real
        # payload (bytes + shas) for the rows the user actually ticked, at pack-write time.
        _blobs, shas = head_payload(hid, game_dir, light=True)
        if not shas:                                  # bytes differ but nothing we can carry
            log(f"  heads: {head_asset(hid)} differs from stock but no geometry or map could be "
                f"read out of it — skipped")
            continue
        out[str(hid)] = {"head_id": int(hid), "sha": shas,
                         "maps": sorted(shas.keys() - {"geometry"}),
                         "geometry": shas.get("geometry") is not None, **bind}
    return out


def _head_who(e):
    who = (" ".join(x for x in (e.get("first"), e.get("last")) if x)).strip()
    return who or (f"portrait {e['portrait']}" if e.get("portrait") is not None else "no player")


def _head_label(e):
    parts = []
    if e.get("geometry"):
        parts.append("model")
    if e.get("maps"):
        parts.append(" + ".join(e["maps"]))
    what = ", ".join(parts) or "nothing"
    who = _head_who(e)
    tail = f" — worn by {who}" if e.get("row") is not None else " — no roster row wears it"
    return f"Head {e['head_id']} ({what}){tail}"


def head_export_items(game_dir, ros_path=None, log=print):
    """Picker rows for export: one per edited head, all checked by default (that IS the bulk
    option — leave them alone and every head you have edited goes in the pack)."""
    return [{"section": HEADS_KEY, "key": k, "label": _head_label(e), "team": "",
             "category": "Player head"}
            for k, e in sorted(load_heads(game_dir, ros_path, log=log).items(),
                               key=lambda kv: int(kv[0]))]


def local_head_items(game_dir, ros_path=None, log=print):
    return head_export_items(game_dir, ros_path, log)


# A map that has been through the game's texture pipeline once more than the pack's copy is not a
# different map. Installing re-encodes to DXT, and DXT does not land on the same block endpoints
# twice, so a head imported and then re-diffed against its own pack differs by a hair. Measured on
# head 3040: that re-encode moves the colour map by 0.10 levels on average (25 at the worst block),
# while a genuinely different head differs by 22.5 — better than two orders of magnitude of gap.
# A mean of 1 level sits deep inside it, so "same" means same face, and no threshold has to be
# right to within a factor of ten for that to hold.
HEAD_MAP_SAME_MEAN = 1.0


def _head_maps_same(z, key, entry, game_dir):
    """Is the recipient's installed head the same face as the pack's, allowing for re-encode noise?"""
    import io
    import numpy as np
    from PIL import Image
    nm = head_asset(int(entry.get("head_id", key)))
    try:
        recs = {r["label"]: r for r in AT.list_textures(nm, game_dir)}
    except Exception:
        return False
    for label in (entry.get("maps") or []):
        rec = recs.get(label)
        if rec is None:
            return False
        try:
            theirs = np.asarray(Image.open(io.BytesIO(z.read(f"heads/{key}/{label}.png")))
                                .convert("RGB"), np.int16)
            mine = np.asarray(AT.decode_record(nm, rec, game_dir, live=True).convert("RGB"), np.int16)
        except Exception:
            return False
        if mine.shape != theirs.shape:
            return False
        if float(np.abs(mine - theirs).mean()) > HEAD_MAP_SAME_MEAN:
            return False
    return True


def diff_heads(in_heads, game_dir=None, ros_path=None, z=None):
    """Incoming-pack rows. `same` when the recipient already wears this face (geometry byte for
    byte, maps within encoder noise), so re-importing a pack is a no-op; anything else is a
    conflict they opt into."""
    items = []
    for k in sorted(in_heads, key=lambda s: int(s)):
        inc = in_heads[k]
        loc = None
        if game_dir:
            try:
                _blobs, shas = head_payload(int(k), game_dir)
                loc = shas or None
            except Exception:
                loc = None
        if loc is None:
            status = "new"
        elif loc == inc.get("sha"):
            status = "same"                       # authored here, or an untouched copy of the pack
        elif (loc.get("geometry") == (inc.get("sha") or {}).get("geometry")
              and z is not None and _head_maps_same(z, k, inc, game_dir)):
            status = "same"                       # already imported once — see HEAD_MAP_SAME_MEAN
        else:
            status = "conflict"
        note = {"new": "not on your install",
                "same": "identical to yours",
                "conflict": "REPLACES the head you have in this slot"}[status]
        items.append({"section": HEADS_KEY, "key": k, "status": status, "incoming": inc,
                      "local": loc, "label": f"{_head_label(inc)}  [{note}]",
                      "team": "", "category": "Player head"})
    return items


def _resolve_head_row(t, want_row, portrait, first, last):
    """(row, note) — the recipient row this head belongs to, or (None, why-not).

    row -> portrait -> name, in descending confidence. Unlike a goalie mask there is no ordinal
    fallback and there should not be: a mask on the wrong goalie is a wrong colour, a face on the
    wrong player is a different man, so a head that cannot be pinned is left unbound rather than
    guessed onto somebody.
    """
    if want_row is None:
        return None, "the pack carries no roster row for this head"
    nrec = t.nrec
    if 0 <= want_row < nrec and (portrait is None or t.portrait(want_row) == portrait):
        return want_row, ""
    if portrait is not None:
        hits = t.find_rows_by_portrait_key(portrait)
        if len(hits) == 1:
            return hits[0], f"row {want_row} moved -> {hits[0]} (matched by portrait {portrait})"
        if len(hits) > 1:
            return hits[0], (f"row {want_row} moved -> {hits[0]} (portrait {portrait} is on "
                             f"{len(hits)} rows — took the first)")
    if first or last:
        hits = t.find_rows_by_name(first=first or None, last=last or None)
        if len(hits) == 1:
            return hits[0], f"row {want_row} moved -> {hits[0]} (matched by name)"
        if len(hits) > 1:
            return hits[0], (f"row {want_row} moved -> {hits[0]} ({len(hits)} rows are named "
                             f"{(first + ' ' + last).strip()} — took the first)")
    if 0 <= want_row < nrec:
        return want_row, f"row {want_row}: neither portrait nor name matches — applied by row anyway"
    return None, f"row {want_row} does not exist in this roster ({nrec} records)"


def _clear_head_slot(t, head_id, keep_row, game_dir, log):
    """Move every OTHER row wearing `head_id` onto a head nobody is using. Returns rows moved.

    The imported face is about to overwrite that asset, so anyone else pointing at it would wake up
    wearing it — the exact "one head, one player" breakage the feature exists to prevent. They are
    moved rather than the import being refused, because a free slot is a real shipped face: those
    players change appearance, which is a visible, reversible, honest outcome, and it is logged
    player by player so the recipient can see who was touched.
    """
    C, _FB, _FS = _head_mods()
    others = [r for r in t.head_usage().get(int(head_id), []) if r != keep_row]
    if not others:
        return 0
    free = sorted(set(C.head_ids(game_dir)) - set(t.head_usage()))
    moved = 0
    for r in others:
        if not free:
            log(f"  heads: row {r} also wears head {head_id} and there is no free head slot left "
                f"to move it to — it will share this face")
            continue
        hid = free.pop(0)
        t.set_head(r, hid, validate=False)
        who = (" ".join(t.name(r))).strip() or f"row {r}"
        log(f"  heads: {who} also wore head {head_id} — moved to free head {hid}")
        moved += 1
    return moved


def head_roster_choices(ros_path):
    """[(row, "Last, First — TEAM")] for every named row, for an importer's "who gets this face?"
    picker. Sorted by surname.

    Rows with no letters in the name are dropped — the shipped roster carries ~180 empty
    created-player slots whose names are a run of asterisks, and they sort straight to the top of an
    alphabetical list where they are the first thing the user sees and the last thing they want.
    Nobody can search for a name that has no letters, so they are unreachable in this dialog anyway.
    """
    t = PA.PlayerTable(Path(ros_path))
    out = []
    for r in range(t.nrec):
        first, last = t.name(r)
        if not any(c.isalpha() for c in first + last):
            continue
        code = t.team(r)[2]
        out.append((r, f"{last or '?'}, {first or '?'}" + (f"  —  {code}" if code else "")))
    out.sort(key=lambda p: p[1].lower())
    return out


def apply_heads(z, in_heads, decisions, game_dir, ros_path=None, log=print, targets=None):
    """Install each taken head: geometry, then maps, then the roster binding. Returns how many.

    Order matters. The geometry is written first because it is the write that can legitimately
    fail (a reshaped mesh has to re-compress into the slot it came from), and a failure there must
    leave the head untouched rather than half-Makar. The roster binding is written last, and only
    after the asset is really in the file, so a roster can never point at a face that isn't there.

    `targets` = {key: row} lets the importer say who gets the face on THEIR roster, and it beats
    the pack's own binding outright — no portrait or name matching is attempted for a key that
    appears here. That is the point: the pack's row/portrait/name chain is a good guess about a
    roster the author has never seen, and a person looking at their own roster is not guessing.
    A key mapped to None installs the art and leaves the roster alone.
    """
    C, FB, FS = _head_mods()
    from PIL import Image
    import io

    t = None
    if ros_path and Path(ros_path).is_file():
        try:
            t = PA.PlayerTable(Path(ros_path))
        except Exception as e:
            log(f"  heads: roster not readable ({e}) — installing the art, not the binding")

    done, touched_roster = 0, False
    for k in sorted(in_heads, key=lambda s: int(s)):
        e = in_heads[k]
        hid = int(e.get("head_id", k))
        who = _head_who(e)
        try:
            geom = z.read(f"heads/{k}/geometry.bin") if e.get("geometry") else None
            maps = {lab: Image.open(io.BytesIO(z.read(f"heads/{k}/{lab}.png"))).convert("RGB")
                    for lab in (e.get("maps") or [])}
        except KeyError as ke:
            log(f"  heads: {who} skipped — the pack is missing {ke}")
            continue

        row, note = (None, "")
        if t is not None and targets and k in targets:
            row = targets[k]
            if row is None:
                log(f"  heads: {who} — you chose not to assign this face; installing the art into "
                    f"head {hid} only")
            else:
                row = int(row)
                who = (" ".join(t.name(row))).strip() or f"row {row}"
        elif t is not None:
            row, note = _resolve_head_row(t, e.get("row"), e.get("portrait"),
                                          e.get("first"), e.get("last"))
            if row is None:
                log(f"  heads: {who} — {note}; installing the art into head {hid} anyway, "
                    f"assign it in the Roster Editor tab")
            elif note:
                log(f"  heads: {who} — {note}")

        try:
            if geom is not None:
                log(f"  {C.write(geom, Path(game_dir), log=log, asset=head_asset(hid))}")
            if maps:
                log(f"  {FB.install(hid, maps, Path(game_dir), log=log, only=tuple(maps))}")
        except Exception as ex:
            log(f"  heads: {who} FAILED — {ex}")
            continue

        if t is not None and row is not None:
            _clear_head_slot(t, hid, row, game_dir, log)
            try:
                t.set_head(row, hid, validate=True, game_dir=game_dir)
            except Exception as ex:                     # an added-asset id the validator rejects
                log(f"  heads: {who} — head id not accepted by the roster ({ex})")
            else:
                if e.get("eye") is not None:
                    t.set_eye_color(row, int(e["eye"]))
                log(f"  heads: {who} (row {row}) now wears head {hid}"
                    + (f", eyes {PA.EYE_COLORS[int(e['eye'])]}" if e.get("eye") is not None else ""))
                touched_roster = True
        done += 1

    if touched_roster:
        t.save()
    return done


def revert_heads(head_ids, game_dir, log=print):
    """Put the artist's head back for each id — the whole asset, byte for byte.

    One `ensure_clean` splice rather than "write the stock geometry back, then re-install the stock
    maps": re-installing maps RE-ENCODES them, and DXT does not land on the same block endpoints
    twice, so that route leaves a head that looks stock but no longer IS stock. That matters here
    more than anywhere else, because "differs from pristine" is precisely how this section decides a
    head was edited — a lossy restore would leave the head in the export picker forever, offering to
    ship the artist's face back as if it were the user's work.

    The roster binding is NOT undone: which face a player wears is a roster value, and the revert
    pass has the same "restore your own ROS backup" rule for every roster section.
    """
    n = 0
    for hid in sorted(int(i) for i in head_ids):
        try:
            if AT.ensure_clean(head_asset(hid), Path(game_dir), log):
                n += 1
            else:
                log(f"  head {hid} was already the artist's — nothing to revert")
        except Exception as e:
            log(f"  ERROR reverting head {hid}: {e}")
    return n


# ── custom collars (collar meshes: six rigs x five styles, across two archives) ────────
COLLARS_KEY = "collars"
COLLARS_LABEL = "Custom collars — collar meshes (Collar strings ON = laced, OFF = plain V)"
# The ROS uniform record's collar style (+0x18 bits 24-26, the editor's "Collar strings" box) picks
# the collar submesh out of the five each rig carries side by side: style n is rec REC0+n. All five
# are replaced on every rig: style 1 gets the laced shape, the other four get the plain V. 0 and 1
# are the styles the box exposes, but 2/3/4 are worn by 40 of the 407 roster uniform rows, so
# leaving them stock left the old collar on those uniforms.
#
# The five rigs are not five characters. global.iff carries the SAME skater four times: the
# gameplay body (0x1323AF4, head/helmet/gloves streamed separately) and three self-contained
# copies that pack those inline at falling vertex budgets -- 0x11F8040 (12,522 v) and the
# byte-identical low twins 0xF0AC60 / 0x12AC510 (7,081 v each). Those copies are what the bench,
# distant and MENU players draw, which is why replacing the gameplay body alone left the old
# collar on the front-end jersey-select screen. Every copy has its own five-collar set, and the
# copies' slots are much smaller than the body's (103..268 v against 141..372), so the ring is
# coarsened per slot on the way in -- replace_part can shrink a record but never grow it.
# The jersey-select screen does NOT draw any of those copies -- that was a wrong guess that cost
# two install rounds. Its jersey is a standalone garment mesh in frontend_sync.iff (0x171FC0), the
# only 3D mesh in that archive, with its own five collar styles as parts 6-10. They map one-for-one
# onto skater recs 16-20 (radial-profile chi-square 0.075/0.002/0.042/0.066/0.042, diagonal minimal
# in every row) and sit in the same coordinate space, so the same build feeds them. Because it is a
# different ARCHIVE, collar rigs are grouped by asset below and each archive is re-encoded once.
COLLAR_RIGS = ("skater", "goalie", "skater2", "skater3", "skater4", "frontend")
COLLAR_RECS = {"skater": (16, 17, 18, 19, 20), "goalie": (6, 7, 8, 9, 10),
               "skater2": (7, 8, 9, 10, 11), "skater3": (19, 20, 21, 22, 23),
               "skater4": (19, 20, 21, 22, 23), "frontend": (6, 7, 8, 9, 10)}


def _collar_asset(rig):
    """Which archive holds this rig. Old packs carry no per-rig asset and are all global.iff."""
    return _collar_mods().rig_asset(rig)


def _by_asset(entries):
    """Pack entries grouped by the archive they touch, so each is read/re-encoded once."""
    out = {}
    for e in entries:
        out.setdefault(e.get("asset") or _collar_asset(e.get("rig", "skater")), []).append(e)
    return out
COLLAR_STYLE = {"skater": {16: "skater plain V (Collar strings off)",
                           17: "skater laced (Collar strings on)",
                           18: "skater plain V (style 2)",
                           19: "skater plain V (style 3)",
                           20: "skater plain V (style 4)"},
                "goalie": {6: "goalie plain V (Collar strings off)",
                           7: "goalie laced (Collar strings on)",
                           8: "goalie plain V (style 2)",
                           9: "goalie plain V (style 3)",
                           10: "goalie plain V (style 4)"},
                "skater2": {r: "skater copy 1 " + ("laced (Collar strings on)" if r == 8
                            else "plain V" + (" (Collar strings off)" if r == 7
                                             else " (style %d)" % (r - 7))) for r in range(7, 12)},
                "skater3": {r: "skater copy 2 " + ("laced (Collar strings on)" if r == 20
                            else "plain V" + (" (Collar strings off)" if r == 19
                                             else " (style %d)" % (r - 19))) for r in range(19, 24)},
                "skater4": {r: "skater copy 3 " + ("laced (Collar strings on)" if r == 20
                            else "plain V" + (" (Collar strings off)" if r == 19
                                             else " (style %d)" % (r - 19))) for r in range(19, 24)},
                "frontend": {6: "front-end jersey plain V (Collar strings off)",
                             7: "front-end jersey laced (Collar strings on)",
                             8: "front-end jersey plain V (style 2)",
                             9: "front-end jersey plain V (style 3)",
                             10: "front-end jersey plain V (style 4)"}}


def _collar_mods():
    try:
        from . import char_model as C
    except ImportError:
        import char_model as C
    return C


def _collar_entries(c):
    """The pack's per-rig collar bodies. Packs written before the goalie collar existed carry the
    skater's fields flat at the top level with no "rigs" list, so they read as a single entry."""
    if not c:
        return []
    if "rigs" in c:
        return c["rigs"]
    e = dict(c)
    e.setdefault("rig", "skater")
    return [e]


def _collar_part_count(c):
    return sum(len(e.get("parts", {})) for e in _collar_entries(c))


def _rig_model(C, b, rig):
    asset = C.rig_asset(rig)
    m = C.rig_model(C.scan_models(b, asset=asset), rig)
    if m is None:
        raise ValueError(f"{asset}: {rig} model not found in blob 0")
    return m


def _part_rows(b, m, part):
    """(position rows, attribute rows, part-local index run) of one submesh — the raw bytes.

    Raw rather than decoded: the rows carry the skin weights / bone slots replace_part inherited
    by nearest, which an OBJ-shaped payload would have to re-derive on the other side. The model
    is the same on every install (one global.iff), so the rows are portable as they are. On the
    goalie the two streams are one interleaved stride-40 stream (pos_off == att_off), so pos and
    att come back as the same bytes twice — harmless, and it keeps one code path for both rigs."""
    import numpy as np
    lo, n = part["first_vtx"], part["n_vtx"]
    pos = bytes(b[m["pos_off"] + lo * m["pos_stride"]:m["pos_off"] + (lo + n) * m["pos_stride"]])
    att = bytes(b[m["att_off"] + lo * m["att_stride"]:m["att_off"] + (lo + n) * m["att_stride"]])
    ib = np.frombuffer(bytes(b[m["ib0"] + part["first_idx"] * 2:
                              m["ib0"] + (part["first_idx"] + part["n_idx"]) * 2]),
                       ">u2").astype(np.int64)
    idx = np.where(ib == 0xFFFF, 0xFFFF, ib - lo)
    return pos, att, idx


def _collar_state(b, rig):
    C = _collar_mods()
    m = _rig_model(C, b, rig)
    parts = {p["rec"]: p for p in C.submeshes(b, m)}
    st = {"b": b, "m": m, "rig": rig, "parts": {}}
    for rec in COLLAR_RECS[rig]:
        p = parts.get(rec)
        if p is not None:
            st["parts"][rec] = (p, _part_rows(b, m, p))
    return st


def _extra_rows(live, prist):
    """Vertices OUTSIDE the collar parts whose rows differ from stock — the back sponsor decal
    window the collar build re-homes under the collar. {vertex index: {"att": hex, "pos": hex}}."""
    import numpy as np
    m, b, m0, b0 = live["m"], live["b"], prist["m"], prist["b"]
    nv, pst, ast = m["nvtx"], m["pos_stride"], m["att_stride"]
    if (nv, pst, ast) != (m0["nvtx"], m0["pos_stride"], m0["att_stride"]):
        return {}
    A1 = np.frombuffer(b[m["att_off"]:m["att_off"] + nv * ast], np.uint8).reshape(nv, ast)
    A0 = np.frombuffer(b0[m0["att_off"]:m0["att_off"] + nv * ast], np.uint8).reshape(nv, ast)
    P1 = np.frombuffer(b[m["pos_off"]:m["pos_off"] + nv * pst], np.uint8).reshape(nv, pst)
    P0 = np.frombuffer(b0[m0["pos_off"]:m0["pos_off"] + nv * pst], np.uint8).reshape(nv, pst)
    da, dp = np.any(A1 != A0, axis=1), np.any(P1 != P0, axis=1)
    for _rec, (p, _rows) in live["parts"].items():
        da[p["first_vtx"]:p["first_vtx"] + p["n_vtx"]] = False
        dp[p["first_vtx"]:p["first_vtx"] + p["n_vtx"]] = False
    out = {}
    for i in np.nonzero(da | dp)[0]:
        e = {}
        if da[i]:
            e["att"] = A1[i].tobytes().hex()
        if dp[i]:
            e["pos"] = P1[i].tobytes().hex()
        out[str(int(i))] = e
    return out


def load_collars(game_dir, log=print):
    """The custom collar parts of every rig as they sit in the live game files, or {} when they
    are all stock. Each archive's two blobs are read once and every rig in it captured out of them
    (the front-end garment is in frontend_sync.iff, the player rigs in global.iff)."""
    import numpy as np
    C = _collar_mods()
    cache = {}

    def blobs(asset):
        if asset not in cache:
            b = C.blob(True, game_dir, asset=asset)
            try:
                b0 = C.blob(False, game_dir, asset=asset)
            except Exception as e:
                b0 = None
                log(f"  collars: pristine {asset} not readable ({e}) — shipping every collar part")
            cache[asset] = (b, b0)
        return cache[asset]

    rigs = []
    for rig in COLLAR_RIGS:
        asset = _collar_asset(rig)
        try:
            b, b0 = blobs(asset)
        except Exception as e:
            log(f"  collars: {asset} not readable ({e}) — {rig} skipped")
            continue
        try:
            live = _collar_state(b, rig)
        except ValueError as e:
            log(f"  collars: {e} — rig skipped")
            continue
        try:
            prist = _collar_state(b0, rig) if b0 is not None else None
        except ValueError:
            prist = None
        parts = {}
        for rec, (p, (pos, att, idx)) in live["parts"].items():
            if prist is not None and rec in prist["parts"]:
                _p0, (pos0, att0, idx0) = prist["parts"][rec]
                if pos == pos0 and att == att0 and np.array_equal(idx, idx0):
                    continue                                # stock collar — nothing to ship
            parts[str(rec)] = {"style": COLLAR_STYLE[rig].get(rec, ""), "n_vtx": p["n_vtx"],
                               "n_idx": p["n_idx"], "pos": pos.hex(), "att": att.hex(),
                               "idx": [int(v) for v in idx]}
        if not parts:
            continue
        m = live["m"]
        rigs.append({"rig": rig, "asset": asset, "nvtx": m["nvtx"],
                     "pos_stride": m["pos_stride"],
                     "att_stride": m["att_stride"], "parts": parts,
                     "extra": _extra_rows(live, prist) if prist is not None else {}})
    if not rigs:
        return {}
    body = {"asset": "global.iff", "rigs": rigs}      # top-level asset kept for older readers
    body["sha"] = _sha(json.dumps([{"rig": e["rig"], "asset": e["asset"], "parts": e["parts"],
                                    "extra": e["extra"]} for e in rigs], sort_keys=True).encode())
    return body


def _collars_label(c):
    styles = []
    for e in _collar_entries(c):
        rig = e.get("rig", "skater")
        for r in sorted(e.get("parts", {}), key=int):
            styles.append(COLLAR_STYLE.get(rig, {}).get(int(r), f"{rig} rec {r}"))
    return "Custom collars — " + ", ".join(styles) if styles else COLLARS_LABEL


def collars_export_item(game_dir, log=print):
    """A picker row when at least one collar part is custom, else None."""
    c = load_collars(game_dir, log)
    if not c:
        return None
    return {"section": COLLARS_KEY, "key": COLLARS_KEY, "label": _collars_label(c),
            "checked": True}


def _collars_same(in_c, game_dir):
    """Does the recipient's game already carry exactly these rows, on every rig?"""
    C = _collar_mods()
    for asset, entries in _by_asset(_collar_entries(in_c)).items():
        try:
            b = C.blob(True, game_dir, asset=asset)
        except Exception:
            return False
        for e in entries:
            rig = e.get("rig", "skater")
            try:
                live = _collar_state(b, rig)
            except ValueError:
                return False
            for rec_s, en in e.get("parts", {}).items():
                got = live["parts"].get(int(rec_s))
                if got is None:
                    return False
                _p, (pos, att, idx) = got
                if pos.hex() != en["pos"] or att.hex() != en["att"] or \
                        [int(v) for v in idx] != en["idx"]:
                    return False
            m = live["m"]
            for i_s, en in e.get("extra", {}).items():
                i = int(i_s)
                if "att" in en and b[m["att_off"] + i * m["att_stride"]:
                                   m["att_off"] + (i + 1) * m["att_stride"]].hex() != en["att"]:
                    return False
                if "pos" in en and b[m["pos_off"] + i * m["pos_stride"]:
                                   m["pos_off"] + (i + 1) * m["pos_stride"]].hex() != en["pos"]:
                    return False
    return True


def diff_collars_item(in_c, game_dir=None):
    status = "new"
    if game_dir:
        try:
            status = "same" if _collars_same(in_c, game_dir) else "new"
        except Exception:
            status = "new"
    return {"section": COLLARS_KEY, "key": COLLARS_KEY, "status": status, "incoming": in_c,
            "label": _collars_label(in_c)}


def _write_collar_rows(b, m, rig, parts_by_rec, in_parts, extra):
    import numpy as np
    for rec_s, e in in_parts.items():
        p = parts_by_rec.get(int(rec_s))
        if p is None:
            raise ValueError(f"{rig} model has no collar part {rec_s}")
        if (p["n_vtx"], p["n_idx"]) != (e["n_vtx"], e["n_idx"]):
            raise ValueError(f"{rig} collar part {rec_s}: slot is {p['n_vtx']} v / {p['n_idx']} "
                             f"idx, the pack's is {e['n_vtx']} / {e['n_idx']}")
        lo = p["first_vtx"]
        b[m["pos_off"] + lo * m["pos_stride"]:m["pos_off"] + (lo + p["n_vtx"]) * m["pos_stride"]] = \
            bytes.fromhex(e["pos"])
        b[m["att_off"] + lo * m["att_stride"]:m["att_off"] + (lo + p["n_vtx"]) * m["att_stride"]] = \
            bytes.fromhex(e["att"])
        idx = np.asarray(e["idx"], np.int64)
        stream = np.where(idx == 0xFFFF, 0xFFFF, idx + lo).astype(">u2")
        ibp = m["ib0"] + p["first_idx"] * 2
        b[ibp:ibp + p["n_idx"] * 2] = stream.tobytes()
    for i_s, e in (extra or {}).items():
        i = int(i_s)
        if "att" in e:
            b[m["att_off"] + i * m["att_stride"]:m["att_off"] + (i + 1) * m["att_stride"]] = \
                bytes.fromhex(e["att"])
        if "pos" in e:
            b[m["pos_off"] + i * m["pos_stride"]:m["pos_off"] + (i + 1) * m["pos_stride"]] = \
                bytes.fromhex(e["pos"])


def apply_collars(game_dir, in_c, log=print) -> str:
    """Write the pack's collar rows into the recipient's live game files and re-encode blob 0
    (slow: the encoder is pure Python over 23 MB — minutes). Rigs are grouped by ARCHIVE and every
    rig in one archive is written into a single buffer, so each archive's re-encode is paid once:
    global.iff for the five player rigs, frontend_sync.iff for the jersey-select garment."""
    C = _collar_mods()
    sts = []
    for asset, entries in _by_asset(_collar_entries(in_c)).items():
        b = bytearray(C.blob(True, game_dir, asset=asset))
        for e in entries:
            rig = e.get("rig", "skater")
            m = _rig_model(C, b, rig)
            if (m["nvtx"], m["pos_stride"], m["att_stride"]) != \
                    (e["nvtx"], e["pos_stride"], e["att_stride"]):
                raise ValueError(f"the pack's {rig} model layout does not match this {asset}")
            parts_by_rec = {p["rec"]: p for p in C.submeshes(b, m)}
            _write_collar_rows(b, m, rig, parts_by_rec, e["parts"], e.get("extra"))
        sts.append(C.write(bytes(b), game_dir, log, asset=asset))
    return (f"custom collars installed ({_collars_label(in_c)[len('Custom collars — '):]}) — "
            + "; ".join(sts))


def revert_collars(in_c, game_dir, log=print) -> str:
    """Stock rows back for every part / vertex the pack touched, on every rig it touched (NOT a
    whole-blob restore — the live global.iff is longer than the pristine one since the speech-DB
    growth). One re-encode per archive."""
    C = _collar_mods()
    sts = []
    for asset, entries in _by_asset(_collar_entries(in_c)).items():
        b = bytearray(C.blob(True, game_dir, asset=asset))
        b0 = C.blob(False, game_dir, asset=asset)
        for e in entries:
            rig = e.get("rig", "skater")
            prist = _collar_state(b0, rig)
            m = _rig_model(C, b, rig)
            m0 = prist["m"]
            parts_by_rec = {p["rec"]: p for p in C.submeshes(b, m)}
            stock = {}
            for rec_s in e.get("parts", {}):
                p0, (pos0, att0, idx0) = prist["parts"][int(rec_s)]
                stock[rec_s] = {"n_vtx": p0["n_vtx"], "n_idx": p0["n_idx"], "pos": pos0.hex(),
                                "att": att0.hex(), "idx": [int(v) for v in idx0]}
            extra = {}
            for i_s, en in e.get("extra", {}).items():
                i = int(i_s)
                r = {}
                if "att" in en:
                    r["att"] = b0[m0["att_off"] + i * m0["att_stride"]:
                                  m0["att_off"] + (i + 1) * m0["att_stride"]].hex()
                if "pos" in en:
                    r["pos"] = b0[m0["pos_off"] + i * m0["pos_stride"]:
                                  m0["pos_off"] + (i + 1) * m0["pos_stride"]].hex()
                extra[i_s] = r
            _write_collar_rows(b, m, rig, parts_by_rec, stock, extra)
        sts.append(C.write(bytes(b), game_dir, log, asset=asset))
    return "; ".join(sts)


# ── revert (undo what a pack applied, from the .orig backups) ─────────────────

def read_pack_contents(pack_path):
    """Cheap inventory of a pack: {'tex_rels': [...], 'audio_keys': [...], 'roster': bool,
    'scoreclock': dict|{}, 'portraits': bool, 'heads': [ids]} — what a revert (or a preview of one)
    needs to know."""
    out = {"tex_rels": [], "audio_keys": [], "roster": False, "scoreclock": {}, "portraits": False,
           "expansion": {}, "heads": [], "collars": {}}
    with zipfile.ZipFile(pack_path, "r") as z:
        names = set(z.namelist())
        out["roster"] = "roster.json" in names
        if "collars.json" in names:
            out["collars"] = json.loads(z.read("collars.json"))
        if "expansion.json" in names:
            out["expansion"] = json.loads(z.read("expansion.json"))
        if "heads.json" in names:
            out["heads"] = sorted(int(e.get("head_id", k))
                                  for k, e in json.loads(z.read("heads.json")).items())
        out["portraits"] = PORTRAITS_ARC in names
        cuemap = json.loads(z.read(CUEMAP_ARC)) if CUEMAP_ARC in names else {}
        if "scoreclock.json" in names:
            out["scoreclock"] = json.loads(z.read("scoreclock.json"))
        for n in sorted(names):
            if n.startswith("textures/") and len(n) > len("textures/") and not n.endswith("/"):
                out["tex_rels"].append(n[len("textures/"):])
            elif n.startswith("audio_wav/") and n.endswith(".wav"):
                parts = n.split("/")
                if len(parts) == 3:
                    try:
                        k = akey(parts[1], int(parts[2][:-4], 16))
                    except ValueError:
                        continue
                    # revert has to name the slots the APPLY touched, so re-key here too
                    out["audio_keys"].append(rekey_audio(k, cuemap)[0])
    return out


def revert_iffs_for_pack(tex_rels, scoreclock=False):
    """Which catalog assets a revert must reset, from the pack's texture paths. Folder -> iff via
    the catalog; a folder shared by MANY assets (e.g. Logos/) only reverts the assets whose OWN
    primary filename appears in the pack, so unrelated staged edits survive. Returns
    (iffs, skipped_rels). A scoreclock section always adds overlay_static.iff (its layout lives
    there even in a texture-less pack)."""
    folders = {}
    for rel in tex_rels:
        folders.setdefault(str(Path(rel).parent.as_posix()), set()).add(Path(rel).name.lower())
    by_folder = {}
    try:
        for row in AT.load_catalog():
            by_folder.setdefault(AT.asset_iff(row["iff"]), []).append(row["iff"])
    except Exception:
        pass
    iffs, skipped = set(), []
    for folder, basenames in folders.items():
        cands = by_folder.get(folder, [])
        if len(cands) == 1:
            iffs.add(cands[0])
        elif cands:                                    # shared folder -> match by primary filename
            matched = False
            for iff in cands:
                try:
                    if AT.texture_filename(iff, None).lower() in basenames:
                        iffs.add(iff); matched = True
                except Exception:
                    pass
            if not matched:
                skipped.append(folder)
        else:
            skipped.append(folder)
    if scoreclock:
        iffs.add("overlay_static.iff")
    return sorted(iffs), skipped


def _revert_audio_slot(game_dir, key, off2entry, log):
    """Copy a stream's original bytes (slot = packets*0x800 at its fixed offset) from <arc>.orig
    back over the live archive. Streams are only ever replaced IN PLACE, so the slot is stable.
    No-op when the archive has no .orig (never patched)."""
    fid, off = parse_akey(key)
    e = off2entry.get(key)
    if not e or not e.get("packets"):
        log(f"  audio {key}: not in catalog — slot unknown, skipped"); return False
    size = int(e["packets"]) * 0x800
    orig = Path(game_dir) / (fid + ".orig")
    live = Path(game_dir) / fid
    if not orig.is_file() or not live.is_file():
        return False                                    # archive never modified -> already original
    with open(orig, "rb") as f:
        f.seek(off); blob = f.read(size)
    with open(live, "r+b") as f:
        f.seek(off); f.write(blob)
    _tree_note(game_dir, fid, off, blob)
    log(f"  audio {e.get('friendly_name') or key}: original stream restored ({size} bytes)")
    return True


def revert_pack(root, game_dir, pack_path, log=print):
    """Undo what a pack applied, using the .orig backups:
      textures    -> per touched asset: reset the game archives to pristine (ensure_clean) AND
                     re-extract pristine files over the staged copies (revert_extract), so a later
                     Apply All doesn't just re-apply the pack. NOTE: per-ASSET granularity — the
                     recipient's own edits to the SAME asset are reverted too.
      audio       -> restore each stream's original bytes from <arc>.orig + delete the staged WAV.
      scoreclock  -> overlay reset (covered by the texture step) + surgical XEX undo (SOG rows
                     reverted, screen anchor back to stock Bottom-Left).
      portraits   -> pixel pack AND portrait.iff both reset to stock (the read table has to go
                     back with the pixels). Wipes the recipient's own portrait work too.
      roster      -> NOT revertible (field values overwrite in place; no .orig concept) — logged.
    Returns a counts dict. Slow when assets are large (each ensure_clean is an archive splice)."""
    root, game_dir = Path(root), Path(game_dir)
    inv = read_pack_contents(pack_path)
    counts = {"tex_assets": 0, "audio": 0, "scoreclock": 0, "portraits": 0, "heads": 0,
              "collars": 0, "notes": []}
    sc = inv["scoreclock"]

    if inv.get("collars"):
        log("  restoring the stock collar meshes (blob-0 re-encode per archive, a few minutes)…")
        try:
            log(f"  {revert_collars(inv['collars'], game_dir, log)}")
            counts["collars"] = 1
        except Exception as e:
            log(f"  ERROR reverting collars: {e}")
            counts["notes"].append(f"custom collars: revert FAILED ({e})")

    if inv.get("heads"):
        log(f"  restoring {len(inv['heads'])} stock head(s)…")
        counts["heads"] = revert_heads(inv["heads"], game_dir, log)
        counts["notes"].append("the players who wore those heads still point at those head ids — "
                               "restore your Roster.ROS backup to undo the assignment")

    if inv["portraits"]:
        log("  reverting portraits (whole pack -> stock)…")
        counts["portraits"] = revert_portraits(game_dir, log)
        if not counts["portraits"]:
            counts["notes"].append("portraits were already stock — nothing to revert")

    iffs, skipped = revert_iffs_for_pack(inv["tex_rels"], scoreclock=bool(sc))
    for folder in skipped:
        counts["notes"].append(f"couldn't map texture folder '{folder}' to an asset — not reverted")
        log(f"  texture folder '{folder}': no catalog match — skipped")
    for iff in iffs:
        try:
            log(f"  reverting {iff}…")
            AT.ensure_clean(iff, game_dir, log)         # archives -> pristine (.orig splice)
            try:
                recs = AT.list_textures(iff)
            except Exception:
                recs = None
            try:                                        # staged files -> pristine re-extract;
                # remove_png: a same-stem PNG outranks the recovered DDS everywhere, so it
                # must go or the revert looks like it didn't take in the launcher previews
                AT.revert_extract(root, iff, rec_list=recs or None, log=log, remove_png=True)
            except Exception as re_:
                log(f"  (staged-file revert skipped for {iff}: {re_})")
            counts["tex_assets"] += 1
        except Exception as e:
            log(f"  ERROR reverting {iff}: {e}")
            counts["notes"].append(f"{iff}: revert FAILED ({e})")

    if inv["audio_keys"]:
        _, off2entry = _catalog_index(root)
        man = AS.load_manifest(root)
        touched = False
        for key in inv["audio_keys"]:
            try:
                if _revert_audio_slot(game_dir, key, off2entry, log):
                    counts["audio"] += 1
                # The imported WAV overwrote the extracted one in place, so there is no staging
                # copy to throw away — drop the file and clear its hash instead. The entry then
                # reads as "not extracted" and Extract puts the restored original back.
                e = man.get(key)
                if e:
                    p = AS.wav_path(root, e)
                    if p.is_file():
                        p.unlink(); log(f"  removed imported {p.name}")
                    e["sha1"] = ""; e.pop("size", None); touched = True
            except Exception as e:
                log(f"  ERROR reverting audio {key}: {e}")
        if touched:
            AS.save_manifest(root, man)

    if sc:
        xex = str(game_dir / "default.xex")
        try:
            from . import scorebug_xex_rows as _sxr
            from . import scorebug_anchors as _sba
        except ImportError:
            import scorebug_xex_rows as _sxr
            import scorebug_anchors as _sba
        try:
            log(f"  SOG XEX rows: {_sxr.revert(xex)}")
        except Exception as e:
            log(f"  SOG XEX revert FAILED: {e}"); counts["notes"].append(f"SOG XEX revert: {e}")
        try:
            n = _sba.restore_stock(xex, modes=[_sba.SCOREBUG_MODE], log=log)
            log("  anchor: restored stock" if n else "  anchor: already stock")
        except Exception as e:
            log(f"  anchor revert FAILED: {e}"); counts["notes"].append(f"anchor revert: {e}")
        counts["scoreclock"] = 1

    if inv["roster"]:
        counts["notes"].append("roster values are NOT revertible from a pack — restore your own "
                               "Roster.ROS backup if needed")
        log("  roster: not revertible (in-place field values) — restore your own ROS backup")

    if inv.get("expansion"):
        who = ", ".join(sorted(inv["expansion"]))
        counts["notes"].append(f"expansion club(s) {who} are NOT removed by a revert — the record "
                               f"and its TOC entries stay; restore your Roster.ROS backup to drop "
                               f"the club")
        log(f"  expansion: {who} left in place (not revertible) — restore your ROS backup to remove")

    if iffs:
        try:
            log(f"  {AT.compact_1b(game_dir, log)}")
        except Exception as ce:
            log(f"  (compact skipped: {ce})")
    return counts


# ── export ────────────────────────────────────────────────────────────────────

def export_names(root, out_path, author=""):
    meta = load_audio_meta(root)
    doc = {
        "format": FORMAT + "-names",
        "version": VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "author": author,
        "audio_meta": meta
    }
    Path(out_path).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return {"audio_meta": len(meta)}

def _tex_catalog_map():
    m = {}
    try:
        for row in AT.load_catalog():
            m[AT.asset_iff(row["iff"])] = (row.get("team", "") or "", row.get("category", "") or "")
    except Exception:
        pass
    return m

def annotate(items, root, meta=None, off2entry=None):
    root = Path(root)
    texmap = _tex_catalog_map()
    meta = load_audio_meta(root) if meta is None else meta
    if off2entry is None:
        _, off2entry = _catalog_index(root)
    for it in items:
        if it.get("section") == "roster":
            it.setdefault("team", "")
            it["category"] = "Roster"
        elif it.get("section") == "scoreclock":
            it.setdefault("team", "")
            it["category"] = "Scoreclock"
        elif it.get("section") == "portraits":
            it.setdefault("team", "")
            it["category"] = "Portraits"
        elif it.get("section") == HEADS_KEY:
            it.setdefault("team", "")
            it["category"] = "Player head"
        elif it.get("section") == COLLARS_KEY:
            it.setdefault("team", "")
            it["category"] = "Custom collars"
        elif it.get("section") == EXPANSION_KEY:
            inc = it.get("incoming") or {}
            it["team"] = it.get("team") or inc.get("code") or str(it.get("key") or "")
            it["category"] = "Expansion team"
        elif it.get("section") == "tex":
            team, cat = texmap.get(str(it["key"]).split("/")[0], ("", ""))
            it["team"], it["category"] = team, cat
        else:
            inc = it.get("incoming") or {}
            e = meta.get(it["key"], {})
            ce = off2entry.get(it["key"], {})
            it["team"] = (it.get("team") or inc.get("team") or e.get("team") or ce.get("team") or "")
            it["category"] = (it.get("category") or inc.get("category") or e.get("category") or ce.get("category") or "")
    return items

def local_items(root, fast=False):
    """`fast` = picker mode: cheap size+mtime edit detection for the WAVs (see load_audio_wavs)
    and one manifest/names parse shared across the whole build, instead of three."""
    root = Path(root)
    manifest = AS.load_manifest(root)
    _, off2entry = _catalog_index(root, manifest=manifest)
    meta = load_audio_meta(root)
    items = []
    for key in sorted(meta):
        e = off2entry.get(key, {})
        items.append({"section": "meta", "key": key, "label": e.get("friendly_name") or e.get("stem") or key})
    for key in sorted(load_audio_wavs(root, fast=fast, manifest=manifest)):
        e = off2entry.get(key, {})
        items.append({"section": "audio", "key": key, "label": (e.get("friendly_name") or e.get("stem") or key) + ".wav"})
    for rel in sorted(load_textures(root)):
        items.append({"section": "tex", "key": rel, "label": rel})
    return annotate(items, root, meta=meta, off2entry=off2entry)

def _write_pack(out_path, meta, wavs, texs, roster=None, scoreclock=None, author="",
                portraits=b"", expansion=None, heads=None, collars=None):
    """`heads`: {"<id>": (entry, {"geometry": bytes|None, "maps": {label: png_bytes}})}."""
    expansion = expansion or {}
    collars = collars or {}
    roster = roster or {}
    scoreclock = scoreclock or {}
    portraits = portraits or b""
    heads = heads or {}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        z.writestr("manifest.json", json.dumps({
            "format": FORMAT,
            "version": VERSION,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "author": author,
            "summary": {
                "audio_meta": len(meta),
                "audio_wav": len(wavs),
                "textures": len(texs),
                "roster": len(roster),
                "scoreclock": 1 if scoreclock else 0,
                "portraits": AT.PORTRAIT_COUNT if portraits else 0,
                "expansion": len(expansion),
                "heads": len(heads),
                "collars": _collar_part_count(collars)
            },
        }, indent=1))
        if meta:
            z.writestr("audio_meta.json", json.dumps(meta, indent=1))
        cuemap = {}
        for key, p in wavs.items():
            if Path(p).exists():
                fid, off = parse_akey(key)
                z.write(p, f"audio_wav/{fid}/{off:08X}.wav")
                ck = cue_key(key)
                if ck:
                    cuemap[key] = ck
        if cuemap:
            z.writestr(CUEMAP_ARC, json.dumps(cuemap, indent=1, sort_keys=True))
        for rel, p in texs.items():
            if Path(p).exists():
                z.write(p, f"textures/{rel}")
        if expansion:
            z.writestr("expansion.json", json.dumps(expansion, indent=1))
        if roster:
            z.writestr("roster.json", json.dumps(roster, indent=1))
        if scoreclock:
            z.writestr("scoreclock.json", json.dumps(scoreclock, indent=1))
        if collars:
            z.writestr("collars.json", json.dumps(collars))
        if heads:
            z.writestr("heads.json", json.dumps({k: v[0] for k, v in heads.items()}, indent=1))
            for k, (_entry, blobs) in heads.items():
                if blobs.get("geometry") is not None:
                    # The DECOMPRESSED blob 0 (~230 KB), deflated by the zip: it has to go in
                    # decompressed because the recipient's copy is re-encoded on their side, into
                    # their slot, by the same writer the editor uses.
                    z.writestr(f"heads/{k}/geometry.bin", blobs["geometry"])
                for label, png in (blobs.get("maps") or {}).items():
                    z.writestr(zipfile.ZipInfo(f"heads/{k}/{label}.png"), png,
                               compress_type=zipfile.ZIP_STORED)   # PNG is already deflated
        if portraits:
            # STORED, not deflated: the blobs inside are already 0E4837-compressed, so deflate
            # burns ~30s of CPU on both ends for well under 1% — and stored members import by
            # a plain read.
            z.writestr(zipfile.ZipInfo(PORTRAITS_ARC), portraits,
                       compress_type=zipfile.ZIP_STORED)
    return {"audio_meta": len(meta), "audio_wav": len(wavs), "textures": len(texs),
            "roster": len(roster), "scoreclock": 1 if scoreclock else 0,
            "portraits": AT.PORTRAIT_COUNT if portraits else 0, "expansion": len(expansion),
            "heads": len(heads), "collars": _collar_part_count(collars)}

def export_pack(root, out_path, include=("meta", "audio", "tex"), ros_path=None,
                game_dir=None, author="", log=print):
    root = Path(root)
    meta = load_audio_meta(root)  if "meta"  in include else {}
    wavs = load_audio_wavs(root)  if "audio" in include else {}
    texs = load_textures(root)    if "tex"   in include else {}
    roster = load_roster(ros_path) if ("roster" in include and ros_path) else {}
    sc = load_scoreclock(game_dir, log) if ("scoreclock" in include and game_dir) else {}
    por = load_portraits(game_dir, log) if ("portraits" in include and game_dir) else b""
    exp = load_expansion(ros_path) if (EXPANSION_KEY in include and ros_path) else {}
    heads = (_collect_heads(load_heads(game_dir, ros_path, log=log), game_dir, log)
             if (HEADS_KEY in include and game_dir) else {})
    col = load_collars(game_dir, log) if (COLLARS_KEY in include and game_dir) else {}
    return _write_pack(out_path, meta, wavs, texs, roster, sc, author, por, exp, heads, col)


def _collect_heads(entries, game_dir, log=print):
    """entries -> the {key: (entry, blobs)} shape `_write_pack` wants, reading the payload bytes."""
    out = {}
    for k, e in sorted(entries.items(), key=lambda kv: int(kv[0])):
        blobs, shas = head_payload(int(k), game_dir)
        if not shas:
            log(f"  heads: head {k} had nothing left to ship (restored to stock?) — skipped")
            continue
        e = {**e, "sha": shas, "maps": sorted(shas.keys() - {"geometry"}),
             "geometry": shas.get("geometry") is not None}
        out[k] = (e, blobs)
        log(f"  heads: packing {_head_label(e)}")
    return out

def export_selected(root, out_path, selected, ros_path=None, game_dir=None, author="", log=print):
    root = Path(root)
    sel = {(s, k) for s, k in selected}
    meta = ({k: v for k, v in load_audio_meta(root).items() if ("meta", k) in sel}
            if any(s == "meta" for s, _ in sel) else {})
    # The ticked keys ARE the answer to "which WAVs" — resolve them straight off the manifest
    # instead of re-running the edit sweep over the whole extract tree.
    sel_audio = {k for s, k in sel if s == "audio"}
    wavs = {}
    if sel_audio:
        man = AS.load_manifest(root)
        wavs = {k: AS.wav_path(root, man[k]) for k in sel_audio if k in man}
    all_texs = load_textures(root)
    texs = {k: v for k, v in all_texs.items() if ("tex", k) in sel}
    roster = {}
    if any(s == "roster" for s, _ in sel) and ros_path:
        full_roster = load_roster(ros_path)
        roster = {g: d for g, d in full_roster.items() if ("roster", g) in sel}
        if GOALIE_MASKS_KEY in roster:
            # Self-contained goalie masks: the record only NAMES a mask; the paint job is the
            # texture. Force-include every mask asset the shipped goalies point at, the same way
            # the scoreclock section drags its overlay_static art along.
            folders = goalie_mask_tex_folders(roster[GOALIE_MASKS_KEY])
            extra = {rel: p for rel, p in all_texs.items()
                     if rel.lower().startswith(tuple(f + "/" for f in folders)) and rel not in texs}
            if extra:
                log(f"  goalie masks: auto-including {len(extra)} mask texture file(s) "
                    f"from {len(folders)} mask asset(s)")
                texs.update(extra)
            else:
                log(f"  goalie masks: no extracted texture files found for {len(folders)} mask "
                    f"asset(s) — extract them in the Textures tab if the paint should ride along")
    sc = {}
    if ("scoreclock", SCORECLOCK_KEY) in sel and game_dir:
        log("  capturing scoreclock state (~2 min)…")
        sc = load_scoreclock(game_dir, log)
        if not sc:
            log("  scoreclock: nothing captured — section skipped")
        else:
            # Self-contained scoreclock: force-include the overlay_static texture files (the
            # scorebug art) even when their rows weren't individually selected — the layout
            # without its textures is only half the mod.
            extra = {rel: p for rel, p in all_texs.items()
                     if rel.split("/")[0].lower() == "overlay_static.iff" and rel not in texs}
            if extra:
                log(f"  scoreclock: auto-including {len(extra)} overlay_static texture file(s)")
                texs.update(extra)
    por = b""
    if ("portraits", PORTRAITS_KEY) in sel and game_dir:
        log("  capturing the portrait pack (~65 MB)…")
        por = load_portraits(game_dir, log)
    exp = {}
    if any(s == EXPANSION_KEY for s, _ in sel) and ros_path:
        exp = {k: v for k, v in load_expansion(ros_path).items() if (EXPANSION_KEY, k) in sel}
        if exp:
            # Self-contained club: the recipe builds a club that looks like the DONOR. The art
            # that makes it Seattle is its texture files, so force-include every staged file
            # belonging to the club's asset family, the way goalie masks drag their paint along.
            extra = {rel: p for rel, p in expansion_texture_files(root, exp).items()
                     if rel not in texs}
            if extra:
                log(f"  expansion: auto-including {len(extra)} texture file(s) for "
                    f"{len(exp)} club(s)")
                texs.update(extra)
            else:
                log(f"  expansion: no extracted texture files found for {len(exp)} club(s) — "
                    f"extract/edit them in the Textures tab if the art should ride along")
    heads = {}
    head_keys = {k for s, k in sel if s == HEADS_KEY}
    if head_keys and game_dir:
        # `ids=head_keys` so the 447-asset scan the picker already did is not repeated — the ticked
        # rows ARE the answer to "which heads are edited", filtered by the user.
        heads = _collect_heads(load_heads(game_dir, ros_path, ids=head_keys, log=log),
                               game_dir, log)
    elif head_keys:
        log("  heads skipped: no game files folder set (Settings tab)")
    col = {}
    if (COLLARS_KEY, COLLARS_KEY) in sel and game_dir:
        log("  capturing the custom collar meshes…")
        col = load_collars(game_dir, log)
        if not col:
            log("  collars: every collar part is stock — section skipped")
    return _write_pack(out_path, meta, wavs, texs, roster, sc, author, por, exp, heads, col)


# ── diff (incoming vs local) ──────────────────────────────────────────────────

def _meta_items(in_meta, local_meta, off2entry, cuemap=None):
    items = []
    for key, inc in in_meta.items():
        # Audio meta is keyed by akey too, so it needs the same portability treatment as the WAVs
        # — otherwise a re-keyed clip lands in the right slot carrying the wrong name.
        key = rekey_audio(key, cuemap)[0]
        loc = local_meta.get(key)
        status = "new" if loc is None else ("same" if loc == inc else "conflict")
        e = off2entry.get(key, {})
        items.append({
            "section": "meta",
            "key": key,
            "status": status,
            "incoming": inc,
            "local": loc,
            "label": e.get("friendly_name") or e.get("stem") or key
        })
    return items

def diff_names(json_path, root):
    doc = _load_json(json_path)
    in_meta = doc.get("audio_meta", doc if "format" not in doc else {})
    _, off2entry = _catalog_index(root)
    return doc.get("manifest", doc), _meta_items(in_meta, load_audio_meta(root), off2entry)

def diff_pack(zip_path, root, ros_path=None, game_dir=None):
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"'{zip_path.name}' is not a valid zip archive (.n2kpack).")
    root = Path(root)
    local_meta = load_audio_meta(root)
    # fast: this map only says "the recipient has an edited WAV at this key" for the status
    # column — the pack members themselves are what gets hashed, a few streams not 80k.
    local_wavs = load_audio_wavs(root, fast=True)
    local_texs = load_textures(root)
    _, off2entry = _catalog_index(root)
    items = []

    with zipfile.ZipFile(zip_path, "r") as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
        in_meta = json.loads(z.read("audio_meta.json")) if "audio_meta.json" in names else {}
        in_roster = json.loads(z.read("roster.json")) if "roster.json" in names else {}
        in_sc = json.loads(z.read("scoreclock.json")) if "scoreclock.json" in names else {}
        in_exp = json.loads(z.read("expansion.json")) if "expansion.json" in names else {}
        in_heads = json.loads(z.read("heads.json")) if "heads.json" in names else {}
        in_col = json.loads(z.read("collars.json")) if "collars.json" in names else {}
        cuemap = json.loads(z.read(CUEMAP_ARC)) if CUEMAP_ARC in names else {}

        items += _meta_items(in_meta, local_meta, off2entry, cuemap)
        items += diff_expansion(in_exp, ros_path)
        items += diff_heads(in_heads, game_dir, ros_path, z)
        items += diff_roster(in_roster, ros_path)
        if in_sc.get("snapshot"):
            items.append(diff_scoreclock_item(in_sc))
        if PORTRAITS_ARC in names:
            items.append(diff_portraits_item(z, game_dir))
        if _collar_part_count(in_col):
            items.append(diff_collars_item(in_col, game_dir))

        for n in sorted(names):
            if n.startswith("audio_wav/") and n.endswith(".wav"):
                parts = n.split("/")
                if len(parts) != 3:
                    continue
                try:
                    key = akey(parts[1], int(parts[2][:-4], 16))
                except ValueError:
                    continue
                # Re-key BEFORE the local lookups: "does this conflict with what I already have"
                # is a question about the recipient's slot, not the sender's address.
                orig_key = key
                key, note = rekey_audio(key, cuemap)
                inc_h = _sha(z.read(n))
                locp = local_wavs.get(key)
                loc_h = _sha_file(locp) if locp else None
                status = "new" if locp is None else ("same" if loc_h == inc_h else "conflict")
                e = off2entry.get(key, {})
                items.append({
                    "section": "audio", "key": key, "status": status,
                    "arc": n, "zip": str(zip_path), "local": str(locp) if locp else None,
                    "label": e.get("friendly_name") or e.get("stem") or key,
                    # `cue` is carried so apply can resolve it AGAIN: an import that also adds an
                    # expansion club grows the speech DB and relocates banks between diff and
                    # apply, and the address that was right when the pack was read is not the
                    # address that is right when it is written.
                    "cue": cuemap.get(orig_key), "rekeyed": note,
                })

            elif n.startswith("textures/"):
                rel = n[len("textures/"):]
                if not rel:
                    continue
                inc_h = _sha(z.read(n))
                locp = local_texs.get(rel)
                loc_h = _sha_file(locp) if locp else None
                status = "new" if locp is None else ("same" if loc_h == inc_h else "conflict")
                items.append({
                    "section": "tex", "key": rel, "status": status,
                    "arc": n, "zip": str(zip_path), "local": str(locp) if locp else None,
                    "label": rel
                })

    return manifest, items

def extract_member(zip_path, arc, dest):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z, z.open(arc) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    return dest


# ── apply ─────────────────────────────────────────────────────────────────────

def _should_take(item, decisions):
    if item["status"] == "new":
        return True
    if item["status"] == "conflict":
        return decisions.get(f'{item["section"]}|{item["key"]}') == "theirs"
    return False

def _write_meta(root, meta):
    """meta: akey -> {field: value}, merged into the single Audio_Names.json."""
    AS.update_names(root, meta)

def _apply_wav(z, item, root, off2entry, log):
    """Write an imported WAV straight over the extracted one.

    There is no Modified/ staging tree any more; the extracted WAV *is* the editable copy, and
    the manifest's pristine sha1 is what makes the import show up as an edit. A stream the
    recipient hasn't extracted has no path to overwrite, so it lands in _imported/ and gets
    picked up once they extract that bank.
    """
    # Resolve the portable key against the layout as it is NOW, not as it was at diff time —
    # see the `cue` note in diff_pack. No cue key, or one that no longer places, keeps the diff's
    # answer, which is the pre-existing behaviour.
    key = cue_key_akey(item.get("cue")) or item["key"]
    if key != item["key"]:
        log(f"  audio  re-keyed again at apply: {item['key']} -> {key} ({item['cue']})")
    item = dict(item, key=key)

    e = off2entry.get(item["key"])
    if e:
        dest = AS.wav_path(root, e)
        unresolved = ""
    else:
        fid, off = parse_akey(item["key"])
        dest = AS.extracted_root(root) / "_imported" / f"{fid}_{off:08X}.wav"
        # Two very different reasons land here, and only one of them resolves itself later:
        #   * the recipient simply hasn't extracted that bank yet -> extracting picks this up.
        #   * the akey names an offset their install does not have. An akey is a PHYSICAL address,
        #     and a wave bank that has been grown gets RELOCATED (see project_relocated_bank_keys),
        #     so a pack built here against lines_ts.bin in 1C addresses bytes that on a stock or
        #     differently-patched copy are a different clip, or nothing at all. That WAV can never
        #     be matched by extracting, so say so instead of leaving a file in _imported/ that
        #     looks like it will sort itself out.
        unresolved = ("  (their install has no stream at this address — if this came from a "
                      "relocated bank it will NOT match after extracting)")
        if not cue_key(item["key"]):
            unresolved += " and it has no shipped cue index to re-key it by"

    dest.parent.mkdir(parents=True, exist_ok=True)
    with z.open(item["arc"]) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    if item.get("rekeyed"):
        log(f"  audio  {item['rekeyed']}")
    log(f"  audio  -> Audio/Extracted/{AS.rel_wav(root, dest)}{unresolved}")

def _apply_tex(z, item, root, log):
    extracted_base = AT.extracted_root(root)
    dest = extracted_base / item["key"]
    if not _is_safe_path(extracted_base, dest):
        log(f"  texture -> SKIPPED unsafe path traversal attempt: {item['key']}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with z.open(item["arc"]) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    log(f"  texture-> Textures/Extracted/{item['key']}")

def apply_items(root, items, decisions, zip_path=None, ros_path=None, game_dir=None, log=print,
                head_targets=None):
    """`head_targets` = {head key: row|None} — see `apply_heads`; it overrides the pack's binding.

    game_dir enables the COLLARS section (slow: a blob-0 re-encode per touched archive, minutes) and
    the SCORECLOCK section (slow: ~2-4 min DRAM rebuild — run the whole call
    in a worker thread when items may include it) and the PORTRAITS section (~65MB read + one
    archive relocate, a few seconds). game_dir=None skips both with a log line, so a GUI can strip
    scoreclock rows out and replay them itself on a background thread."""
    root = Path(root)
    counts = {"meta": 0, "audio": 0, "tex": 0, "roster": 0, "scoreclock": 0, "portraits": 0,
              "expansion": 0, "heads": 0, "collars": 0, "skipped": 0}

    # Cue slots BEFORE audio. Growing the speech DB relocates wave banks, and a relocated bank's
    # streams change physical address — so both the catalog index the WAVs are written through and
    # the live layout the (bank, cue) re-keying resolves against have to be read AFTER the growth,
    # not before it. `apply_expansion` calls this again later; it no-ops.
    if game_dir and Path(game_dir).is_dir() and any(
            it["section"] == EXPANSION_KEY and it["status"] != "same" and _should_take(it, decisions)
            for it in items):
        if grow_audio_slots(game_dir, log):
            try:
                try:
                    from . import wave_banks as WB
                except ImportError:
                    import wave_banks as WB
                WB.set_live_layout(game_dir, log=log)
            except Exception as ex:
                log(f"  live wave-bank layout not re-read after growing slots ({ex})")

    _, off2entry = _catalog_index(root)
    meta_writes = {}
    roster_groups = {}
    expansion_teams = {}
    heads = {}

    z = zipfile.ZipFile(zip_path, "r") if zip_path else None
    try:
        for it in items:
            if it["status"] == "same":
                continue
            if not _should_take(it, decisions):
                counts["skipped"] += 1
                continue
            if it["section"] == "meta":
                meta_writes[it["key"]] = it["incoming"]
                counts["meta"] += 1
            elif it["section"] == "audio" and z is not None:
                _apply_wav(z, it, root, off2entry, log)
                counts["audio"] += 1
            elif it["section"] == "tex" and z is not None:
                _apply_tex(z, it, root, log)
                counts["tex"] += 1
            elif it["section"] == EXPANSION_KEY:
                if ros_path and Path(ros_path).is_file():
                    expansion_teams[it["key"]] = it["incoming"]
                else:
                    log("  expansion team skipped: no Roster.ROS path set (Roster Editor tab → Browse…)")
                    counts["skipped"] += 1
            elif it["section"] == HEADS_KEY:
                if game_dir and Path(game_dir).is_dir():
                    heads[it["key"]] = it["incoming"]
                else:
                    log("  head skipped: no game files folder (Settings tab)")
                    counts["skipped"] += 1
            elif it["section"] == "roster":
                if ros_path and Path(ros_path).is_file():
                    roster_groups[it["key"]] = it["incoming"]
                    counts["roster"] += 1
                else:
                    log("  roster skipped: no Roster.ROS path set (Roster Editor tab → Browse…)")
                    counts["skipped"] += 1
            elif it["section"] == "portraits" and z is not None:
                if game_dir and Path(game_dir).is_dir():
                    log("  installing the portrait pack (~65 MB)…")
                    log("  " + apply_portraits(z, it["arc"], game_dir, log))
                    counts["portraits"] += 1
                else:
                    log("  portraits skipped: no game files folder")
                    counts["skipped"] += 1
            elif it["section"] == COLLARS_KEY:
                if game_dir and Path(game_dir).is_dir():
                    log("  installing the custom collars (blob-0 re-encode per archive, a few minutes)…")
                    log("  " + apply_collars(game_dir, it["incoming"], log))
                    counts["collars"] += 1
                else:
                    log("  collars skipped: no game files folder")
                    counts["skipped"] += 1
            elif it["section"] == "scoreclock":
                if game_dir and Path(game_dir).is_dir():
                    log("  applying scoreclock (~2-4 min)…")
                    log("  " + apply_scoreclock(game_dir, it["incoming"], log))
                    counts["scoreclock"] += 1
                else:
                    log("  scoreclock skipped: no game files folder")
                    counts["skipped"] += 1
    finally:
        if z:
            z.close()

    if meta_writes:
        _write_meta(root, meta_writes)
    # Expansion FIRST: the roster groups all address teams by CODE (and team_order physically
    # permutes the records), and the pack's textures can only be applied to assets that exist,
    # so the club has to be in the file before either runs.
    if expansion_teams:
        log(f"  building {len(expansion_teams)} expansion team(s)…")
        res = apply_expansion(ros_path, game_dir, expansion_teams, log)
        counts["expansion"] = res["teams"]
        if res["teams"] and not res["assets"]:
            counts.setdefault("notes", []).append(
                "expansion clubs were added to the roster but own no art yet")
    if roster_groups:
        apply_roster(ros_path, roster_groups, log)
    # Heads LAST, and on a freshly-opened zip. Last because everything above it can move a player
    # record out from under a row number — `apply_expansion` adds clubs and their players — and the
    # head section's identity chain has to resolve against the roster as it will finally be, not as
    # it was when the pack was read. Freshly-opened because the members are read here, after the
    # loop above has already closed its handle.
    if heads:
        log(f"  installing {len(heads)} custom head(s)…")
        with zipfile.ZipFile(zip_path, "r") as zh:
            counts["heads"] = apply_heads(zh, heads, decisions, game_dir, ros_path, log,
                                          targets=head_targets)
    return counts


# -- extracted-tree write-through ----------------------------------------------
# Reverting an audio stream writes raw packets at a byte offset in an archive, which is not a
# path archive_textures wraps, so the tree has to be told directly. A slot is fixed-size, so
# this is always the in-place case note_write handles.
def _tree_note(game_dir, fid, off, data):
    try:
        try:
            from . import volume_store as _vs
        except ImportError:
            import volume_store as _vs
        if _vs.note_write(game_dir, fid, off, data):
            _vs.save(game_dir)
    except Exception:
        pass
