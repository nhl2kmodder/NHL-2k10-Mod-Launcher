# Doc 15 — Editing ANY Mesh / UI Element via RenderDoc (the general method)

**Status:** proven + in-game verified (2026-07-24) on the scorebug bottom cyan bar.
**One-line:** RenderDoc gives you the *exact* draw — its shader, its constants, and its vertex
data. Read those, figure out which byte controls the thing you want, find that byte in the IFF
file by *fingerprint matching*, edit it, relaunch. No guessing.

This is the workflow that finally cracked the bottom bar after ~a full session of blind
data/CE/trace hunting failed. RenderDoc turns "search 2.4 MB for an unknown value" into "read the
value off the screen, then match a 48-byte fingerprint." Use it for **anything you can see**:
scoreclock text/size, HUD elements, logos, jerseys, arena props, and — with more work — player and
goalie models.

---

## 0. Why this works (the mental model)

NHL 2k10 runs under **Xenia**, which recompiles the Xbox 360 (Xenos) GPU microcode into SPIR-V and
draws with a real GPU. RenderDoc captures that. So for any on-screen element you get, for the draw
that produced it:

- **The pixel shader** — the exact formula for the output colour and **alpha**.
- **The vertex shader** — how each vertex's position/colour/uv is computed, and *where it's fetched
  from* (the `xe_shared_memory` vertex-fetch = the uploaded copy of the IFF's geometry bytes).
- **The constant buffers** (`float_constants[...]`) — per-draw values the game uploaded.
- **The mesh data** — VS input/output, positions, per-vertex colour, indices.

Crucially, **Xenia uploads the IFF geometry to guest memory almost verbatim**, so the vertex bytes
you see in RenderDoc are (modulo endian) the same bytes sitting in `overlay_static.iff` blob0 (or
whatever IFF owns that asset). That's the bridge: **RenderDoc tells you the values → you fingerprint
those values in the file → you edit the file.**

---

## 1. The workflow (step by step)

1. **Capture the frame.** Run the game under RenderDoc (or inject), press F12/PrtScr on the frame
   that shows your target.
2. **Find the draw.** In the Event Browser, click draws until the Mesh Viewer / texture preview
   highlights your element. The Texture Viewer's "pixel history" (right-click a pixel → History)
   jumps you straight to the draw that wrote that pixel — fastest way to isolate a specific bar,
   number, or logo.
3. **Read the pixel shader** (Pipeline State → PS → Edit/View → Decompiled). Identify how the
   channel you care about is computed. For colour/alpha you're looking for the final write to
   `xe_out_fragment_data_0` (`.xyz` = colour, `.w` = alpha). Trace each input back to either:
   - a **float constant** (`float_constants[N]`) → a per-draw value (often computed at load, hard
     to file-edit; see §4), or
   - an **interpolator** (`xe_in_interpolator_k`) → comes from the **vertex shader**, i.e. from the
     **vertex buffer** → file-editable.
4. **Read the vertex shader** to see what each interpolator *is* and the **vertex layout**. Look for
   the `xe_shared_memory[... + index*STRIDE + field]` fetches — they tell you the stride and the
   byte offset of position / colour / uv within each vertex.
5. **Export the mesh** (Mesh Viewer → VS Output → right-click → Export to CSV). You now have, per
   vertex: the index (`IDX`), `gl_Position` (clip space), and each interpolator value. That's your
   **fingerprint**.
6. **Fingerprint-match into the file.** Decompress the owning IFF's DRAM blob (launcher
   `overlay_editor.load_dram`) and scan for the byte pattern that matches your exported vertices
   (colours, or positions, at the stride from step 4). A full multi-vertex fingerprint is almost
   always **unique**.
7. **Edit the byte(s)** and write back with `overlay_editor.apply_dram` (in-place, re-encodes +
   relocates, verifies round-trip). Keep a byte backup for reversibility.
8. **Relaunch.** Overlay/DRAM edits show on the next launch (live pokes don't render — the scene is
   uploaded once at load).

---

## 2. Worked example — the bottom cyan bar (alpha 1 → 0)

