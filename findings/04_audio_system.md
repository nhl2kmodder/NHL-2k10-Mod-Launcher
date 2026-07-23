# 04 — Audio System (streams, banks, cues, authored names)

**Summary:** All NHL 2K10 game audio is raw XMA2 packed back-to-back in the `1A`/`1B` archives; separate IFF "sound bank" directories map each cue to a `{sample_rate, byte-offset, flags}` record that points at a wave in `1A`/`1B`, and the sound *name* system is CRC32-hashed (no uppercasing) with authored strings stored UTF-16BE in the XEX.

**Status:** Solved and verified. 100% of standard XMA2 streams are catalogued (80,347 parsed, 0 unparsed). Extract / replace / repitch works in the launcher **Audio** tab; banks parse + replace-linked in the **Audio Banks** tab. Name system fully reverse-engineered (162 authored names recovered) and shipped as a sortable **Bank / Team** column. Re-pointing a cue's offset (write-back of a bank record's `+0x18`) is NOT yet built.

---

## 1. Where the audio lives

All game audio is **raw XMA2**, packet size **2048 bytes**, packed contiguously (no per-file header, no XACT `.xwb` wrapper — the packing is custom) in two archives:

- **`1A`** — commentary / play-by-play (~37,300 streams). Its name-map declares default category `Commentary`, i.e. `1A` is the announcer archive.
- **`1B`** — goal horns, goal songs, PA announcer, music, SFX (~42,900 streams).

There is no file header: `1A` begins directly with the first XMA packet (`08 00 00 00 … FC`). Decoding uses XAudio2 XMA (`XMACreateContext` @ `0x8426440C`).

> **Important:** the `0A`/`0B` archives are **texture/asset** archives, not audio. In particular `arena_*.iff` (30 assets, magic `FF3BEF94`, in `0A`, CSV category `arena_audio`) is **NOT raw audio** — each is one `0x0E4837`-compressed ~4.7 MB blob that is an arena audio **event/sequence descriptor**, not XMA. The real arena horns/music are raw XMA in `1A`/`1B`. So `0A` correctly holds 0 audio streams; nothing is missed there.

### The stream scanner

A stream start is detected by the XMA packet-sequence pattern: bytes at `+0`, `+2048`, `+4096` have high-nibble `0x0`/`0x1`/`0x2` with a matching low nibble and `0xFC` at byte 6; channels = 1 if byte `+7 == 0x03` else 2; stream length = bytes to the next detected start; `min_pkts = 4`. Reference: `scan_streams()` in the launcher. `PACKET_SIZE = 2048`.

Catalogs are built by extraction from the working archives (byte-identical in size to CLEAN): `NHL2k10_Extracted_Files/{1A,1B,0B}_Audio_Catalog.json` — each entry has offset, packets, channels, duration, sample_rate, friendly name, category.

### Coverage (verified 2026-06-20)

- **100% of standard XMA2 streams parsed: 80,347 present / 80,351 in catalogs, 0 unparsed.**
  Per-archive present→parsed: `0A` 0/0, `0B` 202/203, `1A` 37,302/37,302, `1B` 42,843/42,846.
- Two scanner blind spots, both handled:
  1. **Short clips (<4 packets)** — 578 real ~0.4 s mono clips (128 in `1A`, 450 in `1B`) below the `min_pkts=4` threshold. **All 578 extracted** (`extract_short_clips.py`, 0 false positives) and added to the catalogs (`1A`→37,430, `1B`→43,305; launcher grew to **80,941 rows**). Named by offset stem. `1A`'s went in as `Commentary`; `1B`'s as `Unknown` (they sit at bank boundaries; neighbor-category inference was only 43/450 confident, so not applied).
  2. **Non-sequence-0 first packet** streams (head e.g. `D8`/`C8`/`F8`) — the heuristic can't auto-detect them; they're rescued only when hand-named in the names-map (e.g. `GoalHorn_Carolina/Nashville/Ottawa`, `Pre_Game_Faceoff_Generic_3`). These are extra coverage, not a gap; since `1A`/`1B` byte coverage is ~100% there is little room for large missed streams.

Verify anytime with `verify_audio_coverage.py` (report: `AUDIO_COVERAGE_REPORT.txt`).

---

## 2. How the game maps event → offset (the bank system)

The wave data has no per-sound index inside it. The index lives in separate **sound bank IFFs** (in `0A`/`0B`, `0x0E4837`-compressed) that the game loads by name via `Res_LoadAsset`. Each bank is a **directory** of records pointing into `1A`/`1B`.

**Chain:** game event/cue → context picks a bank (IFF directory) → a bank record gives `{sample_rate, byte-offset, flags}` → an XAudio2 XMA voice streams the wave from that offset in `1A`/`1B`.

### Banks loaded by name

- `bootup_audio.iff` — global bank; `Audio_LoadGlobalBank_bootup_audio` @ `0x83B68FF0`.
- **`arena_{team}.iff`** — per-arena; `Asset_GetArenaIffName` @ `0x83FD6214` formats `arena_<code>.iff` from the current team code (e.g. Vancouver → `arena_van.iff`); precache via `Tex_PrecacheArenas` @ `0x83FDBBA0`.
- `face_speech.iff` / commentary bank (play-by-play), `crowdloops.iff`.

