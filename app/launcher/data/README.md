# launcher/data/ — shipped runtime data

Everything the launcher needs at runtime lives here, resolved through
`launcher/resources.py::data_path()` (from source: this folder; in the built exe:
`sys._MEIPASS`, since the build is one-file). **The launcher never reads data from the
project root** — a past cleanup that moved a CSV out silently broke portrait decoding, which
is why these are all consolidated here and the `.spec` **hard-aborts the build** if any
required file is missing.

## Files

| File | What it is |
|------|-----------|
| `team_iff_catalog.csv` | Per-IFF texture index — the catalog the IFF Textures tab reads (teams × categories). |
| `discovered_assets.csv` | The TOC sweep result: maps synthetic `disc_<crc>` names → real archive assets (crc-alias). Needed to resolve the ~1147 assets not in the main catalog (e.g. portraits). |
| `global_iff_runtime_map.csv` | Runtime offset map for loader-repacked/global IFF packs. |
| `portrait_key_map.json` | Player photo-key ↔ portrait-blob mapping (see `findings/06`). Shipped precomputed. |
| `jersey_map.json` | 224 jerseys / 563 assets, content-hashed — which texture members share one edit (see `findings/08`). |
| `fe_components.json`, `fe_uniform_map.json`, `frontend_logo_tile_map.json` | Front-end / menu asset maps (see `findings/08`). |
| `team_fields.json` | Editable labels for the Teams tab's per-team record-field grid. |
| `audio_authored_names.json` | 162 cracked authored audio names + tags (see `findings/04`). |
| `audio_bank_refs.json` | Audio bank → stream reference map. |
| `face_parsing_resnet18.onnx` | **Optional** face-parsing model (BiSeNet/ResNet-18) used to cut out the head/face when compositing portraits and jerseys. If absent, the launcher falls back to a heuristic alpha-anchored cutout — lower quality but functional. Loaded via `onnxruntime`. |

## Regenerating

Most of these were produced by the reverse-engineering scripts described in `findings/`
(the TOC sweep, the crc dictionary attack, the live-capture catalog, the portrait-key
inversion, etc.). They are checked in precomputed so a fresh clone runs immediately.

**Keep the required-file list in sync** with `resources.py::REQUIRED` and the `_required`
list in `nhl2k10_launcher.spec` — the build enforces it.
