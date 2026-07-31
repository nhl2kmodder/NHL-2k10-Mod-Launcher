# 16 — Player Audio Name IDs (announcer names)

Status date: 2026-07-29

## Summary

Every player record in `Roster.ROS` carries two u16 big-endian "audio name" IDs — one for
the first name, one for the last name. These select a *pre-recorded spoken name*. A player
whose name is not in the game's recorded-name list stores `0xFFFF` and the announcer says
nothing for that part of the name.

## Record layout — VERIFIED

Chunk `0x1E159C31`, 2715 records x 420 (0x1A4) bytes. **Record 0 base = `0x5A1B`** (absolute
file offset in a 2,532,004-byte `.ROS`).

| Offset | Size | Meaning | Grade |
|---|---|---|---|
| `+0x00` | 0x1E | first name text, UTF-16BE, `0x00`-terminated, `0xFF`-padded | verified |
| `+0x1E` | 0x30 | last name text, UTF-16BE, `0x00`-terminated, `0xFF`-padded | verified |
| `+0xB1` | 2 | **last-name audio ID**, u16 BE | partially verified |
| `+0xB3` | 2 | **first-name audio ID**, u16 BE | **verified** |

`0xFFFF` in either field = "no recorded name" = announcer silent for that part.

### How `+0xB3` was verified

Controlled in-game experiments (`Roster_Investigate/test1..test6.ROS`), one edit per save:

| Change made in-game | File delta | Decode |
|---|---|---|
| first name `Adam` -> `Alec` | `0x0F2A4A`: `138b` -> `1757` | 5003 -> 5975 |
| first name -> **first** list entry `A.J.` | `0x0F2BEE`: `147b` -> `172a` | -> 5930 |
| first name -> **last** list entry `Zigmund` | `0x0F2BEE`: `172a` -> `1698` | -> 5784 |
| first name -> **typed** name `shjay` (not on list) | `0x0F2A4A`: `1757` -> `ffff` | -> no audio |

Both probe addresses satisfy `(addr - 0x5A1B) % 420 == 179 (0xB3)` and sit in adjacent
records (2311, 2312) — the stride, base and field offset are all confirmed by this.

The typed-name case independently confirms the user-reported behaviour that a hand-typed
name produces no announcer audio, and pins `0xFFFF` as the sentinel.

`+0xB1` is graded *partially verified*: it is the adjacent u16 and behaves correctly
(e.g. a 2025-converted record with last name `Bedard` — a name that cannot exist in a 2009
game — holds `0xFFFF`), but no single-variable in-game edit has been captured for it yet.

## Known ID values — ground truth

| Kind | Name | ID |
|---|---|---|
| first | `Adam` | 5003 |
| first | `Zigmund` | 5784 |
| first | `A.J.` | 5930 |
| first | `Alec` | 5975 |
| last | `Lee` | 728 |

Observed bands: first-name IDs ~5000–6300, last-name IDs ~0–4200.

## ID ordering — what it is NOT

- **Not alphabetical.** `A.J.` is list entry [1] but has ID 5930, higher than `Zigmund`
  (last entry) at 5784.
- **Not the ROS string-pool index.** Pool index of `A.J.` is 2927, of `Zigmund` 3597;
  neither has a constant offset to its ID.
- **Not stored as a parallel array.** A full 512 MB guest-RAM scan plus both save files
  found no u16 array holding 5930 and 5784 at the required spacing, and no u32 pointer
  table referencing the resident name strings (any of 5 base variants).

The ID space is most likely the order the names were **recorded** in the studio, which is
arbitrary with respect to display order.

## The ROS string pool — CORRECTED

The pool is **two** contiguous UTF-16BE runs, not one (earlier 6155-string / three-block
figures were produced by a scanner that skipped gaps and are wrong):

| Range | Count | Content |
|---|---|---|
| `0x23E928`–`0x244C84` | **1718** | **player names in record order, `[last, first]` per record, name-deduplicated**; tails off into coach names and jersey numbers |
| `0x244C86`–`0x25CE10` | **5049** | created-player `****` slots, then the two UI pickers, then extras (ends `Paul Henderson`) |

Within run B (0-based): `[1207]='2K Winners'`, `[1208]='A.J.'` … `[1878]='Zigmund'` = the
**671-entry first-name picker**; `[1879]='Aalto'` … `[4436]='Zwokil'` = the **2558-entry
last-name picker**. These are exactly the create-player lists the user sees on screen
(confirmed: the first-name list begins `A.J.` and ends `Zigmund`).

The pickers are **dedup leftovers** — `Adam`, `Getzlaf`, `Niedermayer`, `Lee` all appear in
run A and are absent from run B — so the true master lists are larger than 671 / 2558, and
picker index is not the ID (`A.J.` is picker index 1 but ID 5930).

**Run A is written in player-record order.** Simulating that emission reproduces the pool
exactly (1718/1718 consumed, terminating at record 1884), which confirms the ordering. The
`[last, first]` pairing is visible directly: `Getzlaf, Ryan, Carter, Niedermayer, Rob,
Marchant, Todd, …` = Ryan Getzlaf, Ryan Carter (second `Ryan` deduped away), Rob
Niedermayer, Todd Marchant.

File -> guest-RAM delta for the pool: constant `+0x484C166`. The pool is resident in RAM
verbatim (verified by locating `A.J.`, `Zigmund`, `Aalto`, `Zwokil`, `Getzlaf`).

## Open blocker — ID -> name text

The mapping from ID to the spoken name's *text* has not been recovered. Ruled out:

- Any array of IDs, pool offsets, pool indices, or crc32 name hashes inside `.ROS`
  (contiguous and strided scans, strides 2/3/4/5/6/8/10/12).
- `default_flat.xex` — none of the list names appear in ascii, UTF-16BE or UTF-16LE.
- `english.dram`, `loc.iff`, `roster.iff`, `droster.iff`, `roster.dram` — no list names.
- `gamedata.iff`, `global.iff`, `playercreate.iff` — no list names.
- `1A_Audio_Catalog.json` — 37,430 streams, all tagged `Commentary`, zero name metadata.
- Guest RAM — no ID array, no pointer table into the name strings.

*(Correction, 2026-07-27: an earlier revision of this doc claimed "player-table record
order is not the pool serialization order", citing stock records 0/1 carrying different
first-name IDs 5390/5116 where the pool demands both be `Ryan`. That conclusion was wrong —
the real cause was a bad slot model, see the alignment section below. Records ARE in pool
order. The 5390/5116 observation is instead evidence of **alias IDs**: two different
recorded variants of the same spoken name.)*

The 2025-converted roster is *also* not a usable dictionary: most of its IDs are stale,
inherited from whichever 2009 player occupied the slot (record 3 holds text
`Victor`/`Hedman` over stock record 3's IDs 5664/919), which is why the same name maps to
several IDs (103 first-name and 98 last-name conflicts).

## Gap analysis — usable today

Computed from the verified fields alone, stock vs the 2025 roster:

| Metric | first names | last names |
|---|---|---|
| distinct IDs used by stock roster | 482 | 1219 |
| distinct IDs used by 2025 roster | 504 | 1010 |
| **freed** (stock used, 2025 does not) | **63** | **325** |
| 2025 players with **no** audio (`0xFFFF`) | 169 | 478 |

Output: `audio_gap.json` (freed ID lists + the affected player records).

Caveat: the freed IDs are known only as *numbers*. Assigning one to a player makes the
announcer say **that ID's recorded name**, which is not yet known — so this is not safe to
apply until the ID -> name text mapping is solved.

## Display names resolve from the ID — SUPERSEDED (see "How names actually resolve")

*(Correction, 2026-07-27 PM: the claim below is wrong as stated. Display AND play-by-play
resolve through the POOL STRING TEXT a record is bound to, not through the id number —
proven in-game by rewriting the pool string "Lucic" to "Perry" (same length, in place):
both the roster screen ("Milan Perry") and PxP ("Perry") followed, with the id untouched.
Conversely, a single-variable file edit of stock Lucic's `+0xB1` to 450/'Getzlaf' — with a
team-name witness proving the file was loaded — changed NOTHING. The id fields are
save-file bookkeeping/handles; the announcer key for real roster players is still not
fully identified. See the session log section below.)*

Names are resolved **lazily at draw time** — a live RAM scan with the stock roster loaded
found exactly one copy of `Getzlaf`/`Marchant`, inside the ROS image itself
(file -> RAM delta `+0x474417C` for that load), with no resolved-string cache to harvest.

## Route to the full dictionary — alignment solver — BUILT (partially verified)

Run A of the ROS string pool is emitted in **player-record order** with name-dedup, so
aligning records to run A yields `ID -> name`. `scratchpad/align_solver.py` implements this
as a per-record dynamic program. Four model corrections were needed before it worked; each
was previously a wrong assumption in this doc:

1. **Run A is 1695 names, not 1718.** Indices 0…1694 are names (last = `Abe`); from 1695 on
   it is an adjacent position/jersey table (`Center, 2, 4, 1, 3, Defenseman, 5, 6, Goalie,
   Left Wing, Right Wing, 7, 9, …`). Cutting that tail is what makes the budget balance.
2. **`0xFFFF` is a mandatory CONSUME, not a skip.** It means "no recorded *audio*", not "no
   name" — stock records carry no inline text, so such a player still needs a *display* name
   from the pool. These 186 fields (L=181, F=5) act as hard synchronization anchors.
3. **Slot budget:** NONE 3543, BRANCH 1701, FORCE 186 → 1887 consumers vs 1695 names →
   **exactly 192 alias-skips**.
4. **Records are laid out in team blocks, in the same team order as the pool** (alphabetical
   by city). Block 0 = Anaheim, records 0–22 (23 players, 46 fields − 4 dedups = pool[0:42]).

Scoring combines a character-trigram naive-Bayes first/last classifier (EM self-trained),
a counted-lexicon prior, a curated given-name gazetteer, an adjacent-pair bonus, and a
diagonal band constraint. The gazetteer is required: names living in run A are resolved by
ID, so the 2025 roster stores no inline text for them, and the pickers are run A's
complement by construction — `Scott`, `Todd`, `Ryan` get **zero** lexical support from the
file itself.

**Result:** `scratchpad/id_name_solved.json` — **1509 IDs named, 192 alias-unresolved.**

Validation (offline, not yet in-game):
- **46/46 on the hand-labeled Anaheim block**, stable across every hyperparameter setting
  swept (pair bonus 0.0–4.0 × band 12/30/80) — not a knife-edge fit.
- **Untuned blocks reconstruct correctly**: all of Atlanta (Little, Perrin, Slater,
  Peverley, Reasoner, Enstrom, Hainsey, Valabik, Exelby, Bogosian, Oystrick, Salmela,
  Lehtonen, Hedberg, Kovalchuk, Kozlov, Lavallee, Stuart, Armstrong) and Boston/Buffalo
  (Lucic, Thornton, Kobasew, Ryder, Wheeler, Roy, Hecht, Gaustad, Moore, Mair, Tallinder,
  Lydman, Spacek, Sekera, Numminen).
- **Held-out ground truth `728 -> 'Lee'` correct with no anchor.**

Honest residuals:
- A global error proxy (given names appearing in last-name slots) suggests **~5% of L slots
  are misassigned**, scattered 4–8 per 100 pool positions in the NHL region and lower in the
  trailing developer-name block. Not one catastrophic drift.
- **`5003` conflicts**: the picker test reported `5003 = 'Adam'`, the solver reads it as
  `'Milan'` (rec67 = Milan Lucic, with Boston reconstructing correctly around it) and gives
  `'Adam'` to `5122` (rec80 = Adam Mair). `5975` ('Alec') lands in `alias_unresolved`,
  consistent with the alias theory. Both are the alias-ID signature: the create-player
  picker writes its own canonical ID for a name while a roster player carries a different
  recorded variant of the same spoken name. **Needs an in-game check to settle.**
- Anchoring `5003`/`5975` onto their picker positions makes them decline to consume at all
  and flips parity across the whole Boston block, so they are deliberately not anchored.

Dead ends cleared while building this (do not repeat): no player→team field at any record
offset; no index/order/lineup array in any chunk; no per-record pool reference at any offset
or encoding; the mod roster's typed-name table has unusable parity (blank entries); a
picker-only-trained classifier caps at 73.5% holdout.

**Grading: theory / offline-validated.** Nothing here is in-game confirmed yet. The
verification is cheap: set one player's `+0xB1`/`+0xB3` to a solved ID and confirm the
announcer says the predicted name.

Alternative if the residual error proves too high: Ghidra — find the writer of player
`+0xB3` in the create-player list handler and follow it to the table.

## Expected shape of the name audio (user domain knowledge, not yet verified)

Per the project owner, name recordings are **variations of the same name at different
intensities**, not per-player alternates: a flat low-energy read, a mid read for
end-to-end play, a high-excitement read for a scoring chance, plus connective forms
("Over to Ryan!"). Expected layout is therefore **contiguous per name** — name starts at
some offset and its N variations follow at fixed steps, with N likely uniform across names.

Additional expectations to test:
- **Last names have far more variations than first names** (commentators mostly use surnames).
- Separate voice sets exist: **play-by-play**, and **PA announcer** with home-team (excited)
  vs away-team (flat) reads for goal calls. Whether the colour commentator carries the same
  intensity tiers is unknown.

If true, a single name maps to a contiguous block of streams, and the audio ID is most
likely the block index — which would make preview-by-ID straightforward once the block
stride is measured.

## Session log 2026-07-27 PM — how names actually resolve (current state)

Everything below was established by controlled in-game listening tests (grades marked).

**VERIFIED in-game:**
- Rewriting a stock pool string's TEXT in place (same length) changes both display and
  PxP: `"Lucic"@0x23EFA0 -> "Perry"` produced "Milan Perry" on screen and "Perry" from
  PxP. The pool string text is what both consumers ultimately read for a stock record.
- Editing `+0xB1` alone in the file does NOT change PxP for a real roster player (BOSTIN
  team-name witness proved the exact file version was loaded).
- The in-game Edit-Player picker DOES redirect PxP (Lucic -> "Getzlaf" heard). Its diff
  vs the pre-save shows the game wrote: the record's `+0xB1`, a UTF-16BE string appended
  to a string heap (file offset = u32BE cursor at header `+0xE8`, plus 0xE8), the bumped
  cursor, and it also rewrote ANOTHER record's fields (rec56 Wideman: string ptr moved,
  lid 2891->420) — i.e. the game re-serializes pool bindings on save. Replicating
  lid+heap+cursor by file edit on a third player did NOT activate — some invariant is
  still missing.
