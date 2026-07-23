# 02 — The 0x0E4837C3 compression codec

One-line summary: Almost every IFF payload in NHL 2K10 is wrapped in a custom in-engine **flag-byte LZ77** codec (magic `0x0E4837C3`); the LZ window width is a per-blob header field (`wparam`), and re-encoding must match it or the blob inflates and breaks in-place replacement.

Status: **verified.** The format is fully solved: the decoder byte-exactly matches the game's live VRAM, the encoder round-trips, and file-based texture modding via re-encoded blobs works in Xenia. The one-line corrections to earlier (wrong) theories are called out inline.

---

## What it is (and isn't)

The `0x0E4837C3` sub-header is **not** LZX, not Xbox `XMemDecompress` (ordinal 437), not zlib/lzma/lz4/zstd. Standard decoders fail because the data is not Huffman-coded at all (LZX pre-trees come out with Kraft sum > 1.0, i.e. impossible). It is a **custom flag-byte LZ77** implemented in the game engine.

The engine path is fully named in Ghidra (clean XEX project):

- Entry: `VCFILEDEVICE_ReadAndDecompress` @0x84150210 (a VCFILEDEVICE vtable method, invoked virtually; falls back to `VCFILEDEVICE_ReadRaw` @0x84130C20 when the magic is absent).
- Dispatcher: `VC_Decompress` @0x8414BDD8 — reads the 20-byte header, checks `magic == 0x0E4837C3`, then `switch(methodByte) case 1..15` into `VCDecompress_Codec1 .. VCDecompress_Codec15` (`@0x841478E0 … 0x8414B800`; codec 7 = `@0x84149400`, codec 8 = `@0x84149880`).
- Input refill: `VCDecompress_RefillInput` @0x8414C1A8 → `VCFILEDEVICE_ReadRaw`.

In real data only **codec 7 and codec 8** ever appear, and both decode with the same core algorithm.

---

## Header (20 bytes, big-endian)

```
[0x00] magic       = 0E 48 37 C3   (BE32)
[0x04] decomp_size (BE32)   — output size; decoder stops here
[0x08] total_size  (BE32)   — compressed payload + 20-byte header
[0x0C] codec       (BE32)   — 1..15 switch selector; only 7/8 seen in real data
[0x10] wparam (wp) (BE32)   — the LZ WINDOW in bits (the field that actually matters)
```

Compressed data starts at byte 20. Blobs are packed **back-to-back** — the next blob's magic is exactly at `offset + total_size`. (This is what lets a packed multi-blob resource be walked sequentially.)

The encoder writes exactly this via `struct.pack('>IIIII', 0x0E4837C3, decomp_size, 20+len(stream), codec, wparam)` — see `encode_e4837_lazy.py:encode_payload`.

---

## Algorithm

A stream of blocks, each starting with a 1-byte **flag**:

- **flag == 0** — fast path: copy the next **8 bytes literally** (advance input by 9).
- **flag != 0** — the flag is a bitmask, **bit 0 = LSB first**, describing the next 8 tokens:
  - bit `0` → **literal**: read 1 byte, write it.
  - bit `1` → **match**: read a **BE16** token, then
    ```
    offset = token & ((1 << wp) - 1)      # low wp bits — back-distance
    length = (token >> wp) + 3            # remaining (16-wp) bits, +3
    ```
    copy `length` bytes from `output[-offset]`.

Match copies are performed **8 bytes at a time** internally (a PPC/AltiVec alignment artifact). This only changes behavior for `offset < 8`; the canonical decoder replicates the 8-byte-chunk copy exactly.

---

## `wparam` (wp) is the offset width — NOT `codec`

This is the single most important correction for collaborators:

> **Correction.** Earlier notes claimed "codec 7 = 7-bit offset, codec 8 = 8-bit (or 9-bit) offset." **That is wrong.** The offset width is set by the **`wparam` (wp)** field, per blob — *not* by the `codec` field. A `codec=7` blob with `wp=13` decodes as **13-bit** offsets. The `codec` byte is just the 1..15 dispatch selector into `VCDecompress_CodecN` and does not by itself define the offset mask.

`wp` sets **both** the window size and the maximum match length, a trade-off the cooker tuned per blob:

```
window    = 1 << wp                 # how far back a match can reach
max_match = (1 << (16 - wp)) + 2    # the (16-wp)-bit length field, +2 (since +3 - 1)
```

Observed values in real assets:

| Asset | codec | wp | window | notes |
|-------|-------|----|--------|-------|
| menu logo (`0B@0x3FB76080`) | 8 | 9 | 512 B | plain DXT5 256×256 at decompressed `[0:65536]`; long matches |
| `led` / `arena_presentation` | 7 | 10 | 1024 B | |
| `overlay_static` VRAM | 7 | 13 | 8192 B | short matches, big window |

> **Two more corrections from the menu-logo work.** (1) The menu logo was briefly thought to be a **GPU-rendered / SDF** texture that "matches no standard format" — false. It is **plain DXT5 256×256** (gto-tiled) at decompressed bytes `[0:65536]`; the earlier confusion was purely the codec bug (decoding a 9-bit-window blob as 8-bit) garbling the pixels. (2) A disassembly read that the codec used `off & 0xFF / >> 8` was wrong for that blob — when in doubt, brute-force `(off_mask, len_shift)` against ground-truth **live VRAM**, which is the authoritative check.

---

## Encoding / re-encoding rules

