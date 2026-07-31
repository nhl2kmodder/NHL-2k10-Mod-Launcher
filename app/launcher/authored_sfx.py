"""authored_sfx.py — the authored sound-effect family (0A/0B), the game's *other* audio system.

There are two entirely separate audio systems in this game and only one of them was ever in the
Audio tab:

  * **Streamed wave banks** (`1A`/`1B`, 23 `.bin` banks, ~81k streams) — commentary, crowd beds,
    goal songs, horns. Handled by `wave_banks.py` + the cue tables. Already catalogued.
  * **Authored sounds** (`0A`/`0B`, ~1,385 sounds) — *the actual gameplay SFX*: skating, stick
    checks, puck off the post/glass/pads, the ref whistle, menu clicks. These live inside
    scene-style containers and were NEVER extractable, so the Audio tab showed none of them.
    That is what this module fixes.

`findings/04 §143` recorded the in-container wave codec as "undecoded", which is why these sounds
were parked. That was wrong: the payload is **plain XMA2**, identical to the `1A`/`1B` packing —
it just starts at a container-relative base that is not `0x800`-aligned in the archive (alignment
is relative to the *stream* start, not the file, so packets still line up).

Container layout (magic `FF3BEF94`, the same scene container used by `.iff` assets)::

    +0x00  u32  0xFF3BEF94
    +0x08  u32  total size
    +0x10  u32  section count
    +0x20  section records, 0x20 bytes each:
             +0x00 type   +0x04 type (dup)   +0x0C decompressed size
             +0x14 OFFSET (container-relative)   +0x18 STORED size

Two sections matter:

  ``0xBB05A9C1`` (DRAM)   0E4837-compressed; decompresses to exactly ``0x2C * n_sounds`` — the
                          per-sound descriptor table, in wave-offset order.
  ``0x76CBC6E7`` (waves)  stored uncompressed; the XMA2 streams, each at its directory
                          ``wave_off`` relative to this section's start.

The offsets chain exactly, which is how the layout was confirmed:
``dram_off + dram_stored == wave_off`` and ``wave_off + wave_size == total size``.

Descriptor record (0x2C bytes, same shape as the `pasfx.cdf` descriptor in §11.1)::

    +0x00 u32 always 1    +0x08 u32 FORMAT (2 = mono, 5 = stereo)    +0x0C u32 sample count
    +0x10 u32 sample rate    +0x18 u32 byte size    +0x24 u32 samples

``+0x18`` matches the gap to the next ``wave_off`` byte-for-byte on every record, which is what
pins the table to the sounds.

``+0x00`` was read as the channel count for a long time and it is not one: it is **1 on all
1,385 authored sounds in the game**, stereo ones included. The channel count is what ``+0x08``
selects, and getting it wrong does not fail loudly — xma2encode decodes a stereo stream told it
is mono and emits ~8% of the samples as noise, which is what the 395 "cryptic jumble" sounds
were. ``+0x0C`` is the encoded sample count and makes this checkable: decoding every sound and
comparing against it gives 988/1385 complete before, 1383/1385 after (findings 04 §20).

**Which descriptor belongs to which sound** — the 20-byte directory entry's 4th field is the
*byte offset of that sound's descriptor inside the decompressed DRAM*, not an opaque "ref"::

    [crc32(name)] [0x1AEDDA1F] [2] [DESCRIPTOR OFFSET] [wave_off]

That was originally read positionally (sort by ``wave_off``, walk the table), which happens to
give the identical answer for the banks whose DRAM is *nothing but* the table — but it is the
descriptor offset that is the actual link, and following it is what opened the last two families:

  * **``frontend.iff`` / ``disc_099f9a9d``** (113 sounds) — same `FF3BEF94` container, but their
    DRAM is a full scene graph with the 0x2C table buried inside it (at 0x2958 and 0x6DCA0), so
    ``dram_size`` is not a multiple of 0x2C and the positional walk had nothing to walk. The
    descriptor offset points straight at the right record: descriptor size == gap to the next
    ``wave_off`` on 29/29 and 84/84.
  * **``pasfx.iff`` / ``paintro.iff`` / ``loading_audio.iff``** (103 sounds) — magic ``F0985030``;
    their `FF3BEF94` sections are past-EOF stubs and the waves live in a paired ``.cdf``. The
    `.cdf` is simply ``[0x2C descriptor][wave data]`` repeated, ``+0x18`` bytes of wave each time,
    tiling the file *exactly* (14/49/40 records, ending on the final byte). Here the descriptor
    offset is an index into that physical run (``offset // 0x2C``), and the resulting name order
    is alphabetical — which is the independent check that the join is right.

    The directory's ``wave_off`` is **not** a `.cdf` offset for these three (its deltas are a
    permutation of the aligned wave sizes, so it addresses some other packing); the physical walk
    is the authority and ``wave_off`` is used only to order the sounds.
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import tempfile
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from . import archive_textures as at
    from . import audio_store as astore
    from . import resources
except ImportError:                                     # flat import when run un-packaged
    import archive_textures as at
    import audio_store as astore
    import resources

# Run xma2encode WITHOUT flashing a console window or stealing focus. An extract is one spawn
# PER SOUND -- 1,385 of them -- so a bare subprocess.run here strobes the screen with console
# windows for the whole run and repeatedly yanks focus off whatever the user is doing.
# CREATE_NO_WINDOW suppresses the console; the hidden STARTUPINFO is the belt-and-suspenders
# fallback for tools that try to show one anyway. This lives HERE rather than being passed in by
# the caller: the module owns its own spawns, and relying on every caller to hand over a hidden
# runner is exactly how the flashing came back.
if sys.platform == "win32":
    _NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW
    _NO_WINDOW_STARTUPINFO = subprocess.STARTUPINFO()
    _NO_WINDOW_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    _NO_WINDOW_STARTUPINFO.wShowWindow = subprocess.SW_HIDE
else:
    _NO_WINDOW_FLAGS = 0
    _NO_WINDOW_STARTUPINFO = None


def _run(cmd, **kw):
    """subprocess.run wrapper that keeps console windows off the screen on Windows."""
    kw["creationflags"] = kw.get("creationflags", 0) | _NO_WINDOW_FLAGS
    kw.setdefault("startupinfo", _NO_WINDOW_STARTUPINFO)
    return subprocess.run(cmd, **kw)


IFF_MAGIC = 0xFF3BEF94
SECT_DRAM = 0xBB05A9C1
SECT_WAVE = 0x76CBC6E7
DESC_SIZE = 0x2C
PACKET = 2048

#: Descriptor +0x08 -> channel count. See the module docstring: +0x00 is 1 everywhere and never
#: was the channel count. Anything outside this map falls back to mono, which is what the game's
#: own one-shots are; an unknown format is better served by a wrong-but-quiet guess than by a
#: dropped sound.
CODEC_CHANNELS = {2: 1, 5: 2}

#: `FF3BEF94` banks — descriptors in the DRAM section, waves in the same container
INLINE_BANKS = (
    "sfx_arena000.bnk", "sfx_arena001.bnk", "sfx_arena002.bnk", "sfx_arena003.bnk",
    "crowdloops.iff", "bootup_audio.iff", "disc_aaea7e4d",
    # DRAM is a scene graph with the descriptor table embedded in it; reached via the
    # directory's descriptor offset, exactly like the others
    "frontend.iff", "disc_099f9a9d",
)

#: `F0985030` banks — the container is a stub index; descriptors *and* waves are interleaved
#: in a paired ``.cdf``  (bank -> the wave file to read)
CDF_BANKS = {
    "pasfx.iff": "pasfx.cdf",
    "paintro.iff": "paintro.cdf",
    "loading_audio.iff": "loading_audio.cdf",
}

#: every authored bank, in the order the Audio tab lists them
ALL_BANKS = INLINE_BANKS + tuple(CDF_BANKS)

#: banks we can see but cannot yet frame — reported, never silently dropped. Empty since the
#: descriptor-offset link was found; kept because a future container that doesn't parse must be
#: *named* in the extract log rather than vanishing.
UNSUPPORTED: dict[str, str] = {}


# ── categories ────────────────────────────────────────────────────────────────
#
# Only the categories the Audio tab already knows (CATEGORY_FOLDER in the launcher). The stem
# families are the authored names themselves, so this is a rename of known data, not a guess.

_CROWD = re.compile(r"^(cheer|crowd|ohh|rampup|cr-applaud|breath)")
_UI = re.compile(r"^(menu|wipe|2klogo|1nxv|misc\d|whoosh)")

#: fallback category per bank, taken from what that bank's *named* members turn out to be. Banks
#: with no named member at all are deliberately absent: they land in "Unsorted" (folder Unknown)
#: rather than being guessed into a folder that implies knowledge we don't have.
BANK_CATEGORY = {
    "sfx_arena000.bnk": "Arena_SFX", "sfx_arena001.bnk": "Arena_SFX",
    "sfx_arena002.bnk": "Arena_SFX", "sfx_arena003.bnk": "Arena_SFX",
    "crowdloops.iff": "Crowd_Ambient",
    "bootup_audio.iff": "SFX",
    "disc_aaea7e4d": "Arena_SFX",
    # all 14 names are team goal celebrations: sharks-goal, bruins-siren, blue-jackets-canon…
    "pasfx.iff": "Goal_SFX",
    # menu-error / menu-back / menu-forward / 2klogo
    "frontend.iff": "SFX",
    # whoosh1-12 + misc1-4 — front-end presentation stingers
    "disc_099f9a9d": "SFX",
    # Neither of these carries a single cracked asset name, so the category comes from the audio
    # instead: every one of the 89 transcribes as the arena PA announcer.
    #   paintro.iff      "And now, your Vancouver Canucks!"                    (49, 3.9-6.5 s)
    #   loading_audio.iff "Alright Canucks fans, it's hockey time! …"          (40, 6.1-11.8 s)
    "paintro.iff": "PA_English",
    "loading_audio.iff": "PA_English",
}


def category_for(name: str, bank: str = "") -> str:
    if not name:
        return BANK_CATEGORY.get(bank, "Unsorted")
    if name.startswith("ref-whistle"):
        return "Whistle"
    if _CROWD.match(name):
        return "Crowd_Ambient"
    if _UI.match(name):
        return "SFX"
    return BANK_CATEGORY.get(bank, "Arena_SFX")


def team_for(name: str, bank: str = "") -> str:
    """Team for an authored sound — only where the stem spells one out. Otherwise ''.

    Authored stems are lowercase and hyphenated (``blue-jackets-canon_01``); `team_tag` reads
    CamelCase, so they are squashed to it first (``BlueJacketsCanon``). Both halves matter:
    joining the words is what lets the two-token spellings match at all (``BlueJackets``), and
    capitalising them is what satisfies `team_tag`'s standalone-word rule for the nicknames it
    treats as weak — ``Sharks``, ``Kings``, ``Panthers``, ``Ducks`` — which are only ambiguous
    because in 18k lines of play-by-play prose they are ordinary English. These are not prose:
    they are 14 asset names in the team-PA bank, every one of which resolves correctly.
    Restricted to `pasfx.iff` for that reason; elsewhere a stem like "wild-slap" would be a coin
    flip, and a wrong team is worse than a blank one.
    """
    if bank != "pasfx.iff" or not name:
        return ""
    try:
        from . import team_tag
    except ImportError:
        try:
            import team_tag
        except ImportError:
            return ""
    return team_tag.team_from_name("".join(p.capitalize() for p in re.split(r"[-_\s]+", name)))


#: mirrors CATEGORY_FOLDER in the launcher for exactly the categories this module emits, so these
#: WAVs land in the same folders as every other extracted sound
FOLDER = {"Whistle": "SFX", "Arena_SFX": "SFX", "SFX": "SFX", "Crowd_Ambient": "Crowd_Ambient",
          "Goal_SFX": "Goal_SFX", "PA_English": "PA", "Unsorted": "Unknown"}


def display_name(snd: dict) -> str:
    """The Audio-tab name, and the WAV filename.

    `sfx_arena000..003` hold the *same 288 sounds* rendered in four arena acoustics, so the bare
    authored name is not unique across banks — the `_arena<N>` suffix is what keeps four rows (and
    four files) apart instead of silently overwriting three of them.
    """
    base = snd.get("label") or snd["name"] \
        or "%s_%s" % (snd["bank"].split(".")[0], (snd["hash"] or "0x")[2:])
    m = re.match(r"^sfx_arena(\d+)", snd["bank"])
    return f"{base}_arena{int(m.group(1))}" if m else base


# ── container parsing ─────────────────────────────────────────────────────────

def _arc_path(game_dir, arc: str) -> Path:
    """Pristine bytes when we have them — extraction must read stock audio, not a previous mod."""
    p = Path(game_dir) / (arc + ".orig")
    return p if p.exists() else Path(game_dir) / arc


def _authored_doc() -> dict:
    try:
        return json.loads(resources.data_path("audio_authored_names.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return {}


_LABELS: dict | None = None


def _labels() -> dict:
    """bank -> hash -> {name, category, team} for sounds whose real asset name is still unknown.

    Deliberately a *separate* file from ``audio_authored_names.json``. In that file ``name`` means
    "the authored asset name, verified because crc32(name) == the directory hash". These labels
    are transcribed from the audio instead ("And now, your Vancouver Canucks!"), so they would not
    hash — writing them in there would quietly destroy the one property that makes that file
    trustworthy. Optional data: missing file just means the raw `<bank>_<hash>` names show.
    """
    global _LABELS
    if _LABELS is None:
        try:
            doc = json.loads(resources.data_path("authored_sfx_labels.json")
                             .read_text(encoding="utf-8"))
            _LABELS = doc.get("banks") or {}
        except Exception:
            _LABELS = {}
    return _LABELS


def _locate_by_scan(game_dir, arc: str, entry_off: int) -> int | None:
    """Fallback for containers with no TOC name (e.g. `disc_aaea7e4d`).

    The sound directory lives inside the container, so the nearest `FF3BEF94` *behind* the first
    directory entry is the container head. Bounded to 4 MB so this never reads an archive whole.
    """
    path = _arc_path(game_dir, arc)
    start = max(0, entry_off - 0x400000)
    try:
        with open(path, "rb") as f:
            f.seek(start)
            buf = f.read(entry_off - start + 0x40)
    except OSError:
        return None
    needle = struct.pack(">I", IFF_MAGIC)
    found, i = None, buf.find(needle)
    while i != -1:
        found = start + i
        i = buf.find(needle, i + 1)
    return found


def bank_layout(game_dir, bank: str) -> dict | None:
    """{arc, base, wave_base, wave_size, dram_size, n} for an inline bank, or None.

    The container is located through the archive TOC (every bank is a normal TOC asset), not by
    scanning — `disc_*` names go through the same crc alias path as everywhere else. A couple of
    banks have no recovered TOC name at all; those fall back to `_locate_by_scan`.
    """
    arc = local = None
    try:
        got = at.resolve(bank, Path(game_dir), clean=True)
        if got:
            arc, local = got[0], got[1]
    except Exception:
        pass
    if local is None:
        entry = (_authored_doc().get("banks") or {}).get(bank) or {}
        arc = entry.get("arc")
        offs = [int(s["entry_off"], 16) for s in (entry.get("sounds") or []) if s.get("entry_off")]
        if not arc or not offs:
            return None
        local = _locate_by_scan(game_dir, arc, min(offs))
        if local is None:
            return None
    path = _arc_path(game_dir, arc)
    try:
        with open(path, "rb") as f:
            f.seek(local)
            head = f.read(0x100)
    except OSError:
        return None
    if len(head) < 0x60 or struct.unpack_from(">I", head, 0)[0] != IFF_MAGIC:
        return None
    nsec = struct.unpack_from(">I", head, 0x10)[0]
    if not 0 < nsec <= 8:
        return None
    out = {"arc": arc, "base": local, "path": path}
    for k in range(nsec):
        o = 0x20 + k * 0x20
        if o + 0x20 > len(head):
            break
        typ, _dup, _align, dec = struct.unpack_from(">4I", head, o)
        off, stored = struct.unpack_from(">2I", head, o + 0x14)
        if typ == SECT_WAVE:
            out["wave_base"] = local + off
            out["wave_size"] = stored
        elif typ == SECT_DRAM:
            out["dram_off"] = local + off
            out["dram_stored"] = stored
            out["dram_size"] = dec
    if "wave_base" not in out or "dram_size" not in out:
        return None
    return out


def _dram(lay: dict) -> bytes:
    """The decompressed DRAM section, or b'' if it doesn't come back at its declared size."""
    try:
        with open(lay["path"], "rb") as f:
            f.seek(lay["base"])
            blob = f.read(lay["dram_off"] - lay["base"] + lay["dram_stored"] + 0x40)
        for b in at._walk_blobs(blob, len(blob)):
            d = b.get("dec") or b""
            if len(d) == lay["dram_size"]:
                return d
    except Exception:
        pass
    return b""


