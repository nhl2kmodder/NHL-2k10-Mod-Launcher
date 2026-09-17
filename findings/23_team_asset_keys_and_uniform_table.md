# 23 — Team Asset Keys and the Roster.ROS Uniform Table

**Status:** mechanism fully reverse-engineered; fix applied to Roster.ROS, in-game verification pending.
**Date:** 2026-08-01

## The question

Seattle (roster record 85) and Vegas (record 86) were added as expansion teams and given their own
asset keys (`SEA`, `VGK`) backed by ALIAS TOC entries pointing at Anaheim's IFF blobs (doc 21).
All 13 asset names resolved correctly in the archive, yet in game:

| asset key on the record | result |
|---|---|
| `ANA` (record 0, stock) | works |
| `BOS` on record 85 | works — **BOS logo**, but arena/rink/jersey are **ANA** |
| `ANA` on record 86 | works |
| `SEA` on record 85 | **no logo, hang** |
| `WPG` on record 29 (a normal, working team) | **no logo, hang** |

Adding physical blob copies instead of aliases changed nothing. Patching a 30-entry crc32 table
found in `default.xex` changed nothing.

## The answer

> **A team's `logo_<akey>.iff` is loadable only if `<akey>` appears as the asset key of at least
> one row in Roster.ROS's 407-row UNIFORM table.**

`Tex_PrecacheTeamLogos @0x83FDC6B8` builds the entire team-logo texture set by walking **all 407
uniform rows** and loading `logo_<uniform[+0x04]>.iff` for each one:

```c
if (g_TeamLogoTexBaseId == 0) {
  for (i = 0; i < Roster_GetTeamCount(); i++) {
    u = Roster_GetTeamByIndex(i);                 // <-- a UNIFORM row, not a team
    name = Team_GetAssetName(u);                  // uniform[+0x04]
    VC_FormatAssetName(buf, 0x40, L"logo_{0}.iff", name);
    Str_ToLowerInplace(buf);
    id = Tex_LoadByName(buf);
    if (id >= g_TeamLogoTexBaseId) g_TeamLogoTexBaseId = id;
  }
}
```

The archive can contain `logo_sea.iff` all day; if no uniform row names `SEA`, that loop never
loads it, the draw path finds no texture for the team, and gameplay hangs.

## The two tables everyone conflates

`Roster_GetTeamByIndex` does **not** return a team:

```c
longlong Roster_GetTeamByIndex(longlong i) {
  if (g_RosterManager && i >= 0 && i < *(int*)(g_RosterManager + 0xa8))
    return *(uint*)(g_RosterManager + 0xac) + i * 0x11c;
  return 0;
}
```

`g_RosterManager = 0x849DE29C`.

| table | chunk hash | rows × stride | roster-manager slot | fixup fn |
|---|---|---|---|---|
| **team**    | `0x8489FAF3` | 96 × `0x19C` | `+0x48` count / `+0x4C` ptr | `FUN_83D52100` |
| **uniform** | `0x1AEB24EC` | 407 × `0x11C` | `+0xA8` count / `+0xAC` ptr | `FUN_840B8408` |

Consequence: **every `Team_Get*` accessor that takes a `0x11C` object is really a *uniform*
accessor.** `Team_GetAssetName`, `Team_GetUniformBaseId`, `Team_GetDivisionId` and `Team_GetLeague`
are all misnamed in the Ghidra database.

### Uniform record (`0x11C` bytes)

`FUN_840B8408` fixes up exactly three self-relative pointers, at `+0x00`, `+0x04`, `+0x08`:

