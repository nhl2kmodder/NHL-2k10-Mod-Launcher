# 07 — Roster.ROS format, team names, and team colors

One-line summary: `Roster.ROS` is a big-endian chunk-directory save that holds the player table, team records, and a UTF-16BE string pool; from it the launcher edits player attributes, team display names, and per-team primary/secondary colors — the last of which required cracking a self-describing table whose naive chunk-offset math silently dropped 7 teams.

Status: **verified for the container format, the player field map, team-name storage, and the 30-team color table.** In-game confirmation is done for team-name markers (all 3 tested rendered) and most player fields; still **pending**: in-game confirmation of the 7 newly-reachable color teams (writes land *before* the parsed chunk offset). `team_color_map.json` is now **DEAD/wrong** and must not be used (see below).

---

## 1. Container format (`Roster.ROS`)

This is the raw Xbox 360 roster save payload — **not** an STFS wrapper. `Roster.ROS\Roster.ROS` (the outer file/inner file share the name) is the payload directly. All fields big-endian.

```
0x00  u32  filesize - 4
0x04  u32  version (= 1)
0x08  u32  chunk_count (= 237)
0x0C  ...  directory: chunk_count × 12 bytes =
             [u32 type_hash][u32 count][u32 data_offset]
```

- `data_base = 0x0C + chunk_count*12 = 0xB28`. Each chunk's bytes start at `data_base + data_offset`.
- Chunks **tile** the data region with no gaps (validated: Σ chunk sizes = 2529148 = filesize − 4 − 0xB28).
- `type_hash` is a **constant TYPE id** (identical across saves), NOT a content checksum. There is **no global checksum**, which is why raw byte edits are accepted in-game (same reason the string edits work).
- 17 of the 237 chunks are record tables; the rest are misc.

**Stride detection caveat.** The directory's `size // count` rounds the stride *down* (each region is ~24 bytes short of `count*stride`). Detect stride by **byte-autocorrelation**, then use `nrec = size // stride`.

Key chunks:

| type_hash | shape | contents |
|---|---|---|
| `0x1E159C31` | 2714 × 420B | **player table** (autocorr peak 0.59) |
| `0x8489FAF3` | 95 × 412B (file) / 96 in RAM | **team records** (colors, roster ptrs) |
| `0xEB69DFB9` | string blob (last chunk) | **string pool** (team names, jersey names) |
| `0x2A02A5E6` | 9861 × 48 | (unmapped) |
| `0xC1CC86AA` | 7114 × 23 | (unmapped) |
| `0x81305917` | 4997 × 4 | (unmapped) |

Roster path (Xenia canary, profile-specific):
`xenia_canary\content\B13EBABEBABEBABE\54540853\00000001\Roster.ROS\Roster.ROS`
(Stable build uses `xenia_master\...\content\54540853\00000001\...`, ~2.53 MB.)

### File record ≠ live-memory record

**Critical:** the on-disk player record is a *different serialization and different order* from the live-memory record. The best byte-match between a mem record and its file record is only ~77/420, the color block is at file `+0x130` but `0xFF` in memory, and mem `+0x1C` (birthdate) is `0xFF` in the file. The (de)serializer that maps file↔memory was never located. Consequence: two separate field maps exist (file-layout and memory-layout), and known memory offsets do **not** transfer to the file.

---

## 2. Tools

Located in `launcher/`:

- **`ros_file.py`** — `RosFile(path)`: parses the directory, autocorrelates stride/kind per chunk, `get`/`set`/`set_bytes` by `(chunk, row, off, struct-fmt)`, `save()` is **in-place and size-invariant** with a one-time `.bak`. Round-trip verified.
- **`ros_editor_gui.py`** — generic table/field editor. Define FIELDS (name/offset/type `u8..f32`/hex) → editable columns; field defs persist per chunk-hash in `ros_fields.json`. Standalone: `python launcher/ros_editor_gui.py "<Roster.ROS>"`.
- **`ros_live_editor.py`** — master-detail editor of the **running game's** player records (rows labeled by name). Writes live via CE; persist by saving the roster in-game. This is the practical roster editor — Ghidra cleanly yields the semantic memory layout, so the live path is far more tractable than the file path.
- **`roster_editor.py`** — the older team-STRING editor (city/nickname/arena in the string pool); still used by the Teams tab for names.
- **Launcher Teams tab** wires: "Advanced Editor (all tables & fields)…", "Live Roster Editor (running game)…", "Team Rosters (running game)…", "Team Record Fields…".

