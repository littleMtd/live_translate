# Tool and Script Inventory

This is a binding routed extension of `AGENTS.md`. Read it completely before
proposing, adding, or substantially changing a script, harness, analyzer,
replay, benchmark, sampler, or maintenance command.

## Utility Script Inventory

Use this inventory to pick the right existing script before writing a new one.
The scripts below were checked on 2026-05-31 with `python -m py_compile` across
`scripts/**/*.py`, targeted pytest, and non-destructive CLI runs where possible.
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

Situational scripts:
- `scripts/benchmark_nvidia_models.py`: useful for model comparison; actual
  runs call NVIDIA API.
- `scripts/compare_prompt_variants.py`: Qwen prompt A/B; may call NVIDIA.
- `scripts/compare_stt_language_modes.py`: fixed-Korean versus auto-detect
  Groq STT on the same dumped WAVs; record-only, but calls Groq.
- `scripts/evaluate_japanese_retry.py`: evaluates the Japanese retry
  shadow-to-active gate from runtime events.
- `scripts/llm_quality_reviewer.py`: deterministic suspicious-case selection,
  with optional OpenRouter QA; use `--dry-run` for no API call.
- `scripts/download_audio.py`: useful only when preparing offline audio files
  from YouTube.
- `scripts/stt_completeness_check.py`: useful only when a matching Korean VTT
  reference exists for CER/coverage evaluation.

Runtime/log analysis:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\analyze_runtime_events.py `
  --events logs\runtime_events_YYYYMMDD.jsonl `
  --top 10
```

Use this for broad runtime summaries: translation/STT/audio counts, status
breakdowns, latency, queue delay, retry/API diagnostics, dependency markers,
empty targets, flagged samples, circuit/probe fallback events, and per-run
summaries. Always select `--run-id` when a daily file contains multiple runs.

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

NVIDIA model benchmarking:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\benchmark_nvidia_models.py `
  --events logs\runtime_events_YYYYMMDD.jsonl `
  --samples 20 `
  --dry-run
```

`--dry-run` only selects benchmark samples. Without `--dry-run`, this calls the
NVIDIA API, requires `NVIDIA_API_KEY`, and enforces `--rpm <= 20`.

Audio/STT evaluation helpers:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\download_audio.py <youtube-url> --out audio\sample.wav
.\live-subtitle-env\Scripts\python.exe scripts\stt_completeness_check.py `
  --audio audio\sample.wav `
  --ref path\to\sample.ko.vtt `
  --duration 60
```

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
  prompt-variant experiment with stale static variants. Prefer
  `debug_pipeline.py`, `evaluate_translations.py`, or
  `benchmark_nvidia_models.py`.

Current architecture gaps discovered during the 2026-07-24 scan:

- `replay_eval.py` is not part of `check_translator_core.py` or CI.
- Replay cases do not yet retain STT evidence or neighboring context.
- Collection/sampling tools still target runtime schema v2 while the live
  writer emits v3.
- `README.md` still names a removed `prompt_evolver.py` and has an obsolete
  engine table; use the module map in `docs/agent/PROJECT_CONTEXT.md`.
- `frontend-design.md` is a hybrid tutorial/spec and contains older layouts;
  actual Tauri/Vue source wins.
- `system.md` includes a stale schema-v2 paragraph even though
  `utils/runtime_events.py` emits v3.

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
