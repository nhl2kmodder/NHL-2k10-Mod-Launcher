"""team_fields.py — read/edit EVERY editable field of the NHL 2k10 team record.

The team record (chunk 0x8489FAF3, stride 412; see team_colors.py for how the table is
located) has 412 bytes per team, of which ~205 actually vary across the 30 teams. Those
are exposed here as named fields driven by **team_fields.json**, so a field can be
identified hands-on: edit a column in the Team Fields editor, look at the result in-game,
then rename that field in the JSON (no code change).

team_fields.json layout:
  {"record_stride": 412,
   "fields": [{"off": "0x14C", "size": 3, "type": "rgb", "name": "primary_color",
               "note": "free text — record what you learned"}, ...]}
The file you edit lives in %APPDATA%\\NHL2K10 Mod Launcher\\ (seeded once from the bundled
defaults) so your renames survive a rebuild — see defs_path().

type: u8|u16|u32|i8|i16|i32 (big-endian) | f32 | rgb (3 bytes -> #RRGGBB) | hex (raw).
`off` is relative to the start of a team record.

Edits are strictly IN PLACE (file size never changes); a one-time <roster>.ROS.colorbak
backup is written before the first change, and the game caches these at load, so a change
only shows after a full restart.
"""
from __future__ import annotations
import json, os, struct, shutil, sys
from pathlib import Path

import resources as R
import ros_file as RF
import team_colors as TC

DEFS_NAME = "team_fields.json"

_INT = {"u8": ">B", "i8": ">b", "u16": ">H", "i16": ">h", "u32": ">I", "i32": ">i", "f32": ">f"}


def _seed_defs() -> Path:
    """The read-only shipped defaults, from launcher/data/ (see resources.py)."""
    return R.data_path(DEFS_NAME)


def defs_path() -> Path:
    """The USER's editable team_fields.json — %APPDATA%\\NHL2K10 Mod Launcher\\team_fields.json.

    Field names are the whole point of this file: you rename `unidentified_N` as you identify
    it, and those labels must SURVIVE a rebuild. The bundled copy can't: `pyinstaller
    --noconfirm` wipes the onedir app folder (and sys._MEIPASS with it) on every build, so a
    renamed field there would be silently reverted to the shipped default. Same reasoning as
    the launcher's own _resolve_config_path(). On first use the shipped defaults are copied
    out to APPDATA; after that the user's copy always wins and is never overwritten.
    """
    seed = _seed_defs()
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if not appdata:
        return seed
    d = Path(appdata) / "NHL2K10 Mod Launcher"
    user = d / DEFS_NAME
    if not user.exists():
        try:
            d.mkdir(parents=True, exist_ok=True)
            if seed.exists():
                shutil.copy2(seed, user)
            else:
                return seed
        except Exception:
            return seed
    return user


def load_defs() -> list[dict]:
    """Field defs sorted by offset. Raises ValueError on duplicate names (a rename typo)."""
    doc = json.loads(defs_path().read_text(encoding="utf-8"))
    fields = doc["fields"] if isinstance(doc, dict) else doc
    out = []
    for f in fields:
        g = dict(f)
        g["off"] = int(str(f["off"]), 16) if str(f["off"]).lower().startswith("0x") else int(f["off"])
        g["size"] = int(f["size"])
        out.append(g)
    names = [f["name"] for f in out]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"{DEFS_NAME}: duplicate field name(s): {', '.join(sorted(dupes))}")
    out.sort(key=lambda f: f["off"])
    return out


# ── value <-> bytes ──────────────────────────────────────────────────────────
def decode(b: bytes, typ: str) -> str:
    if typ == "rgb" and len(b) == 3:
        return "#%02X%02X%02X" % tuple(b)
    fmt = _INT.get(typ)
    if fmt and struct.calcsize(fmt) == len(b):
        v = struct.unpack(fmt, b)[0]
        return f"{v:.4g}" if typ == "f32" else str(v)
    return b.hex().upper()