---

## 3. Player table field map (chunk `0x1E159C31`, stride 0x1A4 = 420B)

**Memory layout** (verified live against known players; the Ghidra "Rosetta Stone" is the random-player generator `Function_83FE66F0`, whose setters label each field):

```
+0x00  u32  last-name ptr    (UTF-16BE, resolvable)
+0x04  u32  first-name ptr
+0x0C  bits15-22 (8b)  HEIGHT   inches = val/5 + 50
+0x11  bits26-31 (6b)  WEIGHT   lbs ≈ 4*val + 133 (~4 lb granularity)
+0x1C  bits0-3   birth MONTH ; bits4-15 birth YEAR (literal) ; bits16-31 PORTRAIT ID (u16)
+0x20  bits0-1   CAPTAINCY (0 none/1 A/2 C) ; bits12-18 JERSEY# ; bits27-31 birth DAY
+0x28  bit4..10 (7b)  COUNTRY   62..91 alphabetical Austria→USA (see below)
+0x34  bits0-6   OVERALL (cached; game recomputes) ; bit15 SHOOTS (0 L/1 R) ; bits10-12 TYPE
+0x38  bits3-26  SALARY (raw dollars) ; +0x39 bits0-2 CONTRACT years (0-7)
+0x40  bits3-5   POSITION  0=G 1=D 2=LW 3=RW 4=C
+0x52..0xA6  RATINGS BLOCK (see below)
+0xB4  bits23-26 goalie SHELL ; +0xB8 bits0-4 goalie PATTERN
+0x130..0x1A0  RGBA gear/team color palette (u32 0xRRGGBBAA, α=FF; unused = FFFFFFFF)
+0x158/+0x15C/+0x160  the 3 team-recolor colors (same triplet the goalie-mask system uses)
```

**COUNTRY** (7-bit at bit 4 of u32@`0x28`, `(v>>4)&0x7F`): 62 Austria, 63 Belarus, 64 Belgium, 65 Brazil, 66 Canada, 67 Czech, 68 Denmark, 69 Estonia, 70 Finland, 71 France, 72 Germany, 73 Great Britain, 74 Hungary, 75 Italy, 76 Japan, 77 Kazakhstan, 78 Latvia, 79 Lithuania, 80 Netherlands, 81 Norway, 82 Poland, 83 Russia, 84 Slovakia, 85 Slovenia, 86 South Africa, 87 South Korea, 88 Sweden, 89 Switzerland, 90 Ukraine, 91 USA.
(An older map read byte `@0x28 & 0xFF` — values like 32/48/128 — which **collides** (RUS & CZE both 48, SWE & GER both 128). That map is wrong; use the 7-bit field.)

**TYPE** (`+0x34` bits10-12): 0 Playmaker, 1 Scorer, 2 Two-Way, 3 Offensive-D, 4 Defensive, 5 Butterfly (G), 6 Standup (G).

**RATINGS BLOCK — fully cracked** (verified zero mismatch against 4 real players). All 43 ratings are a regular table of 2-byte slots `0x52..0xA6`, each a **10-bit value at bit 4** of the u32 there. **Displayed rating = stored × 0.16** (stored = rating × 6.25). Sequence:

```
0x52 Speed  0x54 Accel  0x56 Agility  0x58 Balance  0x5A Strength  0x5C Stamina
0x5E Durability  0x60 Fighting  0x62 Quickness(G)  0x64 Reflex(G)  0x66 PuckHandling
0x68 Passing  0x6A PokeCheck  0x6C BodyCheck  0x6E Faceoffs  0x70 ShotPower
0x72 ShotAccuracy  0x74 HandEye  0x76 Glove(G)  0x78 Blocker(G)  0x7A Pads(G)
0x7C Rebound(G)  0x7E Positioning(G)  0x80 OffAware  0x82 DefAware  0x84 Discipline
0x86 Aggressiveness  0x88 Clutch  0x8A Creativity  0x8C Courage  0x8E Influence
0x90 Hustle  0x92 Vision(G)  0x94 Anticipation(G)  0x96 Focus(G)  0x98 Potential
0x9A-0xA6 Tendencies (Shoot, StickHandle, BodyCheck, PokeCheck, Fight, BlockShot, Challenge)
```

**OVERALL** is cached at `+0x34` bits0-6; the game recomputes it as a position-weighted linear combo of ratings (goalies R²≈0.999, skaters per-position R²≈0.97-0.99). The editor just displays it read-only.

