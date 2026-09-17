# 19 — Speech Relocation / Voice Replacement Pipeline

*Written 2026-07-31, from the Atlanta Thrashers → Winnipeg Jets and Phoenix/Arizona Coyotes → Utah
Mammoth job. Scripts archived at `scripts/relocation/`, the job's rule table and per-take plan at
`data/relocation/reloc_plan.json`.*

The job: retire two dead franchises everywhere the game SAYS them — play-by-play, colour, PA,
crowd chatter — by regenerating each affected take in the original speaker's voice with the new
name, and writing it back into the audio store in place.

This document is the recipe for doing it again for a different substitution (another relocation, a
player name, a re-voice), and an honest account of what generalises and what does not.

---

## 1. Shape of the pipeline

Seven stages. Only stage 2 is franchise-specific.

| # | Stage | Script | Generic? |
|---|-------|--------|----------|
| 1 | Find affected takes | `reloc_asr.py` | yes |
| 2 | Rule table + per-take edit | `reloc_plan.py` | **no — needs review** |
| 3 | Repair mis-cut references | `reloc_fix.py`, `reloc_reftrim.py` | yes |
| 4 | Route each take to a voice | `reloc_voice.py`, `reloc_route.py` | yes, given trained models |
| 5 | Generate (F5 → RVC → master) | `reloc_gen.py` + the two servers | yes |
| 6 | Gate, then re-roll failures | `reloc_verify.py`, `reloc_retake.py` | yes |
| 7 | Install with backup | `reloc_install.py` | yes |

Final numbers for this job: 436 takes scoped, 429 generatable, 393 actually run (33 PA takes held,
see §6), 40 min wall-clock on a GTX 1070 across two shards.

---

## 2. Finding the takes — the coverage trap

Take names are truncated CamelCase (`PxP_TS_AtlantaWinsTheDrawOn`), so a name scan finds most
affected takes but **cannot** find one whose team word fell off the end of the name. Closing that
gap needs an ASR sweep of every long take (~11,000 takes over 4 s, ~2–3 h CPU) — **still open for
this job.**

**Do this differently next time.** The naming work already produced ASR transcripts for ~81,000
takes (see doc 04 §8–9). Build a searchable transcript index ONCE and every future "find every line
that says X" is instant instead of a 3-hour sweep. This is the single highest-leverage piece of a
future tool.

## 3. The rule table is not fully automatable

Mechanical substitutions (Thrashers→Jets, Coyotes→Mammoth, Phoenix→Utah) are a string rule and the
tool can apply them unattended. What it cannot do is know that Winnipeg is not in the south:

    "Welcome to Atlanta, the heart of the south, where the Thrashers call Philips Arena home"
 →  "Welcome to Winnipeg, the heart of the prairies, where the Jets call Canada Life Centre home"

Arena names, geography, nicknames, and division references all need judgement. The plan format
carries this: `status: "auto"` is a machine substitution and may be re-derived at will;
`status: "approved"` is a hand-written line and **must never be re-derived from a fresh transcript**
— it was written against the audio, not against the ASR text. A future tool needs a review grid,
not a button.

Also expect withdrawals: five French PA lines were dropped once their "transcripts" turned out to be
Whisper's English guess at French ("Booted Thrashers, my cave").

## 4. Reference clips — three real defects, all found the hard way

Each take is its own F5 voice reference, so a bad reference poisons the clone. Three problems:

1. **Some store takes hold more than one take.** `internal_starts` (packet indices) marks the
   boundary; `reloc_fix.py` trims there. A packet is ~0.17 s, so the estimate is coarse.
2. **The trim landed inside the FIRST take** on 10 of 19, so F5 was cloning an abrupt delivery.
   Fixed in `reloc_reftrim.py`: move the cut to the quietest 20 ms frame in a window AROUND the
   estimate — **symmetric**, because several cuts land *late* and catch the next take's onset
   (`"...STOPPED! OH-"`) — widening 0.17→0.34→0.51 s until the trimmed tail measures ≤ −12 dB.
3. **Spurious boundaries.** A start leaving <4 packets is noise. Beyond that guard, where no window
   clears the threshold, a span whose full transcript is ONE unterminated sentence is a mis-split and
   must not be trimmed at all. That punctuation test is what separates "two butt-jointed takes"
   (a full stop partway through) from "the detector fired inside one line" — silence detection does
   not work here, the takes are butt-jointed and room tone sits above −45 dB.

Result: 19/19 references transcribe complete lines; 17 clear −12 dB, and the two that don't end on a
complete sentence butted against the next take, which is correct.

## 5. Generation — the two bugs that cost a whole batch

F5 renders into a fixed budget, it does not stop when the sentence is done:

    duration = ref_audio_len + ref_audio_len * (gen_chars / ref_chars) / speed

That is a character-count estimate with no headroom, and every substitution here keeps or shortens
the character count — so the budget lands at or below the original and the take is cut off at full
amplitude. **90 of the first 157 takes truncated.**

- `--fix_duration` replaces the estimate with an explicit total. **`--speed` is a DEAD LEVER under
  `fix_duration`** — F5 only applies speed by dividing the estimate, and that branch is skipped. Fold
  any slowdown into the budget instead or it silently does nothing.
- More budget is WORSE beyond a point. What cures truncation is having an explicit target at all.
  `BUDGET = 1.35`, `TEMPO_CAP = 1.25`.
