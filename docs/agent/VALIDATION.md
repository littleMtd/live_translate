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
- Comparison, LLM-review, Groq replay, and Gemini probe tools may call external
  APIs unless their documented dry-run/offline mode is selected. The current
  75-case translation benchmark scorer is offline-only.

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

Focused 2026-08-01 human semantic labels are stored separately from the eight
small generic cases:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\evaluate_translations.py `
  --cases data\semantic_quality_eval_20260801.json `
  --json
```

`data/manual_quality_annotations_20260801.json` preserves all 47 supplied
annotations. The evaluator-facing file contains 43 atomic cases: annotations
44-46 add runtime/root-cause evidence to earlier cases, and annotation 47 is a
cross-case diagnostic observation, so they must not be counted as four new
quality failures. Every proposed rule remains a hypothesis until its owning
layer is verified. Eight externally supported cases (qa006, qa010, qa016,
qa020, qa021, qa034, qa042, and qa043) now have bounded expected/forbidden
assertions: references pass 43/43 and the captured known-bad outputs pass 35/43,
failing exactly those eight. The other 35 cases remain schema/reference smoke
cases, and even the eight assertions are not model-quality proof without
matched controls or reviewed A/B outputs. Sixteen `stt_mishear` labels are
human inferences;
the two source runs have no retained WAV assets, so do not describe those
reconstructions as STT ground truth. See
`docs/agent/T25_EVIDENCE_20260802.md` for the runtime comparison pool,
recurrence audit, external verification, ownership map, and evidence classes.
In particular, qa033's supplied Yuri reference was contradicted by official
Hearts2Hearts membership/birth-year context: the canonical file now uses Yuha
as an externally supported candidate and marks it audio-pending. Do not treat
that candidate as audio-verified. The 391 same-run unselected events are a
sampling frame for future matched controls, not assumed-correct controls.

The later audio-retained batch uses a different contract:
`data/manual_quality_annotations_20260802.json` preserves all 95 supplied
records, while `data/semantic_quality_evidence_20260802.json` is a provenance
manifest and must **not** be passed to `evaluate_translations.py`. Its 109
display timestamps join 117 same-second runtime candidates: 101 are unique and
all eight multi-candidate timestamps are text-disambiguated, including
distinctive-token selection for annotations 83 and 91. Every non-empty source
or evidence utterance ID must join exactly one successful schema-v3 STT event
and an existing WAV. Schema v2 of the provenance manifest closes the former
annotation-68 gap by retaining its live evidence IDs `utt-26,utt-27`; selected
missing-provenance count is zero. Frozen timestamp selections are not
recomputed during provenance rebuilding.

Validate the canonical artifact without modifying it:

```powershell
.\live-subtitle-env\Scripts\python.exe `
  scripts\rebuild_semantic_quality_provenance.py --check
```

The rebuild command is intentionally explicit and mutating; use it only when
the frozen runtime joins are being refreshed and reviewed:

```powershell
.\live-subtitle-env\Scripts\python.exe `
  scripts\rebuild_semantic_quality_provenance.py `
  --output data\semantic_quality_evidence_20260802.json
```

The separately reviewed bounded evaluator is executable:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\evaluate_translations.py `
  --cases data\semantic_quality_eval_20260802.json `
  --json
```

Its references must pass 16/16. Supplying each row's captured
`current_output` must fail exactly 16/16; this proves the assertions distinguish
the frozen contrasts, not that a production candidate fixes them. Suggested
full translations and rules remain proposals.

The 21 audio-required cases are frozen in
`data/t25_stt_replay_manifest_20260802.json`: 31 current WAVs plus three
separate evidence WAVs. Preserved dual-engine outputs are
`data/t25_sensevoice_shadow_20260802.json` and
`data/t25_faster_whisper_shadow_20260802.json`. Validate all 34 fingerprints,
the current/evidence separation, manifest SHA, engine metadata, and non-empty
case outputs; do not compute WER/CER because there is no reference transcript.
See `docs/agent/T25_RUNTIME_AUDIO_EVIDENCE_20260802.md` and
`docs/agent/T25_DUAL_ASR_REPLAY_20260802.md`.

### Runtime and failure analysis

Portable one-run evidence can be created with:

```powershell
.\live-subtitle-env\Scripts\python.exe scripts\export_chatgpt_bundle.py `
  --run-id <run_id> [--include-audio]
```

`CHATGPT_PROJECT_README.md` and `manifest.json` are derived indexes; the
sanitized `runtime_events*.jsonl` parts remain the source of truth. Both
`analyze_runtime_events.py --events <bundle-directory>` and
`llm_quality_reviewer.py --events <bundle-directory>` resolve the ordered parts
from the manifest, so validation must prove they work after the original daily
logs are unavailable. Export tests must cover exact-run filtering, recursive
secret/header redaction, raw-event preservation, part ordering, provenance,
optional/missing WAVs, and screenshot exclusion.

Normal runtime teardown must emit its terminal lifecycle event before invoking
the default no-audio export. Export failure is fail-soft and must not replace a
successful or failed pipeline exit status. The Tauri forced-stop path cannot
rely on Python `finally`; it must export the exact launcher-assigned run ID, not
an unconstrained "latest" run.

