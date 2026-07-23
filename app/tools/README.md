# tools/ — audio encode/decode helpers (you supply these)

The launcher's **audio replacement** features shell out to two external tools. They are
**not committed to this repo** and you must place them here yourself:

```
app/tools/
├─ xma2encode.exe          ← you provide (see below)
└─ ffmpeg/
   ├─ ffmpeg.exe           ← you provide
   └─ av*.dll, sw*.dll     ← the DLLs that ship with ffmpeg
```

Everything else in the launcher works without these. If they're missing, only the audio
**import/encode** steps are skipped (the app reports "ffmpeg.exe not found" and keeps going).
You can also point the launcher at copies elsewhere via **Settings** instead of putting them
here.

## Why they aren't in the repo

- **`xma2encode.exe`** is part of the **Microsoft Xbox 360 SDK (XDK)**. It is Microsoft's
  proprietary tool and **cannot be legally redistributed** in a public repository. You must
  obtain it from your own XDK installation.
- **`ffmpeg`** is redistributable (LGPL/GPL) but is ~75 MB of binaries, which doesn't belong
  in git history. Download it instead.

## How to get them

**ffmpeg** — download a Windows build from <https://ffmpeg.org/download.html> (or
gyan.dev / BtbN). Copy `ffmpeg.exe` and its `av*.dll` / `sw*.dll` files into `app/tools/ffmpeg/`.

**xma2encode.exe** — comes with the Xbox 360 XDK (`bin/win32/xma2encode.exe`). The launcher's
build was tested with the standalone exe (it does **not** need the rest of the XDK's
`onyx-resources` beside it). Copy just `xma2encode.exe` into `app/tools/`.

## How the launcher finds them

At runtime, `bundled_tool()` looks for these next to the app first; if absent, the configured
Settings paths are used; if a shared config points at someone else's machine path, it falls
back to whatever is here. The build script (`rebuild_exe.bat`) copies `app/tools/` next to the
built `.exe` as `dist/tools/` — it is deliberately **not** packed inside the one-file exe
(75 MB would unpack to `%TEMP%` on every launch).