| off | meaning |
|---|---|
| `+0x00` | uniform-base key → `uniform_base_<key><slot>.iff` (`Team_GetUniformBaseId` = `lwz r3,0(r3)`) |
| `+0x04` | **asset key** → `uniform_<key><slot>.iff`, and the logo-precache key (`Team_GetAssetName` = `lwz r3,4(r3)`) |
| `+0x08` | label — `Home`, `Away`, `Alternate`, `Mii`, `Home '03` … |
| `+0x0C` | u32 id of the label |
| `+0x10` | u32 per-uniform-set unique id — **not** a hash of the key (the empty key maps to three different values) |
| `+0x14` | u16 **owning team id** (`Team_GetDivisionId` = `lhz r3,0x14(r3)`) |
| `+0x18` | `(v >> 29) & 7` = slot: 0 `_home`, 1 `_away`, 2 `_alt`, else `_mii` (`Team_GetLeague`) |
| `+0x18` bits 24–26 | **collar style 0..4** → skater collar record `16 + style` (1 = laced strings); bit 27 unidentified (5 alternates). See doc 22 §Collars. |

407 rows over 168 distinct keys — every team's home/away/alt/Mii plus classic jerseys
(`ANA_CL05`), created-team uniforms (`*_CRT`), and international sides (`AUT`, `BEL`, `AUS`).

### Team record (`0x19C` bytes)

`FUN_83D52100` fixes up 52 pointers: words `0..0x27`, then `+0xA0..+0xB0`, then `+0xC4..+0xDC`.

> ⚠ The **true record base is 8 bytes past** what `launcher/team_order.py` calls record 0.
> `team_order`'s `OFF_CITY 0xA8 … OFF_AKEY 0xB8` are real `+0xA0 … +0xB0`. The absolute addresses
> the launcher writes are correct; only the framing differs.

| real off | meaning |
|---|---|
| `+0x00..+0x9C` | 40 pointers to this team's player records |
| `+0xA0` `+0xA4` `+0xA8` `+0xAC` | city, nickname, 3-letter code, lowercase nickname |
| `+0xB0` | **asset key** (`FUN_840AD298` = `Team_GetAssetKey` = `lwz r3,0xB0(r3)`) |
| `+0xC4` `+0xC8` `+0xCC` `+0xD0` | division, self, affiliate team, arena |
| `+0xE8` | u16 **team id** (`FUN_840AD4D8` = `lhz r3,0xE8(r3)`) |
| `+0xEA` / `+0xEB` | home / away uniform slot index (`FUN_840AD500` / `FUN_840AD528`) |

## The data flow

`Function_83FD66B8` is the asset-name builder. It picks the template from a caller-supplied tag
and fills two tokens:

```c
u = param_2 ? g_HomeUniform : g_AwayUniform;     // FUN_83bce1a8 / 83bce1b8
t = param_2 ? Game_GetHomeTeam() : Game_GetAwayTeam();
slot = ["_home","_away","_alt","_mii"][Team_GetLeague(u)];
  tag 0x5AF04CD7 -> L"uniform_base_" + Team_GetUniformBaseId(u) + slot
  tag 0xE48E9A13 -> L"logo_"         + Team_GetAssetKey(t)                 // no slot
  tag 0xF62C79B7 -> L"uniform_"      + Team_GetAssetName(u)   + slot
```

**So the logo comes from the *team* record and the jersey comes from the *uniform* record.** That
asymmetry is exactly what produced "BOS logo, ANA jersey".

The team's uniform row is chosen by `Roster_GetNthTeamInDivision(team, n)` — also misnamed; it is
`Team_GetNthUniform`:

```c
id = team[+0xE8];
for (i = 0, k = 0; i < *(int*)(g_RosterManager+0xa8); i++) {
   u = *(uint*)(g_RosterManager+0xac) + i*0x11c;
   if (Team_GetDivisionId(u) == id)          // uniform[+0x14] == team[+0xE8]
      if (k++ == n) return u;
}
return 0;
```
called with `n = team[+0xEA]` (home) / `team[+0xEB]` (away); the result is cached in
`g_HomeUniform @0x84B9D950` and `g_AwayUniform @0x84B9D954` by `Function_83C6E2D8`.

## Why each experiment came out the way it did

Seattle and Vegas were **cloned from Anaheim**, so they inherited **team id 0 — Anaheim's**. They
therefore resolved to Anaheim's uniform rows (hence ANA jerseys/arena), while their *own*
created-team uniform rows sat unused with **empty key strings**.

