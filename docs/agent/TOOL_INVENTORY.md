# Tool and Script Inventory

This is a binding routed extension of `AGENTS.md`. Read it completely before
proposing, adding, or substantially changing a script, harness, analyzer,
replay, benchmark, sampler, or maintenance command.

## Utility Script Inventory

Use this inventory to pick the right existing script before writing a new one.
The current inventory and path existence were checked on 2026-08-26. Individual
historical tools retain the evidence limitations documented below.
They are not all daily-use tools; prefer the "core" scripts first, and use the
"situational" scripts only when their specific preconditions fit.

Core scripts worth checking first:
- `scripts/replay_eval.py`
- `scripts/collection_sanity_report.py`
- `scripts/sample_labeling_cases.py`
- `scripts/labeling_review_server.py`
- `scripts/analyze_runtime_events.py`
- `scripts/analyze_latency_tail.py`
- `scripts/analyze_groq_error_bursts.py`
- `scripts/analyze_cache.py`
- `scripts/check_translator_core.py`
- `scripts/evaluate_translations.py`
- `scripts/suggest_corrections.py`
- `scripts/post_run_quality_loop.py` (snapshot-mutating by default; see the
  mutation boundaries in `docs/agent/VALIDATION.md`)
- `scripts/update_translation_profile_snapshot.py`
- `scripts/debug_pipeline.py`
- `scripts/export_chatgpt_bundle.py`: exports every persisted event for one
  exact `run_id` into a sanitized, portable ChatGPT Project bundle. The bundle
  keeps raw JSONL (deterministically split when needed), derived manifest/
  subtitle/audio indexes, and optional run-scoped retained WAV copies. It also
  accepts `--list-runs` for the dashboard selector. Historical config is not
  reconstructed: any included dashboard config is labeled as the latest
  unbound snapshot. Normal Python teardown invokes the same exporter
  automatically for its own run, without WAV copies; dashboard-forced shutdown
  exports its launcher-assigned run after the child terminates.

Situational scripts:
- `scripts/evaluate_translation_prompt_benchmark.py`: maintained offline scorer
  for the current 75-case production-derived semantic regression suite. Use
  `--production-runtime-baseline` for frozen traces or `--results` for a
  complete external result set. It has no provider execution or retired
  prompt/model experiment modes.
- `scripts/rebuild_semantic_quality_provenance.py`: fail-closed schema-v3
  runtime/STT/WAV provenance refresh for the already-frozen 2026-08-02
  annotation selections; `--check` is read-only, while `--output` writes the
  requested manifest path.
- `scripts/compare_stt_language_modes.py`: fixed-Korean versus auto-detect
  Groq STT on the same dumped WAVs; record-only, but calls Groq.
- `scripts/llm_quality_reviewer.py`: offline semantic-fidelity discovery and
  triage over current runtime-event translations. It reconstructs only prior
  published same-cohort history (explicitly approximate), retains immutable
  profile/activity/provider/attempt/publication evidence, combines deterministic
  hard signals with a strict structured semantic review, and emits ranked JSONL,
  Markdown, token/cost/latency metrics, and blinded bounded-contrast calibration.
  OpenRouter and Groq reviewer routes are supported; use `--dry-run` for no API
  call. This is not the frozen 75-case regression scorer and never promotes a
  reviewer suspicion into regression or production rules automatically.
- `scripts/download_audio.py`: useful only when preparing offline audio files
  from YouTube.
- `scripts/stt_completeness_check.py`: useful only when a matching Korean VTT
  reference exists for CER/coverage evaluation.

Runtime/log analysis:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\analyze_runtime_events.py `
  --events logs\runtime_events_YYYYMMDD.jsonl [logs\runtime_events_NEXTDAY.jsonl] `
  --top 10