- The 2025 roster's wrong announcer names (its records inherited 2009 bindings; e.g.
  the record typed "Evander Kane" is announced "Stewart") are NOT fixed by rewriting the
  solver-predicted lid-bound pool strings: a 185-string fix v1 was applied, tested
  (Lindholm->"Wisniewski", Pastrnak->"Kubina", Kane->"Stewart"), and REVERTED
  (`Roster.ROS.audiofix.bak`).

**RAM structures (measured, launcher plumbing `goalie_equipment` — host = 0x100000000 +
guest):** `g_RosterManager@0x849DE29C` -> chunk table -> player records (2715 x 0x1A4,
RAM order != file order). RAM record `+0x00`/`+0x04` = display last/first string
pointers; recorded names dedup to shared heap strings (`0xA4A8xxxx`), typed customs are
stored inline inside the record itself. **Evander Kane and Patrick Kane share ONE "Kane"
string object yet are announced differently ("Stewart" vs "Kane")** — so the announcer
key for roster players is none of: display text, record string ptr, raw lid. Remaining
candidates: opaque per-record struct ptrs `+0x14`/`+0x28` (-> `0xA496xxxx` blocks), or a
boot-built table. File-side record bytes `+0x85`/`+0x89` are stale runtime addresses
from the last save (garbage at load).

**Dead ends (do not retry):** naive loader re-walk simulations of the mod roster's id
sequence (0/3 on measured ground truth, all four skip/consume variants); RAM value-scans
for id->stream tables (counter arrays produce false 19/19 clusters); stream-offset
references in `global.iff`/`gamedata.iff`/bank hash dirs.

## 1A play-by-play name audio — layout CRACKED (user-labeled by ear)

Working in stream indices of the offset-sorted `1A_Audio_Catalog.json` (37,430 streams):

- **Flat "regular emotion" last-name tier: indices 23777–24475.** Structure = one big
  ALPHABETICAL main batch (~23800–24150, the legacy recording cast from an earlier 2K
  title — LaChance/LaFlamme/Linden present, NO Lucic/Lundqvist/Luongo/Lupul) followed by
  several small alphabetical append-batches of later-recorded players (Seabrook…,
  Ballard–Staal, Olesz–Kovalev, Gorges–Kessel–Turris).
- **"<Name> in the corner" tier starts at 24476** (Adams, Bell, "Chris Drury" with full
  name), alphabetical again. Further tiers beyond, unmapped.
- ~60 labeled anchors + open questions: scratchpad `pbp_name_anchors.json`.
- Stream index is NOT related to the roster id (tested against solved lids).
- Extraction pipeline works: seek 1A at catalog offset, wrap with launcher
  `make_riff_xma2`, decode via `xma2encode.exe` -> WAV.

## TODO (in value order)

1. **Ghidra: find the commentary cue-selection code** — what does it read from the
   roster record to pick the spoken name? Reproducible subject: Evander Kane (announced
   "Stewart"). Alternative shortcut: live-poke his RAM record fields one at a time
   (`+0xB1`, `+0x14`, `+0x28`) mid-game and hear which flips the call.
2. Name the 1A name-call streams. **Auto-labeling shortcuts tested and ruled out
   (2026-07-27):** (a) no available name list matches the batch membership — measured
   gap sizes (49/19/16/12/17/…) are 3–10x smaller than the same alphabetical spans in
   the full recorded set (3619), run-A lasts (1061), or stock-used lids; the ~375-name
   main batch is an earlier 2K title's cast we don't have; (b) duration↔phonetics
   regression on 53 labeled anchors gives R²=0.38, residual 0.11s vs ~0.1s between-name
   spacing — too weak to discriminate alphabetical neighbours. **The exact route is the
   game's own name→cue table (part of TODO #1's Ghidra dig)** — it gives all tiers,
   first names included, in one shot. Manual gap-labeling via
   `scratchpad/name_audio_tool.py` (extract/label/gaps, alphabetical sanity checks,
   52/699 regular-tier streams labeled) remains the fallback.
3. Once the real binding is cracked: announcer-name fixer v2 for the 2025 roster
   (report/apply/revert; v1 scripts in scratchpad are the scaffold).
4. `+0xB1` single-variable in-game verification (still only partially verified).
5. PA announcer: silent for edited players even when PxP works — separate system.
6. Solver residuals: ~5% L-slot error; 192 alias ids unnamed; 5003 'Adam' vs 'Milan'.
7. `global.iff` split (below) and audio-preview tab in the roster editor.

## Launcher support — Speech tab + naming convention (2026-07-29, tool-side)

The user-integration launcher (NOT this repo's app/ copy — the "NHL2K10 Mod Launcher"
dev tree) now splits audio browsing into two tabs built from one reusable pane:

- **Audio tab** — every non-speech folder (music / crowd / SFX), with the category filter.
- **Speech tab** — sub-tabs **Play-by-Play** (folder `Speech_PxP`), **Color**
  (`Speech_Color`), **PA Announcer** (`PA`), **Unsorted** (`Commentary` = the
  not-yet-classified speech bucket). New categories `PxP` / `Color` were added to
  `CATEGORY_FOLDER`/`CATEGORY_LABELS`; `SPEECH_FOLDERS` defines the tab split.
  Both tabs carry the full ops bar (Extract / Check All / Reload Names / Apply
  Changes / Patch Game).

**Naming convention for name-call streams** (supersedes the ad-hoc `PxP_Name_<X>` /
`PxP_Name_Corner_<X>` names the labeling tool wrote):

```
PxP_Name_<Last|First|Full>_<Name>[_<Context>][_VarN]
e.g. PxP_Name_Last_Kovalev, PxP_Name_Last_Adams_Corner, PxP_Name_Full_Chris_Drury_Corner
```

All 60 existing 1A labels were migrated in place (names JSON + extracted WAVs moved to
`Audio/Speech_PxP/`) and exported to a **bundled seed file**
`launcher/data/speech_seed_names.json` (`{fid: {offset_hex: {stem,name,category}}}`).
`op_extract` and `op_reload_names` merge the seed into the user's
`<fid>_Audio_Names.json` (user entries always win), so a fresh extract lands the known
name calls as e.g. `Audio/Speech_PxP/PxP_Name_Last_Kovalev.wav` with no manual work.
Future labeling should write seed-convention names; regenerating the seed from a
labeled names JSON re-ships them to everyone.

Grade: tool-side change, GUI smoke-tested (pane split, filters, sort, selection,
pending-change staging); not yet exercised against a full 80k-track library or an
in-game patch round-trip.

## Speech cue tables CRACKED — the game's own stream inventory (2026-07-29)

**The commentary/speech system's cue tables have been located and dumped.** This replaces
scanner-derived segmentation as ground truth and is the missing "id → stream" backbone.

How it was found (Ghidra): string `SPEECH_PREBUILT_CACHE` → prebuilt variation-cache
object (`Function_83BA1650`, list head `0x84923988`, serialized + pointer-fixup in
`Function_83B9E8E8`) → loaded from IFF section type **`0xBB05A9C1`**
(`Iff_FindSectionByType` in `Function_83BA2998`). `Function_83FC3810` (PBP init)
registers BB05A9C1 handlers for **11 speech-DB instance ids**:
`3B8FA821, 76E32279, B5AB13F3, D56C0AE3` (hosted in **gamedata.iff**) and
`4F018C96, 673DCA33, 03510F37, 264E43A6, 96C22258, 9B11F609, 6392175F`
(hosted in **global.iff**) — confirmed by raw-scanning all 2407 TOC assets for the id
dwords.

