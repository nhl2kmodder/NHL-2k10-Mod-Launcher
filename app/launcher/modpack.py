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

Three whole-league groups, each one selectable checkbox:
  * team_colors  — {CODE: {primary, secondary}}      (chunk 0x8489FAF3, via team_colors.py)
  * arena_names  — {CODE: arena}                      (string pool, via roster_editor.py)
  * team_names   — {CODE: {city, state, team, name}}  (string pool, via roster_editor.py)

Applying names is best-effort: a name longer than its fixed slot in the target save is skipped
(the same in-place constraint the Teams tab enforces), never grown.

Identity keys:
  audio stream  ->  "<fid>:0x<OFFSET8>"   (resolved through the extract manifest, robust to renames)
  texture       ->  its relative path under Textures/Extracted/  (deterministic filename)
  roster group  ->  the group name ("team_colors" | "arena_names" | "team_names")
  scoreclock    ->  the single key "scoreclock" (whole-mod, all-or-nothing)

Merge model per item:
  NEW (recipient lacks it) -> add;
  SAME (identical) -> skip;
  CONFLICT (present but different) -> caller decides keep-mine / keep-theirs.
"""

import json
import zipfile
import hashlib
import shutil
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
except ImportError:
    import team_colors as TC
    import roster_editor as RE

FORMAT = "nhl2k10-modpack"
VERSION = 1
FILE_IDS = ["0A", "0B", "1A", "1B"]
PACK_EXT = ".n2kpack"
NAMES_EXT = ".n2knames.json"

ROSTER_GROUP_ORDER = ["team_colors", "arena_names", "team_names"]
ROSTER_GROUPS = {
    "team_colors": "Team Colours — primary + secondary (all teams)",
    "arena_names": "Arena Names (all teams)",
    "team_names":  "Team Names — city / state / team / name (all teams)",
}


# ── small helpers ─────────────────────────────────────────────────────────────

akey       = AS.akey
parse_akey = AS.parse_akey

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

def _catalog_index(root):
    """(name -> (fid, off), akey -> entry) from the extract manifest.

    `friendly_name`/`_fid` are kept as aliases because pack files written before the layout
    change carry those field names and still have to import.
    """
    name2off, off2entry = {}, {}
    for key, e in AS.load_manifest(root).items():
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

def load_audio_wavs(root):
    """akey -> WAV path, for streams the user has actually edited.

    Editing is in place now, so "edited" is the manifest sha1 comparison rather than
    "a file exists in Modified/Audio/".
    """
    out = {}
    for key, e in AS.load_manifest(root).items():
        p = AS.wav_path(root, e)
        if AS.is_edited(root, e, p):
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

def load_roster(ros_path):
    out = {}
    ros_path = Path(ros_path)
    if not ros_path.is_file():
        return out
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
    return out

def _roster_item(group, data):
    label = ROSTER_GROUPS.get(group, group.replace("_", " ").title())
    return {"section": "roster", "key": group, "label": label, "team": "", "category": "Roster", "count": len(data)}

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
        label = ROSTER_GROUPS.get(g, g.replace("_", " ").title())
        items.append({"section": "roster", "key": g, "status": status, "incoming": inc, "local": loc, "label": label, "team": "", "category": "Roster"})
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


# ── revert (undo what a pack applied, from the .orig backups) ─────────────────

def read_pack_contents(pack_path):
    """Cheap inventory of a pack: {'tex_rels': [...], 'audio_keys': [...], 'roster': bool,
    'scoreclock': dict|{}} — what a revert (or a preview of one) needs to know."""
    out = {"tex_rels": [], "audio_keys": [], "roster": False, "scoreclock": {}}
    with zipfile.ZipFile(pack_path, "r") as z:
        names = set(z.namelist())
        out["roster"] = "roster.json" in names
        if "scoreclock.json" in names:
            out["scoreclock"] = json.loads(z.read("scoreclock.json"))
        for n in sorted(names):
            if n.startswith("textures/") and len(n) > len("textures/") and not n.endswith("/"):
                out["tex_rels"].append(n[len("textures/"):])
            elif n.startswith("audio_wav/") and n.endswith(".wav"):
                parts = n.split("/")
                if len(parts) == 3:
                    try:
                        out["audio_keys"].append(akey(parts[1], int(parts[2][:-4], 16)))
                    except ValueError:
                        pass
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
      roster      -> NOT revertible (field values overwrite in place; no .orig concept) — logged.
    Returns a counts dict. Slow when assets are large (each ensure_clean is an archive splice)."""
    root, game_dir = Path(root), Path(game_dir)
    inv = read_pack_contents(pack_path)
    counts = {"tex_assets": 0, "audio": 0, "scoreclock": 0, "notes": []}
    sc = inv["scoreclock"]

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