### Team rosters / lines (team records in RAM)

In memory the team table is chunk `0x8489FAF3` (96 teams × 412B; VAN = record #28). The roster is an array of **player-record POINTERS** at `team_rec+0x00` (4B each, ~21 slots): `player_index = (ptr − player_array_base) / 0x1A4`. Moving a player between teams = rewrite the pointer slot (the "Team Rosters" editor does this live; swap mode exchanges two pointers in one write). Line/PP/PK slots live in `0x54..0x19C` of the team record and are not fully mapped.

---

## 4. Team display names (string pool ~`0x24A000..0x24C500`)

UTF-16BE, null-terminated, tightly packed, read by the game from **fixed offsets**. The string-pool chunk `0xEB69DFB9` is the **last chunk** (`0x22F965`..EOF), so nothing after it can be disturbed by growth.

**Per-team block:** `[Arena][City?][State?][CODE]`. City and State are both optional and the stock save genuinely omits them (authentic 2K data, not edit damage):
- `DAL = [American Airlines Center|TM|][Texas][DAL]` — no city; `STL = […][Missouri][STL]` — no city.
- `NYR = [Rangers Arena][NYR]` — neither. `EDM/LAK/PIT/SJS/TOR/TBL` have no state.

**Nickname pool** (after the 30 blocks): `[Team?][Nickname][ShortCode?][nickname-lowercase]`.
- The `Team` prefix is stored for **only 3 teams** — Carolina (city Raleigh), Phoenix (Glendale), Tampa Bay (Tampa) — exactly those whose prefix matches neither city nor state.
- `ShortCode` = `LA`/`NJ`/`SJ`/`TB` (for LAK/NJD/SJS/TBL).

**Which string the game displays per team — CONFIRMED in-game 2026-07-15** (same-length marker written to each candidate, restarted, all 3 rendered: `Anaheim→Anahxim` = City, `Colorado→Colorxdo` = State, `Carolina→Carolxna` = stored prefix):
- **= City**: ANA, ATL, BOS, BUF, CGY, CHI, CBJ, DET, EDM, LAK, MTL, NSH, OTT, PHI, PIT, SJS, TOR, VAN, WSH
- **= State**: COL (Denver), MIN (St. Paul), NJD (Newark), FLA (Sunrise)
- **= stored prefix**: CAR, PHO, TBL
- **unexplained**: DAL (shows "Dallas"), STL (shows "St. Louis"), NYR, NYI

The City-vs-State selector flag has **not** been found (likely one of the ~45 unidentified team-record fields — worth partitioning fields on {COL,MIN,NJD,FLA} vs the rest).

**Ruled out — do not re-investigate** the other 27 teams' prefix as an editable string:
- `default.xex` contains **no** team city names (the `Stars`/`Devils` hits there are French localization matchmaking strings).
- The full names at ~`0x2599C0..0x25C000` are **alumni/legends labels**, not live names (interleaved with player surnames, includes defunct teams; the tell: NSH — the youngest franchise — is the only team missing).
- Do NOT synthesize `display = city + nickname` (that produced the bogus "Glendale Coyotes").

### Name growth — solved, no pointer needed

Each string is read from its own fixed offset **until the `00 00` terminator**, so a string can grow *past its own slot into the next string's*: the neighbor's fixed offset then lands inside our bytes and reads as **empty**, but nothing moves and the file size never changes. So growth works by **sacrificing the next field**:
- **Only grow CITY into STATE** (safe). Ceiling: city can reach ~15 chars eating the state. Example: Atlanta→Winnipeg works by sacrificing the state ("Georgia"), invisible since ATL displays from City.
- Do NOT grow state (eats the 3-letter CODE = team identity), arena (eats city), or nickname (eats its lowercase internal key).

The byte-aligned string-reference table was **not** found (ROS bit-packs fields, so refs are probably not byte-aligned → a Ghidra job). Spare space if ever needed: ~130 unused `****` slots at `0x24A000..0x24B110` (~4.3 KB, 7 chars each).

**Teams tab** columns: `code | City | State | Team | Name | Arena | Primary | Secondary`. City/State split is positional (`tail[0]`=city, `tail[1]`=state) for 2-string blocks; a 1-string block falls back to a region-name test (so renaming a city to a state's name flips its classification — unavoidable with one string). `—` means "not stored" (double-click explains rather than pretending to edit).

---

## 5. Team primary/secondary colors (chunk `0x8489FAF3`, stride 412)

All 30 teams now resolve. **Layout — two blocks of 30, each record self-describing:**

```
+0x12B  record's own index (0..59)
+0x12C  TEAM ID (0..29, wraps in the LED block)
team block = records 0..29   PRIMARY @ +0x14C, SECONDARY @ +0x14F  (3 bytes RGB each)
LED  block = records 30..59  (same team +30) = arena LED; usually a byte-copy of the team
             colors (DAL/PIT swapped, SJS differs)
```

- `team id == roster alphabetical order`: 0=ANA, 1=ATL, 2=BOS, 3=BUF, 4=CGY, 5=CAR, 6=CHI, 7=COL … 23=PIT … 29=WSH. The code→record map is derived from the roster — **no map file needed**.

### Root cause of the 7 teams that could never be mapped

`ros_file`'s parsed chunk offset (`foff`) for `0x8489FAF3` lands **7 records (2884 B) INTO** the table — record 0 (ANA) sits *before* `foff`. The old `team_colors.py` did `base = foff + rec*stride` with a `0 <= rec` guard, so the 7 teams sorting before Colorado (**ANA, ATL, BOS, BUF, CGY, CAR, CHI**) were negative indices and silently skipped; PIT was simply absent. The old map's 22 entries were all `roster_index − 7` — correct but offset by 7.

### `team_color_map.json` is DEAD and was WRONG

It is no longer read. Its **`CAR: 23` entry pointed at ANA's arena LED**, not Carolina. (Real CAR = red `#E2373E` / black.) Do not resurrect it.

### The fix (shipped)

`team_colors.py::_team_base()` **anchors by the `+0x12B/+0x12C` signature** — it scans ±16 record shifts for the run of 30 records where `+0x12B == +0x12C == k`, and raises rather than guess. Never index this table off `chunk.foff` — the parsed chunk bounds even overlap a neighbor (`0xE35B988E`), so the container model is unreliable in this region; the id signature is ground truth. Colors are cached at load → **full game restart** to see a change. `.colorbak` on first write.

**PENDING:** in-game confirmation for the 7 newly-reachable teams (their writes land before `foff`). Cheap test: set CAR (rec 5) hot pink, restart, confirm *Carolina* (not Anaheim) turns pink.

### Team Record Fields editor

Teams tab → **"Team Record Fields…"** → grid of 30 teams × 49 fields (every one of the ~205 team-record bytes that vary across teams). Driven by **`team_fields.json`** (project root, bundled). Modules `team_fields.py` + `team_fields_gui.py`.
- To label a field: edit `team_fields.json` (rename `"name"`, record findings in `"note"`) → "Reload defs". **No code change.** Types: `u8|u16|u32|i8|i16|i32|f32|rgb|hex` (BE). "Show arena-LED records (+30)" toggles to the LED block.
- Leads worth chasing: `+0x154`, `+0x170`, `+0x172` (LED-same, 30 distinct values = more team-identity fields); `+0x157`/`+0x15B` (color-shaped triplets); `+0x10C..0x11F` (five u32s ~`0x12CFD300` that look like string-pool/asset handles).

---

## 6. Open questions / caveats

- **The 7 newly-reachable color teams are not yet visually confirmed** (writes land before the parsed chunk offset). Run the CAR hot-pink test.
- **City-vs-State display selector** is unknown — some flag among the ~45 unidentified team fields.
- **DAL / STL / NYR display source** is not fully explained (they show a name that is neither their stored city nor state field cleanly).
- **File↔memory player serializer** is not located — the file player-record field map beyond the color block at `+0x130` is unmapped; use the live editor (memory layout is fully labeled) for real editing.
- **String-reference table** (byte offsets that point at pool strings) never found; name growth relies on the terminator/sacrifice trick, not repointing.
- **Jersey slot code → `disc_` asset tie** is unresolved (not raw crc, not a raw pool byte-offset; the game uses an indirect string index). Jersey display names live in the pool as ordered `[CODE, name]` pairs (see doc 08 / the jersey map).

### Superseded old-doc claims

- Old doc `06_crowd_menu_misc.md` said team names are "editable in place (same length or shorter)… **Longer names corrupt the save**." That is **superseded**: growth into the next field *does* work for City→State (Atlanta→Winnipeg proven in simulation); the launcher can grow a city up to ~15 chars by sacrificing the state.
- Any reference to `team_color_map.json` or a `CAR: 23` mapping is **dead/wrong** — colors are now derived from the roster order via the `+0x12B/+0x12C` self-describing signature.