**Cue table format** (in the decompressed containers; header found by searching
`[00000001][sample_rate][00000800]`):

```
-8: count u32 | -4: hash u32 | +0: 1 | +4: sample_rate | +8: 0x800 | +0xC: channels | +0x10: f32
+0x14: count × [rel_offset u32 BE][duration f32 BE]   (rel_offset 0x800-aligned, monotone)
```

19 tables, ~78,900 cues total — dumped to
`NHL2k10_Extracted_Files/speech_cue_tables.json`:

| container | hash | rate | count |
|---|---|---|---|
| gamedata.iff | AC367D4B | 44100 | **14227** (44.1k commentary incl. PxP name calls) |
| gamedata.iff | E9165335 / B43615B8 / BDE7C7CC / FAC016EA / 0 | 44100 | 119 / 1677 / 3242* / 3849 / 317* |
| gamedata.iff | D1E251C2 / 2B5E1725 / 0 / 0 | 48000 | 10 / 39 / 2 / 912 |
| global.iff | 973F378F / D8F0B99E / 1C81296E / 778B4A2C / 2054B23A | 48000 | 20101 / 771 / 19321 / 10303 / 6920 |
| global.iff | 3114FD37 / 96D1A124 / 0 / 0 | 48000 | 118 / 793 / 13 / 1 |

*two tables parse short (monotonicity break mid-table — likely sub-table boundary, unparsed).

**Wave-bundle pairing (VERIFIED for the big table):** the 14227-cue table's rel offsets
are relative to **1A @ 0x3FB91800** = TOC asset `0x09EAA3C0` (spilling into `0x927BE1D6`;
1A holds 7 big wave assets: 5F4304E3@0x0, 09EAA3C0@0x3FB91800, 927BE1D6@0x4D2ED000,
69CE5F1F@0x5A128800, F835B086@0x5D24E000, 66931D74@0x5FEB8000, 9B8205F9@0x60349000).
Verified by 6 ear-labeled anchors landing exactly (Hecht=cue 5486, Lapointe=5510,
Lidstrom=5515, Selanne=5576, Adams_Corner=5770, Chris_Drury_Corner=5847). Other tables'
bases not yet paired (same technique applies: match rel deltas / anchor offsets).

**Important:** the cue table's segmentation differs from our XMA packet scanner around the
44.1k name-call region — the game's cues sometimes start 1–3 packets before the scanner's
detected starts and group differently. The cue tables are authoritative; the catalog
scanner over-/under-splits 44.1k streams.

After the 14227 table sits an index layer (count repeated, a u16 array that is identity
0..2299 then increasingly skips — alias/remap suspicion) and the BB05A9C1 serialized
speech-DB objects (variation-group slots `[startCue..startCue+n)` per the prebuilt-cache
code). **This group/line layer is the remaining piece for automatic per-name labeling** —
it encodes which cue range belongs to which spoken name/line, and is where the roster
audio-id join should land. global.iff also carries 40-byte line records embedding ids
(`[hash u32][id<<16][0][7fff…]`) — unparsed.

Dead ends cleared this session (do not repeat): literal 1A offsets/sectors do NOT appear
in the XEX, Roster.ROS, or any TOC asset raw/decompressed (all-encodings sweep); the
H-run delta signature appears nowhere except the cue tables themselves; audio-bank IFF
hash directories and scene containers (FF3BEF94, 248 of them) do not reference the
name-call region (scenes embed their own waves via section 411536D5).

Grade: cue-table format + 14227-table base **verified** (6/6 anchors exact); everything
else offline-parsed, not in-game confirmed.

### Session 2026-07-29 PM — every table NAMED; index layer mapped; id join still open

**Every cue table carries its authored source `.bin` name** (UTF-16 string block right
before each table header, plus dev-path fragments `…maincodeline/out/vcsports/FUL…`):

| container | table hash | rate | count | source file |
|---|---|---|---|---|
| gamedata.iff | E9165335 | 44100 | 119 | **chants.bin** |
| gamedata.iff | B43615B8 | 44100 | 1677 | **chatter.bin** |
| gamedata.iff | BDE7C7CC | 44100 | 3242 | **palines.bin** |
| gamedata.iff | AC367D4B | 44100 | 14227 | **paplyrs.bin** ← the tier the ear-labels live in |
| gamedata.iff | FAC016EA | 44100 | 3849 | **streamedchatter.bin** |
| gamedata.iff | (0) | 44100 | 317 | **pamusic.bin** |
| gamedata.iff | D1E251C2 / 2B5E1725 / 0 / 0 | 48000 | 10 / 39 / 2 / 912 | **env_amb.bin / horns.bin / crowd-idle-loop.bin / crowd.bin** |
| global.iff | 973F378F | 48000 | 20101 | **players.bin** (TV commentary player names!) |
| global.iff | D8F0B99E / 96D1A124 | 48000 | 771 / 793 | **playercom.bin / playercom2.bin** |
| global.iff | 1C81296E / 778B4A2C / 2054B23A | 48000 | 19321 / 10303 / 6920 | **lines.bin / lines_ps.bin / lines_ts.bin** |
| global.iff | 3114FD37 | 48000 | 118 | **teams.bin** |
| global.iff | (0) / (0) | 48000 | 13 / 1 | **jukeboxmusic.bin / femusic.bin** |

Table hash ≠ crc32 of the .bin name (ascii/utf16/upper/lower/inverted all tested) —
probably a hash of the full dev path or a non-crc algorithm.

Note the labeled "PxP" name-call tier actually lives in **paplyrs.bin** (PA players);
TV play-by-play names are presumably **players.bin** (20101 cues, 48 kHz, wave base
unknown). Our 2026-07-27 tier labels are therefore PA reads, not TV PxP — re-grade later.

**Index layer after each cue table** (paplyrs case, region 0x253C04–0x262338 in
decompressed gamedata): `[end_rel][0][0][count 0x3793][0x19][0x15][0x11][0x7151][0x7177]
[0x100][0x15][0x38A9][0x54F1][0x10E26][…][1]` then a **u16 line-slot → cue map**
(identity 0..2299, then skips; every labeled anchor sits at position `cue − 2653`
exactly), followed by further u16/u8 arrays (a stride-4 "groups of 4 variations"
region among them) and 0x10-byte group records. Loader:
`SpeechDB_CreateInstanceFromSection`(0x83BA2610) + `SpeechDB_UnpackDescriptorArrays`
(0x83B9F8D8): section header `[instance_id][ptrA][ptrB][ptrC][val][cntC u8][cntB u8]
[cntA u8][flags]`, descriptor records 0xC/0x10 bytes. Play path:
`PBP_PlayInterruptionLine` → vtable resolve (**line key = soundId*10 + variation**) →
`Speech_PlayLine`(0x83BA35C0) → `Speech_PlayLineWithVariant`(0x83BA31F0) →
`Speech_QueueCuePlayback`(0x83BA1318).

**Line registry found** (global.iff ~0x1578068): 0xC-byte triples
`[line_hash][group_hash 894E81D6][db_instance_id]` — the semantic layer, but
hash-encoded with an unknown algorithm.

**Roster-id join attempts (all negative, do not repeat):**
- u16 map is NOT indexed by roster audio id (anchor positions ≠ solver ids).
- duration↔name-length correlation ≈ 0 for both `cue=id` and u16-mapped hypotheses
  (cues are full lines, so name length is a weak signal anyway).
- solver ids and id*10 do not form arrays in either container's index regions
  (adjacent "hits" at gamedata 0x269F18–0x26A258 were a dense count array, 56/1509
  membership, GT id 728 absent).
- no `lhz rD,0xB1/0xB3(rA)` in the code — RAM record layout differs from file layout,
  so the id-field reader can't be found by displacement scan.

**Two routes left to close the name join:**
1. **Live CE watch (strongest):** run a game, watch reads against the cue-table RAM copy
   / the 1A wave regions while the PA announces a KNOWN lineup — each announcement gives
   one (player → cue) sample; a dozen games of lineups would anchor the whole paplyrs
   alphabetical structure. Variant: live-poke a record's `+0xB1/+0xB3` to a solved id
   and hear/watch which cue fires.
2. **Crack the hash algorithm** of the line registry / table hashes (find the build-time
   hash by testing more algorithms — FNV, ELF, CRC variants w/ different polys/seeds —
   against the .bin-name/id pairs, or locate a runtime hasher in the XEX).

Ghidra names added this session: SpeechDB_CreateInstanceFromSection,
SpeechDB_UnpackDescriptorArrays, Speech_PlayLine, Speech_PlayLineWithVariant,
Speech_QueueCuePlayback (+ comments on SpeechPrebuiltCache_UpdateOne,
PBP_RegisterSpeechDBInstances, SpeechDB_UnpackDescriptorArrays).

### Session 2026-07-29 PM2 — LIVE CAPTURE: cue semantics ear-VERIFIED

Procmon on Xenia's host file reads works perfectly as a speech tracer (VAN/LAK, 2 min
gameplay; scripts: parse_speech_reads / fit_wave_bases / extract_cues / build_cue_labels
in session scratchpad). Results:

**Wave-base fits from live read patterns** (exact-sector, density-corrected):
`1A@0x0` (asset 5F4304E3) = **lines.bin** (11/11 bursts, 9× lift — the TV PxP generic
lines heard in-game); `66931D74@0x5FEB8000` = **crowd-idle-loop.bin** (the two
continuously looping front/rear crowd streams); `9B8205F9@0x60349000` = **crowd.bin**
(likely, 7/12 @ 25×). paplyrs base unchanged (anchor-proven).

**Nine paplyrs cues captured in-game and ear-verified from extracted WAVs:**
960 "Save by Luongo!", 4494 "Salo's got it", 5163 "Here's Brown / Brown now",
5281 "Now Greene with it", 5382 "Now Jack" (FIRST-name call), 6239 "Colaiacovo has it
in the corner", 6240 "In the corner Frolov", 6362 "In the corner <clipped>",
7036 "Frolov's got it". Ground truth now lives in
`NHL2k10_Extracted_Files/paplyrs_cue_labels.json` (30 entries incl. remapped 2026-07-27
anchors).

What this proves (grades: verified unless noted):
- **paplyrs is a bank of PHRASE TIERS** — save-calls, "X's got it" (two runs),
  "Here's X", "Now X with it", first-name calls, "in the corner X" — each tier an
  alphabetical run; full names sort by FIRST word (Adams 5770 < "Chris Drury" 5847 <
  Colaiacovo 6239 < Frolov 6240 in the corner tier).
- **Disk reads ≠ plays**: cue 6239 (Colaiacovo — in neither lineup) was a
  SPEECH_PREBUILT_CACHE refill. Popular names play from RAM with no read at all, so
  read-order can't be matched 1:1 against heard order.
- **True sample rate is 48000 Hz**, not the table header's 44100 (pitch-verified);
  the 44100 field must mean something else (source rate?).
- **Slice calibration**: clips cut ends short / catch the next phrase's start — cue
  boundaries need ~1–2 packets of end padding (or the wave base is +0x800..0x1000 vs
  the anchor-fit value). Adjacent cues can be the SAME player in different phrasings
  (5163/5164 both Brown), so old flat-tier anchor→cue assignments may be off by one
  where names were tightly packed (Hedican/Hejduk region) — recheck by ear after
  fixing padding.