- **F5 prepends ~0.4 s of dead air.** Trimming only the tail left that lead in place; on a 2.01 s
  take that is 20% of the slot, so the atempo fit hit its cap and crushed the line. Fix: strip
  whatever lead F5 invented, then pad back exactly the ORIGINAL's lead, so timing relative to the
  slot is the original's and not F5's.

Then RVC (only where a model exists for that speaker), then −3 dB master to 48 kHz mono.

## 6. The gate — and the two things it could not see

`reloc_verify.py` is the gate, not a formality: F5's characteristic failure is confident,
well-voiced audio containing the WRONG WORDS. It checks the changed words specifically (full-string
equality fails on good takes, since Whisper paraphrases punctuation), fuzzy-matches leftovers
(Whisper spelled one "Threshers", which a literal list would have passed), and then checks the audio:

- tail energy **relative to the take's own original** — some shipped takes end at full amplitude by
  design and must keep doing so; a fixed threshold flagged 11 good takes while missing a bad one
- the opposite defect: a stitch fragment given a resolved ending it must not have
- duration vs the original
- **the applied atempo factor** — a rushed take decays perfectly normally, so no measurement of the
  finished audio detects it; only the factor that was applied does. Record it during generation.
- **lead-in alignment** and **last-word-to-end** (via word timestamps), judged against whether the
  original has trailing air at all

**Two failure modes the gate never caught, both found by ear:**

1. The rushed-not-chopped truncation above — now covered by the tempo/lead/trailing checks.
2. **A speaker with no trained model.** The PA takes had no PA-announcer RVC model, so they fell
   through to F5-only, and F5 cloning a stadium announcer off two seconds of his own reverb sounds
   nothing like him. Every acoustic check passed. **A future tool must refuse to generate for a
   speaker with no model rather than silently degrade.** Those 33 takes carry
   `hold: pa_voice_model_not_trained` — a field SEPARATE from `status`, so they keep their review
   state and resume with no re-review once the model exists.

Failures re-roll rather than get rewritten: F5 is stochastic (the same take measured 202 Hz and
264 Hz on two runs) and roughly one roll in six garbles a word. Respelling was measured and lost —
plain "Utah" beat "Utaw", "Yoo-tah", "Yutah", "Youtah".

## 7. Speed — resident model servers

The job is NOT GPU-bound in CLI form: the card sat at 1–2% while every take paid ~18 s to load F5 and
~17 s to load RVC in a fresh process, against ~2 s of actual work.

`reloc_f5_server.py` (F5 venv) and `reloc_rvc_server.py` (Applio env) keep the models resident and
answer one JSON line per take. F5 and Applio live in different virtualenvs, so this is two processes,
not one import. Two gotchas:

- **fd 1 must be dup'd aside and pointed at stderr before any import.** torch, hydra and Applio all
  print to stdout and would corrupt the protocol; redirecting at the Python level does not cover
  child processes.
- **A script run by absolute path does not get the cwd on `sys.path`.** The RVC server must
  `sys.path.insert(0, os.getcwd())` to find `core`.

The RVC server hands argv to Applio's own `core.parse_arguments()`/`core.main()`, so every default is
resolved by the code the CLI uses. The F5 server has to MIRROR `infer_cli`, so it imports every
sampler default (`nfe_step`, `cfg_strength`, `sway_sampling_coef`, `target_rms`,
`cross_fade_duration`, `speed`) from `utils_infer` rather than restating them — a wrong value would
shift the voice across the whole batch, silently.

**A/B it before trusting it.** Byte-equality is impossible (no seed is set, deliberately, because
that is what makes re-rolling work), so `reloc_ab.py` rolls each take N times on each path and
compares the CLI-to-server distance in `reloc_voice`'s feature space against the CLI's own re-roll
spread. Ratios came out 0.90–1.12 — indistinguishable from a re-roll. Result: 2.5× overall, 6× on
short F5-only takes, and the GPU then sits at 99%, so two shards is the right number on 8 GB.

## 8. Install

`reloc_install.py` writes into `Audio\Extracted` in place (the store is edit-in-place with a sha1
dirty bit; Patch Game picks up anything whose hash differs) and backs up every original first. Two
guards, both learned the hard way: a take whose `status` was withdrawn AFTER generation still has
passing audio sitting in `_results.json` and would otherwise install, and the same is true of a
`hold`.

---

## 9. If this becomes a tool

Build it as a **tab in the Mod Launcher, not a separate app** — the audio store, the sha1 manifest,
Patch Game and the transcripts already live there, and a second app would fight it for the same
files. Suggested shape:

1. A transcript index (§2) — do this first, it unlocks everything else.
2. A job spec: find rules + substitutions + voice routing, saved as JSON (the existing plan format
   is close).
3. A review grid before generation — `auto` rows pass through, rows needing a flavour rewrite are
   flagged for a human.
4. Generate → gate → retake, unchanged.
5. Dry-run, then install with backup.

Carry these constraints across:

- Refuse to generate for a speaker with no trained model (§6).
- Keep `status` and `hold` separate.
- The tuned constants (budget 1.35, tempo cap 1.25, index_rate 0.6, the abruptness thresholds) were
  calibrated on Randy Hahn and this corpus. Expose them **per voice**; a new model likely needs a
  re-sweep.
- A player-name job has one extra piece this one did not: the roster's UTF-16BE name pool must stay
  in sync with the audio, or the scoreboard and the commentator disagree. The audio side is easier —
  the name-audio ID fields (player record +0xB3 / +0xB1) are already mapped.
- "Re-voice with a new model" is nearly free today: it is a `.pth` path change plus a re-run.
