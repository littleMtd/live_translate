# Validation and Evidence Workflow

This is a binding routed extension of `AGENTS.md`. Read it completely before
code changes or when choosing tests, replay/runtime evidence, labeling, or
evaluation methods.

## Existing-Tool-First Rule (Mandatory)

Before proposing a new script, harness, replay flow, evaluator, sampler, or
maintenance command:

1. Search the repository first:
   `rg --files | rg -i "replay|eval|harness|benchmark|analy|sample|review|gate|scout"`.
2. Search script descriptions and call sites:
   `rg -n "ArgumentParser|replay_eval|check_translator_core|post_run_quality_loop" scripts tests AGENTS.md docs/agent`.
3. Read the relevant script and its tests, then run its `--help` or a
   non-mutating baseline command.
4. Extend the existing tool when its responsibility already matches the task.
   Create a new tool only when the existing abstraction cannot represent the
   required inputs or outputs, and record that limitation in the task plan.

Do not describe an offline replay/evaluation workflow as net-new until
`scripts/replay_eval.py`, `scripts/check_translator_core.py`,
`scripts/evaluate_translations.py`, and the task-specific Phase 0/shadow tools
have been checked.

Important mutation boundaries:

- `scripts/replay_eval.py run` is read-only; `--update` rewrites
  `data/replay_eval_snapshot.jsonl` and is allowed only when intentionally
  accepting reviewed deterministic behavior changes.
- `scripts/post_run_quality_loop.py` updates the replay snapshot by default.
  Use `--skip-replay-update` for analysis unless snapshot acceptance is
  explicitly in scope.
- `scripts/update_translation_profile_snapshot.py --check` is read-only.
  Running it without `--check` edits the stored prompt-profile hash.
- Benchmark, comparison, LLM-review, Groq replay, Gemini probe, and NVIDIA
  scripts may call external APIs unless their documented dry-run/offline mode
  is selected.

`check_translator_core.py` does **not** currently run `replay_eval.py`, and the
GitHub Actions Python job does not run the frozen replay snapshot either.
Deterministic policy/correction/name-rendering changes therefore require a
separate replay command even when the core check and full pytest suite pass.

## Harness and Validation Routing

Choose validation by the layer changed; one green command does not substitute
for a different layer's harness.

### Deterministic policy/correction/name rendering

Primary harness:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\replay_eval.py run `
  --snapshot data\replay_eval_snapshot.jsonl
```

It replays 750 frozen real-runtime cases at the 2026-07-24 scan and compares:

1. `TranslationPolicy.rejection_reason(source)`;
2. source normalization;
3. source-aware target correction/name rendering with shipped model output
   held fixed.

It does not call APIs. Run it before and after relevant edits. A diff is a blast
radius report, not automatically a regression. Never use `--update` until each
intentional diff is reviewed.

Current limitation: the snapshot stores source/target/profile/status but not
STT confidence, cut metadata, character ratios, or neighboring context.
Evidence-aware gates (`avg_logprob`, `no_speech_prob`, forced cuts,
mixed-language/background contamination) require extending this harness rather
than inventing a parallel replay system.

At the 2026-07-24 scan, the working tree baseline was:
`750 cases, 2 TARGET divergences`, both from the unaccepted local
`릴파 -> Lilpa` deterministic rendering change. Re-check current state; do not
blindly encode this count as permanently expected.

### Translator maintenance and fixed eval cases

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\check_translator_core.py --skip-pytest
.\live-subtitle-env\Scripts\python.exe scripts\check_translator_core.py
.\live-subtitle-env\Scripts\python.exe scripts\evaluate_translations.py --json
```

The core check validates JSON fixtures, the translation-profile hash, eight
small eval cases, and optionally focused pytest files. It does not include the
750-case replay harness.

### Runtime and failure analysis

- broad per-run summary: `scripts/analyze_runtime_events.py`;
- T20 record-only completeness diagnostics are included in the broad
  schema-v3 summary under `sentence_early_cut`. They are counterfactual only:
  production defaults `off`, `shadow` must be selected deliberately, and no
  active value is accepted after the frozen T20 shadow gate closed no-go;
- latency attribution: `scripts/analyze_latency_tail.py`;
- Groq STT error bursts: `scripts/analyze_groq_error_bursts.py`;
- SQLite behavior/corpus: `scripts/analyze_cache.py`;
- offline correction mining: `scripts/suggest_corrections.py`;
- name-variant clustering: `scripts/cluster_name_variants.py`;
- optional LLM-assisted offline QA: `scripts/llm_quality_reviewer.py`
  (`--dry-run` avoids API calls);
- wrapper: `scripts/post_run_quality_loop.py` (use
  `--skip-replay-update` unless accepting the snapshot is explicitly intended).

### STT/audio/routing harnesses

- collection joins/readiness: `collection_sanity_report.py`;
- sampling/UI: `sample_labeling_cases.py`, `labeling_review_server.py`;
- Phase 0 sampling/freeze: `build_phase0_eval_candidates.py`,
  `build_phase0_replay_manifest.py`;
- routing spans: `routing_span_annotations.py`,
  `routing_span_review_server.py`, `summarize_phase0_routing_spans.py`;
- SenseVoice/speaker/alert replay: `replay_phase0_stt_candidates.py`,
  `scout_sensevoice_historical.py`, `replay_phase0_speaker_similarity.py`,
  `build_phase0_alert_shadow_dataset.py`,
  `evaluate_phase0_alert_shadow.py`;
- overlap dedupe: `evaluate_short_overlap_dedupe_shadow.py`,
  `evaluate_short_overlap_surgical.py`,
  `short_overlap_dedupe_review_server.py`;
- language-mode comparison: `compare_stt_language_modes.py`;
- reference-audio CER/coverage: `stt_completeness_check.py`;
- input preparation: `download_audio.py`.

The collection/sampling scripts currently hard-code target schema v2 while live
runtime events are schema v3. Until those tools are upgraded and tested, do not
claim a v3 run is “ready for sampling” merely because the runtime analyzer can
read it.

### Prompt/model/API experiments

- deterministic prompt construction: `debug_pipeline.py --no-api`;
- Qwen prompt A/B: `compare_prompt_variants.py` (read its flags before use);
- NVIDIA model sampling/benchmark: `benchmark_nvidia_models.py --dry-run`
  selects only; non-dry runs call NVIDIA;
- Japanese retry gate: `evaluate_japanese_retry.py`;
- Gemini live audio experiment: `gemini_live_translate_probe.py` (external API);
- OCR gates: `gate1_ocr_scout.py`, `gate2_exclusivity.py`.

### Test/build matrix

```powershell
# Python
.\live-subtitle-env\Scripts\python.exe -m pytest tests -q

