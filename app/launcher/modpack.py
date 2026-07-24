"""modpack.py — share & merge NHL 2k10 mod work between people.

Two artefacts, one merge engine (everything is keyed by STABLE asset identity, never by a
user-chosen name, so two people's naming differences never break the merge):

  * Audio Names file (.json)  — just the naming work: per-stream {name, category, sample_rate}.
  * Mod Pack (.n2kpack = zip) — everything: audio names + replacement WAVs + replacement
                                textures, compressed (lossless).

Identity keys:
  audio stream  ->  "<fid>:0x<OFFSET8>"   (resolved through the catalog, robust to renames)
  texture       ->  its relative path under Textures/Extracted/  (deterministic filename)

Merge model per item: NEW (recipient lacks it) -> add; SAME (identical) -> skip;
CONFLICT (present but different) -> caller decides keep-mine / keep-theirs.
"""
import json, zipfile, hashlib, shutil, time
from pathlib import Path

try:
    from . import archive_textures as AT
except ImportError:
    import archive_textures as AT

FORMAT   = "nhl2k10-modpack"
VERSION  = 1
FILE_IDS = ["0A", "0B", "1A", "1B"]
PACK_EXT = ".n2kpack"
NAMES_EXT = ".n2knames.json"

# ── small helpers ─────────────────────────────────────────────────────────────
def akey(fid, off):  return f"{fid}:0x{off:08X}"
def parse_akey(k):   fid, h = k.split(":"); return fid, int(h, 16)

def _names_path(root, fid):   return Path(root) / f"{fid}_Audio_Names.json"
def _catalog_path(root, fid): return Path(root) / f"{fid}_Audio_Catalog.json"
def _modified_audio(root):    return Path(root) / "Modified" / "Audio"

def _load_json(p):
    try: return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception: return {}

def _sha(b):
    return hashlib.sha1(b).hexdigest()

def _sha_file(p):
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()

# ── local-state scanners ──────────────────────────────────────────────────────
def load_audio_meta(root):
    """{akey: <names-entry>} from the per-archive names JSONs. The whole entry travels
    (name/category/sample_rate/team/notes/…) EXCEPT the derived `stem` (it's reconstructable
    from the offset and would otherwise be redundant noise). Two same-version users therefore
    produce identical dicts for an unchanged stream -> clean SAME/CONFLICT detection."""
    meta = {}
    for fid in FILE_IDS:
        for k, v in _load_json(_names_path(root, fid)).items():
            if k.startswith("_") or not isinstance(v, dict):
                continue
            try: off = int(k, 16)
            except ValueError: continue
            e = {kk: vv for kk, vv in v.items() if kk != "stem" and not kk.startswith("_")}
            if e: meta[akey(fid, off)] = e
    return meta

def _catalog_index(root):
    """(name2off {name|stem -> (fid,off)},  off2entry {akey -> catalog entry + _fid})."""
    name2off, off2entry = {}, {}
    for fid in FILE_IDS:
        for stem, e in _load_json(_catalog_path(root, fid)).items():
            off = e.get("offset")
            if off is None: continue
            off2entry[akey(fid, off)] = {**e, "_fid": fid}
            if e.get("friendly_name"): name2off[e["friendly_name"]] = (fid, off)
            name2off[stem] = (fid, off)
    return name2off, off2entry

def load_audio_wavs(root):
    """{akey: Path} replacement WAVs, resolved to a stream via the catalog."""
    name2off, _ = _catalog_index(root)
    out = {}
    base = _modified_audio(root)
    if base.exists():
        for p in base.rglob("*.wav"):
            fo = name2off.get(p.stem)
            if fo: out[akey(*fo)] = p
    return out

def load_textures(root):
    """{relpath: Path} for Modified texture files (deterministic filenames)."""
    out = {}
    base = AT.extracted_root(root)
    if base.exists():
        for p in base.rglob("*"):
            if p.is_file():
                out[p.relative_to(base).as_posix()] = p
    return out

# ── export ────────────────────────────────────────────────────────────────────
def export_names(root, out_path, author=""):
    """Write the audio-naming work as a plain, human-readable / git-friendly JSON."""
    meta = load_audio_meta(root)
    doc = {"format": FORMAT + "-names", "version": VERSION,
           "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "author": author,
           "audio_meta": meta}
    Path(out_path).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return {"audio_meta": len(meta)}

