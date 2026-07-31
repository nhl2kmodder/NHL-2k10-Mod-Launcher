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

- **Scene-container banks** (magic `FF3BEF94`: crowdloops, the `.bnk` files, jukebox, `disc_099f9a9d`) embed waves in a `76CBC6E7` section. ~~The in-container wave format is NOT the `1A`/`1B` raw-XMA2 packing (no packet-sequence signatures; `xma2encode` won't decode slices) — codec still unknown~~ **CORRECTED 2026-07-30 — it *is* plain XMA2, identical to the `1A`/`1B` packing.** The earlier negative result came from slicing at the wrong base: the section record was read as a TOC-style entry, so the wave base was ~0x2000 off and every slice started mid-packet. See §7.6.
- **Directory-only banks** (magic `F0985030`: pasfx/paintro/loading_audio) pair with a same-stem **`.cdf` wave container** (e.g. `pasfx.cdf`, 635 KB, in the TOC). ~~all entry wave offsets fit inside the paired `.cdf`~~ **CORRECTED 2026-07-30 — `wave_off` does not address the `.cdf` at all**; the `.cdf` is a self-contained `[0x2C descriptor][wave]` run and the `.iff`'s own section records point past EOF. See §7.7.
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
- ~~**In-container wave codec** (`.bnk` / `.cdf` / crowdloops `76CBC6E7` sections) is undecoded — until cracked, those ~1,385 sounds aren't extract/replace-able and don't appear in the Audio-tab catalogs.~~ **FULLY RESOLVED (§7.6 + §7.7): all 1,385 authored sounds are located and 1,372 decode into the Audio tab** — the 13 failures are 1-packet stubs, not a format gap. The `F0985030` + `.cdf` banks and the two scene-graph containers fell to the descriptor-offset join in §7.7.
- **Commentary** (`1A`, ~37k) has no shipped authored names — bank tags are the practical ceiling unless the 2K speech-DB format is cracked.
- `jukebox.iff`'s 13 streamed tracks ↔ soundtrack song titles — titles likely live in `loc.iff`.
- **Cue offset write-back** (re-pointing `+0x18`) not implemented.

### Corrections vs old docs

- **Doc 04 / Doc 09 correction (superseded):** the 44-byte bank record field at `+0x0C` is a **sample count**, NOT an "id/hash". The name hashes live in the IFF **header** hash directory, not in the record blob. (Old doc 04 body labeled `+0x0C` as "id/hash"; only its later update banner noted the fix — this document supersedes the body.)
- Stream/row counts here reflect the post-short-clip state (80,941 launcher rows; catalogs `1A` 37,430 / `1B` 43,305).

---

## 6. Session 2026-07-29 PM10 — the containers are the only audio files that ship, and where Extract time really goes

### 6.1 The question

"Can we extract the `.bin` files out of the raw game files and have Xenia read straight from the
`.bin`s, so re-packing only touches a small file?" The 19 recovered bank names (`lines.bin`,
`horns.bin`, `paplyrs.bin`, …) made this look promising. It is not possible game-side, and it would
not have helped anyway. Both halves are worth recording because the reasoning generalises.

### 6.2 Game-side: no `.bin` ships, and the containers are a size split — VERIFIED

The game folder holds exactly four audio files — `0A`, `0B`, `1A`, `1B` — and no `.bin` of any kind.
`1A` and `0A` are **both exactly 1,803,550,720 bytes**, which is the same constant the bank layout
calls `VOL`. So `A`/`B` is not a content division at all: it is one logical byte space cut at a
fixed 1.68 GiB file-size limit, which is exactly why an offset `>= VOL` resolves into the second
file at `logical - VOL`. The 19 banks are concatenated inside that space.

There is therefore **nothing on disc for Xenia to read straight from**, and no per-bank file to
re-pack in place of the containers.

### 6.3 The `%s.bin` format string — real, but not a file open — PARTIALLY VERIFIED

Searching the XEX for `.bin` in ASCII returns **zero** hits — including zero `.iff` hits, which is
the tell that the encoding was wrong, not that the strings are absent. This XEX keeps asset-name
strings in **UTF-16BE** (94 `.iff` hits, 743 `vcsports`), the same encoding as the `Roster.ROS` name
pool. In UTF-16BE there are exactly two `.bin` strings:

| VA | string |
|---|---|
| `0x83B18044` | `%s.bin` |
| `0x83B18054` | `%s.bin` |
| `0x83B18064` | `CROWD_AUDIO` |
| `0x83B1807C` | `MUSIC_AUDIO` |

So the shipped build *does* format `<name>.bin` at runtime. Following it:

- `0x83B18044` is referenced once, by `lis r6,0x83B2` + `addi r4,r6,-0x7FBC` at **VA 0x83B6031C**,
  inside the function starting at **VA 0x83B60250**.
- That function first calls `0x8410D7C0` with `r4 = 0xBB05A9C1` (the cue-table/sound-bank class tag)
  and `r7 = 0x61DF2234`, stores the result at `+0x10` of its object, calls vtable slot `+0x28` to
  get a name, then calls `0x8412AC20(r3 = that object, r4 = "%s.bin", r5 = &out)`.
- `0x8412AC20` is a thin wrapper over `0x8412A9B0`, which builds a `{type=2, fmt, args, buf}` record,
  lazily initialises a global at `0x850D7A18`, and calls `0x84129930`.
- `0x84129930` is a **printf-family formatter**: it walks the wide format string looking for `0x25`
  (`'%'`) and emits literal runs through a sink vtable slot `+0x18`.

So `%s.bin` is consumed by string formatting, not by the VFS. Combined with 6.2 (no `.bin` on disc),
the honest reading is that this path builds a bank *name* — a label or a lookup key — and the shipped
data has no file behind it. **Not yet proven:** what the sink object ultimately does with the string.
It is not worth more RE time, because even a live file-open path would need `.bin` files that do not
exist and a game that prefers them over the containers (and per findings/13, stock override devices
only cover `roster.dat`).

### 6.4 Launcher-side: the bottleneck was never the container size — VERIFIED by measurement

| step | measured cost |
|---|---|
| `scan_streams` over `1A` (1.68 GB, 37,430 streams) | **1.8–2.8 s** |
| scanning all four containers | **~7 s** |
| one stream decode (temp dir + RIFF header + one `xma2encode.exe` spawn) | **≥0.145 s** |
| decoding all 80,969 catalogued streams | **≥3.3 h** |

The 0.145 s figure is a floor: the throwaway RIFF header used for the timing was wrong, so 0/12 of
those decodes actually produced audio and the number is essentially pure process-spawn latency. Real
decodes cost more.

Extract time is therefore **~100% per-stream `xma2encode.exe` process spawns**. Re-pack was already
surgical before this session — `op_reimport` opens the archive `r+b` and does `seek(off)` +
`write(raw_new)`, never a whole-container rewrite; the only bulk I/O is the one-time `.bak` copy.

**A `.bin` split would have optimised the 7 seconds and left the 3.3 hours untouched.** What actually
moves the number is doing less work and overlapping the spawns.

### 6.5 What shipped instead