# Frontend
Push-Location src-frontend
npm test
npm run build
Pop-Location

# Rust/Tauri
Push-Location src-tauri
cargo test --locked
Pop-Location
```

GitHub Actions runs Python 3.11/3.12/3.13 unit tests, a main-branch
secret-backed API integration job, Vue tests/build, and Rust tests. Local
feature work should run the narrow relevant tests first and the full affected
stack before handoff.

### Pytest artifact hygiene

- Do not pass a repo-relative `--basetemp` or override `cache_dir` back into
  the project tree.
- The shared pytest cache is configured in `pyproject.toml` as
  `~/.cache/live_translate/pytest`. `tests/conftest.py` places pytest's
  numbered per-run temp directories under
  `~/.cache/live_translate/pytest/tmp`. Both stay outside the repository.
- Do not remove the external temp-root setup in favor of this host's default
  `%TEMP%\pytest-of-<user>` path without verifying its ACL first; that default
  path produced `WinError 5` during the 2026-07-25 hygiene check.
- Use `tmp_path` / `tmp_path_factory` for test-owned files. Tests must not
  create ad-hoc `.pytest-*`, coverage, or report directories at the project
  root.
- If a command needs a stable explicit temp directory for debugging, place it
  under `~/.cache/live_translate/pytest/debug/` and remove it after the
  investigation.
- `.gitignore` retains defensive patterns for legacy pytest artifacts, but
  ignored output in the repository is still pollution and should not be used
  as the normal workflow.

## Labeling / Collection Workflow

Use these scripts for the STT-vs-translation labeling workflow. They were
verified on 2026-05-31 with:
`python -m pytest` (`585 passed, 4 skipped`)

This is a historical schema-v2 workflow. The current live writer emits schema
v3, while `collection_sanity_report.py` and `sample_labeling_cases.py` still set
`TARGET_SCHEMA_VERSION = 2`. Upgrade and retest those tools before using this
section on new runtime logs.

Recommended order:
1. Validate a collected run before sampling.
2. Sample only from a run where `ready_for_sampling` is `yes`.
3. Open the browser labeling UI for the generated sample.

Collection sanity report:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\collection_sanity_report.py `
  --events logs\runtime_events_YYYYMMDD.jsonl `
  --audio-root logs\audio_dump `
  --run-id <run_id> `
  --fail-on-not-ready
```

This checks schema_version 2 translation population, profile consistency,
source_utterance_ids, STT event joins, wav joins, confidence fields, multi-chunk
coverage, duplicate source ids, empty text, long text, repeated source text, and
glossary/prompt echo candidates.

Random labeling sample:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\sample_labeling_cases.py `
  --events logs\runtime_events_YYYYMMDD.jsonl `
  --audio-root logs\audio_dump `
  --run-id <run_id> `
  --samples 60 `
  --seed <seed>
```

This samples uniformly from `schema_version == 2` and `event_type ==
"translation"`, joins every `source_utterance_ids` entry back to STT evidence
and wav paths, classifies each source chunk as `primary`, `supporting`, or
`prior_overlap`, and fails by default if replay evidence is missing.

Browser labeling UI:
```powershell
.\live-subtitle-env\Scripts\python.exe scripts\labeling_review_server.py `
  logs\labeling_sample_YYYYMMDD_HHMMSS.json `
  --host 127.0.0.1 `
  --port 8765
```

Open `http://127.0.0.1:8765/`. The UI plays each source wav chunk, shows source
text, romanization, translation, chunk roles, overlap warnings, STT confidence
fields, and writes annotations with optional context tags to
`logs\labeling_sample_YYYYMMDD_HHMMSS.annotations.json`. Keyboard labels:
`1=a_translation_error`, `2=b_stt_error`, `3=both`, `4=ok`, `5=unclear`;
space toggles the first audio chunk, arrow keys move between samples.