```

Use this for broad runtime summaries: translation/STT/audio counts, audio
startup device/host/format/readiness (kept separate from chunk counts), status
breakdowns, latency, queue delay, retry/API diagnostics, dependency markers,
empty targets, flagged samples, circuit/probe fallback events, recent-STT-
context provenance, and per-run summaries. The provenance summary is backward
compatible: old included-context rows are counted as legacy/unavailable rather
than violations. Groq sources are joined only to earlier successful same-run
Groq STT events; non-Groq sources are counted separately because SenseVoice
successes do not currently emit equivalent STT runtime rows. Samples contain
IDs and numeric metadata only, never context text or a text hash. Always select
`--run-id` when a daily file contains multiple runs. Historical schema-v5 files
can also expose the retired `translation_model_shadow` Qwen-versus-Flash pair
summary: integrity,
independent success rates/delta, provider latency delta, prompt-cache usage,
cost/request and paired-audio-hour, QA/canonical/script rates, exhaustive
candidate-only regression IDs/categories, similarity, and bounded
disagreements. Multiple
`--events` paths are accepted so a cross-midnight run is not truncated.
Protected Flash-primary files additionally report
`api_diagnostics.deepseek_output_guard`: total Flash attempts, provider
failures, content-guard rate/reasons, successful Qwen continuity, bounded
candidate/selected samples, explicit all-attempt versus selected-attempt cost,
and any impossible selected guarded attempt. This
is the post-cutover owner; do not add a separate guard analyzer.

Cache analysis:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\analyze_cache.py `
  --db logs\live_translate.db `
  --top 10
```

Use this for cache inspection. It opens SQLite read-only and reports
engine/model/prompt buckets, daily inserts, top cache hits, output-length
outliers, and suspicious untranslated rows.

Translator maintenance checks:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\check_translator_core.py --skip-pytest
.\live-subtitle-env\Scripts\python.exe scripts\check_translator_core.py
```

`--skip-pytest` runs fast JSON fixture, translation-profile snapshot, and eval
case checks. Without `--skip-pytest`, it also runs focused translator-related
pytest files. Neither form runs `scripts/replay_eval.py`.

Frozen deterministic replay:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\replay_eval.py run `
  --snapshot data\replay_eval_snapshot.jsonl
```

Use this for policy filters, source normalization, target corrections, or name
rendering. The read-only `run` command must precede `--update`; snapshot update
means accepting every shown diff into the golden baseline.

Offline eval cases:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\evaluate_translations.py --json
```

Use this to validate `data/eval_cases.json` reference outputs or pass
`--results <json>` to evaluate a model/prompt output file against those cases.
The same evaluator loads `data/semantic_quality_eval_20260801.json`, the
43-case canonical view of the 47 supplied human annotations. With no
`--results`, that focused command evaluates each reference. Eight externally
supported cases currently have bounded expected/forbidden assertions; the
other 35 remain structural/reference checks. Supplying the captured
`current_output` values makes exactly those eight fail (35/43 pass), but the
tool does not run a translation model or establish that a prompt candidate
improved quality. The lossless source annotation file is
`data/manual_quality_annotations_20260801.json`; evidence provenance and
limitations are in `docs/agent/T25_EVIDENCE_20260802.md`.

The 2026-08-02 audio-retained intake is stored separately as
`data/manual_quality_annotations_20260802.json` plus the non-evaluator
`data/semantic_quality_evidence_20260802.json` provenance manifest. Validate
that provenance manifest with
`scripts/rebuild_semantic_quality_provenance.py --check`; do not route it
through `evaluate_translations.py`. The tool preserves frozen timestamp
selection decisions and rebuilds current/evidence attribution from schema-v3
runtime rows. Its `--output` mode is an explicit artifact mutation. The separate
`data/semantic_quality_eval_20260802.json` contains 16 reviewed bounded
contrasts: references pass 16/16 and captured current outputs fail 16/16.

The same batch has a frozen 21-case audio manifest and dual local-ASR outputs:
`data/t25_stt_replay_manifest_20260802.json`,
`data/t25_sensevoice_shadow_20260802.json`, and
`data/t25_faster_whisper_shadow_20260802.json`.
`replay_phase0_stt_candidates.py` keeps no-flag SenseVoice compatibility and
adds `--engine faster-whisper`. Both engines emit engine-neutral
`candidate_*` fields, while legacy SenseVoice and engine-specific aliases stay
available. Current and evidence audio must remain separately aggregated. These
outputs are secondary evidence, not reference transcripts or production
configuration. Details are in
`docs/agent/T25_DUAL_ASR_REPLAY_20260802.md`.