### Record format (proven from `crowdloops.iff`, fixed 0x2C / 44-byte records)

| Offset | Field |
|---|---|
| `+0x0C` | **sample count** (see correction below — earlier docs guessed "id/hash") |
| `+0x10` | sample_rate (`0xBB80` = 48000) |
| `+0x18` | **wave byte-offset into `1A`/`1B`, `0x800`-aligned** |
| `+0x20` | flags (`0x20`) |
| `+0x24` | total samples |

Arena banks use a larger record (adding 3D-emitter / timing fields) but still embed the same absolute wave offsets. `arena_van.iff` (~14 MB) holds **5,480 references to real `1B` offsets + 640 to `1A`** — literally a pointer table into the wave archives.

### Event → cue → play

- `Sound_RegisterReactiveEvents` @ `0x83F9D860` maps gameplay physics events to numeric cue IDs: table of `(desc, internalId, soundId 0x0b..0x76, "SRE_*name")`, e.g. `SRE_SLAP_SHOT`→`0x1d`, `SRE_PUCK_NET`→`0x29`, `SRE_REF_WHISTLE`→`0x32`.
- `Sound_GetManager` @ `0x83FA03B8` (= `DAT_84FF4184`) → `Sound_AllocVoiceAndPlay` @ `0x83FA2830` calls manager vtable `+0x40` to resolve a descriptor → sound pointer (null → *"Voice Error - no sound pointer"*) → allocates a voice (`Function_84128910`) → XMA.

**Worked examples:**
- *Vancouver goal song*: home = VAN → load `arena_van.iff` → goal cue → its record → `1B` offset of Vancouver's goal song → play.
- *PBP goal call*: the commentary engine picks a goal-call phrase cue → speech/PBP bank record → offset in `1A` → play.

Full RE detail: `ghidra_re_reports/09_Audio_Bank_System.txt`.

---

## 3. The sound-name system (authored names)

Reverse-engineered 2026-07-18. Names ship **only as hashes**; plaintext exists only where the XEX composes a lookup.

### Name chain

1. Code composes a name, e.g. `"crowd-cheer-loop" + "_%02d"` → `crowd-cheer-loop_01`, or `sfx_arena%03d`, or a static `slap-shot`. Templates in the XEX: `%s_%02d` @ `0x83B2DAE8`, `%s_%02d.wav` @ `0x83B2DAF8`, `sfx_arena%03d` @ `0x83B2DB14`, `%s_01` @ `0x83B2EF6C`, more at `0x83B18000`.
2. **`Str_Hash` @ `0x84113740` = plain zlib CRC32** (init `0xFFFFFFFF`, final NOT inverted per usual crc32), taken over the *wide* string's low bytes. For ASCII names this equals `zlib.crc32(name.encode())`. **No uppercasing** — unlike the archive TOC's `crc32(name.UPPER())`.
3. The hash is looked up in the runtime registry @ `0x850D220C` (filled at bank load) via `Function_83FA05F8` / `Function_83FA0550`, using node-type args `0xBB05A9C1` and **`0x1AEDDA1F`** (the playable-sound node class).

### Bank IFF hash directory

Every audio bank IFF header carries a **20-byte-per-sound hash directory** (in the header region, NOT in the record blob):

```
[crc32(name)] [1A ED DA 1F] [00 00 00 02] [record_ref] [wave_off]
```

`0x1AEDDA1F` is a grep-able marker. A full `0A`+`0B` scan found **1,385 entries / 521 unique name hashes** across 12 banks:

| bank | entries | notes |
|---|---|---|
| `sfx_arena000–003.bnk` | 4×288 | same 288-name set (arena-acoustic SFX variants); the `.bnk` files are 16 MB each in `0B` |
| `disc_099f9a9d` | 84 | UI/presentation scene (whoosh1-12, misc1-4 …), 2 MB, `0B` |
| `paintro.iff` | 49 | PA intros; waves in `paintro.cdf` |
| `loading_audio.iff` | 40 | waves in `loading_audio.cdf` |
| `frontend.iff` | 29 | menu UI sounds (`menu-back_01`, `2klogo_01` …) |
| `pasfx.iff` | 14 | waves in `pasfx.cdf` |
| `crowdloops.iff` | 13 | `crowd-<type>-loop_01/02` (01 = front, 02 = rear speakers) |
| `bootup_audio.iff` / `disc_aaea7e4d` | 2+2 | |

