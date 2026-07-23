# 13 — Models & geometry

One-line summary: All of NHL 2K10's model/geometry data has been located and raw-extracted (58 MB), but it ships packed/compressed with an unknown scheme, so usable meshes come not from the files but from **runtime vertex-buffer capture** — a working tool (`vertex_capture.py`) that scans a 512 MB guest-RAM dump and exports OBJ point clouds of every model on screen (validated on a real 3545-vert helmet).

Status: **files located & extracted, but not decodable; runtime capture WORKS (positions only).** The static mesh-parser RE is a multi-session job and is parked. The runtime path is validated and is the recommended route to real geometry. Index buffers, normals, UVs, and packed SHORT4N positions are still open.

---

## 1. What's in the files (located & extracted)

2026-07-12. All model/geometry data was located and raw-extracted to `NHL 2k10 Extracted/model_data/` (56 files, 58 MB, `manifest.json`):

- **51× geometry_block (`02000100`)** — uniform ~728 KB each, archive `0B`, entropy 7.37. LE header: magic `0x00010002`, then counts/sizes `[0x100, 0x80, 0x12c, 0xbb5 (2997), 0x64, …, 0x8000, 0x1d7c, 0x8000]`. Per-entity geometry (2997 = plausible index/vert count). **NOT raw float32/SNORM verts** — tested, no coherent vertex cloud → packed or compressed.
- **1× geometry_container (`0006f000`)** — 25 MB (toc #1921, crc `0xCCFB051A`): a bundle of 13 nested `02000100` blocks.
- **3× model_anim_bundle (`00000001`)** — up to 3.9 MB, entropy 7.9, resource-ref hashes at the tail = compressed model/animation.

All of them: **no zlib, no `0x0E4837` magic, high entropy ⇒ unknown compression/packing.** The game decompresses/unpacks these at load, then builds Xenos vertex/index buffers in RAM — the raw file bytes are not plain geometry. A GPU-trace vertex-fetch scan of the July-7 trace found **0** vertex fetch constants via the simple type-3 heuristic (recovering them needs the `LOAD_ALU_CONSTANT` path + 2-dword sub-slot fetches — non-trivial).

---

## 2. Why the files aren't directly usable — and the parser RE

The class hash `0xBB05A9C1` is the mesh/geometry IFF section, and `0x5C369069` is its registered handler id — but decompiling the actual vertex/index parser is a **multi-session RE task**, because it is not one static function. It is a **context-dispatched C++ vtable handler** whose implementation differs by load context.

**Registration chain (Ghidra, XEX loaded):**
- `Model_RegisterAllMeshes @0x84099F18` — builds `g_MeshHandleRegistry` from `g_MeshDescriptorTable`; sets `DAT_850C6108 = 1` when ready.
- `Model_RegisterMeshSection_bb05a9c1 @0x84099E50` — registers the IFF section handler for class `0xBB05A9C1` with handler id **`0x5C369069`** on device `@0x850D220C`. → `Iff_RegisterSectionHandler @0x8410D7C0` → `Function_8410D6C8 @0x8410D6C8` (registry dispatch, calls handler vtable `+0xC` to register).
- `Res_RegisterSectionHandlers @0x83B689D0` registers 4 IFF section class hashes with per-context callbacks: `0x306CD146`, **`0xBB05A9C1`** (mesh/geometry, at struct `+0x38`), `0x411536D5` (logo/portrait bundle — see finding 09), `0x76CBC6E7`. The callback for a class is a parameter passed by the caller.
- **Callers** (each registers its OWN handlers for the same hashes, so `bb05a9c1`'s parser DIFFERS by context): `Function_83B68E88 @0x83B68E88`, `TitlePage_Load @0x83C9C198`, `LoadingScreen_Init @0x84057674`, `Function_83FE1180 @0x83FE1180` (×2).

**Consumers:**
- `Model_GetMeshHandleById @0x84099EF8` = `(&g_MeshHandleRegistry)[cat * 0x12E + id]` — a 2D array, stride 302 (matches the 302 mesh records in scene descriptors).
- `Player_SetMeshPartById @0x8408EA28` → `Function_84160BB8` → `Function_8415EBA8` (5+ more indirection layers to the VB/IB struct).
- `Goalie_GetMaskMeshId @0x84090770`, `Goalie_UpdateGearMeshParts @0x84093F88`.
- `Gpu_SetVertexFetchConstant @0x841C2840` — packs a runtime vertex-buffer **descriptor** (fields at `src+0x1C/0x20/0x24/0x28/0x2C/0x30` = base/size/stride/format) into the GPU regs. Confirms standard Xenos vertex fetch; the descriptor is built by the parser above.
- Mesh registry base ~`0x850C610C`.

**Vtable-read technique:** pointers live at file offset = `VA − 0x82000000` in `default.xex` (1:1 only for `VA < 0x84536000`; data at `0x84xxxxxx` is NOT 1:1). Caution learned the hard way: one handler object chased down (`0x820074A0`) turned out to be an **audio/DSP biquad filter** (`Function_83B66470` = SinCos biquad) — the WRONG context. The mesh context's handler is a different object (need the caller that passes the mesh factory). Also note: `bb05a9c1` is the **generic** resource class (2305 of 2407 TOC entries), so it is NOT a mesh discriminator on its own.

**Conclusion:** fully decompiling the exact vertex/index parser is layered, context-dispatched, and multi-session. The faster path to real geometry is runtime capture (§3).

---

## 3. Runtime vertex capture — WORKS

2026-07-13. Built and validated the runtime model-capture path — the practical route to game meshes, versus the static parser RE.

**Tool:** `NHL2K10 Mod Launcher/launcher/vertex_capture.py`. It scans a 512 MB guest-RAM dump for coherent geometry: interprets bytes as BE/LE float32 at strides 3/4/6/8/12/16, finds runs where values stay in model-coordinate range, and **scores** for a real mesh — rejecting the false positives (`x==y==z` scalar ramps, perfectly-correlated lines, outlier-driven cross/star patterns, origin-clustered noise). Each hit is exported as an OBJ point cloud + raw `.bin` + `vbuf_manifest.json`.

Run: `python vertex_capture.py <slab_dir> <out_dir>`.

**Validated:** on a leftover team-select RAM dump it cleanly isolated ONE real model — guest `0x06836AC4`, BE float32, stride 3, **3545 verts**, bbox `[-15.6, -0, -11.3] .. [15.6, 92.5, 20.1]` — rendered front/side/top = a coherent symmetric helmet/head-shaped mesh. All noise rejected.

**Capture workflow (do when a model is on screen):**
1. Get the game to a screen with a clear 3D model — best: **gameplay** (players + goalie on ice) or the goalie-mask 3D preview; the team-select jersey render also works.
2. CE-dump guest RAM to 4 slabs (128 MB each), named `ram_slab0..3.bin`, at guest phys bases:
   `0x1C0000000`, `0x1C8000000`, `0x1D0000000`, `0x1D8000000` (= guest phys `0..0x20000000`).
3. `python vertex_capture.py <slab_dir> <out_dir>` → OBJ point clouds of every model on screen.

Coordinate map: slab byte-offset `O` == guest phys `O` (host = `0x1A0000000 + O`). Bbox is in game units (~cm).

---

## Open questions / caveats

- **Positions only.** The scan captures a point cloud. For solid meshes you also need the **index buffer** (uint16 triangle lists near the VB) plus other vertex attributes (normals/UVs). The raw `.bin` holds the packed vertex data for that hit — parse further from there.
- **Packed-format positions not caught yet.** SHORT4N (packed-normal) positions are invisible to the float32 scan. Add a SHORT-normal pass if a known model doesn't appear.
- **Exact stride/format** for a buffer can be confirmed from the live vertex fetch constant (`0x4800` reg block, type-3 slots — base/size/stride/format) or by RE via `Gpu_SetVertexFetchConstant @0x841C2840`.
- **File decode still unsolved.** The 51 `02000100` geometry blocks + the 25 MB container remain packed with an unknown scheme (no zlib, no 0E4837, entropy ~7.4). Cracking the file path means decompiling the `0x5C369069` context-dispatched parser (multi-session).
- **`bb05a9c1` is generic**, not a mesh flag — do not use it to discriminate meshes from other resources.
- No superseded old-doc conflicts: there is no prior published models doc; this consolidates `project_model_data` and `project_vertex_capture`.