Canonical tools (keep the project-root and `launcher/` copies in sync — they have diverged before):

- **Decode:** `launcher/decode_e4837_fixed.py` → `decompress_codec(stream, decomp_size, off_mask, len_shift)` with `off_mask = (1<<wp)-1`, `len_shift = wp` (e.g. `wp=13` → `0x1FFF, 13`).
- **Encode:** `launcher/encode_e4837_lazy.py` → `encode_payload(data, wparam=wp, codec=codec)`.

Rules that must be followed for in-place replacement to work:

1. **Re-encode at the blob's NATIVE `wp`, preserving its `codec`.**
   > **Correction.** An earlier encoder **hardcoded `wp=9`** and wrote `wp=9` to the header regardless of source. A smaller window than the original cannot reach the long-range matches the cooker used, so the blob **inflates** — a `wp=13` blob re-compressed at `wp=9` grows ~**13.8% even on byte-identical data**, overflowing its slot ("edit too large", no in-place edit possible). It also silently corrupted `wp=10` assets (`led`, `arena_presentation`): the decoder reads a 10-bit offset out of a 9-bit-packed stream and mismatches at byte 8. The fix parameterizes `encode_stream(data, wp, max_tries)` (`WIN=(1<<wp)-1`, `MAXM=(1<<(16-wp))+2`, `<<wp` packing) and preserves the source `wp`/`codec`. `archive_textures.py` reads the source blob's `wp`/`codec` via `_walk_blobs` and passes them through.

2. **The header `wparam` MUST equal the window actually used to encode.** The game decodes the offset width straight from the header `wp`; a mismatch decodes garbage.

3. **Emit `offset >= 8` only.** Small offsets (1–7) would read the game's **non-zero-initialized** output buffer (our decoder zero-inits), producing artifacts in-game even when the round-trip looks clean. `offset >= 8` reads only already-written bytes, so it is decode-identical to the game. (This is also why the fast encoder can validate matches by a direct self-referential compare on `data` instead of simulating the 8-byte chunk copy: for `off >= 8`, source `[q-off : q-off+8]` ends at or before `q`, so a chunked copy equals a plain byte copy.) The encoder enforces `if off < 8: continue`.

4. **Bigger windows need a deeper match search.** `encode_payload` auto-selects `max_tries` via `_auto_tries(wp)` = **4096 chain tries for `wp >= 11`** (needed to actually reach far matches) else 256. A multi-MB `wp=13` blob takes ~20s; small windows stay sub-second. (The 2026-06-18 lazy-matching rewrite made this practical: logo 23.2s→0.31s, uniform ~8min→8.5s, output only ~0.4–0.5% larger.)

### In-place replace pipeline (works, verified in Xenia)

```
decode blob at its (off_mask, wp)                          # decode_e4837_fixed
edit the decompressed payload (e.g. DXT5 at blob[0:65536])
enc = encode_payload(new_data, wparam=src_wp, codec=src_codec)
assert len(enc) <= original_total_size                     # must fit the slot
_verify_blob(enc)                                          # decode enc by its own header wp; must round-trip
splice enc over the original bytes, zero-pad to original total_size
```

`_verify_blob()` decompresses the freshly encoded blob using **its own header `wp`** and **aborts the replace if it doesn't round-trip**, so a bad blob can never reach the shipped archives. Keep `total_size` ≤ original and zero-pad; multi-texture / stored-offset packs are **in-place only** (they must not relocate).

---

## Section payloads after decompression

- **VRAM (`411536D5`)** decompresses to Xenos-tiled DXT (BC1/BC3). To turn it into an image: untile the 8×8-block Morton/Z-curve tiling, byte-swap each LE16 color word (big-endian Xbox → little-endian DXT), and skip the leading resource-header zero padding. See `decode_dxt_to_png.py` for the untile logic.
- **DRAM (`BB05A9C1`)** decompresses to serialized game structures: small DRAMs (88 B–10 KB) are object counts / scene data; large DRAMs (240 KB–6 MB) are player/arena records or XMA2 audio. A texture's DRAM section holds the descriptor (width/height/format/offset) that indexes into the paired VRAM.

---

## Open questions / caveats

- **The `codec` 1..15 dispatch is only partly exercised.** Only codec 7 and 8 appear in real data. `decode_e4837.py` historically observed raw codec-field values like 27 / 264 / 30889 and used an odd→7 / even→8 heuristic, whereas `VC_Decompress` is a clean 1..15 switch — likely a header-field-masking nuance. If a blob with an unusual codec value ever appears, confirm which byte `VC_Decompress` actually switches on. In practice `decode_e4837.py` decoded all 4,349 `0x0E4837C3`-headered chunk files with zero failures, so this has not bitten anyone yet.
- **`wparam` is described as "unused by the decompressor" in one old comment — that is wrong.** `wparam` *is* the offset width; treat it as load-bearing.
- **Duplicate-file footgun.** `encode_e4837_lazy.py`, `decode_e4837_fixed.py`, and `encode_dxt5.py` exist at BOTH the project root and `launcher/`. When running from source the root copy wins (`_PROJ` is first on `sys.path`); when frozen (PyInstaller) the bundled `launcher/` copy wins. They have silently diverged after editing only one. **Keep both copies in sync.**
- **Live edits don't display.** Writing decompressed VRAM into a running Xenia via Cheat Engine does not change what's drawn (Xenia caches a decoupled host texture). Modding must be file-patch + reload.
