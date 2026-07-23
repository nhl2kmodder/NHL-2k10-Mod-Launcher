# 10 — Custom Arena Music (Xbox 360 Custom Soundtracks / XMP)

**Summary:** NHL 2K10's in-game "Arena Music" feature is the Xbox 360 **Custom Soundtracks** feature driven entirely through **XMP** (the console Music Player API), completely separate from the game's own `arena_<code>.iff` → `1B` bank audio engine; it fails under Xenia because Xenia's `XMPCreateUserPlaylistEnumerator` returns 0 items (no ripped-music library), so the game shows the "must have soundtracks on your HDD" gate.

**Status:** Mechanism fully identified and proven from Ghidra + Xenia logs + live CE. Fix NOT yet implemented. The launcher's Arena Music tab is a **scaffold only** (saves folder paths, no patching) and is currently **hidden** in the shipped launcher. User has OK'd patching the flat XEX.

---

## 1. What the feature is (and is NOT)

"Arena Music" exposes user-supplied music for arenas via the standard 360 **Custom Soundtracks** mechanism: the user rips CDs / copies music to the console HDD, and titles read that library through the **XMP** kernel APIs (`xam.xex` exports). This is entirely separate from the shipped arena audio.

| System | What it is | Where it lives | Doc |
|---|---|---|---|
| Shipped arena audio (horns/songs/organ/PA) | Baked XMA streams indexed by IFF banks | `arena_<code>.iff` → `1B` | 04 |
| **Custom arena music** | **The user's own songs via XMP custom soundtracks** | **Console music library + a `Music` XContent package** | this doc |

Editing `1B` streams (doc 04) changes the *shipped* sounds; it has nothing to do with the Arena Music screen. Confirmed: there are **no** `XMP` / `soundtrack` / `.wma` / `track` / `song` strings in the game image — the custom-music path is pure XMP + XContent.

---

## 2. The failure, proven from Xenia's log

On the Arena Music screen the in-game error is: *"You must have soundtracks saved to your Xbox 360 Hard Drive in order to continue."*

Source: `xenia_canary\xenia.log` (session entering the screen). The reproduced sequence:

```
F> HostPathDevice::ResolvePath(\content\0000000000000000\54540853\00000001)
i> XamContentCreateEnumeratorInternal_entry: Adding: Music (Filename: Music) ...   <— the Music pkg
i> XamContentCreateEnumeratorInternal_entry: Adding: Roster (Filename: Roster.ROS) ...
i> XamContentCreateEnumeratorInternal_entry: Adding: Settings (Filename: Settings.STG) ...
i> XMPCreateUserPlaylistEnumeratorHandle: added 0 items to enumerator               <— the gate
```

Interpretation:
1. The screen enumerates the title's saved content (savedata type `00000001`) and finds a package named **`Music`** alongside Roster/Settings.
2. It then calls **`XMPCreateUserPlaylistEnumerator`** to enumerate the **user's HDD music library** → **0 items** under Xenia → the "must have soundtracks" error.

Nothing after the enumerator touches the `Music` package in that session — the game errors out before it would build or play a playlist. Title id `54540853` = NHL 2K10.

---

## 3. The `Music` XContent package (on disk)

Xenia stores content "deconstructed" (a folder per package, real files inside, no STFS header). Found:

```
xenia_canary\content\54540853\Music\Van_song.wma
xenia_canary\content\B13EBABEBABEBABE\54540853\00000001\Music\Van_song.wma
```

- `54540853` = title id; `00000001` = content type (savedata); `B13EBABEBABEBABE` = a profile XUID.
- `Van_song.wma` — real ASF/WMA (header GUID `30 26 B2 75 8E 66 CF 11 …`), 929,894 bytes. **The name is arbitrary** — the user placed it hoping the game would pick it up. There is **no** `Van_song` / `song` / `.wma` string in the XEX, so the game does not key off that filename, and the file is **never read** (the game gates on the user-library step first). The correct filenames / manifest the game expects are still unknown (needs a post-gate trace).

The game *creates* this `Music` package itself (it appears in the content enumerator). A title only makes a content package if it stores/plays from it → strong evidence the real design is "import chosen songs into `Music`, then play them via a **title** playlist (`XMPCreateTitlePlaylist`)."

---

## 4. Why the obvious fixes don't work