**Goal:** make the bar fully transparent (editing its colour only made it *black*, never gone).

- **Pixel shader** said: `output.rgb = c3.rgb * c4.rgb * interp.xyz`, `output.a = interp.w * c3.w *
  c4.w`. The constants `c3=(1,1,1,1)` and `c4=(0.174,0.764,1.0,1.0)` — so `c4.w`/`c3.w` are a
  hardcoded `1.0`, **not in any file** (that's why the whole-session alpha hunt via scene floats
  failed). But `output.a = interp.w * 1 * 1` = **`interp.w`**.
- **Vertex shader** said `xe_out_interpolator_0 = registers[1]`, and `registers[1]` is the
  **per-vertex RGBA colour fetched from the vertex buffer**: 7-dword (28-byte) vertex, colour dword
  at **+12** (ARGB byte order), so **`interp.w` = the vertex's alpha byte at offset +12**.
- **The "256-stride" index puzzle** = the VS `vertex_index_endian` 8-in-16 byteswap: file index
  `0x002E` (46) reads back as `0x2E00` (11776). So RenderDoc's `IDX 11776,12032,…` = file indices
  **46,47,48,…**. The bar = **indices 46..61** (16 verts) of a *shared* overlay geometry pool (NOT
  a named scene mesh — the reason `glow_cylinder_color` index/vertex hides were all no-ops).
- **Fingerprint match:** the 16 verts' colours from the VS-Output CSV were
  `(0,0,0,255),(0,0,0,0),(255,255,255,255),(255,255,255,0),…`. Scanning blob0 for that exact
  ARGB-@+12 sequence at stride 28 → **one** hit at file `0x215690` (= vertex idx46). Positions
  confirmed a thin bottom strip (Y≈−172, Z=2.894 flat, X +46…−234).
- **Edit:** zero the alpha byte (`base + k*28 + 12`, k=0..15). `apply_dram`. Relaunch → **bar gone.**

Shipped in the launcher as `scorebug_layout.set_teal_bar_hidden` /`set_bar_vertexalpha_hidden`
(reversible; self-relocates by the RGB+position fingerprint). Backup:
`scoreclock_barvertexalpha.json`.

---

## 3. Format facts you'll reuse (overlay geometry)

| Thing | Value |
|---|---|
| Overlay vertex stride (this pool) | **28 bytes** (7 dwords): pos.xyz @+0 (3×f32 BE), **colour @+12 (ARGB u8×4)**, dword4 @+16 skipped, uv @+20/+24 |
| Colour byte order | **ARGB** → alpha = colour dword's **first** byte (vertex offset **+12**) |
| Index format | u16 **big-endian**, `0xFFFF` = triangle-strip restart |
| Index endian trap | VS applies **8-in-16 byteswap** (`vertex_index_endian`): file `0x002E` → GPU `0x2E00`. RenderDoc shows the *swapped* value; divide by 256 (or byteswap) to get the file index. |
| Position | 3× **f32 big-endian** (model space, pre-transform; gl_Position is after the `float_constants[0..3]` matrix) |
| DRAM blob | `overlay_editor.load_dram(iff, gdir)` → decompressed `bytearray`; edit; `apply_dram(...)` to write back in place |
| Other overlay meshes | named meshes use different strides (0x14 glow, 0x18 flat quad — see `scorebug_layout._mesh_vertexbuf_range`). Always confirm the stride from *that draw's* vertex shader. |