def _dir_entries(game_dir, bank: str) -> list[dict]:
    """The bank's 20-byte sound-directory entries, read from the archive, in wave-offset order.

    ``audio_authored_names.json`` keeps only the hash / name / offsets, so the descriptor-offset
    field — the thing that actually links a name to its audio — is re-read here from the entry
    the harvester recorded. At most 288 entries per bank, so this is a few small seeks.
    """
    entry = (_authored_doc().get("banks") or {}).get(bank) or {}
    arc = entry.get("arc")
    rows = [s for s in (entry.get("sounds") or []) if s.get("entry_off")]
    if not arc or not rows:
        return []
    out = []
    try:
        with open(_arc_path(game_dir, arc), "rb") as f:
            for s in sorted(rows, key=lambda s: int(s["entry_off"], 16)):
                f.seek(int(s["entry_off"], 16))
                raw = f.read(20)
                if len(raw) < 20:
                    return []
                _hash, _tag, _two, desc_off, wave_off = struct.unpack(">5I", raw)
                out.append({"hash": s.get("hash"), "name": s.get("name") or "",
                            "desc_off": desc_off, "wave_off": wave_off})
    except OSError:
        return []
    out.sort(key=lambda s: s["wave_off"])
    return out


def _cdf_records(game_dir, bank: str) -> tuple[str, int, list[dict]]:
    """Walk a paired ``.cdf``: ``[0x2C descriptor][wave]`` repeated until the file runs out.

    Returns ``(arc, base, records)``; records is empty unless the walk lands *exactly* on the end
    of the file, which is the check that the framing is right rather than merely plausible.
    """
    got = None
    try:
        got = at.resolve(CDF_BANKS[bank], Path(game_dir), clean=True)
    except Exception:
        pass
    if not got:
        return "", 0, []
    arc, base, size = got[0], got[1], got[2]
    try:
        with open(_arc_path(game_dir, arc), "rb") as f:
            f.seek(base)
            data = f.read(size)
    except OSError:
        return "", 0, []
    recs, p = [], 0
    while p + DESC_SIZE <= len(data):
        codec, rate, sz = (struct.unpack_from(">I", data, p + 0x08)[0],
                           struct.unpack_from(">I", data, p + 0x10)[0],
                           struct.unpack_from(">I", data, p + 0x18)[0])
        # The old sanity check read +0x00 and asked for 0 < ch <= 6, which passed on anything
        # because +0x00 is always 1. Checking the format id instead actually rejects a bad walk.
        if codec not in CODEC_CHANNELS or not (1000 <= rate <= 96000) or sz <= 0:
            return "", 0, []
        recs.append({"codec": codec, "channels": CODEC_CHANNELS[codec],
                     "rate": rate, "size": sz, "off": p + DESC_SIZE})
        p += DESC_SIZE + sz
    if p != len(data):
        return "", 0, []
    return arc, base, recs