- **Slot/id join still open**: slot 960 = Luongo but Luongo's solver id is 5948;
  save/got-it slots don't equal solver ids (more negatives logged above).

**Side quest registered (launcher TODO):** the .bin sub-files suggest a future mod
route — split gamedata/global/1A into their authored .bin units and investigate a VFS
override so Xenia reads loose files (cross-check findings/13 override-device dead ends
first). Tracked as a task; lower priority than finishing audio naming.

## `global.iff` — container split (open work item)

`global.iff` is the big mixed container: 43.5 MB packed in `1B`, **89,525,720 bytes**
decompressed, referencing 22,988 catalogued audio streams (18,200 in `1A`, 4,783 in `1B`)
alongside textures and scene data. Parsing/repacking it is slow enough to be a workflow
problem in its own right.

Structure found: a flat sequence of `0x100`-aligned descriptor records shaped
`[name_hash u32][0][type u32][type u32]…` — 3618 descriptors across 44 distinct type tags.
Most common: `0xFFFFFFFF` (1412), `0xFFFF0000` (1304), `0x1A200154` (144), `0x0C100171` (99),
`0x1A200152` (87), `0x28000102` (64), `0x18280186` (20, the known texture descriptor).
No rate-anchored audio records exist in it, so `bank_parser`'s rate heuristic finds nothing
and all 22,988 hits come from the loose-reference scan.

This descriptor table is the handle for splitting the file into addressable sub-assets so
individual sections can be extracted and repacked without touching the whole 89 MB.

## Session 2026-07-29 PM3 — 1A+1B is ONE logical space; ALL 18 banks located and decode-confirmed

This closes the "where does each `.bin` live" question completely, and corrects the
paplyrs base used earlier in this document.

### 1A + 1B form a single logical byte space

`0A` and `1A` are both exactly `0x6B800000` bytes (1720.00 MB) — a fixed **volume size**,
not a natural end of data. Proof that the volumes are logically continuous: the last three
packets of `1A` carry XMA packet sequence numbers 4, 5, 6 and the first three packets of
`1B` carry 7, 8, 9. A stream straddles the boundary. `1B` is not packet-aligned because it
is the real end of the data.

So a bank's base is one offset into the combined space:

```
logical_off < 0x6B800000  ->  1A at logical_off
logical_off >= 0x6B800000 ->  1B at logical_off - 0x6B800000
```

`0A`/`0B` are the multi-stream (5.1) counterpart pair of the same volume scheme.

### The confirmed layout

Bases were found by requiring a bank's cue offsets to land on authored stream starts (two
widely separated cues as anchors, set-intersected against all 80k stream starts, survivors
scored over 300 probes — `bin_layout.py`), then **confirmed by decoding audio at each base
and listening to what it says** (`bank_probe.py`). Offset arithmetic alone can be fooled by
a dense region; content cannot.

| bank | logical base | streams | MB | confirmed content |
|---|---|---|---|---|
| `lines.bin` | `0x00000000` | 18,373 | 1019.2 | generic TV play-by-play |
| `lines_ps.bin` | `0x3FB91800` | 10,045 | 215.0 | **player-specific** PxP lines |
| `lines_ts.bin` | `0x4D2ED000` | 6,655 | 206.2 | team-specific PxP lines |
| `chants.bin` | `0x5A128800` | 109 | 49.0 | crowd chants (incl. "USA! USA!") |
| `chatter.bin` | ~~`0x5D21E000`~~ `0x5D24E000` | 1,559 | 44.6 | color/bench chatter |
| `crowd-idle-loop.bin` | `0x5FEB8000` | 2 | 4.6 | the two 137 s looping crowd beds |
| `crowd.bin` | `0x60349000` | 862 | 227.8 | cheering / applause reactions |
| `env_amb.bin` | `0x6E72E800` | 10 | 8.3 | arena ambience |
| `horns.bin` | `0x6EF71000` | 36 | 4.6 | goal horns |
| `palines.bin` | `0x6F403800` | 3,126 | 94.1 | PA announcer lines |
| `pamusic.bin` | `0x75224000` | 38 | 64.6 | licensed music (with vocals) |
| ~~*(no cue table)*~~ | `0x792C0000` | 259 | 249.5 | `jukeboxmusic` / `femusic` — see below |
| `paplyrs.bin` | `0x88C43000` | 14,074 | 241.0 | **PA announcer surname reads** |
| `playercom.bin` | `0x97D82800` | 732 | 93.5 | per-player color commentary |
| `playercom2.bin` | `0x9DB08800` | 746 | 107.7 | per-player color commentary, cont. |
| `players.bin` | `0xA46C3800` | 19,701 | 217.6 | **PxP surname reads + phrase templates** |
| `streamedchatter.bin` | `0xB22A5800` | 3,700 | 87.2 | on-ice player chatter |
| `teams.bin` | `0xB79CD000` | 118 | 2.9 | team / nationality names |

**80,145 of 80,145 authored streams in the logical space are accounted for.** Nothing is
orphaned. The only region with no cue table is the 249.5 MB gap at `0x792C0000`, which by
elimination is `jukeboxmusic.bin` + `femusic.bin` — music needs no cue index.

### CORRECTION to earlier sessions

`0x3FB91800` was previously recorded as the paplyrs wave base. It is **`lines_ps.bin`**.
The nine ear-verified cues (960 Luongo, 4494 Salo, 5163 Brown, 5281 Greene, 5382 Jack,
6239 Colaiacovo, 6240/7036 Frolov) were read at that base and they *did* match, which now
identifies them correctly as **`lines_ps.bin` cues** — player-specific play-by-play lines,
which is exactly what they sound like ("Save by Luongo!", "In the corner Frolov"). The
phrase-tier structure documented for "paplyrs" above therefore belongs to `lines_ps.bin`.
Real `paplyrs.bin` is at `0x88C43000` and is a much plainer bank: bare surname reads by the
PA announcer, e.g. cue 0 decodes to *"Abbott Abbott Abbott Abbott Abbott Adams Adams Adams"*.

### The cue table `channels` field is not a channel count

Every speech table says `2`, but those streams decode correctly only as **mono**; the
tables that say `5` decode as 2. Use the stream scanner's channel byte, not the table field.
(Consistent with the already-noted fact that the table's `44100` rate field is not the true
48 kHz playback rate.)

### `teams.bin` — 100% named, deterministically

118 streams = **59 subjects x 2 takes**, in a fully known order, so every stream gets an
exact name with no phonetic guessing (`gen_teams.py`): the 30 NHL clubs alphabetical **by
city** (Anaheim..Washington), then East/West All-Stars, then 17 nationalities
(Austrians, Belarusians, Canadians, Czechs, Danes, Finns, French, Germans, Kazakhs,
Latvians, Russians, Slovakians, Swedes, Swiss, Ukrainians, Americans, Nordics), then the
retro teams (Jets, North Stars, 60s/70s/80s/90s All-Stars, Czechoslovakians, Soviets), then
Home Team / Road Team. Naming: `PxP_Team_<Subject>_Var<1|2>`.

### Why the name banks are the tractable prize

`players.bin` cue 0 decodes to *"Abbott to Abbott, over to Abbott, from Abbott, by Abbott,
Abbott!"* — one surname, a fixed set of ~6 phrase templates, strictly alphabetical.
`paplyrs.bin` is the same shape with bare reads. Together that is 33,775 streams (42% of all
audio) whose naming reduces to "which surname is this run", which the monotone
alphabetical-run DP solves far more reliably than the conversational `lines*` banks.

---

## Session 2026-07-29 PM4 — the name VOCABULARY comes from Roster.ROS (UTF-16BE pool)

The alignment DP is only as good as the list of names it is allowed to choose from. Until now
that list was `id_name_solved.json` — the ~1,509 surnames recovered from the audio-id solve.
`players.bin` reads the **whole roster**, so more than half its groups had no correct answer
available at all and fell back to the raw ASR spelling.

**Roster.ROS stores every player name as a UTF-16BE (big-endian, NUL-high-byte) string** —
which is why an ASCII search for `Luongo` finds nothing and had previously suggested the names
were not in the file:

```
0x23E928  Getzlaf      <- Last
0x23E938  Ryan         <- First
0x23E942  Carter
0x23E950  Niedermayer
0x23E968  Rob
...
0x25CDF2  Paul Henderson   <- legends section switches to "First Last" in one string
```

Layout: a packed string pool starting at **0x23E928**, alternating **Last, First** per player,
each NUL-terminated, running to **0x25CDF2**. The tail of the pool (from the legends/retro
teams) uses single `"First Last"` strings instead of pairs.

Extraction (regex over the raw file, no record walking needed):

```python
pat = re.compile(rb'(?:\x00[A-Za-z\x27 .-]){3,26}')
strings = [m.group()[1::2].decode('latin1') for m in pat.finditer(data)]
```

That yields **5,999 strings / 5,785 unique**, which reduce to **5,190 unique surnames**
(taking the last token of multi-word entries, plus the `"Van Ryn"`-style compound tail as its
own candidate). It contains every name the old vocabulary was missing — verified present:
Abbott, Barnaby, Luongo, Kiprusoff, Colaiacovo, Frolov, Greene, Khabibulin, Holmqvist.

Effect on `players.bin` (measured on the first 525 streams / 97 groups):

| vocabulary | groups resolved |
|---|---|
| `id_name_solved.json` only (1,508) | 71 / 97 = 73.2% |
| + Roster.ROS pool (5,207) | 90 / 97 = 92.8% |
| + alphabetical-window interpolation | **96 / 97 = 99.0%** |

and the resulting name sequence is **perfectly monotone alphabetically** — Abbott, Adams,
Aebischer, Afanasenkov, Afinogenov, Albelin, Alexeev, Alfredsson, Allan, Allison, Amadio,
Anderson, Andrews, Andreychuk, Antropov, Arkhipov, Armstrong, Arnason, Arnold, Arnott,
Arvedson, Asham, Aulie, Avery, Axelsson, Bacashihua, … Brodeur, Brooks, Brown — which is
independent evidence that the labels are right, since a wrong pick almost always breaks
monotonicity.

### Interpolation: a group the DP cannot place is still alphabetically bracketed

Its nearest *resolved* neighbours bound where it must sit. Restricting candidates to that
window and removing names already assigned (every group is a different player) usually leaves
a handful, so a phonetic match that was hopeless against 5,200 names becomes decisive against
five. This is what recovered the last 6 groups above (garbled ASR reads like `pav`, `coin`,
`debt`, `ola`, `booyah`). Guard rails: only interpolate when **both** neighbours are resolved
(otherwise the window is unbounded and it is a guess, not an interpolation), and skip windows
wider than 400 candidates.

### The roster vocabulary must stay OPT-IN for the tiered banks

