# CLAUDE.md — working notes for AI assistants on the NHL 2K10 Mod project

This file orients an AI assistant (e.g. Claude Code) working in this repository. Read it
before making changes. The human-facing overview is in `README.md`; the technical ground
truth is in `findings/`.

---

## What this project is

A modding toolkit for **NHL 2K10 (Xbox 360)** running under the **Xenia** emulator. The
deliverable is the **Mod Launcher** — a Python/Tkinter GUI (`nhl2k10_launcher.py`, an `App`
class) that edits the game's asset archives **directly on disk** so changes appear in-game.

There is a **second, separate effort**: a native **PC recompilation** (RexGlue, in a
different project tree at `C:\NHL2k10_Recomp`). It is *not* the launcher and shares no code.
Don't conflate the two. See `findings/11`.

---

## The one rule that matters most

**A finding is not true until it is confirmed in-game.** This project has a long history of
plausible theories that were later disproven by actually running the game. The `findings/`
docs deliberately grade every claim:

- **verified** — observed working in the running game.
- **partially verified** — mechanism works, full effect not yet confirmed.
- **theory** — reasoned but untested. Treat as a hypothesis, not a fact.

When you write or edit docs, preserve these grades. When you make a change to the tools,
**do not claim it works** — say what you changed and that it needs an in-game test. Never
upgrade a "theory" to "verified" without evidence.

---

## Where the truth lives

- **`findings/`** — the verified knowledge base, one doc per system. Start at
  `findings/README.md`. This is the canonical, cross-checked account. If code and a findings
  doc disagree, the code is authoritative for *current behavior* — update the doc and note
  the correction.
- **The launcher code** is the other source of truth for how things actually work today
  (the `launcher/` package: `archive_textures.py`, `encode_e4837_lazy.py`, `bank_parser.py`,
  `goalie_equipment.py`, `ros_file.py`, `team_colors.py`, `portrait_*.py`, `scorebug_*.py`,
  etc.).
- **Do not trust the older `docs/` tree** (in `NHL 2k10 Xbox 360 Modding Information/docs/`,
  outside this repo). The `findings/` docs here supersede it and note where it was wrong.

---

## Architecture facts you must not get wrong

These have each been the source of a hard-to-find bug. Respect them.

1. **Texture/asset edits are file-based and offline.** The pipeline is
   *team → asset name → resolve in archive TOC → 0E4837-compressed blob → VRAM texture*.
   Edits rewrite the archive files, and the game is **relaunched** to see them.
   **Live in-memory texture patching was tried and is a proven DEAD END** — the GPU draws
   from a decoupled copy. Don't reintroduce it. (Live *memory* writes are used only for a
   few things: goalie-mask/portrait live-assign and roster fields.)

2. **0E4837 compression** is a custom flag-byte LZ77 (not LZX). Each blob carries its own
   **window parameter (`wparam`)** — re-encoding must **preserve the native `wparam`** (an
   earlier bug hardcoded `wp=9` and corrupted `wp=10` assets and inflated big-window blobs).
   See `findings/02`.

3. **Xenos 8-in-16 endianness.** Block-format textures on the 360 are byte-swapped in
   16-bit words. Getting this wrong swaps pixel rows / flips edge alpha. The fix
   (`_dxt_endian` / `_bc3_8in16`) is applied across all block encode/decode — this was the
   root cause of the "portraits won't import cleanly" saga. See `findings/03` and `06`.

4. **Replace must fit, or it relocates.** In-place replace requires the new blob to compress
   **≤** the original. Bigger edits relocate (append + redirect record `+0x6C`), which grows
   `1B` over iteration; there's a compaction step to reclaim it. Relocation must also patch
   the grown IFF's size fields (`_patch_grown_iff`) or the game reads a stale size.

5. **Shipping data lives in `launcher/data/` only**, resolved via `resources.py::data_path()`
   (source: `launcher/data/`; frozen: `_internal/data/` — but the current build is **onefile**,
   so `sys._MEIPASS`). **Never read data from the project root.** A cleanup that moved a CSV
   out of the root once silently broke every portrait decode. User-writable state goes to
   `%APPDATA%\NHL2K10 Mod Launcher\`, never the app folder (a rebuild wipes it).

6. **`.orig` = pristine bytes.** The launcher writes `<archive>.orig` once before first
   modifying an archive. Pristine reads use `X.orig` if present, else `X`. There is **no
   separate "clean files" folder** anymore (the old `CLEAN_DIR` is gone). The `clean` flag in
   `resolve()`/`load_toc()` must stay **explicit** — some assets resolve to a different
   archive live vs. pristine (relocation), so auto-preferring `.orig` sends writes to the
   wrong file.

---

## Build & run

- The app lives in **`app/`**: `app/nhl2k10_launcher.py` (the `App` class), the `app/launcher/`
  package, `app/launcher/data/` (shipped data), `app/tools/` (user-supplied audio tools).
- **Run from source:** `cd app; python nhl2k10_launcher.py`
  (needs `Pillow`, `numpy`, `requests`, `onnxruntime`).
- **Build the exe:** `cd app; python -m PyInstaller nhl2k10_launcher.spec --noconfirm`
  (or `rebuild_exe.bat`).
  - It is **onefile** (deliberate — see the comment block in the spec; don't switch it back
    to onedir without asking). Output: `dist/NHL2K10 Mod Launcher.exe`.
  - `uac_admin=True` (needed for the live-memory features). This means a running launcher is
    **elevated** — a non-elevated session can't kill it or overwrite `dist/`. **Close the
    running launcher before rebuilding.**
  - The `.spec` **hard-aborts** if any required file is missing from `launcher/data/`. Keep
    that list in sync with `resources.REQUIRED`.
  - `launcher/` is also on `sys.path` so its modules can import each other bare.
  - Some tool modules are duplicated between the project root and `launcher/` and must be
    kept in sync (`encode_e4837_lazy`, `encode_dxt5`, `decode_e4837_fixed`, etc.).
- **Rebuild the exe after any launcher/module change**, or it runs stale code.

---

## Environment & tooling notes

- **OS is Windows**, shell is **PowerShell** (a Bash tool is also available for POSIX
  scripts). Prefer the dedicated file/search tools over shelling out.
- **Ghidra MCP bridge** is available for reverse engineering the XEX (`default.xex`, base VA
  `0x82000000`; file offset = VA − `0x82000000`). Named function spine is in `findings/12`.
- **Cheat Engine MCP** is available for live memory work under Xenia.
  **⚠ CRITICAL:** never run large memory scans over Xenia's `MEM_MAPPED` guest RAM — a scan
  with 100k+ hits has wedged CE and forced a restart. Use small, targeted pair-scans (a few
  hundred results) and intersect them in your own code. If CE restarts, the MCP connection
  drops — reconnect it.
- The game archives are **multi-GB files** (`0A`, `0B`, `1B` are ~1.2–1.8 GB each). Don't
  read them whole; seek to offsets.

---

## When you finish a task

- State plainly what you changed and **what still needs an in-game test**. Don't overclaim.
- If you disproved something in a findings doc, **update the doc** and leave a one-line note
  of the correction so the next person doesn't repeat it.
- Keep the verified/partial/theory grading honest.
