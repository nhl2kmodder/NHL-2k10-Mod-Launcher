# Findings — NHL 2K10 modding knowledge base

This is the verified technical account of how NHL 2K10 (Xbox 360) stores its data and how
the Mod Launcher edits it. Each doc was cross-checked against the working tools and the
project's memory of what has actually been confirmed **in-game**.

## How to read these docs

Every doc opens with a one-line summary and a **Status** line. Individual claims are graded:

- **verified** — observed working in the running game.
- **partially verified** — the mechanism works; the full effect isn't fully confirmed.
- **theory** — reasoned but untested. A hypothesis, not a fact.

This project has repeatedly disproven plausible theories by actually running the game, so
the grading is not decoration — respect it. Where an earlier doc or theory was later shown
to be wrong, the current doc says so, so nobody repeats the mistake. **If you confirm or
disprove something, update the relevant doc and its Open-questions section.**

## The docs

### Core formats — read these first
- **[01 — Archive & IFF format](01_archive_and_iff_format.md)** — the `0A`/`0B`/`1B`
  archives, the IFF table-of-contents, and the name→asset hash (`crc32` of the
  uppercased name).
- **[02 — 0E4837 compression](02_compression_0E4837.md)** — the custom flag-byte LZ77
  codec every asset blob is packed with, and the per-blob window parameter you must
  preserve on re-encode.

### Assets & how to edit them
- **[03 — Texture modding](03_texture_modding.md)** — the full texture pipeline: formats
  (DXT1/DXT4_5/DXT5/BC4 + linear), in-place vs. relocate-grow replacement, mip-chain
  rebuild, the Xenos 8-in-16 endian fix, alpha/premultiply handling, lossless 8888,
  multi-texture packs, and why live in-memory texture patching is a dead end.
- **[04 — Audio system](04_audio_system.md)** — the IFF bank → raw-XMA (`1A`/`1B`) cue
  system, ~80k stream coverage, and the authored-name cracking.
- **[05 — Goalie masks & equipment](05_goalie_masks_and_equipment.md)** — mask/gear assets,
  repaint (in-place + grow), live mask assignment, quality (DXT1→8888) and the team-tint
  recolor bypass, and the custom-mask *add* investigation.
- **[06 — Player portraits](06_player_portraits.md)** — where headshots live, the
  portrait↔player key mapping, the import-quality endian fix, and NHL.com auto-download.
- **[07 — Teams, roster & colors](07_teams_roster_and_colors.md)** — the `Roster.ROS`
  format & editor, team names (string pool), and the fully-cracked team/arena color table.
- **[08 — Scorebug, overlay & front-end](08_scorebug_overlay_frontend.md)** — the
  scoreclock scene graph & layout editor, the shots-on-goal XEX-hook effort, the menu/logo
  system, jersey decal mapping, and the crowd.

### Discovery, tooling & the wider RE effort
- **[09 — Asset discovery & live capture](09_asset_discovery_and_live_capture.md)** — how
  undiscovered assets were found (TOC sweep, crc dict-attack, live capture from the running
  game, GPU trace) and the VFS override-device chain.
- **[10 — Custom arena music](10_custom_arena_music.md)** — "Arena Music" = Xbox 360 Custom
  Soundtracks (XMP), why it fails under Xenia, and the paths to fix it.
- **[11 — PC recompilation](11_pc_recompilation.md)** — the **separate** native-recomp
  effort (RexGlue): status, build workflow, and the solved/open blockers. Not the launcher.
- **[12 — Reverse-engineering infrastructure](12_reverse_engineering_infrastructure.md)** —
  the XEX function map, the file-device/decompress class hierarchy, the RE naming sweep, the
  Ghidra bridge, and the game-window-icon patch.
- **[13 — Models & geometry](13_models_and_geometry.md)** — model data located but packed;
  runtime vertex capture (works); what it would take to edit geometry.

## Cross-cutting facts worth knowing up front

- **Edits are file-based and offline.** Rewrite the archive, relaunch the game. Live texture
  injection was tried and abandoned (`findings/03`).
- **The XEX** (`default.xex`) loads at base VA `0x82000000`; a file offset is `VA − 0x82000000`.
- **Pristine bytes** are `<archive>.orig` (written once before first edit) — there's no
  separate clean-files folder.
- **⚠ Never run large Cheat Engine memory scans over Xenia's mapped guest RAM** — it wedges
  CE. Use small targeted scans and intersect in code.