def _tex_catalog_map():
    """{asset-folder -> (team, category)} from the texture catalog, for filtering by team/category."""
    m = {}
    try:
        for row in AT.load_catalog():
            m[AT.asset_iff(row["iff"])] = (row.get("team", "") or "", row.get("category", "") or "")
    except Exception:
        pass
    return m


def annotate(items, root):
    """Add `team` + `category` to each item (in place) from the local catalogs so the picker can
    filter. Works for both export (local) and import (incoming) item lists."""
    root = Path(root)
    texmap = _tex_catalog_map()
    meta = load_audio_meta(root); _, off2entry = _catalog_index(root)
    for it in items:
        if it.get("section") == "tex":
            team, cat = texmap.get(str(it["key"]).split("/")[0], ("", ""))
            it["team"], it["category"] = team, cat
        else:                                   # meta / audio
            inc = it.get("incoming") or {}
            e = meta.get(it["key"], {}); ce = off2entry.get(it["key"], {})
            it["team"] = (it.get("team") or inc.get("team") or e.get("team")
                          or ce.get("team") or "")
            it["category"] = (it.get("category") or inc.get("category") or e.get("category")
                              or ce.get("category") or "")
    return items


def local_items(root):
    """Every locally-modified item available to export, as a flat checkbox-friendly list:
    [{section: meta|audio|tex, key, label, team, category}]. Mirrors the diff_* item shape (minus
    status) so the same selection dialog drives both export and import."""
    root = Path(root)
    _, off2entry = _catalog_index(root)
    items = []
    for key in sorted(load_audio_meta(root)):
        e = off2entry.get(key, {})
        items.append({"section": "meta", "key": key,
                      "label": e.get("friendly_name") or e.get("stem") or key})
    for key in sorted(load_audio_wavs(root)):
        e = off2entry.get(key, {})
        items.append({"section": "audio", "key": key,
                      "label": (e.get("friendly_name") or e.get("stem") or key) + ".wav"})
    for rel in sorted(load_textures(root)):
        items.append({"section": "tex", "key": rel, "label": rel})
    return annotate(items, root)


def _write_pack(out_path, meta, wavs, texs, author=""):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        if meta:
            z.writestr("audio_meta.json", json.dumps(meta, indent=1))
        for key, p in wavs.items():
            fid, off = parse_akey(key)
            z.write(p, f"audio_wav/{fid}/{off:08X}.wav")
        for rel, p in texs.items():
            z.write(p, f"textures/{rel}")
        z.writestr("manifest.json", json.dumps({
            "format": FORMAT, "version": VERSION,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "author": author,
            "summary": {"audio_meta": len(meta), "audio_wav": len(wavs), "textures": len(texs)},
        }, indent=1))
    return {"audio_meta": len(meta), "audio_wav": len(wavs), "textures": len(texs)}


def export_pack(root, out_path, include=("meta", "audio", "tex"), author=""):
    """Write a compressed Mod Pack zip with the chosen change types (whole sections)."""
    root = Path(root)
    meta = load_audio_meta(root)  if "meta"  in include else {}
    wavs = load_audio_wavs(root)  if "audio" in include else {}
    texs = load_textures(root)    if "tex"   in include else {}
    return _write_pack(out_path, meta, wavs, texs, author)


def export_selected(root, out_path, selected, author=""):
    """Write a Mod Pack of ONLY the chosen items. `selected` = iterable of (section, key)."""
    root = Path(root); sel = {(s, k) for s, k in selected}
    meta = {k: v for k, v in load_audio_meta(root).items() if ("meta",  k) in sel}
    wavs = {k: v for k, v in load_audio_wavs(root).items() if ("audio", k) in sel}
    texs = {k: v for k, v in load_textures(root).items()   if ("tex",   k) in sel}
    return _write_pack(out_path, meta, wavs, texs, author)

# ── diff (incoming vs local) ──────────────────────────────────────────────────
def _meta_items(in_meta, local_meta, off2entry):
    items = []
    for key, inc in in_meta.items():
        loc = local_meta.get(key)
        status = "new" if loc is None else ("same" if loc == inc else "conflict")
        e = off2entry.get(key, {})
        items.append({"section": "meta", "key": key, "status": status,
                      "incoming": inc, "local": loc,
                      "label": e.get("friendly_name") or e.get("stem") or key})
    return items

def diff_names(json_path, root):
    doc = _load_json(json_path)
    in_meta = doc.get("audio_meta", doc if "format" not in doc else {})
    _, off2entry = _catalog_index(root)
    return doc.get("manifest", doc), _meta_items(in_meta, load_audio_meta(root), off2entry)

