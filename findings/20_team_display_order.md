# 20 — Team display order: the menu list is physical record order

**Status: SOLVED and verified in game, 2026-08-01.** Shipped as `launcher/team_order.py`
+ Teams tab → **Team Order…**, and as a `team_order` mod-pack roster group.

The goal was to make Winnipeg sort last and Utah sort between Toronto and Vancouver
after the Atlanta→Winnipeg / Phoenix→Utah relocation (doc: `project_team_names`).

---

## 1. The answer

**The team-select list is walked in physical record order in `Roster.ROS`.** To change
the order you physically permute the team records. There is no order table anywhere.

## 2. What it is NOT (all tested, all negative)

Every one of these was built, patched into the running game, and verified live before
being ruled out. Do not retry them.

| Hypothesis | How it was killed |
|---|---|
| An order table in the XEX indexing the team array | Patched an order table into **all seven** team-array sites *including* `Roster_GetTeamByIndex@83d4b5e4` (~100 callers). Confirmed in guest RAM that the patch was live and that `GetTeamByIndex(1)` returned Boston. **Menu unchanged.** |
| Sorted by a stored id field | Permuted all five id fields (`+0xC8`, `+0xC9`, `+0xF0`, `+0x10C`, `+0x10E`) on the 30 NHL records. **Menu unchanged** — but the *default matchup* moved, which is how doc 19's sibling finding (`project_default_matchup`) was discovered. |
| Alphabetical by current name | Winnipeg sat in Atlanta's old 2nd slot, not last. |
| A stored 30-entry order table in the ROS or the XEX | Scanned both. The three XEX hits were `tolower`/`toupper` byte tables (`0x1AE3B00`, `0x1AE3C80`) and VMX shuffle constants (`0x1AE74C0`). |
| A front-end scene list of team buttons | Never needed — see below. |

Also ruled out earlier and worth repeating: the 13 "team enumeration loops" enumerate
**uniforms** (407 of them, stride `0x11C`), not teams, and `Team_GetLeague@840b62c0` is
`(v >> 3) & 7`, not `>> 29`.

## 3. The decisive experiment

Xenia maps guest memory at a **different host base every launch** (it was
`0x200000000` that session, `0x100000000` earlier ones) — probe for it, don't assume.

```
g_Mgr   = *(0x849DE29C)
teams   = *(g_Mgr+0x4C), count *(g_Mgr+0x48) = 96, stride 0x19C
memory record offset + 8 == disk record offset
```

The in-memory records hold **absolute** pointers to their own strings and players, so a
raw 412-byte swap of two records is self-contained. Swapping records 1 (Winnipeg) and 29
(Washington) in the running game and reopening the team list showed **Washington second
and Winnipeg last**. That is the whole proof.

Method note, again: static analysis lost to experiment repeatedly on this problem.

## 4. The record, and why a memmove corrupts the save

96 records × 412 (`0x19C`) at file `0x11D7CC`:

* `0..29` NHL, in display order
* `30..59` their AHL affiliates — record `30+i` is the same organisation as record `i`
* `60..95` all-star / national / custom / HOME / AWAY — never moved

Every pointer is **self-relative**: `target = field_offset + value - 1` (value signed),
and strings are UTF-16BE. Pointer fields, established from live memory (where the same
structs hold absolute addresses) and cross-checked on disk:

| disk offset | points at |
|---|---|
| `+0x08..+0x70` | this team's players, zero-terminated (20–26 used) |
| `+0xA8 / +0xAC / +0xB0 / +0xB4 / +0xB8` | city / nickname / display code / lowercase / asset key |
| `+0xCC` | this team's arena record |
| `+0xD0` | the organisation's **pro** roster array (`= record i, +8`) |
| `+0xD4` | the organisation's **farm** roster array (`= record 30+i, +8`) |
| `+0xD8` | a per-team block in another chunk |

Two things make this more than a memmove:

1. **`+0xD4` points into another team record that is itself moving.** A pointer's new
   value depends on where its *target* lands, not just where the pointer lands.
2. **~11,100 pointers from outside the table point into it.** Every rostered player carries
   a back-pointer to its team at player record **`+0x14`** (2,018 of them on the live save),
   and ~9,000 more come from the `0x100000..0x1FB800` region. Miss these and every player
   silently changes team. *(An earlier draft put this at `+0x190`; that was measured before
   the player table was framed correctly — see §5. The team's pointers to its players land on
   `player_record + 0x00`, and each player points back at `team_record + 8`.)*

So the algorithm is: resolve each pointer's target in the old file → map that target
through the permutation → recompute the self-relative value from the pointer's **new**
offset. Every genuine inbound pointer lands on exactly `record + 8` (which is where the
in-memory team struct starts), which is a clean filter; ~180 words elsewhere in the file
resolve into the table by coincidence, about what chance predicts for 2.5 MB, and are left
alone.