- broad per-run summary: `scripts/analyze_runtime_events.py`;
- Historical DeepSeek/Qwen shadow evidence remains readable under
  `translation_model_shadow`. Analyze the same complete run (pass both daily
  JSONL paths when it crosses midnight), require pair/fingerprint integrity and
  record-only violations to be zero. Check the independent success-rate delta,
  then use only comparable successful Qwen
  API-miss pairs for latency/cost/quality denominators. Similarity is a review
  prioritizer, not semantic ground truth; inspect the highest disagreements and
  every candidate-only QA/canonical/script regression before any cutover;
- after the owner-authorized protected cutover, use
  `api_diagnostics.deepseek_output_guard` for the production guard rate/reasons,
  Flash provider failures, Qwen-after-guard continuity, bounded candidate/
  selected samples, and the zero-tolerance
  `guarded_attempt_selected_violations`. Keep all-attempt cost separate from
  selected-route cost. For the first controlled and next two representative
  natural runs, review every guarded pair and at least 30 QA-prioritized Flash
  successes; exercise `LIVE_TRANSLATE_DEEPSEEK_ROUTE=off` once and verify zero
  DeepSeek attempts plus the exact Qwen -> DeepL -> Groq route;
- recent STT-context provenance is summarized under
  `stt_summary.context_provenance`. New rows distinguish telemetry coverage
  from legacy rows, require Groq context sources to join an earlier successful
  same-run Groq STT event, count SenseVoice/other sources separately, and report
  bounded text-free candidates for missing, self/future, stale, confidence,
  current-threshold, or metadata inconsistencies. `context_text_len` is the
  length of the whitespace-normalized, 120-character-truncated context payload
  handed to the prompt budget, not the raw accepted transcript length;
- Windows capture startup is summarized under `audio_startup_summary`, separate
  from VAD chunk counts under `audio_summary`; use the startup fields to verify
  actual device/host API, requested format, and stream-ready latency;
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

The LLM reviewer is the semantic discovery/triage owner, not a confidence gate
or regression oracle. For current schema-v5 runs, use `--mode broad` when the
goal is to inspect every published translation rather than only deterministic
anomalies. Reviewer context is limited to earlier published same-cohort rows and
is labeled approximate because runtime telemetry does not retain the byte-exact
provider history payload; future subtitles are never supplied as context.
Review output is fail-closed and requires one strict semantic verdict per case.
`summary.json` records reviewer calls/model, tokens, reported cost, latency,
coverage, suspicious rate, and category counts. `--calibration-cases` builds
blinded reference/current contrasts only from cases with bounded expected or
forbidden assertions and reports known-failure recall, known-good false-positive
rate, and top-K precision. Labels are attached only after review. Use
`--case-offset` for bounded/resumable slices when provider budgets require it.
Do not promote triage results into the 75-case suite or production corrections
without source/context/root-cause verification and matched negative controls.

### STT/audio/routing harnesses

- collection joins/readiness: `collection_sanity_report.py`;
- sampling/UI: `sample_labeling_cases.py`, `labeling_review_server.py`;
- Phase 0 sampling/freeze: `build_phase0_eval_candidates.py`,
  `build_phase0_replay_manifest.py`;
- routing spans: `routing_span_annotations.py`,
  `routing_span_review_server.py`, `summarize_phase0_routing_spans.py`;
- local dual-ASR/speaker/alert replay: `replay_phase0_stt_candidates.py`,
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
runtime events are schema v5. Until those tools are upgraded and tested, do not
claim a schema-v5 run is “ready for sampling” merely because the runtime analyzer can
read it.

`replay_phase0_stt_candidates.py` defaults to the legacy SenseVoice behavior
and field aliases. `--engine faster-whisper` selects the second local engine;
run the engines in separate processes. Engine-neutral `candidate_*` fields are
the comparison surface. Evidence assets must stay out of
`candidate_current_text`. Model/package versions and artifact hashes identify
an evidence run, but the owner decision does not permanently pin the external
ASR environment; after an update, write a new result instead of overwriting a
baseline.

For the T25 weak-signal cohort, build and analyze with
`scripts/evaluate_weak_signal_dual_asr.py`; continue to run the two engines in
separate `replay_phase0_stt_candidates.py` processes. The frozen selector uses
the four provenance run summaries, comparison-safe single-WAV Groq alignment,
and fail-closed 1:1 control calipers. Faster-whisper replay explicitly uses
`temperature=0.0`; do not compare a default-temperature result with this
baseline. Validate manifest/result SHA links, all WAV fingerprints, 20/20
non-empty local outputs, the six-per-cohort three-way denominators, and the
three-case exact repeat. Similarity/repetition signals prioritize evidence;
they are not WER, correctness, or authorization for live behavior.

The later single-case T25-092 blind adjudication is preserved as
`data/t25_092_blind_listen_20260803.json` plus
`data/t25_092_blind_listen_annotations_20260803.json`. It is exact
owner-heard evidence for `utt-281` only. Keep its hidden-candidate input, saved
`ok` label, verbatim note, and WAV identity together; do not reinterpret it as
a persisted Groq per-chunk transcript or a population-level threshold gate.
T25-CX adds attribution for future requests only; it cannot retroactively prove
which text supplied context to T25-092, nor whether context caused that error.
Do not change context gates until an owner-authorized identical-settings Groq
context-on/off comparison passes blind review with matched weak and ordinary
controls.

### Prompt/model/API experiments

- deterministic prompt construction: `debug_pipeline.py --no-api`;
- deterministic translation evaluation: `evaluate_translations.py`;
- current 75-case semantic regression scoring:
  `evaluate_translation_prompt_benchmark.py --production-runtime-baseline` or
  `--results <json>`; it is offline-only and contains no retired experiment
  variants;
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