def diff_pack(zip_path, root):
    """Returns (manifest, items). Each item carries enough to preview + apply."""
    root = Path(root)
    local_meta = load_audio_meta(root)
    local_wavs = load_audio_wavs(root)
    local_texs = load_textures(root)
    _, off2entry = _catalog_index(root)
    items = []
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        manifest = json.loads(z.read("manifest.json")) if "manifest.json" in names else {}
        in_meta = json.loads(z.read("audio_meta.json")) if "audio_meta.json" in names else {}
        items += _meta_items(in_meta, local_meta, off2entry)
        for n in sorted(names):
            if n.startswith("audio_wav/") and n.endswith(".wav"):
                parts = n.split("/")
                if len(parts) != 3: continue
                key = akey(parts[1], int(parts[2][:-4], 16))
                inc_h = _sha(z.read(n))
                locp = local_wavs.get(key); loc_h = _sha_file(locp) if locp else None
                status = "new" if locp is None else ("same" if loc_h == inc_h else "conflict")
                e = off2entry.get(key, {})
                items.append({"section": "audio", "key": key, "status": status, "arc": n,
                              "zip": str(zip_path), "local": str(locp) if locp else None,
                              "label": e.get("friendly_name") or e.get("stem") or key})
            elif n.startswith("textures/"):
                rel = n[len("textures/"):]
                if not rel: continue
                inc_h = _sha(z.read(n))
                locp = local_texs.get(rel); loc_h = _sha_file(locp) if locp else None
                status = "new" if locp is None else ("same" if loc_h == inc_h else "conflict")
                items.append({"section": "tex", "key": rel, "status": status, "arc": n,
                              "zip": str(zip_path), "local": str(locp) if locp else None,
                              "label": rel})
    return manifest, items

def extract_member(zip_path, arc, dest):
    """Extract one pack member to `dest` (for previewing an incoming conflict file)."""
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z, z.open(arc) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    return dest

# ── apply ─────────────────────────────────────────────────────────────────────
def _should_take(item, decisions):
    if item["status"] == "new":
        return True
    if item["status"] == "conflict":
        return decisions.get(f'{item["section"]}|{item["key"]}') == "theirs"
    return False                                   # "same" -> nothing to do

def _write_meta(root, meta_by_fid):
    for fid, entries in meta_by_fid.items():
        p = _names_path(root, fid); raw = _load_json(p)
        for oh, e in entries.items():
            cur = raw.get(oh) if isinstance(raw.get(oh), dict) else {}
            cur = dict(cur or {}); cur.update(e); raw[oh] = cur
        p.write_text(json.dumps(raw, indent=2), encoding="utf-8")

def _apply_wav(z, item, root, off2entry, log):
    e = off2entry.get(item["key"])
    if e and e.get("wav"):
        wr = Path(e["wav"])                        # Audio/<folder>/<name>.wav
        name = e.get("friendly_name") or wr.stem
        dest = Path(root) / "Modified" / "Audio" / wr.parent.name / f"{name}.wav"
    else:                                          # recipient hasn't catalogued this stream
        fid, off = parse_akey(item["key"])
        dest = Path(root) / "Modified" / "Audio" / "_imported" / f"{fid}_{off:08X}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with z.open(item["arc"]) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    log(f"  audio  -> Modified/Audio/{dest.parent.name}/{dest.name}")

def _apply_tex(z, item, root, log):
    dest = AT.extracted_root(root) / item["key"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    with z.open(item["arc"]) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    log(f"  texture-> Textures/Extracted/{item['key']}")

def apply_items(root, items, decisions, zip_path=None, log=print):
    """Apply a resolved item list. meta -> names JSON; audio/tex -> Modified/ staging
    (the recipient then reviews and runs Patch Game / Apply to Game). Returns counts."""
    root = Path(root)
    _, off2entry = _catalog_index(root)
    counts = {"meta": 0, "audio": 0, "tex": 0, "skipped": 0}
    meta_by_fid = {}
    z = zipfile.ZipFile(zip_path) if zip_path else None
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
                _apply_wav(z, it, root, off2entry, log); counts["audio"] += 1
            elif it["section"] == "tex" and z is not None:
                _apply_tex(z, it, root, log); counts["tex"] += 1
    finally:
        if z: z.close()
    if meta_by_fid:
        _write_meta(root, meta_by_fid)
    return counts
