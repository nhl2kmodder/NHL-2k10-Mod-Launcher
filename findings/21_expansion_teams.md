# 21 — Expansion teams: a 31st NHL team works, from roster data alone

**Status: mechanism PROVEN in game, 2026-08-01. Reverted afterwards — nothing shipped.**
Parked deliberately so the Winnipeg/Utah relocation can ship in a clean state first.

---

## 1. The answer

A 31st NHL team enumerates from `Roster.ROS` **data alone**. Cloning Anaheim's 412-byte team
record into empty slot 85 (rebasing every self-relative pointer) produced a second Anaheim at
the end of the team list — and it **survived reverting the executable to stock**. No XEX patch
is required, so the feature is console-portable and belongs in the mod-pack roster machinery
alongside `team_order`.

A full Seattle Kraken was then built and verified: own strings, own 22 player rows, own
back-pointers, Anaheim untouched.

## 2. The gate

Team records carry **league in the high nibble of disk `+0x110`, division in the next nibble**
(`FUN_840ad870` = `*(u32*)(mem+0x108) >> 28`; mem+8 = disk). League 0 = the 30 NHL teams,
1 = the 30 AHL affiliates, 3 = 16 nationals, 5 = the created-team slots. Divisions are the real
2009-10 alignment, **exactly 5 teams each** (0 Atl, 1 NE, 2 SE, 3 Pac, 4 NW, 5 Cen).

Enumeration requires league 0 **and a structurally complete record**. The exact gating field was
never narrowed — the build clones a real team and inherits all of them.

## 3. Three probes (each an elimination — do not retry)

| Probe | Result | Kills |
|---|---|---|
| existing *created* "Seattle Kraken" (record 81) → league 0 | no change | the league nibble alone is not the gate |
| `GetNHLTeamCount` @ `0x83D4CF08` `li r3,30`→`31`, verified live in guest RAM as `38 60 00 1F` | **no change** | that accessor does not drive the list, despite 14 callers + a thunk |
| full ANA clone → slot 85, league 0, own `+0xD0`, unique id | **worked**, and still worked with a stock XEX | — |

Created records are structurally incomplete: `+0xF4`, `+0xF8` and `+0x164..+0x170` zeroed, and a
`+0xD0` that doesn't resolve to a team. **Clone a real record; never promote a created one.**

Static corroboration: there is **no `cmpwi/cmplwi #30` within 0x300 bytes of any of the 98
`g_RosterManager` load sites**. The list is not bounded by an inline literal near roster code.
Vestigial: `NextTeam` @ `0x83d4cfa0` wraps on a hardcoded `0x1e` (one caller).

## 4. The recipe that worked

Copy the 412 bytes, then for every field in `team_order.PTR_FIELDS`:
`new_val = (old_field_off + old_val - 1) - new_field_off + 1`. Then set `+0xD0` to its own
`record+8`, give a unique `+0xC8` id, and set `+0x110`'s high byte to `0x0<div>`.

Players: clone each source row (stride 420), rebase pointers at `+0x00` (surname), `+0x04`
(first name) and `+0x24`, set the team back-pointer `+0x14` to the new `record+8`, then point the
team's `OFF_PLAYERS` array at the new rows, zero-terminated.

Capacity: 96 team records with **86–90 empty**; 2715 player rows with **697 free**; 40 arena
records with **5 free** (33, 35, 36, 37, 39).

## 5. Assets — the naming convention and how to add a team's own

Lookup is `crc32(NAME.UPPER)` → **one TOC** (in `0A`, count at `0x10`, entries at `0x58`,
16 bytes = `flags/size/crc/offset÷0x800`) covering all four archives. The team's **asset key**
(`+0xB8`) composes the names. Eleven assets per team:

| asset | archive |
|---|---|
| `logo_<CODE>.iff` | 0B |
| `arena_<CODE>.iff`, `rink_<CODE>.iff`, `led_<CODE>.iff`, `arena_presentation_<CODE>.iff` | 0A |
| `zamboni_<CODE>.iff`, `zamboni_team_<CODE>.iff` | 0A |
| `uniform_<CODE>_home/away.iff`, `uniform_base_<CODE>_home/away.iff` | 0A |

23 of 30 teams add `uniform_<CODE>_alt.iff` + `uniform_base_<CODE>_alt.iff`; Anaheim does not.

**Plan: alias entries + copy-on-write.** A new TOC entry may point at the *same* offset/size as
the source team's blob, so a full asset family costs **zero copied bytes**. Add the 11 SEA names
pointing at ANA's data, flip the asset key to `SEA`, and the game looks identical while Seattle
owns its namespace. ⚠ While aliased the two share bytes, so the first edit of any SEA asset must
force a relocate (copy to archive tail, repoint the entry) or it would edit Anaheim too.

⚠ Never point an asset key at a name with no assets: that means no logos/jerseys **and a hang**
(doc: `project_team_asset_key`).

**TOC capacity.** The table ends at `0x96C8`, the first data blob starts at `0x9800` ⇒ exactly
**19 spare entry slots**. Seattle's 11 fit; Seattle + Vegas (22) do not. Fix: relocate whichever
blob sits at `0x9800` to the archive tail (the existing safe-grow primitive), freeing a `0x800`
block = **128** more slots. Do this once, up front.

## 6. Audio — not name-keyed, and the open lead

None of the audio follows the asset-key convention, so a new team inherits whatever its **team
id** maps to, or silence:

* **Goal horns** — all 44 named, ordered alphabetically by *city*; selection is by team index/id
* **Crowd chants** — the roster supplies a team id and the **bank trailer** maps id → cue
* **Goal songs** — established as *not present in static data at all*; nine avenues eliminated
* **Team PA / arena SFX** — selection was a static dead end

**Lead to pursue (2026-08-01):** the commentary line indexes and cue tables are already largely
mapped (docs 14/19, `project_speech_cue_tables`, `project_phrase_bank_naming`). Rather than more
static search, **reverse the call sites that consume those indexes** — find who reads the cue id
for a horn/chant/name drop and what it keys off — and the same approach should crack horn and
chant selection. That is the next audio task, and it is expected to be substantial.

Commentary actually *saying* "Seattle" is a voice-synthesis job, not a lookup one; the pipeline
built for Atlanta→Winnipeg / Phoenix→Utah applies directly (`project_voice_replace_tool`).

## 7. Risk carried forward

31 teams is an odd league with a 6-team division. The menu handles it; **season schedule
generation and playoff brackets are untested** and are the most likely objectors. Adding Seattle
and Vegas together for 32 is probably less fragile than shipping 31 — test a season sim either way.

## 8. Revert state

Everything above was reverted on 2026-08-01. The roster was restored byte-identical to its
pre-experiment backup (`Roster.ROS.seabak`) — slots 85–90 empty, 697 free player rows, league 0
back to exactly 30, Winnipeg and Utah intact — and `default.xex` is byte-identical to
`default.xex.teamcountbak` with `GetNHLTeamCount` at stock 30.