Adding it to `lines_ps.bin` changed 2,009 stream labels and was **a wash**: it fixed
Chistov→Cassivi and Marian→Markkanen (both correct), but broke Weekes→Weeks,
Lehtinen→Luttinen and Turco→Turek. Reason: the tiered `lines*` banks only read players who
carry an **audio id**, so the extra 3,700 roster names are not candidates at all there — they
only steal matches. `align_names.py` therefore gates it behind `--roster`; `players.bin` /
`paplyrs.bin` (which read the whole roster) use it by default.

Ear-verified cue score is unchanged at 7/9 with or without it (the 2 "misses" remain the
known bad-offset cases that land one stream early).

### Scripts

`gen_players.py` (groups → names: DP + interpolation + `PxP_Name_Last_<Surname>_<Slot>`),
`players_segment.py` (Viterbi over the 6-take cycle; a group now ends whenever the cycle fails
to advance, not only at slot 0, and fragments re-merge on **fuzzy** spelling agreement),
`roster_vocab.json` (the 5,190-surname pool).

---

## Session 2026-07-29 PM5 — all 19 cue tables located by NAME, and the cue-name hash cracked

Two independent results, both obtained with no ASR (the four speech shards were saturating the
CPU at the time): the bank→cue-table binding is now exact rather than inferred, and the
non-speech banks turn out to carry **real authored cue names** that can be recovered by hash.

### 1. Every cue table is introduced by its own bank-name string

Earlier work located cue tables by intersecting their offset fields against known authored
stream starts — which works but is a guess, and it silently missed banks. The reliable anchor is
much simpler: **each bank's cue table is immediately preceded by that bank's file name stored as
a UTF-16BE string** (`"horns.bin"`, `"lines_ps.bin"`, …), usually followed by a
`streambank.000001.<n>` identifier. Scan for `*.bin` in UTF-16BE, then walk forward to the
`0x800` granularity field and back up 0x10 bytes to reach the header:

| off | type | meaning |
|---|---|---|
| +0x00 | u32 | cue count |
| +0x04 | u32 | **unidentified** — see below |
| +0x08 | u32 | always 1 |
| +0x0C | u32 | nominal rate, `0xAC44`=44100 / `0xBB80`=48000 (**not** the true playback rate) |
| +0x10 | u32 | 0x800 — offset granularity |
| +0x14 | u32 | "channels" (**not** a channel count: 2 decodes mono, 5 decodes stereo) |
| +0x18 | f32 | duration of stream 0 (duplicates `dur[0]`; purpose unknown) |
| +0x1C | … | `count ×` (u32 relative byte offset, f32 duration seconds) |
| +0x1C+8·count | u32 | **end sentinel** — one past the last stream |

*(Correction, 2026-07-29 PM8: this row previously read `+0x18: count × (f32 duration, u32
offset)`. That pairing is off by 4 bytes and gave every cue its **predecessor's** duration.
The earlier session's reading further up this doc — `[rel_offset u32][duration f32]` with an
`f32` at `+0x10` of its own header base — was right all along; PM5 regressed it. See PM8 §1
for the proof.)*

All 19 banks, 82,735 cues total (`cue_table_map.py` → `audio_cue_tables.json`):

| file | bank | header | +0x04 | cues | rate | ch |
|---|---|---|---|---|---|---|
| gamedata | chants.bin | 0x0022A840 | 0xE9165335 | 119 | 44100 | 5 |
| gamedata | chatter.bin | 0x0022AEF0 | 0xB43615B8 | 1677 | 44100 | 2 |
| gamedata | palines.bin | 0x0022F9F0 | 0xBDE7C7CC | 3242 | 44100 | 2 |
| gamedata | paplyrs.bin | 0x00237F50 | 0xAC367D4B | 14227 | 44100 | 2 |
| gamedata | env_amb.bin | 0x0025AE00 | 0xD1E251C2 | 10 | 48000 | 5 |
| gamedata | horns.bin | 0x0025AEC0 | 0x2B5E1725 | 39 | 48000 | 2 |
| gamedata | streamedchatter.bin | 0x00262330 | 0xFAC016EA | 3849 | 44100 | 2 |
| gamedata | crowd-idle-loop.bin | 0x0026C204 | 0 | 2 | 48000 | 5 |
| gamedata | crowd.bin | 0x0026C288 | 0 | 912 | 48000 | 5 |
| gamedata | pamusic.bin | 0x0026DF7C | 0 | 317 | 44100 | 5 |
| global | players.bin | 0x014EA3D0 | 0x973F378F | 20101 | 48000 | 2 |
| global | playercom.bin | 0x01511C40 | 0xD8F0B99E | 771 | 48000 | 2 |
| global | lines.bin | 0x015134D0 | 0x1C81296E | 19321 | 48000 | 2 |
| global | lines_ps.bin | 0x01540940 | 0x778B4A2C | 10303 | 48000 | 2 |
| global | lines_ts.bin | 0x0155D830 | 0x2054B23A | 6920 | 48000 | 2 |
| global | teams.bin | 0x01574FF0 | 0x3114FD37 | 118 | 48000 | 2 |
| global | playercom2.bin | 0x01575500 | 0x96D1A124 | 793 | 48000 | 2 |
| global | jukeboxmusic.bin | 0x01577B40 | 0 | 13 | 48000 | 5 |
| global | femusic.bin | 0x01579D9C | 0 | 1 | 48000 | 5 |

**This filled the last gap in the bank map.** `jukeboxmusic.bin` (13 cues) and `femusic.bin`
(1 cue) had no cue index found by the old offset-intersection method, which is why the 249.5 MB
region at logical `0x792C0000` sat unidentified. It is the jukebox / front-end music.

**About `+0x04`:** it is nonzero for exactly the 14 speech/SFX/horn banks and zero for the 5
crowd-and-music banks. It is *not* `crc32` of the bank name in any casing, not `crc32` of any of
the 604,257 ASCII/UTF-16BE strings present in either decompressed file, and not a checksum over
the record array or over header+records. Each value occurs exactly once in the whole file — only
in its own header. Treat it as unknown; the zero/nonzero split is the only usable signal, and it
correlates with "this bank's cues have authored names" (below).

### 2. Cue and event names hash as `crc32(NAME.upper())`

Scattered through `gamedata_dec.bin` / `global_dec.bin` are runs of **12-byte records
`(u32 hash, u32 a, u32 b)` with strictly ascending hashes** — a sorted hash lookup table. The
hash is plain zlib **`crc32` of the name uppercased**:

```python
h = zlib.crc32(name.upper().encode()) & 0xFFFFFFFF
```

This is the same `Str_Hash` already documented for audio bank names (findings 14), confirmed
again on a fresh, independent table.

**Verification — the 11-record table at `gamedata_dec.bin@0x22239C` resolves 11/11** against an
11-name blob found nearby, all team goal-horn/siren events, and every `b` satisfies
`b ≡ 1 (mod 4)`:

| name | b | b>>2 |
|---|---|---|
| bruins-siren | 17 | 4 |
| predators-growl | 265 | 66 |
| ducks-goal-siren | 429 | 107 |
| hurricanes-goal | 605 | 151 |
| hurricanes-thunder-faceoff | 865 | 216 |
| kings-bells | 1065 | 266 |
| panthers-growl | 1229 | 307 |
| predators-growl2 | 1321 | 330 |
| blue-jackets-canon | 1593 | 398 |
| coyotes-growl | 1721 | 430 |
| cheechoo-train | 1945 | 486 |

Note `predators-growl2` — the blob spells it `predators-growl_02`, and only the `growl2` form
hashes correctly. So the hashed string is the **event** name, which can differ from the asset
string sitting next to it. Expect to try spelling variants when resolving by hash.

`b ≡ 1 (mod 4)` holds for **every record in every one of these tables** (11/11, 87/87, 48/48), so
the field is `b = soundId*4 + 1` and `b>>2` is a **sound-event id** — the same shape as the
`soundId*10 + variation` line key already documented for speech.

I first guessed `b>>2` was a `crowd.bin` cue index (912 cues; the 11 siren indices 4…486 are all
in range, with plausible 5.1–23.4 s durations). **That guess is wrong.** The 87-record SFX table
uses the same encoding and reaches `b>>2 = 5982`, far past any single bank's cue count, so the id
space is global, not per-bank. The horn→stream binding remains unsolved.

### 3. 128 SFX cue names recovered verbatim

`gamedata_dec.bin` `0x2211B4`–`0x222368` holds a dense blob of 128 SFX event names — the first
human-readable inventory of the game's sound effects:

- **skating** `skate-left`, `skate-sweet`, `skate-right`, `skate-scrape-stop`, `skate-carve-loop`
- **puck/stick** `pass-recieve` *(sic)*, `slap-shot`, `snap-shot`, `wrist-shot`,
  `stick-hit-stick`, `stick-hit-boards`, `stick-on-ice`, `poke-check`, `gm-puck-handling-l`,
  `goalie-stick-hit-ice`, `goalie-stick-end-powerplay`
- **puck impacts** `puck-goalie-blocker` / `-glove` / `-helmet` / `-pads` / `-stick` / `-skate` /
  `-body`, `puck-dropped`, `puck-glass`, `puck-post`, `puck-hit-net`, `puck-bottom-bar`,
  `pond-puck-hit-snow`
- **contact** `grunt-check`, `glass-bang`, `check-boards`, `check-body`, `check-med`,
  `check-soft`, `punch`, `punch2`, `punch-soft`, `punch-swish`
- **crowd** `cheer-small`, `cheer-large`, `cheer-lrg-short-front` / `-rear`, `ohh-large-front` /
  `-rear`, `ohh-small-front` / `-rear`, `rampup-def` / `-off` / `-off-small` × `-front` / `-rear`,
  `crowd-boo-front` / `-rear`, `crowd-cheer-med-front` / `-rear`, `cr-suspense-1` / `-2` / `-3`
- **zamboni** `zamboni-idle`, `-drive-auger`, `-hit-wall`, `-horn1`, `-horn2`, `-revup`,
  `-slowdown`, `-spinout`, `-scrape-wall`, `-hit-zambonii` *(sic)*
- **misc** `ref-whistle`, `clock-tick-beep`, `door-open`, `door-close`, `player-climb-wall`,
  `player-climb-wall-out`, `wii-count-down` ×4

The 128-name blob does **not** hash into the 87-record table below — see §4 for why: the hashed
string is the `"Player: Skate-Left"` display form, not this lowercase asset form. These two blobs
are the same event set in two spellings, so the lowercase list is still the better guide to what
each sound *is*.

### 4. SOLVED — the event names live in `default.xex` as `"Category: Name"` strings

Two more hash tables sit at `gamedata_dec.bin@0x21AF9C` (87 records) and
`global_dec.bin@0x147B1EC` (48 records). Neither table's hashes match **any** string in either
decompressed audio archive — because the names are not there. They are display strings in
**`default_flat.xex`**, in `"Category: Name"` form:

```
Player: Skate-Left        Puck: Goalie-Glove       Crowd: Ohh-Badcall
Ambient: Ref-Whistle      Ambient: Menu Select     Overlay: Instant Replay Controls
```