- `SEA` — not a key in any uniform row → never precached → no logo → hang.
- `BOS` on record 85 — `BOS` is a uniform key → logo loads; uniforms still come from team id 0 → ANA.
- `ANA` on record 86 — works trivially.
- `WPG` on record 29 — Winnipeg is the relocated Atlanta and still carries team id 1; no uniform
  row is keyed `WPG` → no logo → hang. This is the control that proves the archive was never the problem.

## The fix — 20 bytes, Roster.ROS only, console-portable

Created-team slots already own blank uniform rows, keyed by team id:

| team id | uniform rows |
|---|---|
| 106, 107, 108, 109 | 253/254, 255/256, 257/258, 259/260 |
| 200, 201 | 261/262, 263/264 |
| 244 | 265 |

`scratchpad/fix_uniform_rows.py` (applied; backup `Roster.ROS.preuniform`):

- uniform rows 253/254 `+0x00`,`+0x04` → the `SEA` string already in the pool at `0x24C9C2`
- uniform rows 255/256 `+0x00`,`+0x04` → the `VGK` string at `0x24CA0E`
- team record 85 `+0xE8` `0` → `106`; record 86 `+0xE8` `0` → `107`
- team record 86 `+0xB0` `ANA` → `VGK`

Result: `uniform_SEA_home.iff`, `uniform_base_SEA_away.iff`, `logo_SEA.iff` and the VGK equivalents
are now all requested, and all 13 alias TOC entries per team already resolve. No `default.xex`
patch and no archive change, so this survives on real hardware.

## Shoulder patches — stamps, picked by the ROS, placed by the base asset