def annotate(items, root):
    root = Path(root)
    texmap = _tex_catalog_map()
    meta = load_audio_meta(root)
    _, off2entry = _catalog_index(root)
    for it in items:
        if it.get("section") == "roster":
            it.setdefault("team", "")
            it["category"] = "Roster"
        elif it.get("section") == "scoreclock":
            it.setdefault("team", "")
            it["category"] = "Scoreclock"
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

def local_items(root):
    root = Path(root)
    _, off2entry = _catalog_index(root)
    items = []
    for key in sorted(load_audio_meta(root)):
        e = off2entry.get(key, {})
        items.append({"section": "meta", "key": key, "label": e.get("friendly_name") or e.get("stem") or key})
    for key in sorted(load_audio_wavs(root)):
        e = off2entry.get(key, {})
        items.append({"section": "audio", "key": key, "label": (e.get("friendly_name") or e.get("stem") or key) + ".wav"})
    for rel in sorted(load_textures(root)):
        items.append({"section": "tex", "key": rel, "label": rel})
    return annotate(items, root)

def _write_pack(out_path, meta, wavs, texs, roster=None, scoreclock=None, author=""):
    roster = roster or {}
    scoreclock = scoreclock or {}
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
                "scoreclock": 1 if scoreclock else 0
            },
        }, indent=1))
        if meta:
            z.writestr("audio_meta.json", json.dumps(meta, indent=1))
        for key, p in wavs.items():
            if Path(p).exists():
                fid, off = parse_akey(key)
                z.write(p, f"audio_wav/{fid}/{off:08X}.wav")
        for rel, p in texs.items():
            if Path(p).exists():
                z.write(p, f"textures/{rel}")
        if roster:
            z.writestr("roster.json", json.dumps(roster, indent=1))
        if scoreclock:
            z.writestr("scoreclock.json", json.dumps(scoreclock, indent=1))
    return {"audio_meta": len(meta), "audio_wav": len(wavs), "textures": len(texs),
            "roster": len(roster), "scoreclock": 1 if scoreclock else 0}

def export_pack(root, out_path, include=("meta", "audio", "tex"), ros_path=None,
                game_dir=None, author="", log=print):
    root = Path(root)
    meta = load_audio_meta(root)  if "meta"  in include else {}
    wavs = load_audio_wavs(root)  if "audio" in include else {}
    texs = load_textures(root)    if "tex"   in include else {}
    roster = load_roster(ros_path) if ("roster" in include and ros_path) else {}
    sc = load_scoreclock(game_dir, log) if ("scoreclock" in include and game_dir) else {}
    return _write_pack(out_path, meta, wavs, texs, roster, sc, author)