- **"Just drop WMAs where Xenia reads them":** there is no such spot in this Xenia. `XMPCreateUserPlaylistEnumerator` (the user library) is hard-wired empty; `XMPSetMediaSourceWorkspace` is **unimplemented**; the only XMP knobs are `enable_xmp = true` and `xmp_default_volume = 70`. Placing files in the `Music` package does nothing because the gate is on the *user library*, not the package.
- **"Make the game scan a folder":** the game never scans a directory for music. `NtQueryDirectoryFile` (the only dir-enum path) is used only by DLC (`DlcDevice_RegisterContentPackage`) and file-info (`VCWIN32FILEDEVICE_GetFileInfo`).

---

## 5. Solution paths

### Option A — emulator side (make XMP surface a folder)
Make `XMPCreateUserPlaylistEnumerator` return the user's songs from a chosen folder. Cleanest match to "point at a folder"; no XEX patch; the game's normal flow then works. **But** it requires a Xenia build/fork that feeds a music folder into XMP user playlists (or a host-exe patch to Xenia's XMP handler). Not supported by the user's current `xenia_canary` out of the box.

### Option B — game side (patch the flat XEX)  ← user OK'd this
1. **Neutralize the gate** — after `XMPCreateUserPlaylistEnumerator`, the game checks the item count and branches to the error when 0. Patch that branch to always proceed.
2. **Feed our audio** — get the game to build a **title** playlist (`XMPCreateTitlePlaylist`, which Xenia implements) from WMAs written into the `Music` package.
3. **Launcher piece** — Arena Music tab: pick a folder → transcode to WMA via ffmpeg → write into `content\54540853\00000001\Music\` (+ any manifest the game expects).

**Open crux for Option B:** once past the gate, does the game play a **title** playlist built from the `Music` package (patchable → works), or does it stream the **user** playlist directly (not feedable under this Xenia → Option B fails, only Option A helps)? Must be confirmed by tracing the code immediately after the gate.

---

## 6. Xenia XMP support (host binary facts)

From `xenia_canary.exe` strings/config:
- **Implemented:** `XMPCreateUserPlaylistEnumerator`, `XMPCreateTitlePlaylist`, `XMPDeleteTitlePlaylist`, `XMPGetStatus`, `XMPRegisterCodec`.
- **Unimplemented:** `XMPSetMediaSourceWorkspace`.
- Config: `enable_xmp = true`, `xmp_default_volume = 70`. No music-folder / user-library option.
- Xenia bundles **ffmpeg** for audio decode → it can decode real audio for XMP playback; it just has no user-library source wired.

Implication: **title** playlists are viable under Xenia; **user** playlists are not (return 0). This is the hinge for Option A vs B.

---

## 7. Live guest-memory access under Xenia (key capability)

CE attached to `xenia_canary.exe`. The guest↔host mapping was derived and **validated**:

```
host_address = 0x100000000 + guest_VA
```

Rationale: the launcher reads guest **physical** 0 at host `0x1A0000000` (`xenia_mem.PHYS_BASE`); guest physical is reached via guest VA `0xA0000000`, so Xenia's virtual membase = `0x1A0000000 − 0xA0000000 = 0x100000000`. Verified by reading guest VA `0x83b18108` → the wide string `"d:/builds2k10/vcsports/nhl/code/aigamelib/bware/blist.h"`.

Why it matters: the on-disk `default*.xex` keep the high-VA data section **compressed** (the audio/IFF strings are NOT plain in the flat/unpacked file; only a small early region is). The **live process is the only place the fully-decompressed image is readable** — undefined strings, runtime-patched import thunks, and live state can only be inspected there (or via Ghidra's named-function decompiled view). Helper: `launcher/xenia_mem.py` (`read_bytes` / `write_bytes`, page-wise unlock for writes). CE MCP `read_memory` / `write_memory` also work with the host address.

---

## 8. Import-thunk layout (for finding the XMP call site)

Parsed from `default.xex`'s uncompressed import-libraries optional header (`key 0x000103FF`):
- Libraries: `xam.xex` (216 records), `xboxkrnl.exe` (323 records).
- Records alternate **(data-slot, thunk)** pairs:
  - data slots: `0x82000400 + 4·i` (the IAT entry; holds the resolved pointer at runtime).
  - thunks: `0x842637CC + 0x10·i` (the callable stub the game `bl`s).
- Calibration: `XamContentCreateEx @ 0x8426393C` = thunk **#23** (`(0x8426393C − 0x842637CC)/0x10 = 23`), data slot `0x8200045C`.

**Problem:** each import's ordinal is stored at the data slot before resolution (compressed on disk, overwritten by Xenia at runtime), and Ghidra shows every import thunk as `halt_baddata`. So the specific **`XMPCreateUserPlaylistEnumerator` thunk index is not yet resolved statically.** Named xam thunks Ghidra did resolve: `XamContentCreateEx @ 0x8426393C`, `XamCreateEnumeratorHandle @ 0x842644BC`, the `XamContentCreateEnumeratorEx`-family, achievement/stats enumerators — the XMP ones are unnamed.

Ways to resolve it when resuming:
1. **Breakpoint Xenia's XMP handler** (the host x64 fn that logs `"…added {} items to enumerator"`), re-enter the screen, read the guest **LR** from `PPCContext` → the game caller → map the VA in Ghidra. Most reliable (needs the `PPCContext.lr` offset for this build).
2. **Resolve the ordinal** for `XMPCreateUserPlaylistEnumerator` from Xenia's export table, compute the thunk index → `get_xrefs_to` in Ghidra → the gate function.

---

## 9. Reference — addresses, IDs, paths

Guest (VA) — game image:
- Audio type tokens: `MUSIC_AUDIO @ 0x83B1807C`, `CROWD_AUDIO @ 0x83B18064`, `"Rink Music" @ 0x83B1AE80`, `"pamusic" @ 0x83B2E200`.
- Content/VFS: content-create wrapper `Function_841DEDD0` → `Function_841DEF08` (`XamContentCreateEx`); async I/O worker `Function_83BAA678` (content ops: `0xc` enumerate = `Function_8416BDD0`, `0xd` open = `Function_8416BBF8`, `0xe` create = `Function_8416BA08`, `0xf` close = `Function_8416B990`).
- Import thunks: `0x842637CC + 0x10·i`; data slots `0x82000400 + 4·i`. Guest→host: `host = 0x100000000 + guest_VA`.

Content / files (host):
- Title id **`54540853`** (NHL 2K10); save content type `00000001` (also enumerates `00000002`).
- `…\xenia_canary\content\54540853\Music\` and `…\content\<XUID>\54540853\00000001\Music\` hold the WMAs.
- The "must have soundtracks" error text lives in the localized string table (`loc.iff`), **not** the XEX — so it won't appear in a string search of `default.xex`.

---

## 10. Launcher state

The **Arena Music** tab exists only as a **scaffold** (`_build_arena_tab`): it lets you pick a folder per made-up "event" (intro/warmup/goal/…) and saves the paths to `nhl2k10_launcher_config.json["arena_music"]`, with the note *"requires additional game-file patching (coming soon)."* Those event names are **placeholders**, not game-derived. No patching or file placement is wired, and the tab is currently **hidden** in the shipped launcher (WIP).

When the fix is confirmed, replace the scaffold with: pick folder → ffmpeg→WMA → write to the `Music` content package (+ manifest) → apply the XEX patch (Option B), or point Xenia at the folder (Option A).

---

## 11. Open questions / caveats

- **Post-gate playback type unknown** — title playlist (patchable) vs user playlist (not feedable under this Xenia). This decides whether Option B is even viable.
- **`Music`-package layout the game expects** — filenames, count, any index/manifest — unknown.
- **`XMPCreateUserPlaylistEnumerator` xam ordinal / thunk index** — not yet resolved statically.
- **`PPCContext.lr` offset** for this Xenia build — needed for the breakpoint-LR method.
- The gate `count == 0` branch instruction has **not** been located/decompiled yet, so no live test-patch has been attempted.

### Resume checklist
1. Decide Option A vs B (depends on the post-gate crux, §5).
2. Resolve the `XMPCreateUserPlaylistEnumerator` call site (§8): breakpoint Xenia's handler + read guest LR (needs the user to re-enter the Arena Music screen), or resolve the ordinal.
3. Decompile the gate fn; identify the `count == 0` branch and what the success path does.
4. Live test-patch the gate (CE / `xenia_mem`) and observe whether the game then reads `Music` and plays — settles Option B without a permanent edit.
5. If viable: bake the branch patch into the flat XEX; build the Arena Music tab (folder → WMA → `Music` package + manifest). If not: pursue a custom-soundtrack Xenia (Option A).

### Note vs old docs
Consistent with old doc 09; the only substantive addition here is that the launcher Arena Music tab is not merely a scaffold but is currently **hidden** in the shipped launcher. Note the memory-file date range for this investigation is 2026-06-29 → 07-03.