- `launcher/data/audio_wave_banks.json` — the bank layout as shipping data (`vol`, the `1A/1B` and
  `0A/0B` pairs, each bank's base). Generated by `emit_wave_banks.py`, never hand-typed: the first
  hand-written attempt invented a duplicate entry sharing `horns.bin`'s base and mis-converted several
  hex bases. *(§7 supersedes this: the file is now generated from the archive TOC and carries exact
  sizes; `unpinned` and the invented `_gap_music` entry are gone.)*
- `launcher/wave_banks.py` — `bank_for(fid, offset)` maps a physical `(container, offset)` to its
  owning `.bin` via the pair's `vol` and `[base, next_base)` bounds; `all_banks()` lists them in
  layout order. Resolves **80,968 of 80,969** catalogued streams (the one miss is a `0B` stream below
  `jukeboxmusic`'s base, i.e. `femusic` territory — the one bank with no pinnable base).
  *(§7 correction: that reading of the miss was wrong, and the `[base, next_base)` bound was a real
  bug — the last bank in each pair had no upper bound and swallowed everything after it.)*
- Audio/Speech tabs: a **Wave Bank** column, a **Bin** filter dropdown, bin included in the search
  haystack, and a bin sort key. This is a different axis from the existing **Bank / Team** column:
  that one answers "which `arena_*.iff` sound banks reference this stream" (usage, many-to-one), the
  new one answers "which `.bin` does it physically live in" (storage, exactly one).
- **Bank-scoped, parallel Extract.** `op_extract` gained `banks=None` and `workers=6`; the Extract
  dialog gained a multi-select bank list (selecting nothing = everything, so old behaviour is the
  default) and a decoder-count picker. The bank filter is applied to both the scanner pass and the
  `nm_map` fallback pass — scoping only the first would have quietly re-decoded every named stream.

Measured, on a machine already saturated with 6 ASR workers:

- `env_amb.bin` + `horns.bin` from `1B`: 42,777 streams scanned → 21 selected → decoded, **4.5 s
  total**, versus hours for the whole container.
- `teams.bin` (118 streams), 1 decoder vs 6: **32.6 s → 13.7 s** (~2.7× on the decode itself). An
  idle machine should do better.

---

## 7. Session 2026-07-29 PM11 — the bank layout is an archive fact: all four containers are ONE TOC space

This section **supersedes every "bank base" claim above and in `16_player_audio_name_ids.md`**. The
bases were right; the method was unnecessary, the sizes were missing, and two conclusions drawn from
the missing sizes were wrong.

### 7.1 `0A` is the master TOC and every wave bank is an ordinary asset in it — VERIFIED

`0A`, `0B`, `1A`, `1B` are not "two asset containers plus two audio containers". They are **one
TOC-addressed asset space**, indexed by the TOC at the head of `0A` (entry count at `0x10`, per-arc
sizes at `0x18 + i*16` in units of `0x800`, 16-byte entries from `0x58` as
`(flags, size, crc32(NAME.UPPER), f3)`, global offset `= f3 * 0x800`). 2,407 entries. Global bounds:

| arc | global range |
|---|---|
| `0A` | `0x000000000` – `0x06B800000` |
| `0B` | `0x06B800000` – `0x0B3D15000` |
| `1A` | `0x0B3D15000` – `0x11F515000` |
| `1B` | `0x11F515000` – `0x17C4D3000` |

And **every wave bank is an entry in that TOC under the plain name `<bank>.bin`** —
`crc32("LINES.BIN")`, `crc32("HORNS.BIN")`, and so on. All 19 known banks resolve, contiguous, with
zero gaps. So a bank's base *and* its exact size are archive facts read straight out of the index.
The whole base-fitting exercise (matching cue offsets against the XMA2 stream-start test) was
reproducing data the game had already written down.

*How it was found:* the TOC keys on `crc32` of the uppercased name, which is one-way — but the xex
carries the name strings, in **UTF-16BE**. (An ASCII string scan of this xex finds zero `.iff`; that
is how the wrong encoding announces itself.) Hash every xex string in both encodings against the TOC
and see which entries light up: `scratchpad/toc_names.py`, which names 48/2,407 that way.

⚠ A first pass at this reported only 2/19 bank names hitting the TOC, and I briefly concluded the
`1A`/`1B` banks were *not* TOC entries. That was a bug in my probe — iterating
`json["banks"].items()` as if it were keyed by bank name when it is keyed by pair label (`"1A/1B"`,
`"0A/0B"`). After fixing the iteration all 19 hit, and a brute force confirmed the key is plainly
`crc32("<name>.BIN")`.

### 7.2 Three consequences — two of them corrections

**`femusic.bin` is PINNED** — `0A/0B`-logical `0xAFEC8800`, size **2,048 bytes**. It is a
single-packet stub. Fitting it was hopeless *because* it is one packet: a lone cue matches thousands
of candidate bases. It never needed fitting. It contributes 0 catalogue rows (`scan_streams` has
`min_pkts=4`), and the real front-end music is elsewhere — see §7.3.

**`_gap_music` @ `0x792C0000` was NEVER A BANK — DISPROVEN.** `pamusic.bin` spans
`0x75224000 + 0x13A1F000 = 0x88C43000`, so that offset is strictly *interior* to `pamusic`. The
entry has been deleted from `audio_wave_banks.json`. (It was invented to explain a 249.5 MB span that
the base-only layout appeared to leave unclaimed; with real sizes there is no span to explain.)

**`0A`/`0B` are the asset archive, NOT "the 5.1 counterpart pair" — CORRECTION** to §6, to
`project_audio_bank_layout.md`, and to my own earlier speculation. They are 99.3–99.9% compressed
*asset* data. Their entire audio content is `femusic` + `jukeboxmusic` (~62 MB) plus the four
front-end banks of §7.3, all packed at the tail of `0B`. A strict-scanner census of `0A`+`0B` finds
only **186** authored streams in 3.4 GB (`scratchpad/authored_0a.py`, `census_0a.py`).

### 7.3 Four more wave banks found — content identified, names unrecoverable — PARTIALLY VERIFIED

Immediately after `jukeboxmusic.bin`, four TOC entries run to the exact end of the `0A`/`0B` pair:

| TOC crc | logical span | size | content (ASR-sampled) |
|---|---|---|---|
| `F256379A` | `0xB35C3800`–`0xB372D800` | 1,482,752 | 61 mono matchup teasers — *"Coming up on 2K Sports."*, *"An upset or a blowout?"* |
| `A8FA3038` | `0xB372D800`–`0xB3937000` | 2,136,064 | 118 mono team names — *"The Anaheim Ducks."*, *"and the Minnesota Wild."* (118 = `teams.bin`'s count) |
| `9B8A12F1` | `0xB3937000`–`0xB3ABA800` | 1,587,200 | 5 stereo instrumentals, 12–21 s — the real menu music |
| `80920C42` | `0xB3ABA800`–`0xB3D15000` | 2,467,840 | 5 strict starts only; multi-stream, like `jukeboxmusic` |

They are unmistakably wave banks: each begins `08 00 00 00 … FC`, the XMA2 stream-start pattern
(`frame_offset_in_bits == 0 && skip_count == 0`). That the fourth ends *exactly* at `0xB3D15000`, the
last byte of the pair, is an independent check on the whole layout.

**Their filenames are not recoverable, and this is a negative result worth trusting** — four
independent sources were exhausted:

- **`gamedata.iff` + `global.iff` decompressed** name exactly the 19 known banks and nothing else
  (`scratchpad/scan_bin_names.py`).
- **The raw containers**: 0 `*.bin` name strings — TOC assets are 0E4837-compressed, so raw scans
  can't see names at all. This is why a raw scan is not evidence of absence *and* why the decompressed
  scan above is the one that counts.
- **The xex**: its only `.bin` strings are the two `%s.bin` printf templates of §6.3.
- **The front-end scene containers** (`frontend.iff`, `frontend_sync.iff`, `titlepage.iff`): all three
  are magic `FF3BEF94` (arena/scene containers), which `decode_e4837_fixed.decompress_payload`
  rejects; a raw scan of them yields nothing. A `FF3BEF94` reader is the one remaining avenue.

crc32 is one-way and no wordlist guessed them, so they shipped under **synthetic, traceable** names
flagged `synthetic_name: true` in the data file: `fe_matchup_disc_f256379a.bin`,
`fe_teams_disc_a8fa3038.bin`, `fe_music_disc_9b8a12f1.bin`, `fe_disc_80920c42.bin` — following the
project's existing `disc_<crc>` convention for unnamed assets.

#### 7.3.1 SOLVED — all four are loading-screen banks, named inside `loading.iff` — *verified (static, exact hash match)*

The `FF3BEF94` reader flagged above was written (`scratchpad/scene_names.py`): walk the TOC,
decompress **blob[0] (the DRAM section)** of every `FF3BEF94` container via
`archive_textures._walk_blobs`, and harvest identifier-shaped runs from it in ASCII **and** both
UTF-16 byte phases. 2,301 containers, 2,301 decompressed, 0 failures, **967,751 unique strings**.

All four target hashes hit, and every one of them came out of the *same* container —
**`loading.iff`** (`0xB4720031`, `0A`), whose DRAM was already known to be a scene graph:

| crc32 | real name | old synthetic name |
| --- | --- | --- |
| `F256379A` | **`loadingaudio_lines.bin`** | `fe_matchup_disc_f256379a.bin` |
| `A8FA3038` | **`loadingaudio_teams.bin`** | `fe_teams_disc_a8fa3038.bin` |
| `9B8A12F1` | **`loadingmusic.bin`** | `fe_music_disc_9b8a12f1.bin` |
| `80920C42` | **`loadingsequence.bin`** | `fe_disc_80920c42.bin` |

Each verified by `crc32(name.upper()) == recorded crc32`. Three of the four were independently
found first by a vocabulary brute force (3.66 M candidates) and then confirmed by the scene
harvest; `loadingsequence.bin` came only from the scene harvest. These are **not** front-end/menu
banks as the `fe_` prefix guessed — they are the *loading screen's* audio (its music, its team
name-calls, its commentary lines, and its sequence/one-shot bank). The four spans are contiguous in
`0B`, which fits: they are loaded and dropped together.

`launcher/data/audio_wave_banks.json` now carries the real names with `synthetic_name: false`.
**Zero synthetic bank names remain — all 23 wave banks are named from game data.**

#### 7.3.2 Side effect: the scene DRAM is a general asset-name source — *verified (static)*

The same 967,751-string pool was run against every TOC crc32 not already in
`named_assets.csv`/`discovered_assets.csv` (10.3 M candidates over 10 extension variants). It named
**48 previously-unnamed TOC entries**, now merged into `launcher/data/named_assets.csv`
(1,399 → 1,447 rows). Highlights:

- **A correction:** `0xB9610AAC`, the 68.9 MB headshot archive this project has always called
  `disc_b9610aac.iff`, is really **`portrait.cdf`** — a `.cdf` companion to the known
  `portrait.iff` index, exactly the `pasfx.iff`/`pasfx.cdf` pairing seen in §14.
- 12 of the wave banks themselves (they were in `audio_wave_banks.json` but had never been added to
  `named_assets.csv`); their resolved offsets independently re-confirm the §7 layout
  (`lines.bin` at `1A:0x0`, `pamusic.bin` at `1B:0x9A24000`, …).
- 5 localisation containers — `English/French/German/Finnish/Swedish.iff`.
- UI/scene assets: `player_stats*.iff` (5), `skills_welcome_*.iff` (5), `zamboni_*.iff` (6),
  `fight_meters.iff`, `goalie_control.iff`, `pressbook_control_panel.iff`, `TEAM_VS.iff`,
  `INJURY.iff`, `champ_skate_around.iff`, `game_user_assign.iff`, `teamup_gamertag.iff`.

**Method note worth reusing:** scene-graph DRAM references other assets *by name string*, so it is
a name oracle for the whole archive, not just for audio. 960 TOC entries are still unnamed — the
pool did not cover them, but the technique is not exhausted (only 10 extension variants and no
prefix/path combining were tried here).

### 7.4 The bug the missing sizes were hiding — VERIFIED by measurement

`wave_banks.py` bounded each span as `[base, next_base)`, which leaves **the last bank of each pair
running to infinity**. Every stream past a final bank's true end was therefore mislabelled as that
bank. Concretely: 192 `0B` streams that actually belong to the four §7.3 banks were being reported as
`jukeboxmusic.bin` (which drops from ~208 rows to its true **16**), and anything past `teams.bin`'s
end in `1B` would have been reported as `teams.bin`. Emitting `end` from the TOC size fixes it.

Result: **80,968 of 80,969** catalogued streams attributed. The single exception is honest and is
*not* the old §6.5 miss: `0B` + `0x047F1980` is not in any bank — it sits inside ordinary TOC asset
`1A24756F` (890 KB at global `0x6FF42000`), and is not even `0x800`-aligned. It is audio embedded in
an asset, correctly reported as "no bank".

Per-bank row counts after the fix — `lines` 18,437 · `paplyrs` 14,119 · `players` 20,097 ·
`lines_ps` 10,109 · `lines_ts` 6,655 · `streamedchatter` 3,700 · `palines` 3,141 · `chatter` 1,558 ·
`playercom2` 746 · `playercom` 732 · `crowd` 862 (559 in `1A` + 303 in `1B`) · `pamusic` 317 ·
`teams` 120 · `chants` 110 · `horns` 44 · `env_amb` 12 · `crowd-idle-loop` 2 · `jukeboxmusic` 16 ·
`femusic` 0 · plus 191 across the four front-end banks.

### 7.5 Status

- TOC layout, exact bases + sizes, `femusic` pin, `_gap_music` disproof, `0A`/`0B` identity,
  the four extra banks' spans and the 100%-attribution result: **verified** (archive facts, measured).
- The four banks' *content* labels: **partially verified** (ASR on sampled streams, not heard in-game).
- Their *names*: **unknown**, and not derivable from any source checked here.

### 7.6 The gameplay SFX are decodable after all — the in-container codec is plain XMA2 — *verified (static)*

The user's report — *"I don't see any sound effects. Posts, skating, stick collision, puck hitting
glass, puck hitting pads… whistles"* — has two causes, and only one of them was a mystery.

**Cause 1 (not a mystery).** `launcher/data/audio_authored_names.json` has held the 1,385-entry
authored-sound inventory since the naming pass, but **no code ever loaded it** — `audio_names.py`
documents it in its module docstring and then only ever reads `audio_bank_refs.json`. The Audio tab
renders purely from the extract manifest, and these sounds were not extractable, so they had no
rows.

**Cause 2 (the wrong finding above).** §3 recorded the in-container wave codec as unknown because
`xma2encode` refused to decode slices. That was a **slicing error, not a codec**. The section
records in an `FF3BEF94` container are **not** TOC entries — they are 0x20 bytes starting at
`+0x20`:

```
+0x00 type   +0x04 type (dup)   +0x08 align   +0x0C decompressed size
+0x10 ?      +0x14 OFFSET       +0x18 STORED size   +0x1C 0
```

Reading them as `0x58 + i*16` TOC entries put the wave base ~0x2000 low, so every slice began
mid-packet. The offsets chain exactly once read correctly — for `sfx_arena000.bnk`:
`dram_off 0x1B60 + dram_stored 0xC8C = wave_off 0x27EC`, and `0x27EC + wave_size 0xF53000 =
0xF557EC = 16,078,828 = the container's TOC size`. XMA2 packet alignment is relative to the
**stream**, not the file, which is why a wave base that is not `0x800`-aligned still decodes.

**The DRAM section is the descriptor table.** `0xBB05A9C1` decompresses (0E4837) to exactly
`0x2C × n_sounds` — the same 0x2C descriptor documented for `pasfx.cdf` in §11.1:

| off | field | check |
|---|---|---|
| `+0x00` | channels | all 1 across 1,169 sounds |
| `+0x10` | sample rate | **16000 / 22050 / 44100 / 48000 — per sound, not per bank** |
| `+0x18` | byte size | matches the gap to the next `wave_off` byte-for-byte |
| `+0x24` | — | **NOT a PCM frame count** (a 1-packet 16 kHz sound carries 7,071); duration is read back off the decoded WAV |

Records are positional in these banks: record *i* describes the *i*-th sound in `wave_off` order.
**That is a coincidence of group A, not the rule** — see §7.7 for the actual join, which is stated
by the directory entry itself.

**Why this is graded verified and not "it decoded, ship it".** `xma2encode` decodes garbage into
noise, so a successful decode proves nothing on its own. Three independent checks:

1. **Structural.** `sfx_arena000..003` are supposed to be the *same 288 sounds* in four arena
   acoustics. Envelope cosine for the **same name across banks = +0.994**; for **different names =
   +0.361**. Garbage decoding cannot manufacture that.
2. **Semantic.** The shortest decoded sounds are `puck-stick_02` (0.068 s), `menu-back_00`
   (0.072 s), `ref-whistle_00` (0.100 s); the longest are `skate-forward-loop_01` (3.50 s),
   `puck-hit-board_02` (3.78 s), `against-boards_01` (4.31 s). Ticks are short, loops and board
   impacts are long — a mis-joined name table would not sort that way.
3. **Signal shape.** Crest factors 8–25 with ZCR ≈ 0.1 (impulsive real SFX); white noise is
   crest ≈ 3.5, ZCR ≈ 0.5.

**Coverage — all 1,385 sounds are located; 1,372 decode** (see §7.7 for groups B and C):

| group | banks | sounds | status |
|---|---|---|---|
| A | `sfx_arena000–003.bnk`, `crowdloops.iff`, `bootup_audio.iff`, `disc_aaea7e4d` | 1,169 | **1,157 decoded** (12 fail: 3 distinct 1–4-packet stubs, replicated across banks) |
| B | `pasfx.iff`, `paintro.iff`, `loading_audio.iff` (`F0985030` + paired `.cdf`) | 103 | **103 decoded** — §7.7 |
| C | `frontend.iff`, `disc_099f9a9d` | 113 | **112 decoded** (1 one-packet stub) — §7.7 |

**Shipped:** `launcher/authored_sfx.py` (container parse → descriptor table → XMA2 decode →
manifest merge), wired into the existing Extract dialog behind a "Also extract gameplay SFX"
checkbox. Rows land in the Audio tab under categories `Arena_SFX` / `Whistle` / `Crowd_Ambient` /
`SFX`, with the container name in the **Wave Bank** column (these are in no `.bin`). The four arena
banks carry the same 288 names, so display names get an `_arena0..3` suffix. **Team is written as an
explicit empty string** — which arena each `sfx_arena00N` set belongs to is unknown, and a wrong tag
is worse than none.

**Not verified:** nothing here has been heard in-game, and **replacement is untested**. Re-import
goes through the same bounds-checked slot-fit path as every other sound (`op_reimport` pads or
skips, and backs the archive up to `.bak` first), but no edited SFX has been written back and
launched.

---

### 7.7 The directory entry says which descriptor is yours — the last 216 sounds — *verified (static)*

§7.6 left two families deferred. Both fell to **one** correction, and it was a misreading in §7.6
itself, not a new format.

**The 4th field of the 20-byte sound-directory entry is a byte offset into the decompressed DRAM.**

```
[ crc32(name) ] [ 0x1AEDDA1F ] [ 2 ] [ DESCRIPTOR OFFSET ] [ wave_off ]
```

§7.6 called it an opaque "ref" and joined names to descriptors *positionally* (record *i* ↔ *i*-th
`wave_off`). That happens to give the same answer in group A, because there the DRAM is nothing but
a dense `0x2C × n` table in `wave_off` order — so the coincidence hid the real rule. Following the
offset instead is what opened both remaining groups. Confirmed by checking descriptor `size@+0x18`
against the gap to the next `wave_off`: **288/288 ×4 arena banks, 13/13 crowdloops, 2/2 bootup,
29/29 frontend, 84/84 `disc_099f9a9d`** — byte-exact, no positional assumption anywhere.

**Group C (`frontend.iff`, `disc_099f9a9d`) — the table is *inside* the scene graph.** These are
`FF3BEF94` containers whose DRAM is a full scene graph (§ the overlay/scene work), so `dram_size` is
not a multiple of `0x2C` (11,860 and 453,392 bytes) and there is no dense table to walk. The
descriptor block is simply *embedded* at `0x2958` (frontend) and `0x6DCA0` (`disc_099f9a9d`), and
`desc_off` points straight at the right record. Nothing about the descriptor changed. **112 of 113
decode**; the one failure is a 1-packet 2048-byte 16 kHz stub, the same class as group A's 12.

**Group B (`F0985030` + `.cdf`) — the `.iff` is a stub, the `.cdf` is the whole bank.**
The `.iff`'s `FF3BEF94` section records point **past EOF** — they are placeholders, which is why
§7.6's "framing base is shifted by something not yet identified" went nowhere: there was nothing to
frame. The paired `.cdf` is self-contained and trivially regular:

```
[0x2C descriptor][wave data of size @+0x18]  repeated, tiling the file exactly
```

The physical walk lands on **exactly EOF** in all three files — 14 records → `0x9B268`, 49 →
`0x2A186C`, 40 → `0x3D26E0`. `desc_off // 0x2C` is the index into that run.

> **Correction to §11.1.** The documented `.cdf` layout *is* right; §7.6's claim that it "does not
> hold past the first sound" was wrong. The error was using `wave_off` as a `.cdf` offset —
> **`wave_off` does not address the `.cdf` at all.** Its deltas are a permutation-subset of the
> `align(size, 0x800)` values, i.e. it describes some other (runtime) packing. It is used only to
> order the sounds.

Independent check on group B: the resulting name order for `pasfx.cdf` comes out **alphabetical**
(blue-jackets → bruins → cheechoo → coyotes → ducks → hurricanes-goal → hurricanes-thunder → kings
→ panthers → predators_01/02 → sharks_01/02/03) — an ordering nothing in the join method imposes.
Durations are semantically right too: `panthers-growl_01` 2.05 s (shortest), `kings-bells_01`
7.56 s (longest), `bruins-siren_01` 6.74 s, `cheechoo-train_01` 6.48 s.

**What the two unnamed banks actually are** (settled by ASR — faster-whisper, VAD off):

| bank | content | evidence |
|---|---|---|
| `paintro.iff` (49) | **PA team introductions** | *"And now, your Detroit Red Wings!"*, *"…your Belarus Olympic team!"*, *"…your Eastern Conference All Stars!"*, *"…your home team!"* |
| `loading_audio.iff` (40) | **PA crowd-hype lines** | *"Alright Canucks fans, it's hockey time! Time to get loud!"*, *"…let's hear your voices, Carolina!"*, *"…this is Hockey Town"* |

`paintro`'s 49 slots account **exactly**: 30 NHL teams + 14 Olympic nations + 4 All-Star squads
(Eastern / Western / North American / World) + 1 "home team". Both banks are categorised
`PA_English` (folder `PA`); `pasfx.iff` is `Goal_SFX`, 13 of its 14 team-tagged (`cheechoo-train_01`
is a player name, deliberately untagged).

**These three banks carry no cracked asset name at all** — the crc32 directory hash has no
preimage for them — so the transcripts are the only name source. They are therefore kept in a
**separate** file, `launcher/data/authored_sfx_labels.json`, *not* in
`audio_authored_names.json`, where `name` carries the hard property `crc32(name) == the directory
hash`. Writing descriptions into that file would silently destroy it. 71 of 89 clips are labelled
(47/49 `paintro`, 24/40 `loading_audio`); the rest are left blank on purpose — 16 hype lines name
no team at all, and 2 `paintro` clips defeat both the `small` and `medium` models (transcribed
"Johnson and a Hyde" / "vote Kalinier"). By the 49-slot accounting those two must be **Anaheim and
Montreal**, but which is which is unconfirmed, so neither is guessed. One Olympic nation is
likewise unresolved and ships as `PA_Intro_Olympic_Unidentified`.

**Not verified:** as with §7.6, none of this has been heard in-game.

---

## 8. Session 2026-07-30 — the phrase banks are named from their own transcripts (63,475 / 80,969)

### 8.1 Why this needed a different method from the name banks — VERIFIED

`players.bin` / `paplyrs.bin` / `teams.bin` are **name banks**: a fixed set of takes per surname,
stored in alphabetical runs, so the win there was a monotone alphabetical-run DP against the
`Roster.ROS` surname vocabulary (see `16_player_audio_name_ids.md`).

`lines` / `lines_ts` / `palines` / `chatter` / `streamedchatter` are **phrase banks** — each stream
is a whole spoken line, in no sortable order, with no vocabulary to snap to. There is nothing to
align, so **the transcript is the name**. Per-stream ASR (faster-whisper `small`, `vad_filter=False`)
transcribed all of them; empty-transcript rate on `lines.bin` is only **76/18,373 (0.4%)**.

Names are slugified to CamelCase (leading/trailing filler trimmed, ≤6 words, ≤46 chars, colliding
names get `_Var2…`), e.g. `PxP_SaveWithTheBlocker`, `PxP_FiredHardFromThePoint`,
`PA_LadiesAndGentlemenPleaseDirectYour`, `Chatter_Str_HoldTheLineHoldTheLine`.

The transcript deliberately goes into the **name**, not a new field, for two reasons: the Audio/Speech
tab already searches the name column, so `PxP_SaveWithTheBlocker` is findable by typing "save" with no
UI work; and `merge_speech_seed()` copies seed entries wholesale into the user's editable
`<fid>_Audio_Names.json`, so an extra key would change the shape of a user-visible file for no gain.

### 8.2 ASR must NOT be used on the non-speech banks — VERIFIED, and it fails loudly in the wrong direction

Run over music/noise, the model does not return empty — it **confabulates**. `horns.bin`'s first goal
horn transcribed as *"galore galore"*; `env_amb.bin`'s first stream as *"aw TRELL"*. A shipped name
of `Horn_GaloreGalore` is worse than no name at all: it is invented content in a user-visible column
that looks authoritative. So `horns` / `env_amb` / `pamusic` / `crowd*` get **positional** names
(`Horn_01`, `Amb_01`, `Music_PA_01`) in layout order, and the transcript is discarded.

### 8.3 Bug worth recording: seed keys are PHYSICAL offsets, ASR rows are LOGICAL — VERIFIED

The first merge keyed all 6,910 `1B` names by the **logical** bank-layout offset. Below `VOL` the two
coincide, so every `1A` bank looked correct while every `1B` bank produced keys matching **no
catalogue row**: `palines`' first stream is logical `0x6F403800` but catalogued as
`03C03800_1ch_40p` (`logical - VOL`). Nothing crashed and nothing warned — the names were simply
invisible in the GUI. Caught by cross-checking seeded keys against `wave_banks.bank_for()`, where all
6,910 came back "no bank".

`gen_phrase_names.py` now converts logical→physical from the arithmetic (not from the row's own `vol`
label, which is only what the ASR script wrote) and **hard-gates the merge**: every generated key must
match a real catalogue row or nothing is written. The bad merge was rolled back exactly, by deleting
only the seed entries byte-identical to the generated ones.

### 8.4 Result

`launcher/data/speech_seed_names.json`: **30,634 → 63,475** names = **78.4%** of all 80,969
catalogued streams, 0 malformed, 0 unresolvable to a bank, existing names never overwritten
(657 pre-existing kept).

| bank | named | | bank | named |
|---|---|---|---|---|
| `players.bin` | 19,701 | | `chatter.bin` | 1,558 |
| `lines.bin` | 18,373 | | `teams.bin` | 118 |
| `lines_ps.bin` | 10,048 | | `chants.bin` | 110 |
| `lines_ts.bin` | 6,655 | | `pamusic.bin` | 38 |
| `streamedchatter.bin` | 3,700 | | `horns.bin` | 36 |
| `palines.bin` | 3,126 | | `env_amb.bin` | 10 |

Still unnamed (~17.5k): `paplyrs.bin` 14,119 (ASR/segmentation outstanding), `crowd` 862,
`playercom2` 746, `playercom` 732 (all three still transcribing), the 191 front-end-bank streams,
and 16 `jukeboxmusic`.

**Status:** the names themselves are **partially verified** — the offsets, keys and bank attribution
are verified (checked against the catalogues and the TOC layout), but the transcripts are machine
ASR that has not been ear-checked beyond the 9/9 spot-validation recorded in
`16_player_audio_name_ids.md`. Expect occasional wrong words in a slug; the offset it points at is
right.

> **Correction (§9):** the `horns.bin` row above says 36 named. The bank actually holds **44**
> catalogue rows — `EXPECT["horns"] = 36` in `gen_phrase_names.py` was copied from a stale layout
> note, so the positional pass covered only 36 of them *and* numbered them out of layout order.
> All 44 are now named from the user's in-game-proven set; see §9.2. `playercom`/`playercom2` in
> the "still transcribing" list were likewise superseded — they were already hand-named.

---

## 9. Session 2026-07-30 — curated names promoted into shipping data; horns closed; paplyrs cracked structurally

### 9.1 The user's own names file is the best name source available — VERIFIED

`NHL2k10_Extracted_Files\<fid>_Audio_Names.json` is the launcher's **user-editable** names map,
regenerated output of an extract. It held **2,790 hand-curated entries** across 16 banks — including
`playercom` 724/732 and `playercom2` 741/746, the two banks an ASR pass was about to name. Machine
slugs over ear-verified names would have been a straight downgrade.

`merge_speech_seed()` already protects them *locally* (it only fills gaps in the user's map), but
they live in regenerated output, so **a clean re-extract loses them**. Promoting them into
`launcher/data/speech_seed_names.json` makes them permanent and re-seeded afterwards.
`scratchpad/bake_curated.py` does this, with curated always beating generated; `gen_phrase_names.py`
grew a `CURATED` set (`horns`, `playercom`, `playercom2`) that it refuses to emit for, so a re-run
with `--overwrite` cannot clobber them.

Unexpected win: the curated set also covers the four **loading-screen banks** whose names were
unrecoverable in §7.3 — `loadingaudio_teams` 115/118, `loadingaudio_lines` 56/61, `loadingmusic`
5/5, `loadingsequence` 5/5, plus 13 `jukeboxmusic`. Those banks were content-identified *and*
stream-named well before their bank names were cracked in §7.3.1 — and the content matches the
recovered names, which is corroborating evidence for them.

Curated entries carry richer fields than generated ones (`team`, `sample_rate`, `notes`); the seed's
shape is whatever the names map accepts, so they are copied verbatim.

### 9.2 `horns.bin`: 44 rows, 39 distinct offsets, 5 offsets duplicated — VERIFIED

All 44 rows are now named from the user's set, which they had **replaced and confirmed working
in-game** — the strongest grade of evidence available in this project. Layout is alphabetical by
city, with **Arizona filed under "Phoenix"** (it sits between Philadelphia and Pittsburgh), then a
tail block of alternates: Columbus (a cannon, not a horn), Atlanta full + short, Dallas full +
short, `GoalHorn_Unidentified_03BA2000`, and `Period_Horn_1..4`.

**Five offsets appear twice with different packet counts** — this is the mechanism behind "multiple
horns in one stem":

| offset | rows | reading |
|---|---|---|
| `0x037E4000` | 77p / 159p | Calgary alone / Calgary + dead air + Carolina |
| `0x0390E000` | 108p / 172p | Montreal / Montreal + Nashville |
| `0x039B3800` | 63p / 130p | NY Rangers / NYR + Ottawa |
| `0x03A10800` | 92p / 662p | Arizona / Arizona + everything after |
| `0x03BA2000` | 21p / 70p | both unidentified |

The long row in each pair is **stale scanner output** from a pass that had not yet detected the
intermediate stream start, so its length ran on into the following horn. The short row is the
correct single-horn cut. **Limitation:** the names map is keyed by offset alone, so the long and
short rows at one offset necessarily share a single name entry and cannot be named separately.

Open (task #14, user-flagged stretch goal): how the game *chooses* a horn — the Atlanta/Dallas
full-vs-single-blast pairing, Columbus's cannon, and the mapping of teams to `Period_Horn_1..4` vs a
siren (the user wants Montreal and Boston on a siren, as in real life). Most promising lead is the
`horns.bin` post-table ID array from §PM9: **29 IDs over 39 cues** — shorter than the cue count,
which is exactly the shape a per-team selection table would have.

### 9.3 Why 543 streams were unreachable: the strict scanner drops 3-packet streams — VERIFIED

`players.bin` 396, `lines.bin` 64, `lines_ps.bin` 61, `palines` 9, `playercom` 8, `playercom2` 5 —
**every one of them exactly 3 packets**. They are not scanner over-splits and not padding:
`probe_offsets.py` decodes them at peak-clipping amplitude, rms ~8,300, 90%+ of samples above
−46 dBFS over 0.35–0.55 s — i.e. real short spoken words, indistinguishable from a named row.

They were invisible because `streams_1a/1b.json` (which every ASR pass slices) comes from the
**strict scanner with `min_pkts=4`**. The catalogues, which the Audio tab actually shows, have no
such floor. `bank_asr.py` gained an `--offsets` mode that takes an explicit stream list, and
`emit_unnamed.py` builds that list from the catalogues rather than the scanner.

Related stale-layout bug found the same way: `scratchpad/bank_streams.py` still hardcodes the old
**fitted** `LAYOUT`, phantom `_gap_music` @ `0x792C0000` included. That truncates `pamusic.bin`'s
upper bound, which is why its ASR pass only ever saw **38** streams. Task #16.

**Corrected 2026-07-30 by direct diff of the two layouts:**

```
_gap_music    old=0x792C0000..0x88C43000          new=None            (phantom)
pamusic.bin   old=0x75224000..0x792C0000          new=..0x88C43000    (truncated)
teams.bin     old=0xB79CD000..0x4000000000000000  new=..0xB7B5B800    (unbounded tail)
pamusic.bin   38 streams  ->  297 streams
```

- The truncation hid **259** streams (38 → **297** scanned). The earlier "38 of 317" phrasing
  compared incompatible numbers: 317 is the *catalogue* count, which has no `min_pkts` floor.
- **The naming impact is nil.** `pamusic` is a music bank, so under §8.2 it takes *positional*
  names and is never ASR'd — which is why it already reads 100% named. The stale LAYOUT cost
  bank *attribution* correctness, not names.
- `bank_streams.py` also still carries the unbounded `teams.bin` tail that was already fixed on
  the launcher side (§7.4).
- **`paplyrs.bin`'s bounds are byte-identical under both layouts** (14,074 streams either way),
  which is what makes #16 safe to apply while a paplyrs ASR run is in flight.

### 9.4 `paplyrs.bin` is structurally the same problem as `players.bin` — VERIFIED

A 30-stream probe settles it: the PA announcer's surname calls, **~4 takes per surname in strict
alphabetical order**, take lengths following a consistent short/medium/shortest/long template
(≈7p / 9p / 5p / 14p).

```
0x88C43000  6p  'Abbott'      0x88C53800  7p  'Adams'     0x88C65800  7p  'Adkins'
0x88C77800  7p  'Abisher'   <- Aebischer   0x88C8A800  9p  'Af extenkov'  <- Afanasenkov
0x88CCC800  7p  'Albaleen!' <- Albelin
```

ASR cannot *spell* these, but it does not need to — it only has to keep the DP on the right
alphabetical run. So the transcripts feed the **same monotone alphabetical-run DP against the
Roster.ROS surname vocab** that took `players.bin` to 98%. Transcription of all 14,074 streams is
running in 6 shards.

### 9.5 Result

`speech_seed_names.json`: **63,475 → 66,298** = **81.9%** of 80,969 catalogued streams.
**19 of 23 banks are now 100% named**; `crowd.bin`'s 560 un-curated stereo loops took positional
`Crowd_Loop_NNNN` names (ASR confabulates on crowd noise — §8.2), under the user's own
`Crowd_Ambient` category but a deliberately different prefix so they cannot be mistaken for their
hand-numbered sequence.

Remaining **14,671**: `paplyrs.bin` 14,119 (transcribing), the 543 three-packet strays
(transcribing), and 9 front-end/no-bank rows.

**Status: partially verified.** The horn names are the user's in-game-proven set. The curated
promotions are verbatim copies of ear-verified work. The generated slugs remain machine ASR.
Exe rebuild + in-app GUI verification of the new names still pending.

---

## 10. Session 2026-07-30 PM13 — the strays land, the 0A/0B pair is reachable, and paplyrs needs a second DP pass

### 10.1 The 543 three-packet strays are all named — VERIFIED (mechanically)

`gen_strays.py` closes §9.3. Two rules, because the strays come in two shapes:

- **`players.bin` (396)** — bare surnames, and crucially still in **alphabetical order among
  themselves** (`Abbott`, `Allen`, `Arnett`, …), because they are interleaved into the same
  recording session as the 4-packet takes. So the monotone DP applies unchanged. Skip stays
  **enabled** here (unlike `paplyrs`, §10.3) — these are one clean take each, so a low score
  genuinely means "not in the vocabulary". **281 / 396 matched**; the rest keep a reviewable
  ASR slug.
- **`lines` / `lines_ps` / `palines` / `playercom` / `playercom2` (147)** — whole phrases with
  no vocabulary to snap to, so the transcript *is* the name. Reuses `gen_phrase_names.slug`
  verbatim so a stray is indistinguishable from the 30k rows it sits between.

The `playercom`/`playercom2` strays (8 + 5) are exactly the rows the user's hand-naming left
blank (724/732, 741/746), so this **adds to** the curated work. The merge **never overwrites an
existing key**, curated or generated.

### 10.2 The front-end banks needed the *other* container pair — VERIFIED

The last 8 unnamed non-`paplyrs` streams were unreachable because `bank_asr.py` and
`emit_unnamed.py` both **hardcoded `1A`/`1B`**. Restating §7's fact precisely:

> The four containers are **two** logical spaces, each split at `VOL = 0x6B800000`.
> `1A`+`1B` is one; `0A`+`0B` is the other, and **the front-end banks live in the 0A/0B pair**.

Both scripts gained `--pair 1A/1B | 0A/0B`. The matching logical-offset line in
`emit_unnamed.py` had to become `off + (VOL if fid in ("1B", "0B") else 0)` — the `0B` case was
silently missing, which is the same class of bug as §8.3.

Names follow the user's own convention for those banks: `Pre_Game_Faceoff_<Descriptor>`,
category `Pre_Game_Faceoff`, matching the 171 rows they hand-curated there.

### 10.3 `paplyrs.bin` needs a two-pass DP — the single-pass one fragments — VERIFIED by measurement

Running the `players.bin` DP unchanged on `paplyrs` gave **90 groups over 294 streams with only
187 placed**. Root cause: **24% of paplyrs transcripts come back empty** (the PA takes are short
and dry), and in `run_dp` the skip branch is `skipr + prev[v]` (0.74) while the match branch is
`score + pmax`. On an **all-zero** score vector skip *always* wins, so **every empty stream
became a group boundary**.

Two fixes, in order:

1. **`NeutralScorer` + `SKIP = 0.0`.** A transcript carrying no evidence must be *neutral*, not
   *skippable* — it returns a flat `0.80` for every vocabulary entry, so an empty stream can
   neither create nor prevent a boundary. This alone recovered most groups.
2. **A banded second pass with a position-in-group state (`run_dp_shape`).** Residual drift
   remained (a 12-take "Alexi" weld costing 2 names; Afanasenkov reduced to 1 take). The fix
   exploits the **take-length template**, but as a *directional* signal only:

   > Absolute packet thresholds do **not** work — short takes span 4–8p and long ones 10–15p,
   > which overlaps. But **takes alternate short/long and a group's last take is its longest**:
   > measured `last is max` 41/47, shape `s,M,s,L` 42/47, `q4 >= q2` 45/47.

   So pass 2 re-runs the DP over a ±40-entry band around pass 1's pick, with a state `p` =
   position within the group, `R = 6` max takes, a `SIZE` prior per group length, and a
   `SHAPE = 0.35` reward for matching the alternation. Only `r == 1` needs backpointers
   (`r >= 2` is reachable only by staying), and advance uses an **exclusive** cumulative max.

   **4-take groups went from 47/82 to 80/90.**

Validated on the first shard — 90 surname runs over 352 streams, takes-per-run
`{2:2, 3:6, 4:80, 5:2}`, correct alphabetical order (Abbott → Adams → Adkins → Aebischer →
Afanasenkov → Afinogenov → Aguilar → Albelin → … → Aucoin).

Grouping and naming are deliberately **separate**: the group is a maximal run of the same pick;
the name comes from that group's *best-scoring* take, falling back to ASR consensus
(`PA_Name_<Asr>`) then positional (`PA_Name_Unknown_%08X`). Every stream gets a `.review.json`
sidecar row `{name, asr, score, kind, group, pkts}`.

### 10.4 Known limitation of the vocabulary — VERIFIED, not fixed

`roster_vocab.json` is **not 5,190 surnames**. The Roster.ROS pool is **deduplicated in
first-encounter order**, not alternating Last/First — a shared first name is written once (Ryan
Getzlaf, then Carter, with no second `Ryan`), so **positional Last/First separation is
impossible**. The vocabulary therefore mixes first names in, and the DP occasionally snaps a
group onto `Alexei`/`Alexey`/`Alexi` where a surname belongs.

The second pool region (~`0x259xxx`–`0x25Cxxx`) is not a First,Last table either — it is
full-name strings (`'Roberto Luongo'`) plus historic team names, i.e. a records/legends list.
And the *file-layout* player chunk `0x1E159C31` (`foff=0x5a2d`, `nrec=2714`, `stride=420`) leads
with RGBA colors with only 303 records populated — `portrait_assign.py`'s
`REC=0x1A4`/`OFF_LNAME=0x00`/`OFF_FNAME=0x04`/`OFF_KEY=0x1C` describes the **live in-memory**
record only.

A real fix needs RE of the ROS player→pool references. The errors are local and every one is
reviewable via the sidecar, so this was left as a known limitation rather than chased.

**This corrects §"Roster.ROS name pool" in `07`/the memory notes, which claimed Last/First
pairs.**

### 10.5 `paplyrs.bin` also contains a first-name block and a jersey-number block — VERIFIED

The 45 `paplyrs` streams below the `min_pkts=4` floor are **not** more surname takes. Their
transcripts are unmistakably **first names** — `Benny`, `Carter`, `Cody`, `Daryl`, `Derrick`,
`Frederick`, `Gregory`, `Ilya`, `Liam`, `Louis`, `Philippe`, `Tanner`, `Tuukka`, `Will` — running
alphabetically among themselves, one take each rather than four. So the bank is not purely
surnames, and the Roster.ROS pool's first names are legitimately in scope for *part* of it (the
main surname groups are still contaminated by them — §10.4 stands).

Two more transcribed as `'number two'` / `'Number two.'` — the PA's **jersey-number** calls
("Number two, …") — and they sit immediately before a run of **14 evenly spaced (`0x1800`)**
streams that ASR returned empty for, almost certainly the rest of that number block. Those get
`PA_Number_<Word>_Stray` and the empty run keeps a positional name: snapping a number call onto
the name vocabulary would **invent a player who does not exist**.

Labels: `PA_Name_First_<Name>_Stray` (20/45 vocabulary-matched), `PA_Number_<Word>_Stray`,
`PA_Name_First_Unknown_%08X` for the 22 that transcribed empty.

### 10.6 The one remaining "unnamed" stream is a scanner false positive — VERIFIED

Coverage stops at 80,968 / 80,969. The holdout is `0B 047F1980_1ch_9p`, and two independent
tests say it is not a real stream:

- **Alignment.** Every authored stream starts on a `0x800` boundary. 80,968 of 80,969 catalogued
  offsets do; `0x047F1980` is the *sole* exception (`% 0x800 = 0x180`).
- **Layout.** Its logical offset `0x6FFF1980` falls outside every bank span — the first bank in
  the 0A/0B pair (`femusic.bin`) does not begin until `0xAFEC8800`.

So it is a catalogue scan artifact, not an unnamed asset. **Every real audio stream in the game
is named.**

### 10.7 Status

`speech_seed_names.json`: **66,298 → 80,968** = **100.0%** of 80,969 catalogued streams
(`1A` 37,430, `1B` 43,296, `0B` 203). **All 23 banks are 100% named.**

paplyrs final numbers: **3,639 surname groups over 14,074 streams**, takes per run
`{1:46, 2:120, 3:176, 4:3226, 5:70, 6:1}` — **88.6% land on exactly 4 takes**; evidence
`{vocab: 12,570, asr: 982, empty: 522}`.

**Status: partially verified.** Offsets, keys, bank attribution and the catalog gate are verified
mechanically. The names themselves are machine ASR except the 44 horns (user-proven in-game) and
2,790 curated entries — expect an occasional wrong word, but the offset it points at is right.
Exe rebuild + in-app GUI verification still pending, as is the user's in-game check of the horns.

---

## 11. Team PA sound effects and the arena SFX banks (2026-07-30)

The Boston "woo"/siren, the Florida growl, the Columbus cannon and friends are **not** in the
`1A/1B` wave banks at all. They live in a second, completely separate audio family inside `0A/0B`
that the bank layout in §6 deliberately reports as "no bank" — those bytes are `.iff`/`.cdf`/`.bnk`
assets that merely contain audio.

### 11.1 `pasfx.iff` + `pasfx.cdf` — the 14 team PA sound effects — VERIFIED (static)

| asset | archive | local off | size |
|---|---|---|---|
| `pasfx.iff` | `0B` | `0x36948000` | 740 B |
| `pasfx.cdf` | `0B` | `0x40469800` | 635,496 B |

`pasfx.iff` is a pure index: 14 × 20-byte node records at `+0xA0`,
`[crc32(name)][0x1AEDDA1F][2][ref][wave_off]` (the `audio_authored_names.json` format), followed by
a 28-entry `(offset, size)` table at `+0x1F0` and the literal string `pasfx.cdf`. The table's
entries alternate `0x2C`-byte descriptor / XMA2 payload, 14 of each, and the last pair ends at
exactly `0x9B268` = the size of `pasfx.cdf`.

**Descriptor layout (44 B), read off all 14:** `+0x00` channels (all 1), `+0x10` sample rate
(`0x0000AC44` = **44100 Hz**, not the 48 kHz of `1A/1B`), `+0x18` payload size, `+0x24` total
samples. The payload's first bytes are an ordinary XMA2 packet header, so the in-container codec
**is** XMA2 — this supersedes the "in-container wave codec is NOT the 1A/1B XMA2 packing" note
carried in `audio_authored_names.json` since 2026-07-18.

**All 14 names cracked** (hash = plain `crc32(name)`, no uppercasing, matching `Str_Hash`), in
index order — index order is alphabetical:

```
 0 0x9470C398 blue-jackets-canon_01           wave 0x00000
 1 0x5160EBE4 bruins-siren_01                 wave 0x0C000
 2 0xE1FA499D cheechoo-train_01               wave 0x1B800
 3 0x643092A1 coyotes-growl_01                wave 0x25800
 4 0x42154AB3 ducks-goal-siren_01             wave 0x32000
 5 0xC69AEC4C hurricanes-goal_01              wave 0x42000
 6 0xF50EDE4A hurricanes-thunder-faceoff_01   wave 0x4B000
 7 0xB532BBA8 kings-bells_01                  wave 0x52000
 8 0xC1721E69 panthers-growl_01               wave 0x67000
 9 0xE777B0C6 predators-growl_01              wave 0x6C000
10 0x7E7EE17C predators-growl_02              wave 0x77800
11 0x0FCFDCC9 sharks-goal_01                  wave 0x83800
12 0x96C68D73 sharks-goal_02                  wave 0x8B000
13 0xE1C1BDE5 sharks-goal_03                  wave 0x93000
```

Note `sharks-goal_01..03` have audio but **no event** (see below) — three unused/orphan cues.

### 11.2 The gamedata sound-event table — VERIFIED (static)

`gamedata_dec.bin @ 0x222390`: `[count = 0x0B][0x09]` then **12** 12-byte records
`[crc32(name.upper())][1][event_id]` at `0x22239C`, then 11 × `0xBC` sound-definition structs
(volume `1.0`, max distance `100.0`/`1e9`, rolloff `0.3`, priority — no trigger data), then the
names as UTF-16BE strings at `0x222C38`:

```
0x3991224B id 1065  kings-bells                 0x808589A3 id  429  ducks-goal-siren
0x583532AB id  865  hurricanes-thunder-faceoff  0xAB9C19A2 id  605  hurricanes-goal
0x5A7B6FFC id 1229  panthers-growl              0xBCCB5A7E id 1721  coyotes-growl
0x68065247 id 1593  blue-jackets-canon          0xD5B6C90D id   17  bruins-siren
0x6E7734C5 id  265  predators-growl             0xF1B5FF06 id 1321  (predators-growl_02 alias)
0x802993ED id 1945  cheechoo-train
```

The event name and the sound name are the same string; the audio just adds an `_NN` take suffix.
The two hash conventions differ: **gamedata events use `crc32(NAME.UPPER())`, the `.iff` sound
directory uses `crc32(name)`.**

### 11.3 `sfx_arena000..003.bnk` — the generic arena SFX — VERIFIED (static)

Four ~16 MB banks in `0B` (`0x37EC0000`, `0x38DFC800`, `0x39D19800`, `0x3ACC4000`), 288 sounds
each, indexed by `sfx_arena.iff` (`0B @0x36948800`, 280 B) which stores the four bank filenames
as UTF-16BE and their `crc32(name-without-extension)`. `ArenaSfx_LoadBankForArena`
(`0x83FA0968`, renamed) formats `"sfx_arena%03d"`, and `Sfx_LoadBankByNameAsync` (`0x83FD6038`,
renamed) `Str_Hash`es it and kicks off a once-guarded async load. The four banks carry identical
sound-name sets — they are per-arena-acoustic variants of the same effect list.

**+320 names cracked** by hashing every UTF-16BE/ASCII string in `gamedata_dec.bin` + the XEX with
an `_NN` take suffix: `check-boards-{soft,med}_NN`, `cheer-{large,small,lrg-short}-{front,rear}_NN`,
`ohh-{large,small}-{front,rear}_NN`, `skate-sweet_01..07`, `grunt-check_01..04`, `punch-soft_01..04`,
`stick-hit-stick`, `puck-hit-goal-{hard,soft}`, `rampup-{off,def}-{front,rear}`, … Shipped in
`launcher/data/audio_authored_names.json`: **566 → 886 named of 1,385** (`pasfx.iff` 14/14,
each `sfx_arena00N.bnk` 210/288).

### 11.4 What selects a team's SFX — NOT FOUND (static analysis exhausted)

Everything below was checked and came up empty, and it is worth recording so it is not re-run:

- No horn/siren/growl/cannon/bell/thunder/blast string anywhere in `default.xex` — the **only**
  horn-family string in the executable is `"Player: Zamboni-Horn%d"` (`0x83B39458`, consumed by
  `Zamboni_PlayHornSfx` @ `0x840E6A78`, renamed).
- Each event name and each event hash occurs **exactly once** in `gamedata_dec.bin` (in the table
  itself). No second reference anywhere.
- The 11 event ids are not `li`-immediates in the XEX (only two isolated, unrelated hits), so the
  dispatch is not a hardcoded per-team `switch`.
- No 30-entry `u8`/`u16`/`u32` array in gamedata or the XEX is non-blank at exactly the nine teams
  that own an effect (ANA BOS CAR CBJ FLA LAK NSH PHO SJS) — scanned at every alignment. This is
  the same negative result already recorded for the goal horns.
- `arena_presentation_<team>.iff` is pyro/lighting only (fireworks, spotlights, `intro_climax`,
  `intro_loop0/1`) — no audio events; `arena_<team>.iff` contains neither the event hashes nor the
  sound hashes.

So both the team → PA-SFX and the team → goal-horn wiring are resolved at runtime by a path that
leaves no static table. The remaining avenue is a **targeted** runtime probe (breakpoint the
sound-event play entry while a Boston goal / Florida PA line fires and read the id argument), not
another static sweep.

**Status: verified (static) for §11.1–11.3; §11.4 is a documented negative result.** Nothing here
has been confirmed in-game yet.

---

## 12. Crowd chants: names shipped, selection partly cracked, player chants still anonymous (2026-07-30)

### 12.1 The bank

`chants.bin` occupies `1A:0x5A128800..0x5D24E000` and holds **110 authored streams**, all 2-channel.
The cue table reports 119 cues. Layout, by stream index:

| Range | Content |
|---|---|
| 0–18 | team chants (one or two per team) |
| 19–25 | goal chants, "We Want The Cup", "MVP", silent-arena beds |
| 26–39 | short cluster: `Woo` ×5, `OnThePowerplay` ×3, `HeyOh`, plus Detroit at 39 |
| 40–85 | **player chants** — 45 streams, uniform 89–147 packets except a few long ones |
| 86–100 | second team-chant block (Calgary, Edmonton, Ottawa, Vancouver, Pittsburgh, San Jose, St. Louis, Tampa Bay, Washington) |
| 101–109 | unidentified tail |

All 110 are now named. The user's ear-verified labels cover the team chants and four players
(Brodeur 42, Luongo 54, Mason 55, Smith 77); the remaining 41 player streams stay
`Crowd_Chant_Player_Unknown_N`.

The player run is **alphabetical by surname** — the four anchors are in strict order, and that
ordering is the constraint every naming hypothesis below has to satisfy.

### 12.2 The post-cue-table region is the selection table

`chants.bin`'s trailer (file `0x22AC30`, 728 bytes) parses as the standard shape: header
`[cues=119][25][437][553][601][601]`, a 4-byte tag `03020100`, three 16-byte descriptors, then
ascending `u16` arrays. The **first array is the interesting one — 40 values**:

```
0 1 2 3 4 6 7 8 9 12 13 14 15 16 17 18 19 21 27 28
2001 2002 2003 2004 2005 2010 2011 2012 2013 2014 2015 2021 2022 2023 2024 2025 2026 2027 2028 2029
```

Twenty low values and twenty in a `2000+N` block — and the user's ear pass found chants for
**exactly 20 teams**. The low half is team ids; the high half is the chant event ids the crowd
system resolves. This is the same mechanism proved on `horns.bin`, whose single 29-value array is
missing exactly ids 1, 8 and 9 = Atlanta, Columbus and Dallas, the three teams already known to
carry alternate horn entries (cannon / full + short blast).

So the answer to "there's no linkage in the roster — how does the game know what to play for who"
is: **the linkage is a team-id array in the bank's own post-cue-table region, not in Roster.ROS.**
The roster never names an audio asset; it supplies a team id, and the bank's table maps id → cue.

Caveat, stated plainly: taking team ids as city-alphabetical 0..29 (the ordering verified on
horns.bin) yields a 20-team set that agrees with the user's ear labels on **12 of 20**:

| | teams |
|---|---|
| agree (12) | Buffalo, Calgary, Chicago, Dallas, Montreal, NY Islanders, NY Rangers, Nashville, New Jersey, Philadelphia, Toronto, Vancouver |
| in the table, not heard by ear (8) | Anaheim, Atlanta, Boston, Colorado, Columbus, Florida, Los Angeles, Minnesota |
| heard by ear, not in the table (8) | Detroit, Edmonton, Ottawa, Pittsburgh, San Jose, St. Louis, Tampa Bay, Washington |
| no chant either way (2) | Carolina, Phoenix |

The disagreements are not scattered. **Every one of the 12 agreements is in chant block 1
(stream indices 0–18); every disagreement is in block 2 (indices 86–100), plus Detroit at
index 39.** That is the signature of block 2 being a second, differently-ordered run rather
than a continuation of block 1 — so the re-listen list is exactly streams 39 and 86–100, and
the 8 "in the table but not heard" teams are the likely true labels for them.

The disagreements are also all generic "Let's go &lt;team&gt;" chants that are genuinely hard to
tell apart by ear. Treat "20 teams have chants, selected by a team-id table" as verified and
the specific id→team assignment as unconfirmed.

### 12.3 Event registries in the XEX

`CommentarySystem_Init` @`0x83F9E8D8` calls, in order:

* `Sound_RegisterReactiveEvents` @`0x83F9D860` — ~60 `SRE_*` reactive SFX events
* `Crowd_RegisterAmbientEvents` @`0x83F9E698` — 25 `SE_*` events (`SE_NO_EVENT`,
  `SE_APPLAUD_1..4`, `SE_BOO_1..4`, `SE_CHEER_1..4`, `SE_OHH_1..4`, `SE_SUSPENS_1..4`,
  `SE_SWELL_1..4`)
* `Commentary_RegisterEventNames` @`0x83F9DD38` — ~140 `CE_*` events, ids `0..0x8B`
* `CommentaryMgr_InitTables(0x8c, 0x3b, 0x19)` — 140 / 59 / **25**

`0x19` = 25 is the same value carried at `+0x04` of both the horns and chants trailer headers, so
that header field is the ambient-event count. These named enums also correct an earlier
conclusion in this document: horn/siren names do not appear as standalone strings, but the event
namespace **is** present in the XEX as `SE_` / `SRE_` / `CE_` prefixed literals.

### 12.4 Naming the 41 unknown player chants — two documented negatives

**ASR does not work on these.** A `medium.en` pass with beam 5 and a hockey-surname
`initial_prompt` over indices 26–84 produced nothing usable: stream 54 (Luongo, ear-verified) came
back empty, stream 55 (Mason) came back "USA! USA!", and the rest returned `Hillary`, `Russia`,
`go team`. A crowd shouting one surname over rink noise is outside what Whisper will emit. The
cheaper `small.en` pass was worse ("Bee Gees State", "Black Lives Matter"). **Do not retry ASR on
chants.bin** — the failure is in the acoustics, not the settings.

**The trailer's 50-value array is not the player list.** The second array
(`10 62 96 194 … 2277 2301`, 51 values with an end sentinel) is the right size for the 45-stream
player run and its maximum, 2301, sits just under the roster's 2715 records, which made it the
obvious candidate. It fails three independent ways:

1. As announcer name-ids resolved through `id_name_solved.json`: 16/51 resolve, and the results
   (`Kurt`, `Bolt`, `Dwayne`, `Moss`) are not alphabetical.
2. As player-record row indices: the default roster slot stores no inline names for stock
   players, and column `+0x09C` — the only near-unique `u16` column, 2310 distinct values —
   contains only 24 of the 51 values.
3. As direct indices into the packed name pool: this *does* return real 2010 names
   (Kiprusoff, Robidas, Roloson, Hamrlik, Biron, Berglund, Forsberg…), which is why it looked
   promising — but the list is not alphabetical, it is half first names, and **none of the four
   ear-verified chant players appear in it**. That last point is decisive.

### 12.5 Roster name pool, corrected

The pool at `0x23E928..0x25CDF2` is **deduplicated**, not one Last/First pair per player: Anaheim
reads `Getzlaf, Ryan, Carter, Niedermayer, Rob, Marchant, Todd, …` — Ryan Carter's first name is
absent because "Ryan" was already interned for Ryan Getzlaf. 6,755 unique strings. Any scheme that
assumed `pool[2k]` for player *k* is wrong, and pool order confirms the roster is stored
city-alphabetical (Anaheim first = team id 0).

### 12.6 What shipped

`launcher/data/speech_seed_names.json` previously carried 110 entries in the chants.bin span, all
of them raw `small.en` guesses — `Crowd_Ambient_PlainsFlames_Generic` for Calgary,
`Crowd_Ambient_LetHawksLetHawks_Generic`. Those were replaced wholesale (not merged: a confidently
wrong name is worse than none) with the 110 ear-verified labels. Users get correct chant names on
extract with no local `Audio_Names.json`.

The roster editor now exposes `audio_last` (`+0xB1`) and `audio_first` (`+0xB3`) on the player
table, and back-fills them for users who already have a saved field list.

**Status: §12.1 verified; §12.2 verified as a mechanism, per-team assignment unconfirmed;
§12.3 verified (static); §12.4 documented negative results; §12.5 verified; §12.6 shipped.**
Nothing here is confirmed in-game.

---

## 13. Category audit against bank membership, the Team column, and the three booth voices (2026-07-30)

### 13.1 Cross-verifying the shipped names against the bank each stream lives in

Every entry in `launcher/data/speech_seed_names.json` was re-resolved to its owning `.bin`
via `wave_banks.bank_for(fid, off)` and its category compared against what that bank
actually holds. The shipped names and the user's own names agree almost everywhere; the
mismatches were a small set of strays from an early generic auto-naming pass, plus two
whole banks filed under the wrong voice.

| bank | was | now | n |
|---|---|---|---|
| `chatter.bin` | `Color` | `Crowd_Ambient` | 1,558 |
| `streamedchatter.bin` | `Color` / `Unknown` | `Crowd_Ambient` | 3,591 |
| `crowd.bin` | `Commentary` / `Whistle` / `PA_English` | `Crowd_Ambient` | 9 |
| `crowd-idle-loop.bin` | `Unsorted` | `Crowd_Ambient` | 2 |
| `playercom.bin` + `playercom2.bin` | `Color` | `Commentary` | 13 |
| `players.bin` | `Commentary` | `PxP` | 2 |
| `loadingaudio_lines.bin` (was `fe_matchup_disc_f256379a.bin`, §7.3.1) | `Unknown` | `Pre_Game_Faceoff` | 2 |

The nine `crowd.bin` strays were also renamed — `PA_English_133/134`, `Whistle_001/002`,
`Commentary_237..240`, `Commentary_422` became `Crowd_Loop_0702..0710`, matching the bank's
own convention. Their names, not just their categories, were putting them in the wrong tab.

`chatter.bin` and `streamedchatter.bin` are the same content family — on-ice bench callouts
("Puck puck puck", "Keep the pressure", "On Minnesota", "Cycle it cycle") — so they were
moved together. **Only `chatter.bin` was explicitly reported; `streamedchatter.bin` was moved
by inference from identical content.**

Deliberately left alone: `palines.bin` and `pamusic.bin` mix content legitimately;
`horns.bin`'s four `Period_Horn_*` really are SFX rather than goal horns; and `lines.bin`'s
single `Color_PokeCheck_1` is not a stray at all — see §13.3.

Also fixed launcher-side: `CATEGORY_FOLDER` had no `"Unsorted"` key, so any stream carrying
that category silently fell through to "Unknown".

### 13.2 The Team column

The Audio/Speech browsers had a stale "Bank / Team" column showing a raw bank-reference
string. It is now **Team**, populated by `launcher/team_tag.py` from two sources:

1. A stream referenced by exactly one `arena_<code>` bank is that team's by construction.
   This is an archive fact but covers only ~7% of the catalogue.
2. Otherwise, the team is parsed out of the stream's own name — but **only when the name
   spells it out in full**: a city (`Crowd_Chant_Vancouver`, `Intro_StLouis`) or a full
   nickname (`Goal_Horn_Canucks`).

Name parsing tags **5,285 / 80,929 (6.5%)**, spread evenly at ~180 lines per team, which is
the expected shape: each team has its own `PxP_TS_*` run.

Everything else stays blank on purpose. A wrong team is worse than no team, because the
Team filter exists to find one team's audio and a false positive sends the user to edit the
wrong file — the tag is deliberately sparse, and the blanks are meant to be filled in by
hand. Three guards enforce that:

- **Abbreviations are never matched from a name.** Not three-letter codes (`VAN` is a
  substring of `PxP_Name_Last_Vandermeer`, `COL` of `Colorado`, `TB` of countless stems) and
  not short forms (`Habs`, `Leafs`, `Sens`, `Pens`, `Caps`, `Wings`, `Hawks`, `Avs`, `Bolts`,
  `Canes`, `Isles`, `Nucks`, `Jackets`, `Preds`). Codes survive only via source 1, where the
  arena bank rather than the name is the evidence.
- **Ambiguous nicknames** (Wild, Stars, Kings, Blues, Devils, Rangers, Sharks, Flames,
  Lightning, Ducks, Panthers, Islanders, Senators, Capitals, Avalanche) only count when
  nothing stronger matched *and* the token stands alone between delimiters rather than being
  a syllable of a longer word. `PA_Wild_Goal` and `PxP_TS_WildGetThePuckBack` resolve to
  Minnesota; `PxP_TheyGoWildInTheCrowd` stays blank.
- A name mentioning two teams resolves to neither.

The Audio-tab filter now matches the resolved team exactly instead of sweeping the name for
a substring, which used to match "Boston" inside any line that merely said Boston.

### 13.3 lines.bin holds three voices, and they are named in the audio itself

`lines.bin` is not one announcer. The transcripts name all three participants outright:

- **Randy Hahn** — play-by-play. `PxP_HelloEveryoneThisIsRandyHahn`,
  `PxP_ForDrewRamendaImRandyHahn`.
- **Drew Remenda** — colour. 278 takes address "Drew" (so Randy is speaking); 586 address
  "Randy" (so Drew or the reporter is).
- **John Schrader** — the intermission / rinkside reporter, a role with no category until
  now. `PxP_JohnSchraderIsWaitingToBreak`, `PxP_HeyEveryoneItsMeJohnOnce`,
  `PxP_ThanksJohnNowBack`, `PxP_AllRightJohnThanksAsAlways`.

(Hahn and Remenda are the San Jose Sharks' real broadcast duo, which is consistent.)

The handoff structure is visible directly in offset order: Randy/Drew set up the break
(`PxP_LetsHandThingsOverToJohn`, `PxP_TimeNowToLetOurColleague`, index ~9632–9641), John
delivers the report (`PxP_ThanksRandyWelcomeToThe2k` … `PxP_RandyItsTheFirstPeriodEdition`,
~9642–9700; the coach-interview run `PxP_HeToldMe` / `PxP_HeSaidHeLaidDown` / `PxP_MadeItClear`
at ~9933–10075), then signs off (`PxP_GuysBack`, `PxP_RandyDrew`, `PxP_BackToYouGuysIn`,
~10076–10078) and Randy/Drew thank him (`PxP_ThanksForTheInsightJohnLet`, ~10084–10092).

**Text alone cannot attribute the other ~18,000 takes**, because the transcript says what was
said, not who said it. Speaker identity is acoustic — but only for *one* of the two unknown
voices. The two announcers needed two different methods.

#### Drew Remenda (`Color_`) — acoustic, margin-gated

25-dimensional per-take features (log-mel cepstral + pitch summary), `whiten`, then
`scipy.cluster.vq.kmeans2(X, 5, minit="++", seed=7, iter=300)` over the **full 18,373-take**
feature set. One cluster holds **498 of the 546 takes that address "Randy"** against only 13
that address "Drew". Ground truth is *who a take talks to*: a take saying "Randy…" is spoken
by Drew.

Attribution is gated on the centroid margin
`margin = d[:, [0,2,3,4]].min(1) - d[:, 1]`, swept against that ground truth:

| threshold | takes labelled Color | Drew precision | Drew recall |
|-----------|---------------------|----------------|-------------|
| 0.0 | 3,895 | 0.975 | 0.912 |
| 0.3 | 3,735 | 0.986 | 0.910 |
| **0.6** | **3,628** | **0.990** | **0.896** |
| 1.0 | 3,391 | 0.991 | 0.839 |
| 1.5 | 2,826 | 0.997 | 0.696 |
| 2.0 | 1,806 | 1.000 | 0.456 |

**0.6 was chosen** — 99.0% precision, 89.6% recall. Everything below the margin deliberately
stays `PxP_`, per the project rule of preferring blanks to false positives. Strip validation:
the Schrader block reads 6% Drew, the control-booth block 4%, control action calls 0%.

#### John Schrader (`Reporter_`) — textual, NOT acoustic

Schrader **clusters with Hahn** — his block reads only 6% Drew — so clustering cannot find
him. He is bracketed instead by the explicit hand-offs in the ASR names, at take indices
**9643–9889** and **9930–10075**:

```
9630 LetsThrowItOverToJohn / 9635 LetsHandThingsOverToJohn / 9638 JohnSchraderIsWaitingToBreak
  9643 ThanksRandyWelcomeToThe2k ... 9890 ManyThanksJohnHiAgainFolks / 9895 ThanksJohnGreatJobBuddy
9930 ThanksRandyBetweenPeriodsISpoke / 9931 ThankYouRandyDuringTheIntermission
  ... 10075 BackToYouGuysIn -> 10081 AllRightJohnThanksAsAlways / 10087 ThanksJohnNowBack
```

Reporter wins over the Color margin inside those spans. The `2kSportsNetworkIsProud`
broadcast-intro blocks at ~3717–3796 and ~17188–17328 are *not* Schrader and stay `PxP_`.

#### Result

Applied to `launcher/data/speech_seed_names.json` (backup `.json.prevoice.bak`):
**393 `Reporter_`, 3,640 `Color_`, 51,381 `PxP_`**, each entry's `category` field set to match
so it routes to `Speech_Reporter` / `Speech_Color` on the next Reload Names. Spot-checks:
`Reporter_ThanksRandyWelcomeToThe2k`, `Reporter_AlrightThe2kSportsIntermissionReport`,
`Color_RandyThisWasAGreatPlay`, `Color_FiveHoleGoalLetsWatchHow`.

#### Two gotchas worth recording

- **Judge cluster quality only on the completed feature pass.** An earlier run against a
  *partial* file (11,267 of 18,373 rows — the background job was still writing) split Drew
  across two clusters and looked unusable per-take. It is one clean cluster on the full set.
  This produced a wrong conclusion that had to be retracted.
- Clustering a narrow index window groups short one-word fragments (`PxP_Pass_Var10`,
  `PxP_Sends`) into their own cluster — utterance length leaks into the features. Always run
  the full bank.

**Status: §13.1 shipped and verified against the archive; §13.2 shipped, spot-verified on
true and false positives; §13.3 shipped — the three voices and their names are verified from
the transcripts, and the per-stream attribution is validated against text ground truth at
99.0% precision / 89.6% recall.** Nothing here is confirmed in-game.

---

## 14. Sample rate is a per-BANK fact, and the extractor was measuring its own assumption (2026-07-30)

### 14.1 The bug — VERIFIED

The user reported `pamusic.bin` extracting "at too high of a pitch". It was a launcher bug,
not a mislabelled bank.

The extract worker called `decode_xma2(raw, ch, wav, xma2encode)` with **no `sample_rate`
argument**, so it took the hardcoded `48000` default — and then "detected" the rate by reading
it back off the WAV it had just written. It was measuring its own assumption. All 80,884
streams got stamped 48000.

**Why nothing errored:** raw XMA2 packets carry **no sample rate at all**. The rate lives only
in the RIFF `fmt ` chunk handed to the decoder. Decoding at the wrong rate silently resamples;
the XMA2 bitstream fixes the sample *count*, not the duration. 48000/44100 is 8.8% fast —
about a semitone and a half sharp, which is exactly what was heard.

### 14.2 The rate is in the cue-table header — VERIFIED

Each bank's speech cue table (§ on cue tables) begins:

```
[count u32][hash u32][1][sample_rate u32][0x800][channels u32][?f32]
```

Six banks declare **44100**: `chants`, `chatter`, `streamedchatter`, `palines`, `paplyrs`,
`pamusic`. The other 13 named banks declare 48000.

### 14.3 Independent cross-check — VERIFIED over 17 banks / ~76,000 cues

The cue records carry a per-cue duration, so:

```
cue_duration / duration_decoded_at_48k  ==  48000 / true_rate
```

The ratio must land on **1.0000** (48 kHz) or **1.0884** (44.1 kHz). Nothing else appeared.
`horns.bin` matches only on its *unpadded* rows, because its cue windows are declared longer
than the horns themselves — the same window-longer-than-content behaviour §15 documents for
pamusic.

### 14.4 What shipped

- `launcher/wave_banks.py`: `BANK_RATES` + `rate_for(fid, offset)`, which resolves the bank
  from the offset and never guesses per stream.
- `audio_store.SCHEMA = 3` with `repair_sample_rates()`, restamping the `fmt ` chunk in place
  on first run — 22,877 affected WAVs, samples untouched. User-edited WAVs are skipped but
  their manifest rate is corrected.
- The manual **"Set Sample Rate…" menu item was REMOVED** at the user's request. Its 48000
  default was the thing that caused the bug. Do not reintroduce a per-stream rate.

### 14.5 The one remaining assumption

The four unnamed front-end banks (§7.3) have no cue table, so their 48000 is still a guess —
192 streams, 0.24% of the total. Closing it needs an FF3BEF94 scene-container reader.

**Status: verified statically over every bank with a cue table; the repair has not yet been
run in the user's build.**

---

## 15. Goal songs: which team gets which pamusic track is NOT in static data (2026-07-30)

The user identified one by ear — `GoalSong_Vancouver` — and asked for the rest.

### 15.1 The bank, and the one anchor — VERIFIED

`pamusic.bin` is **317 cues at 44100 Hz**, cue table at `0x26DF84` in the decompressed
`gamedata.iff`. `GoalSong_Vancouver` is **cue 71**, `1B:0x110BE800`, rel `0x0769A800`,
668 packets.

Its cue window declares 69.867 s but only ~43.2 s of audio decodes. That is **not** a
truncated extract — other 668-packet cues fill the whole 69.87 s, so cue 71's tail is padding.
Same behaviour as `horns.bin` (§9.2).

### 15.2 Nine avenues eliminated

1. **The pamusic trailer carries no selection table.** After the cue table: bank size, five
   zero dwords, then a flat run of `FF000000` — **one 0xFF per cue, all 317 unassigned**.
   `horns.bin` and `chants.bin` both put a team-id → event-id array exactly here (§9.2, §12.2);
   pamusic has none. Read positively: **pamusic cues are not addressed by sound-event id at
   all**, so something else selects them by index.
2. **CORRECTION — the pamusic rows in `audio_bank_refs.json` are noise.** They were produced
   by scanning arena files for literal offsets, but `arena_van.bin` is the arena
   *geometry/shader* asset, not an audio bank. The 45 "hits" for cue 66 land inside DXT
   texture data at `0x788640`. Disregard all 26 pamusic rows in that file.
3. `arena_van.bin` byte-searched for cue 71's rel / logical / physical offsets and `>>11`
   variants: **0 hits**; zero `goal` / `pamusic` / `music` strings.
4. **No music strings in gamedata.** Its strings are UTF-16BE. The SFX-event name block at
   `0x221518`–`0x222DBC` holds the per-team PA SFX names (§11.2) — `bruins-siren`,
   `ducks-goal-siren`, `panthers-growl`, `blue-jackets-canon`, `cheechoo-train` — but nothing
   music-related. The only pamusic string is the bank header `"pamusic.bin"` +
   `"streambank.000001.3716"` immediately *before* the cue table at `0x26DF3C`.
5. **CORRECTION — the Ghidra program is not `default.xex`.** Ghidra lists `"pamusic"` at
   `83b2e200`, `"Rink Music"` at `83b1ae80`, `"MUSIC_AUDIO"` at `83b1807c`. All three occur
   **zero times** in `default.xex`, `default_flat.xex` and `default_flat_ORIG.xex`. Verify any
   Ghidra address against the flat file before trusting it.
6. Exhaustive scan of xex + gamedata + global for a 28–32 entry table of u8/u16/u32 in range
   1..316, at every byte alignment: nothing team-shaped.
7. No per-arena audio-config asset in any launcher asset catalog.
8. The user's own `Audio Name Mapping.txt` has no goal-song data beyond the horns.
9. Content-hashing all 317 cues: only 3 byte-identical duplicates.

### 15.3 Positive structural finding — pamusic stores re-encoded duplicates — VERIFIED

Byte hashing finds almost nothing, but *audio* fingerprinting does. Taking 20 s from t=2 s,
2048-point FFT, mean log-magnitude over bins 0–300, normalised, cosine > 0.985 gives **30
duplicate groups among 222 decodable cues** — e.g. cues 14≡62, 15≡60≡72, 16≡73, 50≡52, and
63/66/67/68/70. Same song, different bytes. **Cue 71 has no duplicate.**

The groups do not form per-arena blocks, so this does not yield the mapping — but it is a real
property of the bank and rules out "one copy per arena, contiguous".

Implementation gotcha: `x.reshape(-1, 2048)` requires the analysis slice truncated to a
multiple of 2048, or every file dies inside the bare `except` and the sweep silently reports
one fingerprint out of 270.

### 15.4 What remains

Only a **runtime probe**: break on the PA-music play call under Xenia and read which cue index
fires on a goal, per arena. Nothing was renamed or recategorised — with one team known, any
label would be a guess.

**Status: §15.1 and §15.3 verified statically; §15.2 is a set of documented negatives.
Nothing confirmed in-game.**

### 15.5 CORRECTION — the declared duration is the byte allocation, and cue 71 is truncated

§15.1 said cue 71's window "declares 69.867 s but only ~43.2 s decodes, so the tail is padding".
**That was wrong.** Dividing each cue's byte allocation (next cue's offset minus its own) by its
*declared* duration gives a near-constant **~18,000-24,000 B/s** across every cue in the bank —
i.e. the bank's XMA2 bitrate. Dividing by the *decoded* length instead scatters from 908 to
31,658 B/s. The declared duration is therefore the honest length of the cue's data, and any
disagreement with the decode is a decode/extract fault, not padding.

Cue 71 is the **only one of 262 cues where the declared duration exceeds the decoded stream**
(69.867 vs 43.214 s). Its allocation is 1,368,064 B — bit-identical to cue 62, which declares the
same 69.867 s and decodes to a full 69.869 s. So `GoalSong_Vancouver.wav` is almost certainly
being **extracted truncated at ~62% of the song**. Open bug, not a property of the data.

(The 37 cues where the stream decodes *longer* than declared are the mirror-image artefact: the
extractor runs to the next cue's offset and the decoder emits a garbage tail. Cue 93 "decodes"
720 s from a 61 s allocation.)

### 15.6 There is no cue-point field — the hook is baked into the audio

The user's question "how does the game know *when* in the song to start, given each team's cue
point differs" has a structural answer: **the cue record is 8 bytes, `[rel_offset u32][duration
f32]`, and both fields are fully accounted for** — the offset locates the stream in the bank and
the duration matches the byte allocation at constant bitrate. There is no room for, and no
observed, per-cue start offset.

Consistent with that, every pamusic cue measured starts **hot at t = 0.00** — first-100 ms RMS of
0.13-0.37, no fade-in, no intro, no lead-in silence. The songs were **trimmed to their hook
before encoding**. The cue point is authoring-time, not runtime.

*Implication for tooling:* the cue point is editable today by re-trimming and re-encoding the
stream through the normal audio replace path — no new game-side field is needed. But a
**per-team** cue-point editor is still blocked on §15.4's runtime probe, because we do not yet
know which cue index belongs to which team.

*Status: **partially verified** (static, arithmetic). The truncation of cue 71 is a strong
inference from byte accounting, not yet confirmed by a fixed decode.*

---

## 16. The post-cue-table trailer block: bank group tables

*Status: **partially verified** (static). The record layout and the group arrays are read
consistently across every bank that has a trailer; the semantics of three header dwords and two
record fields are still unknown. Nothing here has been confirmed in-game.*

Earlier notes said the region after a bank's cue table was "per-bank soundId arrays, no names".
That is right as far as it goes but understates it: **10 of the 19 named banks carry a
structured trailer block**, and it holds the *grouping* of cues — which cues are alternates of
one another — plus, for some banks, the id arrays that select them.

### 16.1 Which banks have a trailer

Locate it as: `cue table end = recs + cues*8`, skip the `[last_dur f32][bank_size u32]`
sentinel, then skip zero dwords. If the next thing is a UTF‑16BE bank-header string, the bank
has no trailer.

| trailer | no trailer |
|---|---|
| `chants`, `chatter`, `palines`, `paplyrs`, `horns`, `streamedchatter`, `lines`, `lines_ps`, `lines_ts`, `playercom2` | `env_amb`, `crowd-idle-loop`, `crowd`, `pamusic`, `players`, `playercom`, `teams`, `jukeboxmusic`, `femusic` |

`pamusic.bin` having **no** trailer is the structural reason the goal-song → team mapping is not
in static data (§15).

**Trap:** the *gap* to the next bank header is not the trailer size. The 29,472-byte gap after
`horns.bin`'s table contains an unrelated **`CAMH`** chunk at +0x120 (32-byte records
`[id][0x40][FFFFFFFF][value][0x64 ×6]`). Parsing a trailer by gap length reads foreign data.

### 16.2 Block layout

```
+0x00  u32   cue count            (equals the cue table's count - use as a validity check)
+0x04  u32   25                   (constant across all 10 banks)
+0x08  u32   ?    \
+0x0C  u32   ?     |  four unknown dwords; horns 153/197/193/189, chants 437/553/601/601
+0x10  u32   ?     |
+0x14  u32   ?    /
+0x18  u8[4] quad; byte[0] = number of 16-byte records that follow
+0x1C  N x 16-byte record
       ...          then each record's u16 array, in record order
```

Record (16 B):

```
+0x00  u32   a      horns 17; chants 49 / 113 / 99
+0x04  u32   b      horns 71; chants 227 / 291 / 279
+0x08  u16   count  number of u16 entries in this record's array
+0x0A  u16   kind   1, 2 or 3
+0x0C  u32   tag    always 0xKK00CC00 (kk in 1..2)
```

Observed headers:

```
chants           119   25   437   553   601   601   03 02 01 00
horns             39   25   153   197   193   189   01 01 00 00
chatter         1677   25   681  5593  5589  5585   01 01 00 20
palines         3242   25   253   477  1733  5309   02 03 03 00
paplyrs        14227   25    21    17 29009 29047   00 00 01 00
streamedchatter 3849   25    21    17  2541  6141   00 00 01 40
lines          19321   25  5537 25709 26501 28583   03 03 03 00
lines_ps       10303   25    21    17   345 19603   00 00 01 00
lines_ts        6920   25    41   109   613  5647   01 01 01 00
playercom2       793   25  1609  3041  3173  3193   1B 0C 04 00
```

### 16.3 `paplyrs.bin` — the group table, exact

`paplyrs` carries **two parallel 3,622-entry u16 arrays**:

- `A` at block+0x30 — `0, 1, 2, … 9100`, strictly monotone (3,587 of 3,621 gaps are 1). Group id.
- `B` at block+0x1C7C — `0, 4, 8, … 14223`, the **cue index where each group starts**.

```python
A = struct.unpack_from(">3622H", d, p + 0x30)
B = struct.unpack_from(">3622H", d, p + 0x1C7C)
```

`B[-1] + 4 == 14227 == the exact cue count`, so `B` partitions the bank with no remainder.
Group sizes: **4 x 3,496 · 2 x 91 · 3 x 13 · 1 x 22**.

This is authoritative and supersedes the heuristic 4-take grouping that the two-pass DP naming
work inferred (§ naming). The game *tells* us the grouping; it does not have to be guessed.
Grouping is still not naming — `A` is an opaque id, not a roster id.

### 16.4 `horns.bin` — 29 groups over 39 cues

One 29-entry u16 array at block+0x2C: `0, 2, 3, 4, 5, 6, 7, 10, 11 … 31`, i.e. `0..31` minus
`{1, 8, 9}`, zero-terminated. Read as group starts against 39 cues:

| group | cues | note |
|---|---|---|
| g0 | 0, 1 | two variants |
| g1–g5 | 2, 3, 4, 5, 6 | one each |
| g6 | 7, 8, 9 | three variants |
| g7–g27 | 10 … 30 | one each |
| g28 | 31–38 | **eight** — the alternates tail |

This is the "29 IDs over 39 cues" lead, resolved. g28 is exactly the tail block already named by
ear — Columbus cannon, Atlanta/Dallas full + short blast, `Period_Horn_1..4`. So the game selects
a **group** (29 of them, one per selectable horn slot), and the extra cues inside g0, g6 and g28
are alternates within a slot, not separate slots. It does **not** explain *how* a team maps to a
group — see §15's open question.

### 16.5 `chants.bin` — three records, ground-truthed

Header quad byte[0] = 3, so three 16-byte records at +0x1C, +0x2C, +0x3C
(`count` = 40, 1, 50; `kind` = 1, 3, 2), then their arrays:

- +0x4C, 20 u16 — team ids `0,1,2,3,4,6,7,8,9,12,13,14,15,16,17,18,19,21,27,28`
- +0x74, 20 u16 — chant event ids `0x07D1..0x07ED` (2001–2029)

This is the already-verified team→chant mapping, now explained: it is record 0's array
(`count = 40` = the 20 + 20 pair), not a special case. Record 2's array at +0x9C is 50 u16 of
cue-ish and larger values, undecoded.

### 16.6 The remaining seven banks

`chatter`, `palines`, `streamedchatter`, `lines`, `lines_ps`, `lines_ts`, `playercom2` all parse
with the same header + record layout, and their arrays extract cleanly, but the values are
heterogeneous (banded event ids — palines 3000–4091, lines_ps 7000–8000 plus 40999/49292/
56891–56894, lines_ts 6000–6615 plus 9903–9905, streamedchatter 101–105/201…/401–408) mixed with
cue-index runs. None of them yields a `B[-1] + size == cues` partition the way `paplyrs` does, so
the group-start reading is **not** universal. Undecoded.

---

## 17. The shipped seed could not correct a name it had already shipped (2026-07-31)

### 17.1 The bug — VERIFIED from source and by dry run

The user reported `chatter` lines still sitting in the **Color commentary** category, months after
§13.3 reclassified `lines.bin` into `PxP_` / `Color_` / `Reporter_` and shipped the result in
`launcher/data/speech_seed_names.json`.

The classification was correct in the shipped data. The delivery was not.
`merge_speech_seed()` was **gap-fill only**:

```python
new = {k: v for k, v in seed.items() if k not in nm_map}   # only keys the user lacks
```

Any stream the user had *already* seeded — which, after a first Extract, is all of them — was
frozen at whatever the seed said on the day they first ran it. Every later correction we shipped
(the voice split, category moves, spelling fixes) reached **new** users only. Existing users' own
hand-edits were protected, but so were our own stale values.

### 17.2 The fix — versioned, conservative refresh

`nhl2k10_launcher.py` now carries `SEED_VERSION`, written into the user's `Audio_Names.json` as
`_seed_version`. When the shipped version is newer, `merge_speech_seed()` makes a **second** pass
over keys the user already has and overwrites a value only when it can prove the user did not
author it (`_stale_seed_value()`):

1. name identical, category differs → take the new category;
2. names equal after stripping our own `PxP_`/`Color_`/`Reporter_` prefix → our reclassification,
   take it;
3. the live name matches `_PLACEHOLDER_RE` (`Commentary_12`, `PA_English_123`, `Unsorted_…`, …)
   → a generated placeholder, take the real name.

Anything else is treated as a user rename and left alone. Version 1 → 2 changed **9,207**
values in a dry run against a copy of the user's real 80,929-entry file; a hand-identified
`GoalHorn_Columbus_Cannon` survived untouched, and a second run was a no-op.

The refresh only edits the names file. **Reload Names** is still what pushes the values onto the
manifest and moves the WAVs between category folders.

### 17.3 A fourth rule: `Unknown` is never a user's choice

Rule 1 (same name → take the category) could not reach a stream the user had *renamed* but
never categorised, which left 3 hand-named Vancouver goal calls stuck in `Unknown`. `Unknown` is
the extractor's fallback bucket, not something anyone selects, so a real category from the seed
now wins outright while the user's name is left alone.

## 18. The 187 generic PA names, transcribed (2026-07-31)

`palines.bin` shipped 187 streams under placeholder names — 159 `PA_English_###` and 28
`PA_French_###`, inherited from an old hand-made `Audio Name Mapping.txt`. They are the last
numbered placeholders in the catalogue.

### 18.1 `PA_French_*` is French-language PA, not Team France — VERIFIED

Worth stating because the seed already uses "French" for the *Olympic* team
(`PA_FrenchNationalTeam`, `PxP_Team_French`). These are different: `PA_French_001` is
*"Mesdames et messieurs, s'il vous plaît, votre attention au centre de la glace…"* — the
French-Canadian PA track. Several clips hold two announcements in one stream
(`Prochain tireur pour Anaheim ! Prochain tireur pour Atlanta !`).

### 18.2 Method

faster-whisper, **two passes per clip** (`small` then `medium`, `vad_filter=False`,
`language=en|fr`), 33 minutes of audio, ~31 min wall clock on CPU int8. The name is built from
`medium`; `small` is only a witness.

Word-for-word agreement was the wrong gate — it failed 68 of 187 on spelling that carries no
information ("alright"/"all right", "shorthanded"/"short-handed", "Goooooool"/"Goal"). The
shipped gate is a `SequenceMatcher` ratio ≥ 0.80 over the normalised first six words, with
repeated-letter runs collapsed. 178 clips passed; the 9 below the floor were read by hand and
all nine name the same line (one model spelling a number out, `28` vs `vingt-huit`, or mangling
a word the other got), so they were allowed individually rather than by lowering the floor.

Two mechanical fixes on top: apostrophes are deleted rather than split on (`Devil's` → `Devils`,
not `Devil`+`S`), and a small word map corrects mishears that the *series* disambiguates —
every one sits in the per-team `"<Team> goal! A shorthanded goal!"` run, so the team is fixed by
position, not guessed: `Euler's` → Oilers, `Creditors` → Predators, `Cadadian's` → Canadiens,
`Threshers` → Thrashers, `Gold`/`Gall` → Goal.

**187/187 named**, category `PA`, in `launcher/data/speech_seed_names.json` at
`_seed_version = 3`. Not heard in-game; the names are transcription, and a transcript is not a
confirmation of what the game does with the cue.

---

## 19. "Is this WAV edited?" — size was lying, and Patch This File was gated on the lie (2026-07-31)

### 19.1 The two detectors, and why there are two

Editing happens **in place**: the WAV you change is the WAV that was extracted, and the manifest
remembers the pristine `sha1` + `size`. So the exact test is a hash comparison
(`audio_store.is_edited()`), and that is what Patch Game and Check All have always used.

The **list** could not afford it. `Audio/Extracted/` on a full extract is **81,275 files /
24.2 GB**, and SHA-1 measures ~40 MB/s over that tree — a **~10 minute** sweep. So the ✓ Modified
column used the only thing a directory walk hands over for free: file **size**.

Size is blind to the case that actually happens. Re-exporting a clip from an editor at the same
settings very often lands on the *same byte count* — the user overwrote an extracted WAV in
place, the size matched, and the row never ticked. "Patch This File" and "Revert" were both
gated on that tick, so both greyed out on a file that genuinely was modified.

### 19.2 The fix: mtime is the free half of the answer

`os.scandir` already returns `st_mtime` in the same entry it returns `st_size` from, so tracking
it costs nothing. The manifest entry gains **`mtime`** — the pristine extract's timestamp —
written wherever pristine bytes land (extract, revert), and the list now decides with:

| signal | meaning |
|---|---|
| `dirty` flag | settled: something hashed it and it differs |
| size ≠ pristine size | settled: edited |
| mtime ≠ pristine mtime (±2 s) | **edited in place** — the case size misses |
| no `mtime` recorded | extract predates this; fall back to size alone |

The ±2 s slack is for filesystem granularity (FAT is 2 s) and copy round-trips. A real edit lands
minutes or days after the extract, never inside that window.

Replace sets `dirty` outright rather than trusting mtime, because `shutil.copy2` carries the
*source* file's timestamp onto the destination — but we don't need to infer anything there, we
just did the replacing.

**Patch Game and Check All keep the hash as the authority** and are now merely cheaper: the hash
only runs on entries whose size or mtime moved, or that have no `mtime` baseline and therefore
cannot rule themselves out. Nothing they used to catch is missed.

### 19.3 Rescan Edits

An extract made *before* `mtime` existed, then overwritten in place at the same length, is
invisible to both cheap tests — there is no baseline to compare against. **Rescan Edits** (Audio
ops bar) is the reconciliation: it hashes, flags the genuinely-changed entries `dirty`, and
stamps a pristine `mtime` on the rest so they answer for free from then on. First run is the
full ~10 min read; afterwards it only touches files whose timestamp moved. It writes nothing to
the game.

### 19.4 Patch This File is no longer gated on "edited"

Writing a track's own extracted audio back into the archive is a legitimate operation — it is how
you re-apply after a `.bak` restore, and how you patch a file you changed outside the launcher.
The menu item now needs only an extracted WAV, and warns in the confirm dialog when the file is
not flagged as modified. Revert stays gated, since reverting an unmodified file is a no-op.

### 19.5 Field observation, unexplained

A 400-entry random sample of the live manifest found **~24% of extracted WAVs differ from their
recorded pristine `sha1` at identical byte length** — concentrated in `1B`, all rewritten inside
one six-minute window on 2026-07-31, mostly `PA_Name_Last_*` (the relocation-batch domain).
Decode is byte-deterministic (two re-decodes of the same packets are identical), but a fresh
re-decode today matches *neither* the manifest hash nor the file on disk — i.e. the archive was
patched, those WAVs were re-decoded from the patched archive, and the archive was then patched
again. They are decodes of already-correct archive audio, not authored edits.

Consequence worth knowing before pressing Patch Game: it currently considers roughly **19,000
files modified** and would re-encode all of them — lossy round-trips over audio that is already
right in the archive. Verified by measurement, not in-game.

---

## §20 — The "cryptic jumble" SFX: descriptor +0x00 was never the channel count (2026-07-31)

**Status: verified by measurement (1,385/1,385 sounds decoded and checked against the game's own
declared sample count). Not in-game verified — the user has not yet re-extracted and listened.**

### 20.1 The report

Some `sfx_arena000.bnk` sounds extract clean (punches, against-boards, `menu-misc_00_arena0`,
`menu-forward_00_arena0`, stick slide) and others come out as harsh noise (`wipe_00_arena0`,
`menu-back_00_arena0`, `puck-post_00/01/02_arena0`, `ref-whistle_00_arena0`). Every "bad" sound
named turned out to sit in one group, and every "good" one in the other.

### 20.2 The descriptor field, corrected

The authored-SFX descriptor is 0x2C bytes, and lives both in the FF3BEF94 DRAM section and inside
the `.cdf` banks (§11.1). The corrected reading:

| off | meaning |
|-----|---------|
| +0x00 | always `1` — **not** the channel count |
| +0x04 | always `5` |
| +0x08 | **format id: 2 = mono, 5 = stereo** |
| +0x0C | encoded sample count (ground truth for verification) |
| +0x10 | sample rate |
| +0x18 | byte size (matches the gap to the next `wave_off` exactly — this is what pins the table to the sounds) |
| +0x24 | unrelated, ~2–2.8× the sample count |

`+0x00` had been read as the channel count. Across all 12 authored banks the observed
`(+0x00, +0x04, +0x08)` triples are only `(1,5,2)` ×887 and `(1,5,5)` ×395 — so the old read
returned mono for everything, including the 395 stereo sounds.

Telling `xma2encode` that a stereo XMA2 stream is mono does not fail loudly: it returns 0 and
emits roughly 8% of the samples as noise. That is the entire "cryptic jumble" symptom.

### 20.3 Verification

Metric: decoded frames ÷ descriptor `+0x0C`. Codec 2 already sat at 0.996–1.186 (0 of 195 under
0.9), which is what makes `+0x0C` a usable target rather than a guess.

| bank | codec 5 (stereo) | as shipped | with the fix |
|------|------------------|-----------|--------------|
| sfx_arena000.bnk | 93 | 0.079 | 1.000 |
| sfx_arena001.bnk | 91 | 0.081 | 1.000 |
| sfx_arena002.bnk | 92 | 0.080 | 1.000 |
| sfx_arena003.bnk | 92 | 0.080 | 1.000 |
| crowdloops.iff | 12 | 0.087 | 1.000 |
| bootup_audio.iff | 2 | 0.090 | 1.000 |
| frontend.iff | 13 | 0.089 | 1.000 |

All 395 stereo sounds go from 0.025–0.172 of their declared length to 1.000. Totals through the
shipping code path: **988/1385 complete before, 1383/1385 after.**

A RIFF-header sweep ruled out every other candidate first — channels 1/2 × NumStreams 1/2 ×
BytesPerBlock 2048/16384/32768/65536 × EncoderVersion 3/4 × declared-sample-count guess/real.
**Only `channels` changed anything**, and every variant that set it to 2 was exact.

### 20.4 Second, smaller bug found by the same pass: the sample-rate clamp

`sounds()` clamped the rate to 8000–96000 and fell back to 48000 outside that. One sound is
authored below the floor: `crowd-lferumble-loop_01` ships at **6000 Hz** (a sub-bass rumble — there
is nothing above 3 kHz in it to keep). The clamp rewrote it to 48000 and decoded 512 of its 42,816
samples. Floor lowered to 1000; it now decodes to 0.998.

### 20.5 What changed in the tool

`launcher/authored_sfx.py`: new `CODEC_CHANNELS = {2: 1, 5: 2}`; `sounds()` (DRAM branch) and
`_cdf_records()` both read `+0x08` and derive channels from it; `_cdf_records()`'s structural
sanity check now validates the format id (the old `0 < ch <= 6` on `+0x00` passed on anything, so
it could never reject a bad walk); rate floor 8000 → 1000.

**The 395 affected sounds must be re-extracted** — the WAVs already on disk were decoded with the
wrong channel count. Affected banks: `sfx_arena000-003.bnk`, `crowdloops.iff`, `bootup_audio.iff`,
`frontend.iff`.

### 20.6 Residual: `disc_aaea7e4d` (2 sounds) — a different, pre-existing problem

`disc_aaea7e4d_8DE56BFD` (0.025) and `disc_aaea7e4d_4DB33DE3` (0.316) still fail. Both are codec 2,
both report the same `want = 31317`, and their two descriptors are byte-identical — a duplicated
descriptor, i.e. the join is wrong, not the channel count. That bank's `wave_base = 0x429A0CD` is
also not 2048-aligned, where every working bank has `wave_off` values that are multiples of 0x800.
Fixing it needs the section-table walk, and it is 2 unnamed sounds — deliberately left alone.
