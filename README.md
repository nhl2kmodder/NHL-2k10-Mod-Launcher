# NHL 2K10 Mod Launcher

A modding toolkit for **NHL 2K10 (Xbox 360)**, built to run the game under the
**[Xenia](https://xenia.jp/) emulator**. The centerpiece is the **Mod Launcher** — a
Windows GUI that edits the game's asset archives directly (textures, audio, rosters,
team data, goalie gear, portraits, the on-screen scoreclock) so your changes show up in
the actual game.

This repository is the **documentation and collaboration hub** for the project. The
launcher source, tools, and game data are added separately — for now this holds the
knowledge base so multiple people can work together from the same, verified set of facts.

> **Legal / scope.** This is a fan preservation-and-modding project. It ships **no game
> code and no copyrighted game assets** — you supply your own legally-obtained copy of
> NHL 2K10. The launcher edits *your* local files.

**NOTE:** The launcher is in a very early stage - there may be work that is unstable / not hooked up / straight up won't work.

---

## What the Mod Launcher can do

The launcher is a Python/Tkinter desktop app. It reads and rewrites the game's archive
files (`0A`, `0B`, `1B`, …) in place, so edits work under Xenia (and, in principle, on
real hardware / JTAG). Current tabs:

| Tab | What it does |
|-----|--------------|
| **Audio** | Browse/replace ~80,000 in-game audio streams; rename and re-tag them; export/import audio-name sets for collaboration. |
| **IFF Textures** | The core texture workflow. Browse thousands of catalogued textures, extract as DDS/PNG, edit, and apply back — jerseys, rink/ice surfaces, arena boards, logos, UI art, player faces, and more. |
| **Teams** | Edit team names, team/arena colors, and per-team record fields; drives an advanced `Roster.ROS` editor. |
| **Goalie Equipment** | Repaint goalie masks and gear, and live-assign masks to goalies. |
| **Portraits** | Replace player headshots — including auto-downloading real portraits from NHL.com and reframing them to fit. |
| **Scoreclock** | Reposition, rescale, recolor, and restyle the on-screen scoreboard/scoreclock elements. |
| **Settings** | Set game/emulator paths and audio tools; **Share & Merge** for trading mod work with other people. |

Two more tabs (**Audio Banks**, **Arena Music**) exist in the code but are **hidden** —
they're work-in-progress.

### Collaboration built in
The **Settings → Share & Merge** section lets modders exchange work:
- **Audio Names** (`.n2knames.json`) — share just naming/tagging work.
- **Mod Pack** (`.n2kpack`) — a zip of everything: audio, textures, metadata.

Both use a merge engine that detects **New / Same / Conflict** per item and lets each
person pick which version to keep. See `findings/` for how it works under the hood.

---

## Getting started (users)

### Requirements
- **Windows 10/11**
- **Xenia** emulator + your own **NHL 2K10 (Xbox 360)** copy, extracted so the archive
  files (`0A`, `0B`, `1B`, …) sit in one folder
- To run from source: **Python 3.10+** and the packages in `requirements.txt`
  (`Pillow`, `numpy`, `requests`, `onnxruntime`)

### Run the launcher

- **From source** (quickest for contributors)
  ```
  cd app
  pip install -r requirements.txt
  python nhl2k10_launcher.py
  ```
- **Build the standalone exe**
  ```
  cd app
  rebuild_exe.bat            # or: python -m PyInstaller nhl2k10_launcher.spec --noconfirm
  ```
  Produces a single self-contained `dist/NHL2K10 Mod Launcher.exe`. It requests
  **Administrator** on launch — needed for the live-memory features (goalie-mask /
  portrait live-assign) that write into Xenia's process.

> **Audio tools not included.** The audio import/encode features need `ffmpeg` and
> `xma2encode.exe`, which aren't committed (licensing + size). Everything else works
> without them — see `app/tools/README.md` to add them.

### First steps
1. Open **Settings**, point the launcher at your **game files folder** (where `0A`/`0B`/`1B`
   live) and your **Xenia** executable.
2. The launcher writes a one-time `.orig` backup the first time it modifies any archive —
   your pristine bytes are always recoverable.
3. Make edits in a tab, then use **Apply All Mods** (top bar) to write everything to the
   game in one pass. Launch the game to see your changes. Live in-memory texture editing
   is **not** supported — changes are applied to files, then you relaunch.

---

## Repository layout

```
NHL2K10-Mod-Collab/
├─ README.md          ← this file (for users & new contributors)
├─ CLAUDE.md          ← working notes for AI assistants (Claude Code) on this project
├─ .gitignore
├─ findings/          ← the verified knowledge base: file formats & how each system works
│  ├─ README.md       ← index of the findings docs
│  ├─ 01_archive_and_iff_format.md      … 13_models_and_geometry.md
└─ app/               ← the Mod Launcher — build & run from here
   ├─ nhl2k10_launcher.py      ← the GUI app (the `App` class)
   ├─ nhl2k10_launcher.spec    ← PyInstaller build recipe (one-file)
   ├─ rebuild_exe.bat          ← one-click build
   ├─ requirements.txt
   ├─ *.ico / *.png            ← window/taskbar + game icons
   ├─ launcher/                ← the Python package (all the real logic)
   │  ├─ archive_textures.py, encode_e4837_lazy.py, goalie_equipment.py,
   │  │  ros_file.py, team_colors.py, portrait_*.py, scorebug_*.py, … (~39 modules)
   │  └─ data/                 ← shipped mapping data (CSV/JSON) + the face-parsing model
   │     └─ README.md          ← what each data file is
   └─ tools/                   ← audio helpers you supply (ffmpeg + xma2encode) — see its README
```

The `findings/` docs and the `app/` code are the two halves of the project: `findings/`
explains *why/how* each system works; `app/` is the tool that *does* it. When you change
behavior, update the matching findings doc.

---

## Contributing

This is early-stage collaboration. If you want to help:

1. **Read `findings/README.md` first.** It's the map of what's known, what's verified, and
   what's still open. Every claim there has been cross-checked against the working tools —
   treat it as ground truth, and flag anything you find to be wrong.
2. **Pick an open question.** Each findings doc ends with an *Open questions / caveats*
   section. Those are the real frontier.
3. **Verify before you document.** The golden rule of this project: a finding isn't true
   until it's confirmed **in-game**. Many early theories were later disproven — the docs
   note where. Don't add a claim you haven't tested.
4. **Share your work** through the launcher's Share & Merge, or via pull request for docs
   and code.

A short note on tone in the docs: they distinguish **verified** (seen working in-game),
**partially verified** (works but not fully confirmed), and **theory** (plausible, untested).
Keep that discipline when you add to them.

---

## Status at a glance

- **Mature / verified:** archive & IFF format, 0E4837 compression, texture replacement
  (DXT + linear, mip chains, lossless 8888), audio replacement & naming, team names/colors,
  roster editing, goalie mask repaint + live-assign, portraits (incl. NHL.com download),
  scoreclock layout.
- **In progress:** shots-on-goal on the scorebug, custom arena music, audio banks tab,
  custom-mask *add* (vs repaint), model/geometry editing.
- **Separate effort:** a native **PC recompilation** (RexGlue) that reaches gameplay + menus
  but isn't the launcher's target — see `findings/11`.