def sounds(game_dir, bank: str) -> list[dict]:
    """Every sound in an authored bank: name (when known), absolute archive offset, size, format.

    Each directory entry carries the byte offset of its own 0x2C descriptor, so the join is by
    that offset rather than by position — which is what lets the scene-graph banks and the `.cdf`
    banks use this same code path.
    """
    dir_sounds = _dir_entries(game_dir, bank)
    if not dir_sounds:
        return []

    if bank in CDF_BANKS:
        arc, base, recs = _cdf_records(game_dir, bank)
        if len(recs) != len(dir_sounds):
            return []
        descs, wave_base, wave_size = recs, base, 0            # offsets are already file-relative
    else:
        lay = bank_layout(game_dir, bank)
        if not lay:
            return []
        dram = _dram(lay)
        if not dram:
            return []
        arc, wave_base, wave_size = lay["arc"], lay["wave_base"], lay["wave_size"]
        descs = None

    offs = [s["wave_off"] for s in dir_sounds]
    out = []
    for i, s in enumerate(dir_sounds):
        if descs is not None:                                  # .cdf: descriptor offset is an index
            idx, rem = divmod(s["desc_off"], DESC_SIZE)
            if rem or not 0 <= idx < len(descs):
                continue
            d = descs[idx]
            abs_off, size = wave_base + d["off"], d["size"]
        else:                                                  # FF3BEF94: offset into the DRAM
            o = s["desc_off"]
            if o + DESC_SIZE > len(dram):
                continue
            r = dram[o:o + DESC_SIZE]
            codec = struct.unpack_from(">I", r, 0x08)[0]
            d = {"codec": codec,
                 "channels": CODEC_CHANNELS.get(codec, 1),
                 "rate": struct.unpack_from(">I", r, 0x10)[0],
                 "size": struct.unpack_from(">I", r, 0x18)[0],
                 "samples": struct.unpack_from(">I", r, 0x24)[0]}
            wo = s["wave_off"]
            # descriptor size and the gap to the next sound agree byte-for-byte everywhere except
            # one tiny container that carries a duplicated descriptor -- clamp rather than drop
            # the sound or read past the section
            limit = (offs[i + 1] if i + 1 < len(offs) else wave_size) - wo
            size = min(d["size"], limit) if d["size"] > 0 else limit
            abs_off = wave_base + wo
        if size < PACKET:
            continue
        lab = (_labels().get(bank) or {}).get(s.get("hash") or "") or {}
        out.append({
            "bank": bank,
            "index": i,
            "hash": s.get("hash"),
            "name": s.get("name") or "",
            "label": lab.get("name") or "",
            "label_category": lab.get("category") or "",
            "label_team": lab.get("team") or "",
            "arc": arc,
            "abs_off": abs_off,
            "wave_off": s["wave_off"],
            "size": size,
            "channels": d["channels"],
            # The floor used to be 8000, which is above a rate the game actually ships:
            # crowd-lferumble-loop_01 is authored at 6000 Hz (it is a sub-bass rumble, there is
            # nothing up there to keep) and the clamp silently rewrote it to 48000, decoding 512
            # of its 42,816 samples. Anything the encoder will take is better honoured than
            # second-guessed; the fallback is only for a descriptor that is obviously garbage.
            "sample_rate": d["rate"] if 1000 <= d["rate"] <= 96000 else 48000,
            "packets": size // PACKET,
        })
    return out