They are NOT painted into the base texture (Calgary's base has no flags; its stamps sheet does).
Three things combine:

1. **Sheet cells.** Stamps sheet (2048×512) entry 2 = `[512,256,256,256]` = LEFT shoulder, entry 3
   = `[768,0,256,256]` = RIGHT shoulder (entry 1 `[512,0,256,256]` is the thigh patch). 67 of the
   shipped kits carry ink there; CGY home = Canada (2) / Alberta (3).
2. **ROS picker.** Uniform record `+0x18` bits 9-11 = LeftLogo entry, bits 12-14 = RightLogo (XEX
   getters `0x840b65e0` `(f>>9)&7`, `0x840b6570` `(f>>12)&7`; pants patch `(f>>6)&7`, Stanley
   `(f>>3)&7`). `Uniform_SetStampShaderParams` @`0x8408EAE8` fetches the rect at `obj+(v+1)*16`,
   i.e. **picker value = table entry**, no remap. Most kits use left=2, right=2 — the same mark on
   both shoulders; fill entry 3 only when the shoulders differ.
3. **Placement = material params** in `uniform_base_<team>_<kit>.iff`, DRAM section
   `0xBB05A9C1`: 0x50-byte records, `+4` CRC32 of the name, `+0x20` f32. `SlvLftLogo
   HOffset/HScale/VOffset/VScale` = `A7FCF262/CE65E11C/901D15F1/F7B982F7`, `SlvRgtLogo` =
   `77A7A76D/F52619F9/404640FE/CCFA7A12`, `FrntLogo` (crest) = `63693738/3AEA03E5/5488D0AB/0336600E`.
   Each name occurs once per material set (#0 back, #1 front, #2 arms, #3 pants; stride 0x3390,
   first set at 0x2EB0). Defaults Frnt `(0,3,-1.5,3)`, Slv `(0,9,-2,9)`. Base-UV → decal-UV:
   crest `U=HS·u+0.5+(HOff−HS)/2, V=VS·v+0.5+(VOff−VS)/2`; left `U=−HS·v+0.5+(HOff+HS)/2,
   V=VS·u+0.5+(VOff−VS)/2`; right `U=HS·v+0.5+(HOff−HS)/2, V=−VS·u+0.5+(VOff+VS)/2`; then the
   entry rect's `S.xy/S.zw` takes it to sheet texels. The shader param names the XEX writes:
   `FrontLogoUVScaleAndOffset 0x4c9631a1`, `LeftLogo… 0x618f5b37`, `RightLogo… 0x680caf39`,
   `PantsLogo… 0x945f6531`, `Captain… 0xd8214bde`, `NHLLogoShirt 0x2adac151`,
   `NHLLogoPants 0xcda8e5c3`, `ReebokLogoShirt 0x9b2562f3`, `ReebokLogoPants 0x7c574661`.

The launcher's Jersey Editor preview draws all three from these params
(`stamp_shader.kit_logo_params` / `logo_site`). The patches sit on TOP of the shoulder cap, so
they are edge-on from the front — tilt the figure. ⚠ Until 2026-08-26 the converter composed the
stamps sheet from blank and NHL 23 art has no shoulder mark, so every converted kit lost its
flags; compose mode now carries the stock kit's shoulder cells over. Re-apply older kits.

## Dead ends — do not repeat

- **`default.xex` plays no part.** The 30-entry crc32 table at VA `0x84269624` (file `0x226F624`)
  has **zero** code references — verified with a PPC constant-tracking scanner that finds 210
  references to `g_RosterManager`, over the exact window and ±0x400, and a whole-image search
  showed the 30 values exist in exactly one cluster. Every patch to it was a no-op. Restored.
- **Physical blob copies are unnecessary.** Aliases are fine. 31 MB of ANA copies remain in the
  `1B` tail (`1B.copytest_size` holds the original length) with SEA's TOC entries pointing at them;
  harmless, revertible to plain aliases.
- A team's asset family is **13** files, not 11 — `ice_%s_finals` and `ice_%s_playoffs` were missed.

## XEX address model (two wrong models cost most of a session)

The XEX basefile is a **flat memory image** laid out by virtual address. The PE section headers'
`PointerToRawData` fields are stale and must not be used. There is exactly one delta:

```
file = VA − 0x81FFA000        (MZ at 0x6000, image base 0x82000000)
```

Verified four independent ways, including a live Cheat Engine read of `0x183B3057C` (Xenia maps
guest VA `v` at host `0x100000000 + v`) returning the UTF-16BE bytes of `logo_{0}.iff`.
Using the raw-pointer deltas puts the asset-key table at a bogus VA in the `.text`/`.data` gap and
makes every xref query come back empty.

`.pdata` is an array of 8-byte `RUNTIME_FUNCTION`: u32 `BeginAddress` (a **full VA**, not an RVA)
plus u32 packed, where length = `((packed >> 8) & 0x3FFFFF) * 4`. 23,867 functions.

Tooling: `scratchpad/xexmap.py` (address/section/function map), `scratchpad/xrefscan.py`
(PPC abstract interpreter that finds every instruction computing an address in a VA window),
`scratchpad/uniform_table.py` (uniform + team table reader).

## Named this session

| address | was | is |
|---|---|---|
| `0x83D53AA0` | `Function_83D53AA0` | `Roster_FixupPointers` — the 19-chunk relocation pass |
| `0x83D52100` | — | `Team_FixupPointers` (0x19C record) |
| `0x840B8408` | — | `Uniform_FixupPointers` (0x11C record) |
| `0x83D4C530` | `Roster_GetTeamByIndex` | `Roster_GetUniformByIndex` |
| `0x840AD298` | `FUN_840AD298` | `Team_GetAssetKey` (+0xB0) |
| `0x840AD4D8` | — | `Team_GetId` (+0xE8) |
| `0x840AD500` / `0x840AD528` | — | `Team_GetHomeUniformSlot` / `…Away` (+0xEA/+0xEB) |
| `0x840B6298` | `Team_GetDivisionId` | `Uniform_GetTeamId` (+0x14) |
| `0x840B62C0` | `Team_GetLeague` | `Uniform_GetSlot` (+0x18 >> 29) |
| `0x840B6248` / `0x840B6258` | `Team_GetUniformBaseId` / `…AssetName` | `Uniform_GetBaseKey` / `Uniform_GetAssetKey` |
| `Roster_GetNthTeamInDivision` | — | `Team_GetNthUniform` |
| `0x84B9D950` / `0x84B9D954` | — | `g_HomeUniform` / `g_AwayUniform` |
