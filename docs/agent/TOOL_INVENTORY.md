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
- `scripts/analyze_runtime_events.py`
- `scripts/analyze_forensics_bundle.py`
- `scripts/suggest_identity_ocr_aliases.py`: mines only persisted calibrated
  identity-ROI observations, ranks repeated exact OCR strings against reviewed
  member aliases, and emits advisory JSON/Markdown candidates with collision
  margins. It never activates an alias or modifies the registry; every
  candidate remains manual-review-only.
- `scripts/check_translator_core.py`
- `scripts/evaluate_translations.py`
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
- `scripts/analyze_forensics_bundle.py`: validates manifest/file hashes,
  request-contract references and internal component hashes, then emits an
  evidence-only audio-to-publication causal chain for every translation event.
  Missing or ambiguous lineage stays unresolved. `--json-output` writes the
  machine report and `--report-output` writes compact Markdown; exit code 2
  means an integrity error was found.
- `scripts/replay_translation_contract.py`: offline-only v2 translation
  contract reconstruction and provider-body hash comparison. Selects one
  `translation_request_id` in runtime JSONL and requires its complete route
  event set; v1 contracts expose recorded messages while unavailable options
  and body bytes remain unknown. It has no provider send mode.

Identity OCR alias candidate report:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\suggest_identity_ocr_aliases.py `
  "logs\runtime_events_*.jsonl" `
  --json-output scratch\analysis\identity_ocr_alias_candidates.json `
  --report-output scratch\analysis\identity_ocr_alias_candidates.md
```

Only `profile_resolution` observations with
`identity_authority=calibrated_channel_identity_roi` and a recorded identity
read are counted. Resolved input paths are deduplicated before counting.
Repeated observations in one run may reach `review`; `run_count` remains
separate so a human reviewer can distinguish recurrence across sessions. A
`review` row is still only an advisory candidate, never activation evidence.

The next blind semantic pass is not another repository script. It uses the
designated ChatGPT Project through computer use, following
`docs/agent/BLIND_PHASE1_WORKFLOW.md`. Its input must be the exact bundle from a
user-run natural `python main.py` session; do not collect an external stream as
substitute media.

Situational scripts:
- `scripts/evaluate_translation_prompt_benchmark.py`: maintained offline scorer
  for the current 75-case production-derived semantic regression suite. Use
  `--production-runtime-baseline` for frozen traces or `--results` for a
  complete external result set. It has no provider execution or retired
  prompt/model experiment modes.
- `scripts/download_audio.py`: useful only when preparing offline audio files
  from YouTube.

Runtime/log analysis:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\analyze_runtime_events.py `
  --events logs\runtime_events_YYYYMMDD.jsonl [logs\runtime_events_NEXTDAY.jsonl] `
  --top 10
```

Bundle causal validation:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\analyze_forensics_bundle.py `
  scratch\chatgpt_bundles\chatgpt_bundle_RUN_ID `
  --json-output scratch\analysis\RUN_ID.forensics.json `
  --report-output scratch\analysis\RUN_ID.forensics.md
```

Use this for broad runtime summaries: translation/STT/audio counts, audio
startup device/host/format/readiness (kept separate from chunk counts), status
breakdowns, latency, queue delay, retry/API diagnostics, dependency markers,
empty targets, flagged samples, circuit/probe fallback events, recent-STT-
context provenance, and per-run summaries. The provenance summary is backward
compatible: old included-context rows are counted as legacy/unavailable rather
than violations. Groq sources are joined only to earlier successful same-run
Groq STT events; non-Groq sources are counted separately because SenseVoice
successes do not currently emit equivalent STT runtime rows. Legacy analyzer
samples contain IDs and numeric metadata only. Schema-v6 raw bundles separately
retain exact request context in content-addressed contract events. Always select
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
failures, content-guard rate/reasons, following fallback attempts and selected
routes, bounded candidate/selected samples, and any impossible selected guarded
attempt. Historical Qwen counters remain for old runs; use the route-neutral
fallback counters for the current DeepSeek-to-Groq chain.
`api_diagnostics.cost_usd` keeps final all-attempt and selected-attempt totals
and separately reports observed provisional API cost plus their combined
observed total. Legacy previews and provider calls without returned cost can
make the combined figure a lower bound. This
is the post-cutover owner; do not add a separate guard analyzer.

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

The snapshot also freezes every sentence that had a rejected provider
candidate, with the candidates themselves. `run` re-adjudicates them through
the publication guard and copied-name rescue and compares the entity mapping
capsule, so guard, rescue, and entity-registry changes report each sentence
whose published subtitle would change (`subtitle outcome changes`, and a
warning for any sentence that would lose its subtitle).

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
`data/semantic_quality_evidence_20260802.json` provenance manifest (built by the
`rebuild_semantic_quality_provenance.py` tool removed on 2026-11-11); do not
route it through `evaluate_translations.py`. The separate
`data/semantic_quality_eval_20260802.json` contains 16 reviewed bounded
contrasts: references pass 16/16 and captured current outputs fail 16/16.

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
```