**Ways to hide a mesh, cheapest first:**
1. **Zero per-vertex alpha** (colour dword's alpha byte) — clean transparency, keeps geometry.
   Requires the material to be alpha-blended (test: colouring it black leaves a visible bar → it
   blends).
2. **Collapse positions** (zero the pos.xyz of its verts → degenerate triangles) — works regardless
   of blend mode; guaranteed invisible.
3. **Zero the index buffer** (`_mesh_index_range`) — only if the draw consumes the *file* index
   buffer (many overlay draws regenerate indices → no-op; verify before relying on it).
4. **XEX draw-skip / constant patch** — last resort for fully runtime-generated draws.

**Ways to recolour / move / resize** (no RenderDoc needed once you know the record):
- Per-element transform+colour records: `overlay_editor` (RGBA @+0x30, X/Y @+0x1C, scale @+0x44).
- Scoreclock element X/Y/scale/font: `scorebug_layout` (already shipped).

---

## 4. Applying this to other targets

### Scoreclock font size / text elements
Already partly solved via the scene-graph route (`scorebug_layout`, joints @+0x1C, mesh vertex
translate). If a text glyph's size/position resists the scene route, RenderDoc it: find the text
draw, read the VS to see whether the glyph quad's size comes from a **vertex position** (→ scale the
verts in the file) or from a **constant** (→ it's driven by the joint/scale record). RenderDoc tells
you which, so you stop editing the wrong thing.

### The scoreclock body / other HUD meshes
Same as the bar: isolate the draw, read VS for stride + field offsets, export VS-Output, fingerprint
the verts (colour and/or position) into `overlay_static.iff` blob0, edit. Recolour = edit the colour
dwords; reshape = edit positions; hide = §3.

### Textures behind a UI element
If the pixel shader samples a texture (`tfetch`), RenderDoc's PS Resources tab shows the exact
texture + mip being sampled — cross-reference its dimensions/format against the launcher's texture
list to know *which* IFF record to replace (kills the "which of 4000 textures is this" problem).

### Player / goalie models (harder, but the same idea)
Player/goalie geometry lives in the model IFFs (packed/compressed — see doc on model_data; parser =
RE of mesh handler `0x5c369069` or a runtime VB capture). RenderDoc changes the game here too:
- It shows the **exact vertex stride + attribute layout** of a player draw (position/normal/uv/
  weights) from the VS — which is precisely what the offline model parser is missing.
- It shows the **draw's vertex buffer bytes**; matching a distinctive vertex run (e.g. a known
  landmark position) into the model IFF locates that mesh's VB in the file even without a full
  parser — the same fingerprint trick, just a bigger haystack.
- **Caveat:** player models are real 3D (skinned, indexed, multiple LODs) and their geometry may be
  GPU-fetched from a decompressed runtime buffer that differs from the on-disk packed form. Expect
  to combine RenderDoc (layout + live VB) with the runtime vertex-capture tool
  (`vertex_capture.py`) rather than a pure file fingerprint. Recolour/retexture is very doable;
  reshaping geometry is a project.

---

## 5. Tooling / repro

- Decompress + read a blob: `overlay_editor.load_dram("overlay_static.iff", gdir)`.
- Write back: `overlay_editor.apply_dram(dram, meta, iff, gdir, log)` (verifies re-encode; in-place
  if it fits, else relocates).
- Fingerprint scan pattern (Python): iterate `base` over the blob at 4-byte steps, test the
  multi-vertex byte pattern at `base + k*STRIDE + field`; a full mesh fingerprint is unique.
- Reusable bar functions: `scorebug_layout._bar_base` / `_bar_alpha_offsets` /
  `set_bar_vertexalpha_hidden` — copy the pattern for new elements.
- Session scratch scripts (this crack): `match_bar.py`, `confirm.py`, `apply_alpha.py`.

## 6. Gotchas

- **Live pokes don't render** — the overlay scene is uploaded once at load. Edit the file, relaunch.
- **Endian everywhere** — positions/floats are big-endian; colour bytes are ARGB; indices get an
  8-in-16 swap. When a value "isn't in the file," try the byteswapped form before concluding it's
  computed.
- **Constants vs interpolators** — if the channel you want traces to `float_constants[N]` and that
  value isn't in the file, it's assembled at load (often a hardcoded default like the bar's
  `c4.w=1.0`). Pivot to the **interpolator / vertex** path, which *is* file-resident.
- **Named-mesh hides can be no-ops** — some overlay draws don't consume the file index buffer and
  fetch from a shared pool; confirm via the VS before zeroing an index buffer.
- Keep byte backups; `apply_dram` re-encodes the whole blob, so always confirm it reported a
  successful round-trip.