def encode(text: str, typ: str, size: int) -> bytes:
    text = text.strip()
    if typ == "rgb" and size == 3:
        s = text.lstrip("#")
        if len(s) != 6:
            raise ValueError(f"'{text}' is not #RRGGBB")
        return bytes.fromhex(s)
    fmt = _INT.get(typ)
    if fmt and struct.calcsize(fmt) == size:
        v = float(text) if typ == "f32" else int(text, 0)
        try:
            return struct.pack(fmt, v)
        except struct.error as e:
            raise ValueError(f"{typ} can't hold {text}: {e}")
    s = text.replace(" ", "")
    if len(s) != size * 2:
        raise ValueError(f"hex must be exactly {size*2} digits ({size} bytes), got {len(s)}")
    return bytes.fromhex(s)


# ── read / write ─────────────────────────────────────────────────────────────
def _base_stride(ros):
    c = TC._color_chunk(ros)
    return TC._team_base(ros, c), c.stride


def load_rows(ros_path, led=False) -> list[dict]:
    """[{'code','rec','values': {field_name: text}}] for the 30 teams (or their LED twins)."""
    ros = RF.RosFile(str(ros_path)); d = ros.data
    base, S = _base_stride(ros)
    defs = load_defs()
    rows = []
    for code, rec in sorted(TC.team_map(ros_path).items(), key=lambda kv: kv[1]):
        r = rec + (TC.LED_OFFSET if led else 0)
        b = base + r * S
        vals = {f["name"]: decode(bytes(d[b + f["off"]: b + f["off"] + f["size"]]), f["type"])
                for f in defs}
        rows.append({"code": code, "rec": r, "values": vals})
    return rows


def save_rows(ros_path, edits, led=False, backup=True, log=print) -> int:
    """Apply edits IN PLACE. edits: [(code, field_name, new_text)]. Returns the count written.
    `led=True` writes each team's arena-LED twin (record +30) instead of its team record.

    Validates and encodes EVERY edit before touching a byte, so one bad value aborts the whole
    batch instead of half-writing it.
    """
    ros_path = Path(ros_path)
    defs = {f["name"]: f for f in load_defs()}
    tmap = TC.team_map(ros_path)
    plan = []
    for code, name, text in edits:
        f = defs.get(name)
        if f is None:
            raise KeyError(f"unknown field '{name}' — is it still in {DEFS_NAME}?")
        if code.upper() not in tmap:
            raise KeyError(f"unknown team '{code}'")
        rec = tmap[code.upper()] + (TC.LED_OFFSET if led else 0)
        try:
            plan.append((rec, f, encode(text, f["type"], f["size"]), code, name, text))
        except ValueError as e:
            raise ValueError(f"{code}.{name}: {e}")
    if not plan:
        return 0
    if backup:
        bak = ros_path.with_suffix(ros_path.suffix + ".colorbak")
        if not bak.exists():
            shutil.copy2(ros_path, bak); log(f"  backup -> {bak.name}")
    ros = RF.RosFile(str(ros_path)); d = ros.data
    base, S = _base_stride(ros)
    for rec, f, raw, code, name, text in plan:
        b = base + rec * S + f["off"]
        d[b:b + f["size"]] = raw
        log(f"  {code} rec {rec}: {name} @+0x{f['off']:03X} = {text}")
    if len(d) != ros.orig_size:
        raise RuntimeError("refusing to write: roster size changed (in-place invariant)")
    ros_path.write_bytes(d)
    return len(plan)


def record_hex(ros_path, code, led=False) -> str:
    """The raw 412 bytes of one team's record, as an annotated hex dump."""
    ros = RF.RosFile(str(ros_path)); d = ros.data
    base, S = _base_stride(ros)
    rec = TC.team_map(ros_path)[code.upper()] + (TC.LED_OFFSET if led else 0)
    b = base + rec * S
    raw = bytes(d[b:b + S])
    out = [f"{code.upper()}  record {rec}  file offset 0x{b:X}  ({S} bytes)"]
    for i in range(0, S, 16):
        ch = raw[i:i + 16]
        out.append(f"  +0x{i:03X}  {' '.join(f'{x:02X}' for x in ch):<47}  "
                   + "".join(chr(x) if 32 <= x < 127 else "." for x in ch))
    return "\n".join(out)