`download_audio.py` uses `yt_dlp` and network access. The SenseVoice scout,
CAM++ speaker-similarity replay, and STT completeness checker were removed;
their historical artifacts remain available for read-only analysis.

Removed in Phase 3a: `scripts/scout_sensevoice_historical.py`,
`scripts/replay_phase0_speaker_similarity.py`, and
`scripts/stt_completeness_check.py`. Their historical results remain intact.

Removed 2026-11-10 with their tests and machine-generated data: the Phase 0
routing-span, alert-shadow and short-overlap-dedupe tools, the T25 local
dual-ASR replay (`replay_phase0_stt_candidates.py`,
`build_phase0_replay_manifest.py`, `evaluate_weak_signal_dual_asr.py` and the
`data/t25_*` SenseVoice/faster-whisper outputs and manifests), the OCR gate
scouts, `gemini_live_translate_probe.py`, `compare_stt_language_modes.py` and
`cluster_name_variants.py`. Their experiments had closed; the reports are in
`archive/experiments/` and the code in git history. Owner-heard annotations
(`data/manual_quality_annotations_*`, `data/t25_092_blind_listen*`,
`data/t25_higedan_assisted_listen_annotations_20260812.json`) are kept.

Removed legacy/ad-hoc scripts:
- Removed 2026-11-11 as unused, with their tests: the schema-v2 labeling workflow (`collection_sanity_report.py`,
  `sample_labeling_cases.py`, `labeling_review_server.py`,
  `build_phase0_eval_candidates.py`), `analyze_groq_error_bursts.py`,
  `analyze_latency_tail.py`, `suggest_corrections.py`, `llm_quality_reviewer.py`,
  `post_run_quality_loop.py`, `rebuild_semantic_quality_provenance.py` and
  `data/fan_terms.json`. None had a caller or a recent run; the code is in git
  history.
- `scripts/check_db.py` and `scripts/analyze_cache.py` were removed with the
  translation cache (2026-11-10).
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

## Offline subtitle delivery

- `scripts/make_subtitles.py`: thin subprocess CLI (`translate` / `transcribe`, `--estimate`, `--duration`, `--profile`, `--out-dir`, `--ffmpeg`, `--translator gemini|deepseek`, `--gemini-model`, `--stt elevenlabs|groq`). Parsing, rolling-caption cleanup, timestamp ownership slicing, bounded STT retry, cue-ID translation batches and per-cue fallback, delivery rollback, and report live in `modules/offline_subtitles.py`.
- `--estimate` is local-only; actual translation calls paid Gemini 3.8 Flash by default (`GEMINI_API_KEY`, offline only, never in the live engine chain) or DeepSeek with `--translator deepseek`; no automatic fallback between them. Transcription calls paid ElevenLabs `scribe_v2` by default (word timestamps; per-word slice ownership, then one global grouping into cues; profile registry terms as `keyterms`; a malformed slice aborts the job, a silent slice is fine; 402 / `insufficient_credits` / `quota_exceeded` abort with a top-up message) or Groq Whisper with `--stt groq`; no automatic fallback. Plan: `docs/agent/OFFLINE_ELEVENLABS_STT_PLAN_20261009.md`. The translator and STT keys are checked before any paid transcription. Gemini errors are split by HTTP status: 402 (prepaid credit exhausted) and 400/401/403/404 abort the job with a fixed message; 429/5xx/network share one per-job retry budget; only unusable replies fall back cue by cue. The report records Gemini usage (input/output/thinking), `usage_complete`, and cost. Plan: `docs/agent/OFFLINE_GEMINI_PLAN_20261009.md`.
- Do not run paid jobs without user authorization. Provider tests are mocked; optional local ffmpeg integration skips if unavailable.
- Must run in a fresh subprocess. Sets `LIVE_TRANSLATE_RUN_KIND=cafe_clip` before project imports, binds a manually locked profile and empty activity, and never instantiates the live STT/translator, SQLite cache, translation/conversation memory, history, or shared breaker. No resume/checkpoint support. stdout is JSONL with job IDs; logging goes to stderr.
- Writes `.ko.vtt`, `.zh-TW.vtt`, `.zh-TW.srt`, `.ko-zh.srt` (Korean above Chinese), `.ko-zh.txt` (side-by-side sheet), `.report.json` to the explicit output directory, staging all files before delivery and rolling back rename failures. Existing outputs are protected. Quality flags and untranslated cue IDs are advisory, not proof of translation quality.