**Team ids are not renumbered.** `+0xC8` travels with its record, so Default Matchup and
every id-based lookup keeps naming the same team.

## 5. Container correction — there is no data base at all

Chasing the team table exposed a long-standing error in `ros_file.py`. It read each chunk's
directory entry as `DATA_BASE + data_offset` with `DATA_BASE = 0xB28` (the end of a 237-entry
directory). Both halves are wrong:

* `data_offset` is a **self-relative pointer**, the same convention as every other pointer in
  the file: `target = <offset of the field itself> + value - 1`. There is no data base.
* There are **19 chunks**, not 237. The `0xED` at header `+0x08` is not the entry count —
  entry 19 onward is already chunk-0 data being misread as directory. The directory
  self-terminates: the first entry whose pointer is null, lands inside the directory, runs
  past EOF, or goes backwards is where the data starts.

Read correctly, **every record table tiles exactly**: `size == count × stride` to the byte for
all 19 chunks (2715×420 players, 96×412 teams, 40×40 arenas, 407×284 uniforms, …). The stride
is just `size // count` — `ros_file.py`'s byte-autocorrelation stride guess is now only a
fallback, and the "the region is a hair short of count*stride" note it was built on was an
artefact of the bad base. Chunk `0xE35B988E` (40 × 0x28) is the **arena** table at `0x11D194`.

Two consumers had hand-written corrections for the bad frame, and both are now fixed:

* `player_assign.py` used `foff + 0x73`. That got the phase right by luck but started **7
  records late**, so player rows 0–6 were invisible — and six of them are on a team's roster.
  Record 0 is the chunk start, `0x4F24`. **Every player row index is now 7 higher.** The
  goalie set is byte-for-byte identical; only the numbering moved, and the mod-pack goalie
  section re-resolves by portrait key and ordinal, so shipped packs still land correctly.
* `ros_editor_gui.py` framed the player chunk at `0x5A2D`, which is 6 records **+ 305 bytes** —
  its "records" were a rotation straddling two real players. Its field defs are remapped
  `new = (old + 305) mod 420`; the in-game-pinned audio-name ids move from `+0x9F/+0xA1` to
  `+0x2C/+0x2E`. Its palette block was also mislabelled as the goalie recolour colours; those
  are `player_assign.py`'s independently verified `+0x158/+0x15C/+0x160` (`+0x168` cage).

`team_order.find_table()` does not trust any constant: it locates record 0 by requiring every
record's pro/farm pointers to resolve to some record's `+8`. That signature is a 96-record
agreement on a 4-byte value, and — unlike "record k holds id k" — it **survives a reorder**, so
an already-reordered save can still be found.

One loose end, harmless but worth knowing: the team chunk's own directory pointer targets
`0x11D7D4`, which is `team_order`'s record 0 **+ 8**. So the true record boundary is probably
`0x11D7D4` and `team_order` frames 8 bytes early — which is exactly why "memory offset + 8 =
disk offset" and why inbound pointers appear to land on `record + 8`. The framing is internally
consistent and in-game verified, so it was left alone.

## 6. Fallout in the launcher

`team_colors.py` anchored the table on "record k holds id k" and mapped code→record via
the fixed alphabetical `NHL_CODES` list. Both assumptions die on the first reorder, and
the failure mode is silent: colours land on the wrong teams. Fixed — `_team_base()` now
anchors via `team_order.find_table()` (its frame starts `0x63` earlier, hence
`FRAME_SHIFT`; its `+0x12B`/`+0x12C` are team_order's `+0xC8`/`+0xC9`), and
`team_map(ros_path)` reads the display codes out of the records. Every call site in
`team_colors.py` and `team_fields.py` now passes the roster path.

`roster_editor.py` is unaffected — it edits the string pool, which does not move.

## 7. Code

* `launcher/team_order.py` — `read_order()`, `order_codes()`, `apply_order()`, `revert()`.
  In place, size never changes, one-time `<roster>.ROS.orderbak`, and it verifies every
  roster and back-pointer before writing a byte.
* `launcher/team_order_gui.py` — Teams tab → **Team Order…**: drag or Move up/down, Sort A–Z,
  Apply, Revert.
* `launcher/modpack.py` — roster group **`team_order`** (a list of the 30 display codes).
  Applied *first* of the roster groups, since the others address teams by code.

Round-trip verified: reordering the shipped V5 roster through the module reproduces the
in-game-verified V6 file **byte for byte**, and reordering back reproduces V5.

Restart the game to see a change — the roster is read once at load.