- **Scene-container banks** (magic `FF3BEF94`: crowdloops, the `.bnk` files, jukebox, `disc_099f9a9d`) embed waves in a `76CBC6E7` section. **The in-container wave format is NOT the `1A`/`1B` raw-XMA2 packing** (no packet-sequence signatures; `xma2encode` won't decode slices) — codec still unknown, so these ~1,385 sounds are not yet in the Audio-tab catalogs and not yet extract/replace-able.
- **Directory-only banks** (magic `F0985030`: pasfx/paintro/loading_audio) pair with a same-stem **`.cdf` wave container** (e.g. `pasfx.cdf`, 635 KB, in the TOC); all entry wave offsets fit inside the paired `.cdf`.
- `jukebox.iff` = 13 tracks of node type **`0x5C369069`** = streamed-sound class (same hash previously seen as the "mesh parser handler" — it is NOT mesh-specific).

### Name recovery

Dictionary + structured brute-force vs the 521 hashes → **162 unique names cracked** (552 / 1,385 entries): all crowdloops (11/13), 125/288 of the `.bnk` set (`slap-shot`, `skate-left_00-13`, `gm-puck-handling-l`, `breath-*`, `cr-applaud-1-4` …), frontend partials (`menu-back/error/misc/forward_01`, `2klogo_01`), `whoosh1-12`/`misc1-4`. 359 remain uncracked (PA intro lines etc. — multi-part authored names never present in the XEX).

> **XEX string pitfall:** authored name strings are **UTF-16 BE** in the XEX (`\x00c\x00r\x00o\x00w\x00d…`) — an ASCII `strings` pass misses every one. Dump the full pool with `xextool.exe -b` (in the "NHL 2k10 Extracted" root): it produces a 52 MB flat basefile at `0x82000000` (the on-disk `default.xex` is AES+LZX; `file_offset = VA − 0x82000000` in the basefile).

### Bank / Team attribution (shipped)

`bank_parser.parse_bank` run over `gamedata.iff` + all 30 `arena_<code>.iff` records → which banks reference each catalogued `1A`/`1B` stream:

- `launcher/data/audio_bank_refs.json` — `{fid: {"0x<off>": [bank, …]}}`, **5,837 streams tagged**, **2,566 single-team** (goal-song / horn / team-PA candidates, incl. per-team `1A` announcer lines).
- `launcher/data/audio_authored_names.json` — the full 1,385-entry hash inventory with cracked names.
- `launcher/audio_names.py` — loads the refs, renders a display tag + search haystack.
- Audio tab: sortable **Bank / Team** column (`VAN only` = only Vancouver's bank references it; `ANA +21` = shared; `SFX` = gamedata-referenced; `All arenas`). Team dropdown + search box now match team names/codes via bank tags (e.g. `vancouver` → 384 rows).

---

## 4. Modding implications

Bank records store **absolute** `1A`/`1B` offsets, so moving a sound would require editing the offset in **every** bank that references it. That is why audio replacement is done **in-place / fit the original packet slot** (the same slot constraint as textures). Lower quality is auto-tried; an over-long replacement is offered truncation.

Re-pointing a cue (write-back of a bank record's `+0x18` offset into the compressed bank) is **not yet built**.

### In the launcher

- **Audio tab**: browse/search 80k+ tracks, play, **Replace…** (encode + fit-slot validation → staged in `Modified/Audio/`), **Patch Game** (write all staged into the archives). Sample-rate (pitch) and category/name edits are staged and applied together via **Apply Changes**. Requires `xma2encode.exe` and `ffmpeg.exe` (set in Settings; all calls run windowless).
- **Audio Banks tab**: pick a bank (e.g. `arena_van.iff`) → **Parse** → see each record's wave offset, sample rate, and the linked catalog sound. `bank_parser.parse_bank` extracts records two ways: **rate-anchored** (sample_rate dword, offset 8 bytes later) for clean banks (crowdloops/bootup), and **loose-ref** (any dword equal to a real catalogued stream offset, deduped) for arena banks. Play, **Replace Linked…** (shared `_replace_row` pipeline), Show in Audio Tab, Export Bank. Unlinked records (crowd/bootup) are not replaceable here.

---

## 5. Open questions / caveats

- **359 uncracked name hashes** (PA intro lines, most loading/pasfx) — the names are not in the XEX; next best source is hooking `Str_Hash` live.
- **In-container wave codec** (`.bnk` / `.cdf` / crowdloops `76CBC6E7` sections) is undecoded — until cracked, those ~1,385 sounds aren't extract/replace-able and don't appear in the Audio-tab catalogs (the `0B` catalog's 203 streams include none of them).
- **Commentary** (`1A`, ~37k) has no shipped authored names — bank tags are the practical ceiling unless the 2K speech-DB format is cracked.
- `jukebox.iff`'s 13 streamed tracks ↔ soundtrack song titles — titles likely live in `loc.iff`.
- **Cue offset write-back** (re-pointing `+0x18`) not implemented.

### Corrections vs old docs

- **Doc 04 / Doc 09 correction (superseded):** the 44-byte bank record field at `+0x0C` is a **sample count**, NOT an "id/hash". The name hashes live in the IFF **header** hash directory, not in the record blob. (Old doc 04 body labeled `+0x0C` as "id/hash"; only its later update banner noted the fix — this document supersedes the body.)
- Stream/row counts here reflect the post-short-clip state (80,941 launcher rows; catalogs `1A` 37,430 / `1B` 43,305).