def export_selected(root, out_path, selected, ros_path=None, game_dir=None, author="", log=print):
    root = Path(root)
    sel = {(s, k) for s, k in selected}
    meta = {k: v for k, v in load_audio_meta(root).items() if ("meta",  k) in sel}
    wavs = {k: v for k, v in load_audio_wavs(root).items() if ("audio", k) in sel}
    texs = {k: v for k, v in load_textures(root).items()   if ("tex",   k) in sel}
    roster = {}
    if any(s == "roster" for s, _ in sel) and ros_path:
        full_roster = load_roster(ros_path)
        roster = {g: d for g, d in full_roster.items() if ("roster", g) in sel}
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
            extra = {rel: p for rel, p in load_textures(root).items()
                     if rel.split("/")[0].lower() == "overlay_static.iff" and rel not in texs}
            if extra:
                log(f"  scoreclock: auto-including {len(extra)} overlay_static texture file(s)")
                texs.update(extra)
    return _write_pack(out_path, meta, wavs, texs, roster, sc, author)


# ── diff (incoming vs local) ──────────────────────────────────────────────────

def _meta_items(in_meta, local_meta, off2entry):
    items = []
    for key, inc in in_meta.items():
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

def diff_pack(zip_path, root, ros_path=None):
    zip_path = Path(zip_path)
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"'{zip_path.name}' is not a valid zip archive (.n2kpack).")
    root = Path(root)
    local_meta = load_audio_meta(root)
    local_wavs = load_audio_wavs(root)
    local_texs = load_textures(root)
    _, off2entry = _catalog_index(root)
    items = []

    with zipfile.ZipFile(zip_path, "r") as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
        in_meta = json.loads(z.read("audio_meta.json")) if "audio_meta.json" in names else {}
        in_roster = json.loads(z.read("roster.json")) if "roster.json" in names else {}
        in_sc = json.loads(z.read("scoreclock.json")) if "scoreclock.json" in names else {}

        items += _meta_items(in_meta, local_meta, off2entry)
        items += diff_roster(in_roster, ros_path)
        if in_sc.get("snapshot"):
            items.append(diff_scoreclock_item(in_sc))

        for n in sorted(names):
            if n.startswith("audio_wav/") and n.endswith(".wav"):
                parts = n.split("/")
                if len(parts) != 3:
                    continue
                try:
                    key = akey(parts[1], int(parts[2][:-4], 16))
                except ValueError:
                    continue
                inc_h = _sha(z.read(n))
                locp = local_wavs.get(key)
                loc_h = _sha_file(locp) if locp else None
                status = "new" if locp is None else ("same" if loc_h == inc_h else "conflict")
                e = off2entry.get(key, {})
                items.append({
                    "section": "audio", "key": key, "status": status,
                    "arc": n, "zip": str(zip_path), "local": str(locp) if locp else None,
                    "label": e.get("friendly_name") or e.get("stem") or key
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
    e = off2entry.get(item["key"])
    if e:
        dest = AS.wav_path(root, e)
    else:
        fid, off = parse_akey(item["key"])
        dest = AS.extracted_root(root) / "_imported" / f"{fid}_{off:08X}.wav"

    dest.parent.mkdir(parents=True, exist_ok=True)
    with z.open(item["arc"]) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    log(f"  audio  -> Audio/Extracted/{AS.rel_wav(root, dest)}")

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

def apply_items(root, items, decisions, zip_path=None, ros_path=None, game_dir=None, log=print):
    """game_dir enables the SCORECLOCK section (slow: ~2-4 min DRAM rebuild — run the whole call
    in a worker thread when items may include it). game_dir=None skips it with a log line, so a
    GUI can strip scoreclock rows out and replay them itself on a background thread."""
    root = Path(root)
    _, off2entry = _catalog_index(root)
    counts = {"meta": 0, "audio": 0, "tex": 0, "roster": 0, "scoreclock": 0, "skipped": 0}
    meta_writes = {}
    roster_groups = {}

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
            elif it["section"] == "roster":
                if ros_path and Path(ros_path).is_file():
                    roster_groups[it["key"]] = it["incoming"]
                    counts["roster"] += 1
                else:
                    log("  roster skipped: no Roster.ROS path set (Teams tab → Browse…)")
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
    if roster_groups:
        apply_roster(ros_path, roster_groups, log)

    return counts