# ── decode ────────────────────────────────────────────────────────────────────

def _riff_xma2(raw: bytes, channels: int, rate: int) -> bytes:
    """Wrap a raw XMA2 stream for xma2encode.

    The sample count is the packets*2*512 upper bound. The descriptor's `+0x24` field was tried
    here first and makes no difference — xma2encode decodes to the packet content either way —
    and it is not a PCM frame count (a 1-packet, 16 kHz sound carries 7,071 there), so the
    duration is read back off the decoded WAV rather than computed from it.
    """
    n_pk = len(raw) // PACKET
    n_smp = n_pk * 2 * 512
    le16 = lambda v: struct.pack("<H", v & 0xFFFF)      # noqa: E731
    le32 = lambda v: struct.pack("<I", v & 0xFFFFFFFF)  # noqa: E731
    avg = max(1, (len(raw) * rate) // max(1, n_smp))
    fmt = le16(0x0166) + le16(channels) + le32(rate) + le32(avg)
    fmt += le16(PACKET) + le16(16) + le16(34)
    fmt += le16(1) + le32(4 if channels == 1 else 3) + le32(n_smp)
    fmt += le32(PACKET) + le32(0) + le32(n_smp)
    fmt += le32(0) * 2 + bytes([0, 4]) + le16(n_pk)
    h = b"RIFF" + le32(4 + 8 + len(fmt) + 8 + len(raw)) + b"WAVE"
    return h + b"fmt " + le32(len(fmt)) + fmt + b"data" + le32(len(raw)) + raw


def _decode(raw: bytes, snd: dict, out_wav: Path, xma2encode: str, runner=None) -> float:
    """Decode one sound to WAV; returns its real duration in seconds, or 0.0 on failure."""
    if len(raw) < PACKET:
        return 0.0
    raw = raw[:len(raw) // PACKET * PACKET]
    tmp = Path(tempfile.mkdtemp(prefix="nhl_sfx_"))
    x = tmp / "t.xma"
    try:
        x.write_bytes(_riff_xma2(raw, snd["channels"], snd["sample_rate"]))
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        cmd = [xma2encode, str(x), "/DecodeToPCM", str(out_wav)]
        r = (runner or _run)(cmd, capture_output=True, timeout=120)
        if r.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size <= 512:
            return 0.0
        with wave.open(str(out_wav), "rb") as wf:
            return wf.getnframes() / (wf.getframerate() or snd["sample_rate"])
    except Exception:
        return 0.0
    finally:
        try:
            x.unlink(missing_ok=True)
            tmp.rmdir()
        except OSError:
            pass


def extract(game_dir, root, xma2encode: str, banks=None, log=None,
            progress=None, workers: int = 8, runner=None) -> dict:
    """Decode the authored SFX to WAV and register them in the audio manifest.

    Returns {"ok", "failed", "skipped", "banks", "unsupported"}. Existing manifest entries are
    updated, never replaced wholesale, so a re-extract keeps any name/team the user set.
    """
    log = log or (lambda *_a: None)
    root = Path(root)
    want = list(banks) if banks else list(ALL_BANKS)

    manifest = astore.load_manifest(root)
    ex_root = astore.extracted_root(root)

    todo, seen_banks, skipped = [], [], 0
    for b in want:
        ss = sounds(game_dir, b)
        if not ss:
            log(f"[sfx] {b}: no readable descriptor table — skipped")
            continue
        seen_banks.append((b, len(ss)))
        for s in ss:
            s["display"] = display_name(s)
            s["category"] = s["label_category"] or category_for(s["name"], b)
            s["team"] = s["label_team"] or team_for(s["name"], b)
            s["rel"] = (Path(FOLDER.get(s["category"], "SFX")) / f"{s['display']}.wav").as_posix()
            s["key"] = astore.akey(s["arc"], s["abs_off"])
            # already decoded with its WAV still on disk -> nothing to do, same rule as op_extract
            if s["key"] in manifest and (ex_root / s["rel"]).exists():
                skipped += 1
                continue
            todo.append(s)
    if not todo:
        log(f"[sfx] nothing to decode ({skipped} already extracted)")
        return {"ok": 0, "failed": 0, "skipped": skipped,
                "banks": seen_banks, "unsupported": UNSUPPORTED}

    log(f"[sfx] {len(todo)} authored sound(s) to decode in {len(seen_banks)} bank(s)"
        + (f", {skipped} already extracted" if skipped else ""))

    # read every slice up front, one open file per archive -- the archives are multi-GB, so this
    # seeks rather than reading them whole
    by_arc: dict[str, list] = {}
    for s in todo:
        by_arc.setdefault(s["arc"], []).append(s)
    raws: dict[int, bytes] = {}
    for arc, group in by_arc.items():
        with open(_arc_path(game_dir, arc), "rb") as f:
            for s in sorted(group, key=lambda x: x["abs_off"]):
                f.seek(s["abs_off"])
                raws[id(s)] = f.read(s["size"])

    done = [0]
    total = len(todo)

    def one(s):
        wav = ex_root / s["rel"]
        dur = _decode(raws.get(id(s), b""), s, wav, xma2encode, runner)
        done[0] += 1
        if progress and (done[0] % 50 == 0 or done[0] == total):
            progress(done[0], total)
        return s, wav, dur

    results = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for r in ex.map(one, todo):
            results.append(r)

    ok = failed = 0
    for s, wav, dur in results:
        if dur <= 0:
            failed += 1
            continue
        ok += 1
        e = dict(manifest.get(s["key"]) or {})
        e.update({
            "fid": s["arc"],
            "offset": s["abs_off"],
            "packets": s["packets"],
            "channels": s["channels"],
            "sample_rate": s["sample_rate"],
            "duration": round(dur, 3),
            "wav": s["rel"],
            "size": wav.stat().st_size,
            "sha1": astore.sha1_file(wav),
            "source_bank": s["bank"],
        })
        # a name/category the user already set wins -- re-extract must not clobber their edits
        if not e.get("name"):
            e["name"] = s["display"]
        if not e.get("category"):
            e["category"] = s["category"]
        # explicit "" (not absent) stops the name-based guesser from filling a team in later --
        # which arena each sfx_arena bank belongs to is unknown, and a wrong tag is worse than
        # none (see team_tag.py). Only pasfx.iff actually spells its teams out.
        if not e.get("team"):
            e["team"] = s.get("team", "")
        manifest[s["key"]] = e
    astore.save_manifest(root, manifest)

    log(f"[sfx] decoded {ok}, failed {failed}"
        + (f", {skipped} already extracted" if skipped else ""))
    for b, n in seen_banks:
        log(f"[sfx]   {b}: {n} sound(s)")
    for b, why in UNSUPPORTED.items():
        log(f"[sfx] not extracted: {b} — {why}")
    return {"ok": ok, "failed": failed, "skipped": skipped,
            "banks": seen_banks, "unsupported": UNSUPPORTED}