**The `"Category: "` prefix is part of the hashed string.** That is precisely why the lowercase
`skate-left` blob inside `gamedata` (§3) never hashed into this table — same event, different
string. Hashing `crc32("PLAYER: SKATE-LEFT")` matches; `crc32("SKATE-LEFT")` does not.

`resolve_event_names.py` finds all three tables by scanning for maximal runs of ≥8 ascending-hash
12-byte records, builds a candidate vocabulary from every ASCII + UTF-16BE string in the xex and
both archives (949k candidates, with `_`→`-` and `_02`→`2` variants), and resolves:

| table | records | named |
|---|---|---|
| `gamedata@0x0021AF9C` — gameplay SFX | 87 | **80 (92%)** |
| `gamedata@0x0022239C` — team goal sirens | 11 | **11 (100%)** |
| `global@0x0147B1EC` — menu / replay / overlay UI | 48 | 17 (35%) |
| **total** | **146** | **108 (74%)** |

Record layout, now that the names confirm it: `(u32 crc32(NAME.upper()), u32 variation_count,
u32 soundId*4+1)`. The `a` field being a variation count fits the content — `Player: Check-Body`
has `a=4`, `Crowd: Ohh-Badcall` `a=4`, most one-shots `a=1`.

Ten further names were recovered by generating candidates in the observed
`"Prefix: Token-Token"` grammar and hashing them (`event_names_extra.json`): `Puck: Post`,
`Puck: Hit-Snow`, `Player: Zamboni-Idle`, `Player: Zamboni-Drive`, `Player: Zamboni-Horn1`,
`Player: Zamboni-Horn2`, `Player: Stick-Slide`, `Crowd: Suspense-Low` / `-Med` / `-Large`. Each
lands at the correct position in the `soundId` ordering — `Zamboni-Idle`/`-Drive` fall exactly
between `Puck: Hit-Board-Soft` and `Player: Zamboni-Hit-Wall`, the three `Suspense-*` exactly
between `Crowd: Cheer-Med` and `Player: Dribble Puck` — which is what makes them believable rather
than crc32 coincidences.

One brute-force hit was **discarded**: `"Player: Close Forward Right"` for id 2421 in the menu
table. The preimage is real but the name is semantically absurd for a slot between
`Ambient: Menu Inc Item` and `Ambient: Menu Sub Menu`; a ~7M-candidate search over a 32-bit hash
will throw the occasional false positive, and unverifiable names are worse than none.

The 38 still-unnamed records are 7 gameplay SFX and 31 menu/replay UI sounds whose display
strings are absent from the image (a UI-vocabulary brute force over ~7M candidates found nothing
credible). They are fully usable regardless — the `soundId` ordering places each one between two
known neighbours.

**Full resolved list:** `event_name_tables.json`.

### 5. Stream-bank identifiers seen next to the tables

`streambank.000001.1164` (horns), `.1672` (env_amb), `.3716` (pamusic), `.324` (femusic),
`.2496` (crowd-idle-loop), `.3968` (jukeboxmusic). Useful if the `.bin` split-out task ever needs
to reconstruct the loader's expected file identity.

### Status: horns.bin still unnamed by automation

39 cues / 36 streams, no speech to transcribe, and no 30+6 duration grouping to exploit. The
named-siren table in §2 is the most promising route, but the cue→bank binding is unproven, so
horns stay positionally labelled for now.

### Scripts

`cue_table_map.py` (bank-name → cue table, all 19; writes `audio_cue_tables.json`),
`resolve_event_names.py` (finds the hash tables, resolves them against xex + archive strings;
writes `event_name_tables.json`), `event_names_extra.json` (the 10 grammar-recovered names),
`xex_event_names.json` (the 105 `"Category: Name"` strings in the xex),
`audio_name_strings.json` (53 gamedata + 55 global audio-related name strings).

---

## Session 2026-07-29 PM6 — STREAM-START MARKER found: 15/19 bank bases proven, and the mis-cut clips explained

This is the most consequential result so far, and it **corrects a previously documented
conclusion**. Read §3 before trusting `paplyrs_cue_labels.json`.

### 1. A stream's first packet is self-identifying: `0x08000000`

Measured against the one binding already known to be true (`lines.bin` → `1A@0x0`): the four
bytes at **every** cue offset are exactly `0x08000000` — an XMA2 packet header with
`frame_count = 2` and `frame_offset_in_bits`, `metadata`, `skip_count` all zero — in **3000/3000**
cases. A packet `0x800` further in reads `frame_count = 6` with a scattered frame offset:

| position | frame_count | frame_offset | metadata |
|---|---|---|---|
| at a cue offset (stream start) | 2 (3000/3000) | 0 (3000/3000) | 0 (3000/3000) |
| cue offset + 0x800 (mid-stream) | 6 (3000/3000) | scattered (44, 38, 8, 23, 32, 34 …) | scattered |

Only **5.0%** of `0x800` slots in the whole 3,208 MB logical space carry the marker, so requiring
all N probe offsets to hit it is enormously selective — it leaves exactly one base.

