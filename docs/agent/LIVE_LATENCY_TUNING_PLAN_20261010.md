# Live: cut speech-to-subtitle delay by tuning the provisional hold and VAD boundaries (2026-10-10)

Author: Claude Code. Status: closed — neither stage shipped (see §6). Scope: timing constants in `config.py`
(`_Audio` VAD boundaries, `_Splitter.provisional_hold_seconds`) and the user's local dashboard
override file. No STT/translation provider, prompt, guard, or pipeline-structure change.

## 1. Evidence

### 1.1 Where the delay goes (4 live runs, medians; measured earlier this session)

speech start → chunk emitted ≈ 5.0 s (p90 8 s) · STT ≈ 1.2 s · translation 0.86 s (direct) or
3.1 s via provisional (includes the 1.75 s hold) · **speech start → first subtitle ≈ 8.0 s (p90 15 s)**.

### 1.2 Rejected alternative: ElevenLabs realtime streaming STT (experiment, 2026-10-10)

`scribe_v2_realtime` with VAD commit (0.6 s silence) on 287 s of the frozen talk+MV clip:
end-of-utterance → commit ≈ 1.0 s, but run-on speech produced 20 s and 27 s commits (speech start
→ final 21 s / 28 s, worse than today's hard max), the 178 s song produced **no text at all**
(empty commit every ~35 s; batch Scribe gets CER 0.615 on the same song), and it costs
0.39 vs 0.22 USD/h. Not pursued. Gemini 3.8 Flash vs DeepSeek on the same segments: some wins,
some new errors, +1.5 s per sentence → not adopted for live.

### 1.3 Provisional hold (live runs 2026-09/10 with telemetry, run_kind=live, 36 runs)

- Provisional is the main display path: 2401 requested, 1885 displayed;
  `stt_ready_to_subtitle_ms` p50 2704 / p90 2941 (≈ 1.75 s hold + ≈ 0.9 s API).
- `sentence_hold_shadow` outcomes (n=369): next STT chunk arrives after the hold start at
  p10 441 ms / p50 2820 ms / p90 9712 ms. Within 800 ms: 71 (19 %); within 1750 ms: 122 (33 %).
  So a 0.8 s hold fires before a merging chunk in ≈ 14 % more cases than today; those
  sentences show a provisional that the final revision then replaces (no loss, one extra
  provisional API call, a visible text revision).

### 1.4 VAD boundary shadow (same 36 runs, 5664 production chunks)

Production cuts: silence 2882 · soft_max_pause 2469 · hard_max 313; chunk length p50 6.4 s / p90 8.7 s.
`_VAD_BOUNDARY_SHADOW_CANDIDATES` (already recorded on every chunk):

| candidate | silence / soft / hard (adaptive) | mean saved | p50 | p90 | speech resumed ≤ 300 ms after a would-be silence cut |
|---|---|---|---|---|---|
| A | 0.75 / 5.5 / 8.0 (0.90 / 6.5 / 9.0) | 0.54 s | 0.18 | 1.50 | 434 (7.7 %) |
| B | 0.65 / 5.0 / 7.5 (0.80 / 6.0 / 8.5) | 0.93 s | 0.32 | 2.74 | 697 (12.3 %) |

B's savings split roughly half silence gate (0.90→0.65) and half soft/hard caps.

### 1.5 History that this plan must respect

2026-05-27 `7103bcc` shortened the caps to 5.8 / 8.0 ("feel late"); the same day `48ac088`
("Favor complete STT chunks") restored 6.5 / 9.0 and raised silence 0.75→0.90 because shorter
chunks hurt sentence coherence. That was with the older Groq Whisper path and before provisional
subtitles; the current path is ElevenLabs `scribe_v2` plus provisional/final revision. The
coherence risk is real, so the VAD change is gated on a quality A/B (§3), not adopted on latency
evidence alone.

### 1.6 Hidden override (must be handled or the change is a no-op)

Tauri's "Start Python" sets `LIVE_TRANSLATE_APPLY_DASHBOARD_CONFIG=1`
(`src-tauri/src/handlers/python.rs:92`), so `_apply_dashboard_overrides` applies
`logs/live_translate_config.json` `audio.vad_silence_sec` (0.9) and `audio.vad_max_speech_sec`
(6.5) on top of `config.py`, and `config_export` rewrites the file from the effective config on
each start. New `config.py` defaults for those two fields would never take effect in dashboard
launches. The file is gitignored, user-local data.

## 2. Change

Stage 1 (low risk, ships if §3 shows no regression in provisional churn beyond expectation):

- `_Splitter.provisional_hold_seconds`: 1.75 → **0.8**.

Stage 2 (ships only if §3's quality gate passes; otherwise Stage 1 ships alone):

- `_Audio`: promote shadow candidate B with silence rounded to the dashboard's 0.1 step
  (see §5 round 2) — `vad_silence_sec` 0.90→0.70, `vad_max_speech_sec`
  6.5→5.0, `vad_hard_max_speech_sec` 9.0→7.5, `vad_adaptive_silence_sec` 1.1→0.80,
  `vad_adaptive_max_speech_sec` 6.5→6.0, `vad_adaptive_hard_max_speech_sec` 10.0→8.5. Overlaps
  unchanged (regular 1.0, adaptive 1.2, silence 0.4). Update the stale comment above the caps.
- Local `logs/live_translate_config.json`: set `audio.vad_silence_sec` 0.7 and
  `audio.vad_max_speech_sec` 5.0 (only these two are dashboard-overridable), after the user's OK,
  so dashboard launches pick up Stage 2. Not committed (gitignored).
- Shadow candidates: leave the tuple unchanged in this change (B then records zero savings, A
  records nothing earlier). Re-pointing the shadow to a new counterfactual is a follow-up, not
  part of this card.

Non-goals: realtime STT, translation-engine change, semantic early cut, splitter
`min_wait`/`force_cut`/`pending_incomplete_timeout`, guard behaviour.

## 3. Pre-live validation (the user asked for testing before going live)

### 3.1 Replay A/B harness (scratchpad, not committed)

Runs the real in-process pipeline — `_VadState` (Silero) → `stt.start` (ElevenLabs `scribe_v2`)
→ `sentence_splitter.start` → `translator.start` (DeepSeek, provisional on) — with the audio
device replaced by a WAV feeder pushing 512-sample frames at real-time pace, the profile locked
via `profile_state.configure_source(..., mode="manual")`, scene/vision off,
`LIVE_TRANSLATE_RUN_KIND=benchmark` (excluded from live analyses). `config.cfg` is replaced
with a `dataclasses.replace`d copy before any pipeline module is imported, one process per
variant. A consumer drains `subtitle_queue` and records every `SubtitlePayload` with wall time.

Variants on identical audio: **baseline** (current), **hold** (Stage 1), **hold+B** (Stage 1+2).
Audio: a 10-minute talk segment of a 이세돌 member VOD (주르르 2023-07-08 저챗, 20:00–30:00) plus the
existing 109 s 랑코/SOOP talk clip. Cost ≈ 3 × 12 min of Scribe batch + DeepSeek ≈ US$0.15.

### 3.2 Metrics and gates

From the run's own runtime events (`audio` created_at − raw_audio_seconds as chunk speech start;
`stt` → `sentence` → `translation` / `provisional_translation displayed` by utterance ids):

1. Latency: speech start → first displayed subtitle and → final subtitle, p50/p90 per variant.
   Expect hold ≈ −0.9 s and hold+B ≈ −1.8 s at p50 vs baseline.
2. STT coherence: CER of each variant's concatenated accepted STT text against a single
   full-clip batch Scribe transcript of the same audio (best-context reference). **Gate for
   Stage 2:** hold+B CER not worse than baseline by more than 0.02 absolute.
3. Translation: sentences with no subtitle (lost), provisional `guard_rejected`, provisional
   revisions whose final text differs; side-by-side finals of baseline vs hold+B for manual
   review (Claude Code reads all; the user can spot-check). **Gate:** lost-subtitle count not
   higher than baseline + 2, and no systematic fragment-style translations in the review.
4. Run-to-run noise: STT/translation are nondeterministic; if a gate is borderline, rerun
   baseline once before deciding.

### 3.3 Unit tests

`tests/` assertions that pin the old constants (if any) are updated; run the config/VAD/splitter
suites and then the full suite (`python -m pytest -q` with `--basetemp`/`cache_dir` in the system
temp dir).

### 3.4 First live run after merge

Run `lat_shadow.py`-style analysis on the next live run (run_kind=live) and compare latency
and lost-subtitle rate against the 4-run baseline; roll back by reverting the constants (and the
two JSON fields) if quality visibly drops.

## 4. Assumptions

- The replay feeder at real-time pace reproduces splitter/provisional timing closely enough;
  network latency to ElevenLabs/DeepSeek varies between runs.
- Talk content dominates live use where latency matters; songs are unaffected by the hold and
  only mildly by B (more, shorter chunks).

## 5. Review log

### Codex plan review round 1 (2026-10-10): REVISE

Blockers (summary): (1) shadow `saved` is measured on production's own segmentation and cannot
predict the real trajectory (next chunk start, overlap, adaptive state), so it is directional
only; (2) Stage 1 has no measurable churn gate — extra provisional requests, visible revisions,
cancellations/stale display are unquantified; (3) concatenated-STT CER plus text review cannot
catch the 05-27 failure mode (fragmented sentences that change meaning); all three variants and
visible provisionals must be reviewed, with risky speech patterns and singing included; (4)
per-variant sentence latency compares different populations, and `created_at − raw_audio_seconds`
is a buffer start, not speech onset; lost-subtitle counts lack a common denominator; (5) the
harness must prove it reproduces display behaviour (draining `subtitle_queue` is not "displayed"),
avoid feeder drift, finish EOF tails, and borderline results need interleaved reruns.
Non-blocking: soft max is not a fixed length; short-utterance discards at 0.65 s; dashboard path
(Rust defaults 0.6/8.0, frontend `step=0.1` cannot hold 0.65); add behaviour tests, not just
constant updates.

### Claude Code revision (round 2)

1. **Shadow evidence demoted.** §1.4 is a directional hypothesis for choosing B; the expected
   gain is whatever the replay measures. No latency number from §1.4 is a gate.
2. **Silence 0.70, not 0.65.** `ConfigPanel.vue:139` uses `step="0.1"`; 0.65 would be an invalid
   input the moment the user edits the panel. 0.70 sits between A (0.75) and B (0.65). Adaptive
   0.80 / 6.0 / 8.5 unchanged (not dashboard-exposed). Rust fallback defaults (0.6/8.0, used only
   when the JSON is missing) are pre-existing and out of scope; the completion report records the
   effective limits read back from the running config.
3. **Harness now reproduces display.** It subclasses the production `SubtitleWindow` and runs the
   real `_poll` / `_accept_revision` / `_show` (min display time, pending overwrite, revision
   regression) with only Tk drawing replaced, so `displayed` / `final_revision_displayed` /
   `display_dropped` telemetry is produced by production code; every draw is recorded with its
   audio-timeline time. Feeder uses absolute monotonic deadlines (records max lag), feeds 3 s of
   trailing silence, waits for all queues to drain (≥ 15 s, covering the 8 s pending-incomplete
   timeout) plus the current min display time before stopping. Every pipeline module's `cfg` is
   asserted to be the patched object. Logs and `translations_*.txt` go to the scratch dir;
   `run_kind=benchmark`; the dashboard override env is removed so baseline = `config.py`.
   Smoke-tested on 40 s: provisional `displayed` and `final_revision_displayed` events appear.
4. **Common unit = reference words.** Each clip gets a full-clip batch Scribe transcript with
   word timestamps. For every reference word, the chunk whose new-audio span
   `[end − raw_audio_seconds, end)` contains the word start is followed through STT → sentence
   → first display (provisional `displayed` or final draw) and → final draw. Reported:
   word-weighted first/final latency p50/p90 (speech onset of each word, not buffer start) and
   **word coverage** (share of reference words that ever reached the screen; VAD discards,
   STT rejections, guard drops and cancellations all count as uncovered).
5. **Stage 1 gates (hold vs baseline, same clip):** provisional requests per sentence ≤ baseline
   + 0.25; visible revisions with changed text ≤ baseline + 10 percentage points of sentences;
   word coverage ≥ baseline − 1 pt; no increase in `display_dropped`/`cancelled_late` beyond
   run-to-run spread.
6. **Stage 2 gates (hold+B vs hold):** word coverage ≥ hold − 1 pt; CER vs reference ≤ hold
   + 0.02; song CER vs the official lyrics ≤ hold + 0.03; incomplete-sentence share and
   side-by-side review (all three variants, finals and visible provisionals) show no systematic
   fragment translations. The reference transcript is model output, not listening-verified;
   segments where variants disagree are listed with timestamps so the user can spot-listen.
7. **Material covering risky patterns:** 주르르 10-min 저챗 (long run-on turns, short replies);
   랑코 78 s (rapid storytelling, quoted speech); SOOP 31 s (multi-speaker game dialogue);
   고세구 MV 177 s (singing, official lyrics = ground truth).
8. **Noise control:** the 10-min clip runs twice in reversed order (baseline→hold→holdB, then
   holdB→hold→baseline); a gate decided within the two runs' spread is treated as borderline and
   reported to the user rather than auto-passed.
9. **Tests (§3.3):** existing VAD/splitter behaviour tests are run unchanged first; any assertion
   that pins the old constants is updated, and a config test pins the new values plus
   `vad_max < vad_hard_max` and adaptive ≥ base ordering.

### Codex plan review round 2 (2026-10-10): REVISE

B1 resolved. B2–B5 partially resolved; every remaining point concerned the scratch test tooling,
not the proposed change: visible revisions were inferred from `succeeded` text rather than real
draws; CER used translation `source_text` instead of accepted STT; a missing draw fell back to the
translation event time (counted undisplayed text as displayed); all words of a chunk were credited
to the chunk's earliest displayed sentence; drain only checked queue emptiness and stopped the
display before the pipeline's shutdown flush. **New blocker:** the `hold` variant raised
`unknown variant` (`elif` bound to the `holdB` branch). Non-blocking: shadow B (0.65) ≠ Stage 2
(0.70); sync gates into §3; report near-miss discards separately.

### Claude Code fixes after round 2 (tooling only; the proposed change is unchanged)

- Variant check fixed; smoke run of `hold` on 40 s passed (`valid: true`).
- Display identity: the headless window records, for every real draw, the payload's
  `subtitle_id`, revision, phase and path (pending vs in-place revision); `draws_unknown_identity`
  is reported. Visible revision = a sentence whose drawn provisional text differs from its drawn
  final text.
- No draw ⇒ not displayed. Final draws are consumed once each, in order, by exact text (finals
  without an id) or by provisional id (promoted finals); `emitted_not_drawn` is reported.
- Words in a chunk are split across that chunk's sentences in order by each sentence's share of
  the normalized source length; first/final latency report their own word counts.
- CER is computed on concatenated `stt.accepted_text` (overlap repeats included, equally for all
  variants) — against the full-clip reference, and against the official lyrics for the MV.
- Drain: quiet = no metric counter, draw or queue-size change for 12 s (cap 120 s ⇒ invalid);
  then pipeline `stop_event` + worker joins (shutdown flush), while the display runs on its own
  stop event until the queue is empty and nothing is pending. `valid` = no fatal, no drain
  timeout, no live worker thread; invalid runs are excluded.
- Discards (`discard_*_near_miss_overlap`, `discard_*_no_speech`) are reported per variant.
- §3.2's original gates are superseded by round-2 items 5–6 above.

Per the workflow, blockers remaining after round 2 go to the user; these were tooling defects
that are fixed above, and the harness/analysis code is included in the post-implementation review.

## 6. Replay A/B results (2026-10-10) and decision

All runs `valid` (no fatal, no drain timeout, feeder lag < 0.1 s, 0 unknown-identity draws).
Main clip 주르르 저챗 610 s, 648 reference words; two rounds in opposite order:

| variant | first p50 / p90 (s) | final p50 / p90 (s) | word coverage | lost sentences | STT CER | provisional / sentence | visible changed revisions |
|---|---|---|---|---|---|---|---|
| baseline r1 / r2 | 6.33 / 10.92 · 6.25 / 10.82 | 7.22 / 12.96 · 7.01 / 11.91 | 0.684 · 0.690 | 15 · 18 | 0.147 · 0.149 | 0.43 · 0.45 | 6.0 % · 2.9 % |
| hold r1 / r2 | 6.04 / 10.45 · 5.81 / 9.46 | 6.84 / 11.77 · 6.84 / 12.40 | 0.673 · 0.667 | 18 · 15 | 0.152 · 0.146 | 0.48 · 0.49 | 2.9 % · 4.3 % |
| hold+B r1 / r2 | 5.33 / 9.23 · 5.51 / 10.28 | 6.73 / 12.83 · 7.02 / 13.86 | 0.753 · 0.685 | 11 · 12 | 0.172 · 0.170 | 0.49 · 0.51 | 8.2 % · 6.8 % |

Short clips (랑코 78 s, SOOP 31 s) had 9 and 4 sentences — too few to gate; directions matched.
The MV (177 s singing) produced **no VAD chunk in any variant**: Silero classifies the song as
non-speech, so song behaviour is untestable here and unchanged by either stage.

- **Stage 2 (VAD B, silence 0.70) fails the CER gate**: vs baseline +2.5 / +2.1 points
  (vs hold, the round-2 comparison base, +2.0 / +2.4), mean +2.3 > 0.02, with no final-subtitle
  gain and more visible revisions. CER alone does not prove the fragmentation mechanism; it is
  consistent with the 2026-05-27 finding.
  Reverted; `config.py` comment records the evidence.
- **Stage 1 (hold 0.8 s) not shipped (user decision 2026-10-10)**: the test set already shows a
  missed gate, so the ~0.4 s gain is not worth it; `config.py` keeps 1.75 s. Measured: first-display
  p50 −0.4 s, p90 −0.9 s, final p50 −0.3 s; CER, provisional requests (+0.05/sentence) and
  visible revisions within run-to-run spread. Word coverage fell 1.1 / 2.3 points (mean 1.7),
  missing the 1-point gate. Per-word attribution (analyzer `uncovered`):

  | run | outside any chunk | chunk without sentence | sentence not emitted | emitted, not drawn |
  |---|---|---|---|---|
  | baseline r1 / r2 | 13 / 13 | 2 / 1 | 183 / 176 | 7 / 11 |
  | hold r1 / r2 | 13 / 13 | 1 / 1 | 198 / 187 | 0 / 15 |

  Uncovered words are 85–95 % "sentence not emitted" (output-guard rejection). The lost sets are
  largely the same name-heavy sentences in all four runs (12–14 of 15–18 contain 뢴트/렌트, 버건니,
  챠니, 이세돌, 릴파·이네 언니); the differences are a few borderline sentences whose STT text
  differs between runs (e.g. 왁물원 vs 왕물원). `display_dropped` 0/0 vs 0/0, `cancelled_late`
  1/1 vs 1/0. The hold changes neither the guard nor sentence cuts, so the coverage delta is
  attributed to guard variance on name-heavy sentences, not to the hold.
- Harness tail check (Codex post-implementation blocker 1): the shared-stop shutdown could in
  principle strand an in-flight STT result. Verified on every run: every non-discarded chunk has
  an STT event and every successful STT utterance reaches a sentence (0 orphans in 15 runs), so no
  result above lost tail text. For future runs the harness should wait for the STT worker and
  check empty upstream queues before stopping the splitter.
- Measured gain is smaller than the ~0.9 s the shadow suggested. The larger loss mechanisms found
  while testing are tracked separately: name guard (`LIVE_ISEGYE_NAME_GUARD_PLAN_20261010.md`),
  ElevenLabs hangs to the 15 s timeout, and Silero dropping loud/excited speech.