The bounded weak-signal follow-up uses
`scripts/evaluate_weak_signal_dual_asr.py` to build a deterministic 10+10
matched manifest from the four T25 run summaries and to compare the two
separately generated replay files. It extends rather than replaces
`replay_phase0_stt_candidates.py`; the replay driver now fixes
faster-whisper `temperature=0.0` and records Python/package/runtime versions.
Canonical artifacts are `data/t25_weak_signal_*_20260803.json`; interpretation
limits and hashes are in
`docs/agent/T25_WEAK_SIGNAL_DUAL_ASR_20260803.md`. Agreement and repetition
flags are triage signals, not correctness labels.

The existing collection/sampling tools still reject schema-v3 input because
they target schema v2, so they cannot certify this batch as sampling-ready
without a separately reviewed upgrade.

Translation profile snapshot:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\update_translation_profile_snapshot.py --check
.\live-subtitle-env\Scripts\python.exe scripts\update_translation_profile_snapshot.py
```

Use `--check` in validation. Run without `--check` only after intentionally
editing `data\translation_profiles.json`; it updates the hash in
`tests\test_translation_prompts.py`.

Pipeline debugging:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\debug_pipeline.py --stage split "<korean text>"
.\live-subtitle-env\Scripts\python.exe scripts\debug_pipeline.py --stage translate --no-api "<korean text>"
```

Use this to test sentence splitting or translator prompt construction while
skipping audio/STT. `--no-api` is safe and does not call translation providers;
without `--no-api`, translate/all stages may call the configured translation API.

Audio/STT evaluation helpers:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\download_audio.py <youtube-url> --out audio\sample.wav
.\live-subtitle-env\Scripts\python.exe scripts\stt_completeness_check.py `
  --audio audio\sample.wav `
  --ref path\to\sample.ko.vtt `
  --duration 60
```

For retained Phase 0/T25 chunks, prefer
`scripts/replay_phase0_stt_candidates.py` rather than adding another replay
driver. It supports `--engine sensevoice|faster-whisper`; run each engine in a
separate process and store new experiment output under `scratch/analysis`
until its inputs, settings, and limitations are reviewed for preservation.
The optional ASR dependencies may live in an external disposable environment;
do not add them to production requirements solely to run offline evidence.

`download_audio.py` uses `yt_dlp` and network access. `stt_completeness_check.py`
computes CER/coverage against a reference VTT; local SenseVoice is default,
while `--engine groq` calls the STT provider. It may need optional audio/STT
dependencies such as `soundfile`, `editdistance`, and `librosa`.

Removed legacy/ad-hoc scripts:
- `scripts/check_db.py` was removed; use `scripts/analyze_cache.py`.
- `scripts/database/check_db_schema.py` was removed; it was an ad-hoc schema
  dump and failed on Windows console Unicode output.
- `scripts/database/check_token_len.py` was removed; it had an old
  `scripts\logs\live_translate.db` path assumption.
- `scripts/database/clean_db.py` was removed; it was destructive and had the
  same old DB path assumption.
- `scripts/prompt_tester.py` was removed; it was a legacy Anthropic
  prompt-variant experiment with stale static variants.
- `scripts/compare_prompt_variants.py`, `scripts/benchmark_nvidia_models.py`,
  and `scripts/evaluate_model_context_isolation.py` were removed after their
  model/prompt comparison decisions closed. Historical result artifacts remain.
  The current 75-case evaluator is retained separately in simplified form.

Current tool limitations:

- `replay_eval.py` is not part of `check_translator_core.py` or CI.
- Replay cases do not yet retain STT evidence or neighboring context.
- Collection/sampling tools still target runtime schema v2 while the live
  writer emits v5.
- `frontend-design.md` is a hybrid tutorial/spec and contains older layouts;
  actual Tauri/Vue source wins.

These are facts to route future work, not automatic authorization to modify
unrelated docs or code.

Repo hygiene:
- `config.py` may contain the user's local `streamer_profile` change. Do not
  stage or commit it unless explicitly asked.
- `OPTIMIZATION_*.md`, `.pytest-*`, and local review/planning files generally
  stay untracked and unpushed unless the user explicitly says otherwise.
- Keep `AGENTS.md` as the short global workflow/router. Long-term project
  context, validation detail, tool inventory, and optimization history belong
  in their owning `docs/agent/*.md` routed file.