**Why the earlier fitting attempts were inconclusive.** I first tried a weaker test ("is this
packet-shaped at all"). It validated beautifully on the known case (60/60 vs ~40% chance) but
~50% of all slots are packet-shaped, so long runs of dense XMA meant thousands of bases scored
100% — 313 candidates for `crowd.bin`, 24 for `pamusic.bin`. The marker fixes that. Also note the
first sweep searched each container **separately**, which is wrong: 1A+1B is one logical space, so
a bank whose span runs off the end of 1A had every later offset silently fail (`crowd.bin` scored
68% at its true base for exactly this reason). Always sweep the concatenated pair.

### 2. 15 of 19 bank bases, every single cue verified

`fit_start_marker.py`, logical space `1A+1B` (`VOL = 0x6B800000`; `logical < VOL` → `1A` at
`logical`, else `1B` at `logical − VOL`). "cues verified" is *all* cues, not a sample:

| bank | logical base | cues verified |
|---|---|---|
| lines.bin | 0x00000000 | 19321 / 19321 |
| lines_ps.bin | **0x3FB91800** | 10303 / 10303 |
| lines_ts.bin | 0x4D2ED000 | 6920 / 6920 |
| chants.bin | 0x5A128800 | 119 / 119 |
| chatter.bin | 0x5D24E000 | 1677 / 1677 |
| crowd-idle-loop.bin | 0x5FEB8000 | 2 / 2 |
| crowd.bin | 0x60349000 | 912 / 912 |
| env_amb.bin | 0x6E72E800 | 10 / 10 |
| palines.bin | 0x6F403800 | 3237 / 3242 |
| paplyrs.bin | **0x88C43000** | 14227 / 14227 |
| playercom.bin | 0x97D82800 | 771 / 771 |
| playercom2.bin | 0x9DB08800 | 793 / 793 |
| players.bin | 0xA46C3800 | 20101 / 20101 |
| streamedchatter.bin | 0xB22A5800 | 3849 / 3849 |
| teams.bin | 0xB79CD000 | 118 / 118 |

`crowd.bin @ 0x60349000` was only "likely (7/12)" from live capture — **now proven** at 912/912.

Not fitted: `horns.bin` (best 19/39), `pamusic.bin` (52/60), `jukeboxmusic.bin` (9/13) — no base
puts all their cues on stream starts, so in these banks a cue is evidently *not* a whole stream.
`femusic.bin` has 1 cue, which the marker cannot discriminate. `crowd-idle-loop.bin`'s 2 cues are
also too few to be unique on their own; its base is carried over from live capture and is
consistent with the marker.

### 3. CORRECTION: `0x3FB91800` is **lines_ps.bin**, not paplyrs.bin

Earlier work fixed the 14,227-cue `paplyrs` table to base `0x3FB91800` on the strength of 6
ear-labelled anchors, and `extract_cues.py` has been reading that pair ever since. The marker fit
says otherwise, decisively:

- `lines_ps.bin` at `0x3FB91800` → **10303/10303** cues on a stream start.
- `paplyrs.bin` at `0x3FB91800` → only **12/200** probe offsets on a stream start.
- `paplyrs.bin` at `0x88C43000` → **14227/14227**.

The two tables genuinely overlap — many offsets are byte-identical — which is why the anchors
seemed to work. But where they differ, the paplyrs offset sits *past* a lines_ps stream start and
its duration is longer:

| paplyrs cue | offset | dur | nearest lines_ps cue | offset | dur | offset delta |
|---|---|---|---|---|---|---|
| 960 | 0x011FC800 | 2.62 | 657 | 0x011FC800 | 2.48 | 0 |
| 4494 | 0x0528B800 | 2.73 | 4191 | 0x0528B800 | 0.94 | 0 |
| 5281 | 0x0611E800 | 1.12 | 5230 | 0x0611D000 | 0.94 | **0x1800** |
| 5382 | 0x06304000 | 2.63 | 5365 | 0x06301800 | 1.19 | **0x2800** |
| 6362 | 0x074A3800 | 2.69 | 6691 | 0x074A0000 | 1.84 | **0x3800** |
| 7036 | 0x080A1000 | 2.75 | 7260 | 0x0809C800 | 2.02 | **0x4800** |

**This is the whole explanation for the user's complaint** that clips "start or end midway
through" with "multiple splices per clip". A non-zero delta starts the clip mid-phrase; an
over-long duration runs it into the next take.

The content settles it too. These are **play-by-play** lines, and the user reported hearing them
during gameplay while explicitly noting *"There is no pre-game lineup announcements"* — so they
were never PA (`paplyrs`) reads at all. `lines_ps` = the PxP player-specific line bank.

**Therefore `paplyrs_cue_labels.json` (30 entries) is mislabelled**: its cue indices belong to
`lines_ps.bin`, not `paplyrs.bin`. Do not use it as paplyrs ground truth. The nine re-derived
`lines_ps` indices in the table above are the correct replacements.

### 4. Extracting at the right base+table fixes the clips — verified

`extract_bank_cues.py` pairs each bank with its own cue table and its own fitted base, and ends
each clip at the **next stream start** rather than at `offset + duration`. Re-extracting the same
nine takes as `lines_ps` cues (ASR, 48 kHz):

| lines_ps cue | before (paplyrs table @ 0x3FB91800) | after (lines_ps table @ 0x3FB91800) |
|---|---|---|
| 657 | "Save by Luon" *(clipped)* | **"Saved by Luongo!"** |
| 4191 | "Salo's got it" + "And here's" *(splice)* | **"Salos got it"** |
| 5072 | "Brown now" + "Here's Brown" *(splice)* | **"Brown now."** |
| 5230 | "Now Greene with it" | **"And Josh Grattan with it now."** |
| 5365 | "Now Jack" | **"And here's Johnson, now."** |
| 6570 | "Coliacavo has it in the corner" + "Now" | **"Kolyakovo has it in the corner"** |
| 6571 | "In the corner Frolov" | **"Now in the corner, Frolov."** |
| 6691 | "In the corner" *(clipped)* | **"In the corner, Gleason's got it."** |
| 7260 | "Frolovs got" *(clipped)* | "Along the boards, Koliakovo…" |

Every clip is now one complete take. Two extraction rules matter:

- **`--pad 0`.** Cutting exactly at the next stream start *is* the take boundary. Padding by even
  one packet spills the next take's first word in — `"Saved by Luongo!"` became
  `"Saved by Luongo and"`. This reverses the earlier "add 1–2 packets of end padding" advice,
  which was compensating for the wrong base.
- **`--rate 48000`.** Confirmed again; the cue header's 44100 plays back flat.

### Scripts

`fit_start_marker.py` (marker sweep over a container pair → unique bases),
`bank_bases.json` (the 15 verified bases + the 4 unfitted, with evidence),
`extract_bank_cues.py` (replaces `extract_cues.py`; pairs bank ↔ own table ↔ own base, cuts on
stream starts, `--pad 0` default), `fit_logical_base.py` / `fit_bank_base.py` (the weaker
packet-shape sweeps, kept to document why they were not selective enough).

---

## Session 2026-07-29 PM7 — the stream-start test generalised: 18 of 19 bank bases pinned

This **corrects PM6 in two places** and closes out the bank map. Read §1 before using
`fit_start_marker.py` or `bank_bases.json`.

### 1. CORRECTION: the XMA2 packet-header field widths, and "the marker is 0x08000000"

PM6 read the packet header with the wrong field widths. The real layout is:

| field | width | extract |
|---|---|---|
| frame_count | 6 | `(h >> 26) & 0x3F` |
| frame_offset_in_bits | **15** | `(h >> 11) & 0x7FFF` — PM6 used 11 bits at `>> 15` |
| metadata | 3 | `(h >> 8) & 0x07` — PM6 used `>> 12` |
| skip_count | 8 | `h & 0xFF` |

Read correctly, PM6's "mid-stream packets have frame_count=6 and a scattered frame offset" is
just a packet whose first frame begins partway in — which is the entire purpose of the field.

Consequently `0x08000000` is **not** the stream-start marker; it is the special case
*frame_count=2, metadata=0*, which happens to dominate the low-bitrate speech banks. What
actually identifies a stream start is that a frame begins exactly at the top of the packet and
nothing is skipped:

```
frame_offset_in_bits == 0  and  skip_count == 0        (frame_count and metadata both vary)
i.e.  (word & 0x03FFF8FF) == 0    -- (0x7FFF << 11) | 0xFF
```

`horns.bin` is the proof. At its base only **9 of 39** cues carry `0x08000000`, but **36 of 39**
satisfy the general test — and the region holds exactly 36 authored streams. There `frame_count`
runs 16…56 and `metadata` is 1, both of which the narrow marker rejected. The general test still
needs 23 specific bits zero, so it stays selective: 5.406% of `0x800` slots in 1A+1B, 0.366% in
0A+0B.

### 2. All 15 PM6 bases re-confirmed, and 3 more banks resolved

`fit_stream_start.py` over 1A+1B. Every PM6 base came back **identical**, so the correction is
backward-compatible — plus two upgrades and one new fit:

| bank | logical base | cues on a stream start | change vs PM6 |
|---|---|---|---|
| lines.bin | 0x00000000 | 19321 / 19321 | — |
| lines_ps.bin | 0x3FB91800 | 10303 / 10303 | — |
| lines_ts.bin | 0x4D2ED000 | 6920 / 6920 | — |
| chants.bin | 0x5A128800 | 119 / 119 | — |
| chatter.bin | 0x5D24E000 | 1677 / 1677 | — |
| crowd-idle-loop.bin | 0x5FEB8000 | 2 / 2 | now confirmed by exclusion, see §3 |
| crowd.bin | 0x60349000 | 912 / 912 | — |
| env_amb.bin | 0x6E72E800 | 10 / 10 | — |
| **horns.bin** | **0x6EF71000** | **36 / 39** | **newly resolved** (3 interior cues) |
| **palines.bin** | 0x6F403800 | **3242 / 3242** | **was 3237/3242 — now exact** |
| **pamusic.bin** | **0x75224000** | **317 / 317** | **newly resolved** (was 52/60) |
| paplyrs.bin | 0x88C43000 | 14227 / 14227 | — |
| playercom.bin | 0x97D82800 | 771 / 771 | — |
| playercom2.bin | 0x9DB08800 | 793 / 793 | — |
| players.bin | 0xA46C3800 | 20101 / 20101 | — |
| streamedchatter.bin | 0xB22A5800 | 3849 / 3849 | — |
| teams.bin | 0xB79CD000 | 118 / 118 | — |

`horns.bin`'s 3 misses are cues pointing *inside* a shared stream, the same "a cue is not always a
take boundary" behaviour already documented for the tiered speech banks. **When a cue is interior,
end the clip at `offset + duration`, not at the next stream start** — `extract_bank_cues.py`'s
next-start rule over-runs those (cue 33, duration 3.31 s, decoded 9.1 s).

`horns.bin @ 0x6EF71000` is also **content-confirmed**: cues 0/2/19/28/33/38 all decode to real
non-clipping audio, 9–16 s of sustained tone plus crowd — goal horns. (`audio_shape.py` scores
them `loud_frac` 0.58–0.98, `clip_frac` ≈ 0.)

### 3. The 16 exact bases tile 1A+1B, which resolves crowd-idle-loop and relocates the music

Sorting the 16 proven spans leaves only one hole big enough to hold a bank:
`0x5FEB2800 .. 0x60349000`, **4.6 MB** — exactly `crowd-idle-loop.bin`'s size and containing its
live-capture base `0x5FEB8000`. Its 2 cues can never be unique on their own, but by exclusion the
base is right. Every other gap is ≤ 0.3 MB (inter-bank padding).

Two corrections follow from the same tiling:

- **PM5 said `jukeboxmusic`/`femusic` fill "the previously unidentified 249.5 MB at logical
  0x792C0000". They do not.** `0x792C0000` falls *inside* `pamusic.bin`'s now-proven span
  (`0x75224000` … `0x88BF5800`, 329 MB). That region is simply pamusic, and there is no
  unidentified music gap in 1A/1B at all.
- With 1A+1B fully accounted for, there is **no room left** for jukeboxmusic or femusic there.

### 4. jukeboxmusic.bin lives in the 5.1 pair 0A/0B — the first bank found outside 1A/1B

`0A` is `0x6B800000` bytes, exactly like `1A`, so `0A/0B` is the same logical pairing with the
same `VOL`. Fitting all 19 banks against it:

**`jukeboxmusic.bin` → 0A/0B logical `0xAFEC9000`, 13/13 cues, EXACT.** At 0.366% marker density
that is decisive. Every other bank scores noise there (best: `chatter` 132/1677), confirming the
split is **per bank**, not per stream.

Content-confirmed: cues 0/1/2/5/12 decode to 13 full-length tracks, `loud_frac` 0.98, `rms`
0.18–0.41, no clipping — continuous music, as a jukebox bank should be. Note the durations come
back short (cue 0: 252.5 s decoded vs 265.5 s in the table) because these are 5.1 and
`make_riff_xma2` only writes 1/2-channel headers; that remains an open TODO.

### 5. femusic.bin is the one bank still unpinned — and why it cannot be pinned this way

`femusic.bin` has **1 cue, offset 0, duration 0.91 s** (a front-end stinger, not a track). One cue
with zero span scores 1/1 at *every* stream start — 88,791 candidate bases in 1A+1B, 5,387 in
0A/0B. Fitting is structurally incapable of resolving it; this is a limit of the method, not a
missing measurement. What is known: it must be in 0A/0B, since 1A+1B is fully tiled (§3).

### 6. The bank descriptor decoded — and `streambank.000001.<N>` is *not* the wave base

Each cue table is preceded by a descriptor. Read as raw bytes (a string regex loses the first
character on odd alignment) it is:

```
<bank name>            UTF-16BE, even offset, 00 00 terminated   e.g. "horns.bin"
"streambank.000001.<N>" UTF-16LE, starts on the ODD byte         e.g. "streambank.000001.1164"
<cue table header>     0x18 bytes, then count x (f32 dur, u32 off)
```

Both endiannesses genuinely coexist in the same descriptor. The `<N>` values are horns 1164,
env_amb 1672, crowd-idle-loop 2496, pamusic 3716, jukeboxmusic 3968. **`<N>` is not the wave
offset in any unit** — it is not monotonic in base order (env_amb's 1672 sits *below* horns' 1164
while its base is *higher*), and `N × 0x800` / `N × 0x1000` match nothing. Treat it as a
build-time asset index. So the game's own base lookup is still unlocated; all 18 bases here are
fitted from wave data, not read from a table.

### 7. Open lead: a global sound-id → (bank, cue) array

The event hash tables give `(crc32(NAME.upper()), variation_count, soundId*4+1)`. Recovering
`soundId = (id-1)/4` gives **4…486 for the 11 team sirens** and up to **5408 for the 128 SFX
events** — a *global* id space, far past any single bank's cue count (horns has 39). Something
must map `soundId → (bank, cue)`; decoding it would join every event name to a concrete stream in
every bank at once.

Searched `gamedata_dec.bin` 0x222400–0x22A840 for an array whose entries at the 11 known siren
indices all carry a plausible bank id and in-range cue index. **Not found.** The candidate hits
were all inside one dense uniform 12-byte-record region (0x225F2C onward) at consecutively
shifting bases — the signature of a coincidence, not an anchor. Recorded as unresolved; the
constraint for the next attempt is that the array needs ≥ 5409 entries.

Also settled: the only team-horn names anywhere in the archives are the **11 special sirens** at
`gamedata_dec.bin` 0x222C38–0x222DBC (bruins-siren, predators-growl, ducks-goal-siren,
hurricanes-goal, hurricanes-thunder-faceoff, kings-bells, panthers-growl, predators-growl_02,
blue-jackets-canon, coyotes-growl, cheechoo-train). There is **no** set of 30-odd generic
per-arena goal-horn names to be found, so horns.bin's 39 cues cannot be fully named from strings.

### 8. Gotcha: `BatchedInferencePipeline` and this ASR technique are incompatible

Relaunching the paplyrs shards batched failed every single batch with *"No clip timestamps found.
Set 'vad_filter' to True"*. `BatchedInferencePipeline` requires VAD — and VAD strips the silence
inserted between concatenated streams, which is the only thing that maps words back to a stream.
The two are mutually exclusive. `bank_asr.py` now defaults to `--batch 0` and hard-exits on the
broken combination rather than emitting 4,691 empty rows.

### Scripts

`fit_stream_start.py` (the general test; supersedes `fit_start_marker.py`),
`bank_bases2.json` / `bank_bases_0a.json` (the 1A/1B and 0A/0B fits),
`fit_partial.py` (top-N bases + which cues miss, for banks with interior cues),
`fit_music.py` (tie-break by unclaimed space in the tiling),
`peek_horns.py` (per-cue packet-header fields), `dump_descriptor.py` (raw descriptor bytes),
`bank_descriptors.py` (descriptor strings for all 19), `audio_shape.py` (music/speech/garbage
verdict from rms + loud_frac + clip_frac), `find_horn_names.py`, `find_sound_defs.py`.

---

## Session 2026-07-29 PM8 — CORRECTION: the cue record layout is offset-first, not duration-first

Every clip this pipeline has ever cut to a cue duration was cut to the **wrong** duration — the
one belonging to the *previous* cue. The offsets were always right; only the pairing was wrong.
This is a small edit with a wide blast radius, because "cut to `offset + duration`" is the rule
behind the long-running complaint that clips "start or end midway through".

### 1. The proof

The anomaly surfaced while extracting `horns.bin`: for nine consecutive cues the decoded length
equalled `duration[i+1]`, never `duration[i]`. A field-pairing shift was the obvious suspect, and
a raw dump of the table settled it — but not in the direction expected. `env_amb.bin` stores
**eleven** `(float, u32)` pairs for a `count` of **ten**, so there is one extra 4-byte field at
each end of the record array. Re-reading it offset-first absorbs both extras:

```
+0x18  f32   duration of stream 0        (duplicates dur[0]; purpose still unknown)
+0x1C  count x { u32 rel_offset; f32 duration }
       u32   end_offset                  one past the last stream
```

`validate_cue_layout.py` checks four independent predictions of this structure against **all 19
tables — 19/19 pass, 0 fail**:

1. `off[0] == 0` (every bank's first stream sits at its own origin);
2. offsets strictly increase and stay `0x800`-aligned;
3. the trailing `u32` is a valid end sentinel (aligned, `> off[count-1]`);
4. the leading `f32` at `+0x18` equals `dur[0]`.

Plus the quantitative check that matters: with the sentinel closing the last span, every cue has
a real byte span, and `span / duration` is a near-constant bitrate **within** each bank
(`p95/p05` = 1.06–1.64 across the 19). Under the old pairing the same ratio scattered wildly.
`cue_pairing.py` scores the two alignments directly and picks offset-first in **10/10** banks
tested, by 2–6x in mean relative error.

An independent cross-check fell out of it: `lines.bin`'s end sentinel is `0x3FB91800` — exactly
the separately-verified 1A base of `lines_ps.bin`. The banks abut, which confirms the sentinel
means what it says.

### 2. What it fixes, measured

Re-extracting the nine ear-verified `lines_ps` cues, every one now decodes to **exactly** its
stated duration (1.44 / 1.52 / 1.13 / 1.01 / 0.82 / 1.53 / 1.80 / 2.18 / 1.82 s — 9/9 exact).
`horns.bin` cues 28–38 likewise went from "off by one neighbour" to 11/11 exact.

The per-cue error was not uniform, which is why it hid for so long — it is whatever the
neighbouring take happened to be:

| lines_ps cue | old dur | corrected | error |
|---|---|---|---|
| 960 | 1.73 | 1.44 | −0.29s (−17%) |
| 5163 | 0.84 | 1.13 | +0.29s (+34%) |
| 6239 | 1.89 | 1.53 | −0.35s (−19%) |
| 6362 | 1.71 | 2.18 | +0.48s (+28%) |

Mean error over the nine is only +0.03s (+2%) — the errors cancel in aggregate, so no summary
statistic would ever have flagged this. On 1–2s takes a ±0.3s error is the difference between
"Saved by Luongo!" and "Save by Luon".

### 3. Blast radius — what is and is not affected

- **The Mod Launcher is NOT affected.** It never parses cue tables: every duration it shows or
  stores comes from actually decoding the stream (`wav_info` / `decode_xma2`). No shipped
  behaviour changes, and no rebuild is needed for this fix.
- **Bank base fits are NOT affected** (`bank_bases2.json`, `bank_bases_0a.json`) — they fit on
  offsets, which are identical under both readings.
- **The ASR naming pipeline is NOT affected** — it decodes whole scanned streams, not
  cue-trimmed spans. `teams.bin`'s 118 names and the `players.bin` groups stand.
- **`extract_bank_cues.py` IS fixed** (offset-first parse + the end sentinel now bounds the final
  cue, which previously had no following start to stop at).

### 4. Residual, unexplained

Five of the 39 `horns.bin` cues (0, 1, 2, 27, 33) still decode **shorter** than their stated
duration — 20.54s claimed vs 13.20s of bytes for cue 0 — with no constant ratio between them, so
it is not a channel-count artifact. The other 34 are exact. Most likely these long siren/ambience
streams are authored at a lower bitrate than the bank average or the duration covers a loop
repeat. Low value to chase; noted so it is not mistaken for a fresh layout bug.

Grade: layout **verified** offline against all 19 tables by four structural predictions plus
exact duration↔span agreement; no in-game test is applicable, since this only governs how *we*
cut research clips.

### Scripts

`cue_pairing.py` (scores self vs shifted alignment per bank), `validate_cue_layout.py` (the four
structural predictions over all 19 tables), `cue_dur_diff.py` (old vs corrected duration per
cue), `extract_bank_cues.py` (fixed parse).

---

## Session 2026-07-29 PM9 — the post-table region: per-bank ID/group arrays, and what is *not* there

With the record layout corrected, the region between the end of a cue table and the start of the
next bank's descriptor could finally be read from the right address. That region was the last
unread structure in the `BB05A9C1` system and the standing hope for *real* cue names instead of
ASR transcription. It is now mapped. The short version: it is a genuine group/ID layer, it is
**not** a name source, and knowing that redirects the remaining naming work back to ASR.

### Container layout, corrected

Banks are laid out back to back, and each bank's **name string precedes its header**, not follows
its table. `env_amb.bin`'s "trailing" 80 bytes are `horns.bin`'s preamble; `chants.bin`'s tail
holds `chatter.bin` plus a `/maincodeline/out/vcsports/...` build path. Strings are one character
per big-endian u16. This corrects an earlier assumption that trailing bytes belonged to the bank
they followed.

Full per-bank trailing sizes (`post_layer.py --list`) — note these are gaps to the next header, so
they include the next bank's preamble:

| container | large trailing regions |
|---|---|
| `gamedata_dec.bin` | pamusic 8626196 (rest of file), horns 29464, paplyrs 29176, streamedchatter 9836, palines 8176, chatter 5752 |
| `global_dec.bin` | femusic 67006484 (rest of file), lines_ts 40800, lines_ps 36056, lines 30856, jukeboxmusic 8660, playercom2 3416 |

### Exactly ten banks carry a post-table header

The header's first u32 repeats the bank's own cue count, which is what ties the region to the bank.
It does so for **ten** banks and no others:

`chants` 119, `chatter` 1677, `palines` 3242, `paplyrs` 14227, `horns` 39, `streamedchatter` 3849,
`lines` 19321, `lines_ps` 10303, `lines_ts` 6920, `playercom2` 793.

The other nine (`env_amb`, `crowd-idle-loop`, `crowd`, `pamusic`, `players`, `playercom`, `teams`,
`jukeboxmusic`, `femusic`) have only zero padding followed by the next bank's name — no post-table
structure at all. **`players.bin` is the notable one**: 20,101 cues but only 1,064 trailing bytes,
which cannot hold a per-cue array of any width. So the name-call bank has no group layer here.

Ten is the same set size that `cue_pairing.py` scored, which is a coincidence of which banks are
big enough to test, not a relationship.

### The arrays are banded ID lists, shorter than the cue count

Header shape (u32, big-endian), from `chants` / `palines` / `horns`:

```
+0x00  u32  cue count (repeat)
+0x04  u32  25          constant tag on all ten
+0x08  u32  varies      437 / 253 / 153
+0x0C  u32  varies      553 / 477 / 197
+0x10  u32  varies      601 / 1733 / 193
+0x14  u32  varies      601 / 5309 / 189
+0x18  u32  packed byte quad  0x03020100 / 0x02030000 / 0x01010000
+0x1C  u32  49 / 33 / 17     second tag, always 17/21/25/33/49
+0x20  u32  227 / 129 / 71
+0x24  u16  array length, then u16 1     <-- 40 / 49 / 29
```

`+0x24`'s high half is the length of the ID array that follows, confirmed against all three:
`horns` 29, `chants` 40, `palines` 49.

The arrays hold **fewer entries than the bank has cues**, and the values are banded, not sequential:

```
horns    (39 cues, 29 IDs)  0 2 3 4 5 6 7 10 11 ... 31
chants   (119 cues, 40 IDs) 0 1 2 3 4 6 7 8 9 12 ... 28  2001 2002 ... 2029
palines  (3242 cues, 49)    3000 3001 3025 3073 3202 3995 3996 ... 4091
lines_ps (10303 cues)       0 7000 7001 7010 7100 7102 7110 7111 7120 ...
lines    (19321 cues)       0 40 41 1530 1531 1532 ... 21571
lines_ts (6920 cues)        1 9 13 33 35 39 41 1930 1939 1975 1996
```

Fewer IDs than cues, with per-bank numeric bands, is what an **event ID → interchangeable-take
group** table looks like: 39 horn cues covering 29 distinct horn events, 119 chant cues covering 40
chant events. `chants` carries a second descriptor+array pair after the first (51 more IDs,
10…2301), so a region holds a *sequence* of descriptor/array pairs rather than one.

`paplyrs.bin` is the exception that matters for naming: its array runs 0,1,2,3,… — the **identity**
permutation over all 14,227 cues, filling its whole 29 KB region. So PA player-name cues sit in
canonical order and carry no remap. That is a useful negative: cue index *is* the ordinal.

This is the most likely home of the global soundId space that task #8 has been hunting, and it is
per-bank rather than one global array, which is why offline search for a single global table came up
empty.

### There are no names in it — corrected expectation

I expected the ~29 KB blocks after `horns` and `paplyrs` to be the group layer proper, and expected
a string table. Both expectations were wrong in part:

* `paplyrs`'s 29 KB is almost entirely its own 14,227-entry identity array. Nothing else.
* `horns`'s 39-entry array uses 78 bytes; the remaining ~29 KB is **unrelated gamedata**, not audio.
  A string scan of the whole block returns exactly three hits: `'CAMH'`, and the next bank's
  `streamedchatter.bin` / `vcsports/FUL` preamble. The ~27 sub-blocks of ~900 bytes there (tags
  25/21/17 recurring at 864–1056 byte spacing) are camera data that merely neighbours the tables.

A full string scan of these regions yields **no cue names, no player names, no event names**.
Consequence: there is no offline name table to recover, and ASR remains the only name source for
the ~70,000 unnamed streams. The earlier "identity then increasingly skips" reading of a u16 map was
an artifact of the misaligned start and should be disregarded.

Grade: **verified offline** (structure measured across all 19 banks, header field `+0x24` confirmed
on three). The *meaning* of the ID bands is **theory** — untested, and confirming it needs the
consumer code in Ghidra, which is currently disconnected.

### Scripts

`post_layer.py` (per-bank trailing-region sizes and segmentation), `paplyrs_index.py` (post-table
header field dump + u8/u16/u32 profiling of the two 29 KB blocks), `horns_block.py` (tag-stride scan
that found the ~900-byte camera sub-blocks), `block_strings.py` (ASCII + UTF-16BE scan of an
arbitrary range), `remap_scan.py` (the ten-banks-with-headers measurement across all 19),
`id_array.py` (full ID-array dump with descent reporting), `asr_queue.py` (concurrency-capped ASR
driver over the twelve remaining banks).
