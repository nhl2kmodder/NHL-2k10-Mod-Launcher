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

Three whole-league groups, each one selectable checkbox:
  * team_colors  — {CODE: {primary, secondary}}      (chunk 0x8489FAF3, via team_colors.py)
  * arena_names  — {CODE: arena}                      (string pool, via roster_editor.py)
  * team_names   — {CODE: {city, state, team, name}}  (string pool, via roster_editor.py)

Applying names is best-effort: a name longer than its fixed slot in the target save is skipped
(the same in-place constraint the Teams tab enforces), never grown.

Identity keys:
  audio stream  ->  "<fid>:0x<OFFSET8>"   (resolved through the catalog, robust to renames)
  texture       ->  its relative path under Textures/Extracted/  (deterministic filename)
  roster group  ->  the group name ("team_colors" | "arena_names" | "team_names")

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
except ImportError:
    import archive_textures as AT

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

def akey(fid, off):
    return f"{fid}:0x{off:08X}"

def parse_akey(k):
    fid, h = k.split(":")
    return fid, int(h, 16)

def _names_path(root, fid):
    return Path(root) / f"{fid}_Audio_Names.json"

def _catalog_path(root, fid):
    return Path(root) / f"{fid}_Audio_Catalog.json"

def _modified_audio(root):
    return Path(root) / "Modified" / "Audio"

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
    meta = {}
    for fid in FILE_IDS:
        for k, v in _load_json(_names_path(root, fid)).items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            try:
                off = int(k, 16)
            except ValueError:
                continue
            e = {kk: vv for kk, vv in v.items() if kk != "stem" and not kk.startswith("_")}
            if e:
                meta[akey(fid, off)] = e
    return meta

def _catalog_index(root):
    name2off, off2entry = {}, {}
    for fid in FILE_IDS:
        for stem, e in _load_json(_catalog_path(root, fid)).items():
            off = e.get("offset")
            if off is None:
                continue
            off2entry[akey(fid, off)] = {**e, "_fid": fid}
            if e.get("friendly_name"):
                name2off[e["friendly_name"]] = (fid, off)
            name2off[stem] = (fid, off)
    return name2off, off2entry

def load_audio_wavs(root):
    name2off, _ = _catalog_index(root)
    out = {}
    base = _modified_audio(root)
    if base.exists():
        for p in base.rglob("*.wav"):
            fo = name2off.get(p.stem)
            if fo:
                out[akey(*fo)] = p
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

def _write_pack(out_path, meta, wavs, texs, roster=None, author=""):
    roster = roster or {}
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
                "roster": len(roster)
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
    return {"audio_meta": len(meta), "audio_wav": len(wavs), "textures": len(texs), "roster": len(roster)}

def export_pack(root, out_path, include=("meta", "audio", "tex"), ros_path=None, author=""):
    root = Path(root)
    meta = load_audio_meta(root)  if "meta"  in include else {}
    wavs = load_audio_wavs(root)  if "audio" in include else {}
    texs = load_textures(root)    if "tex"   in include else {}
    roster = load_roster(ros_path) if ("roster" in include and ros_path) else {}
    return _write_pack(out_path, meta, wavs, texs, roster, author)

def export_selected(root, out_path, selected, ros_path=None, author=""):
    root = Path(root)
    sel = {(s, k) for s, k in selected}
    meta = {k: v for k, v in load_audio_meta(root).items() if ("meta",  k) in sel}
    wavs = {k: v for k, v in load_audio_wavs(root).items() if ("audio", k) in sel}
    texs = {k: v for k, v in load_textures(root).items()   if ("tex",   k) in sel}
    roster = {}
    if any(s == "roster" for s, _ in sel) and ros_path:
        full_roster = load_roster(ros_path)
        roster = {g: d for g, d in full_roster.items() if ("roster", g) in sel}
    return _write_pack(out_path, meta, wavs, texs, roster, author)


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

        items += _meta_items(in_meta, local_meta, off2entry)
        items += diff_roster(in_roster, ros_path)

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

def _write_meta(root, meta_by_fid):
    for fid, entries in meta_by_fid.items():
        p = _names_path(root, fid)
        raw = _load_json(p) if p.exists() else {}
        for oh, e in entries.items():
            cur = raw.get(oh) if isinstance(raw.get(oh), dict) else {}
            cur = dict(cur or {})
            cur.update(e)
            raw[oh] = cur
        p.write_text(json.dumps(raw, indent=2), encoding="utf-8")

def _apply_wav(z, item, root, off2entry, log):
    e = off2entry.get(item["key"])
    if e and e.get("wav"):
        wr = Path(e["wav"])
        name = e.get("friendly_name") or wr.stem
        dest = Path(root) / "Modified" / "Audio" / wr.parent.name / f"{name}.wav"
    else:
        fid, off = parse_akey(item["key"])
        dest = Path(root) / "Modified" / "Audio" / "_imported" / f"{fid}_{off:08X}.wav"

    dest.parent.mkdir(parents=True, exist_ok=True)
    with z.open(item["arc"]) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    log(f"  audio  -> Modified/Audio/{dest.parent.name}/{dest.name}")

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

def apply_items(root, items, decisions, zip_path=None, ros_path=None, log=print):
    root = Path(root)
    _, off2entry = _catalog_index(root)
    counts = {"meta": 0, "audio": 0, "tex": 0, "roster": 0, "skipped": 0}
    meta_by_fid = {}
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
                fid, off = parse_akey(it["key"])
                meta_by_fid.setdefault(fid, {})[f"0x{off:08X}"] = it["incoming"]
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
    finally:
        if z:
            z.close()

    if meta_by_fid:
        _write_meta(root, meta_by_fid)
    if roster_groups:
        apply_roster(ros_path, roster_groups, log)

    return counts