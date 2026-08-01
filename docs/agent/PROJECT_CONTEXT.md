# Project Context and Architecture

This is a binding routed extension of `AGENTS.md`. Read it completely before
code changes, architecture review, diagnosis, or claims about current runtime
behavior. Code remains authoritative when this snapshot is stale.

## Current Project Context

This section is working memory for future sessions. Treat it as a snapshot and
verify against code/runtime data before making decisions.

## Repository Architecture Map (verified 2026-07-24)

Code is authoritative when older prose disagrees with it. In particular,
`README.md` and portions of `system.md` / `frontend-design.md` contain stale
examples (old engine lists, a removed `prompt_evolver.py`, runtime schema v2,
or an older frontend layout). Verify current behavior in source before copying
those descriptions.

### Runtime entry points and modes

- `main.py`: Python application entry and thread/queue graph owner.
  - default: audio -> STT -> sentence assembly -> translation -> tkinter
    subtitle overlay.
  - `--stt-only`: stops after sentence assembly and writes/prints sentences.
  - `--listen`: STT-only song/music mode with relaxed STT thresholds.
  - `--donation-ocr`: launches `donation_ocr/app.py` as a separate DB-less
    process alongside the audio pipeline.
- `config.py`: frozen dataclass configuration and `.env` key loading. It also
  applies a strict whitelist of dashboard JSON overrides from
  `logs/live_translate_config.json` when the override environment switch is
  enabled.
- `src-tauri/src/main.rs`: optional Tauri v2 desktop dashboard entry.
- `src-frontend/src/main.ts`: Vue 3/Vite dashboard entry.
- `donation_ocr/app.py`: independent PaddleOCR ROI prototype that reuses the
  translator but disables SQLite/history-file I/O.

### Live pipeline and concurrency

```text
WASAPI loopback / sounddevice
  -> audio_queue
  -> STTEngine (Groq Whisper primary; SenseVoice optional)
  -> text_queue
  -> SentenceBuffer / sentence_splitter
  -> sentence_queue
  -> two translation workers sharing policy, memory, history and fallback state
  -> in-order completion/emission
  -> subtitle_queue
  -> tkinter overlay on the main thread
```

Control uses a shared `stop_event` and `pause_event`. Startup failures must
propagate to shutdown rather than leave zombie threads. Queue backpressure is
intentionally asymmetric:

- audio/text/subtitle queues: drain and keep newest (`put_latest`);
- sentence queue: drop only the oldest (`put_drop_oldest`);
- translation results: workers may complete out of order, but `sequence_id`
  emission is ordered.

Do not infer duplicate sequence IDs by grouping a whole daily JSONL file
without `run_id`: one daily file may contain multiple processes/runs, and each
run restarts translation sequencing at zero.

### Python module ownership

Capture and STT:

- `modules/audio_capture.py`: loopback-device resolution, stereo-to-mono,
  normalization metadata, Silero/RMS VAD, overlap, adaptive chunking, and
  `audio` runtime events.
- `modules/stt.py`: Groq/SenseVoice clients, key selection/retry, timestamp
  overlap dedupe, prompt/context state, transcription events, optional WAV
  dump, and STT runtime events.
- `modules/stt_policy.py`: segment confidence/language rejection, Whisper
  hallucination checks, overlap dedupe, and Groq prompt-budget construction.
- `modules/pipeline_events.py`: typed `AudioChunk`, `TranscriptionEvent`,
  `SentenceEvent`, segment metadata, and source-confidence aggregation.

Sentence assembly:

- `modules/sentence_buffer.py`: completeness rules, prefix/blob force cuts,
  pending fragments, and cut metadata.
- `modules/sentence_splitter.py`: queue-thread orchestration, bounded two-cut
  merge, sentence runtime events, and sentence-queue backpressure.
- `utils/text_heuristics.py`: shared Korean endings, template/garbage/song
  markers, regexes, and language-character helpers.

Translation:

- `modules/translator.py`: facade and pipeline worker pool; deterministic
  policy, source normalization, slang, cache, prompt, fallback, source-aware
  target corrections, quality retry, ordered emission, translation events,
  and the background recovery-probe thread.
- `modules/translation_engines.py`: engine registry and API adapters for
  Claude, Google Translate, DeepL, Ollama, NVIDIA, OpenRouter, and Groq;
  request/history shaping, per-call diagnostics, attempt chains, and token
  usage.
- `modules/translation_runtime.py`: pure cache helpers plus fallback/circuit
  state transitions and NVIDIA recovery probes.
- `modules/translation_policy.py`: pre-translation rejection/sanitization,
  template/garbage/song/low-value rules, duplicate state, slang lookup, and
  evidence-gated repetition exemption.
- `modules/translation_memory.py`: in-memory LRU, recent translation context,
  optional SQLite read/write-through, and invalidation.
- `modules/translation_prompts.py`: standard/Qwen prompt construction and
  JSON profile loading.
- `modules/translation_corrections.py`: validates and loads deterministic
  source-normalization, replacement, conditional, and name-rendering tables.
- `modules/streamer_profiles.py`: canonical profile IDs, aliases, common/profile
  STT glossary terms, and streamer metadata.

Context, display, and persistence:

- `modules/scene_context.py`: optional automatic activity resolver and
  translation-only publisher. It resolves exactly one platform-matching Chrome
  HWND, uses window-only capture plus an exact bounded open-set vision schema,
  and requires two distinct frames for model-derived identities. Known aliases
  stabilize existing IDs but are not an admission list. Broad kinds use fixed
  generic labels, and per-window identity-cap exhaustion stays fail-closed
  until the window generation changes. A separate default-off switch gates
  open-set publication; the resolver never writes
  `cfg.translation.current_activity` and never activates STT hot terms.
- `modules/scene_vision.py`: immutable explicit provider/model routing for
  scene classification. Each route gets one attempt; fallback occurs only
  after a bounded retryable provider failure. Valid `unknown` and
  content/schema rejection stop without trying another route.
- `modules/scene_stt_terms.py`: maps the current activity to hot STT glossary
  terms.
- `modules/subtitle_display.py`: tkinter overlay, pause/resume, timing, and
  queue clearing.
- `modules/db.py`: SQLite schema v2 translation cache, WAL, `RLock`, migration,
  prompt-versioned unique key, hit accounting, and LRU eviction.
- `utils/runtime_events.py`: thread-safe daily JSONL writer and reference-free
  translation quality fields.
- `utils/metrics.py`: in-process counters/latencies and periodic log summary.
- `utils/config_export.py`: secret-free Python config export for Tauri.
- `utils/pipeline.py` / `utils/queue_utils.py`: thread, pause, polling, and
  backpressure primitives.

### Translation selection and state

`cfg.live_engine` / `cfg.clip_engine` selects the backend:

- `nvidia`: NVIDIA primary followed by each configured/available engine in
  `cfg.translation.engine_chain` (currently OpenRouter, DeepL, Groq).
- `anthropic`: historical backend name; it means use `engine_chain` directly,
  not “Claude only”.
- `ollama`: local Ollama only.

All translation workers share `TranslationPolicy`, `TranslationMemory`,
recent history, and `FallbackState`; engine diagnostics remain thread-local per
call. In live NVIDIA mode, primary failure opens a circuit and sends user
traffic to fallback engines. A background probe waits for cooldown and requires
consecutive valid responses before restoring NVIDIA. Probe calls copy recent
production history. Circuit/probe actions are emitted as
`translation_fallback` events.

Quality retry is a separate second-opinion path after a successful first
translation. Japanese residue detection remains part of translation-quality
telemetry, but Japanese-specific retry defaults `off` so production subtitles
do not wait for a diagnostic-only second call. Explicit `shadow` mode is
synchronous and record-only: an output containing `target_has_japanese` never
replaces the shipped subtitle during shadow, even when another bad-output flag
is also present. `off` is fail-closed for Japanese-flagged composite defects,
preserving that production output boundary while removing the extra call.
Existing selective triggers on non-Japanese outputs remain independent.

### Configuration and data ownership

- `.env`: secrets only; never print, export, or commit.
- `config.py`: runtime defaults and validation.
- `logs/live_translate_config.json`: dashboard bridge; not a second unrestricted
  config source. Only `_DASHBOARD_OVERRIDE_FIELDS` /
  `_DASHBOARD_OVERRIDE_TOP` can override Python settings on restart.
- `data/default_slang.json`: exact direct translations for short slang.
- `data/streamer_profiles.json`: profile IDs/aliases and STT glossary terms.
- `data/translation_profiles.json`: standard and Qwen prompt-profile text.
- `data/translation_corrections.json`: deterministic source/target corrections
  and name-rendering rules.
- `data/scene_stt_terms.json`: activity-specific STT vocabulary.
- `data/fan_terms.json`: reviewed terminology/reference inventory.
- `data/eval_cases.json`: small offline output-quality fixtures.
- `data/replay_eval_snapshot.jsonl`: frozen real-runtime deterministic golden
  set; changes require explicit replay review.

The user's local `config.py` profile selection may be intentionally dirty.
Preserve it and do not stage/commit it unless the user asks.

### Runtime observability and storage

- `logs/runtime_events_YYYYMMDD.jsonl` uses runtime schema v3. Event types
  currently include `audio`, `stt`, `sentence`, `translation`, `scene`, and
  `translation_fallback`.
- Every event carries `run_id`, UTC `created_at`, `run_kind`, `git_sha`, and
  `git_dirty`. Use `(run_id, sequence_id)` rather than `sequence_id` alone.
- `run_kind` is `live`, `test`, `replay`, or `benchmark`; analysis should
  normally filter live data explicitly.
- Translation events include attempt chains, selected engine/model, queue and
  API latency, source evidence, profile/activity, correction trace, quality
  flags, and subtitle emission/suppression.
- `activity_shadow` events contain only accepted bounded activity IDs/kinds,
  parse status/rejection reason, resolver/capture generations, consensus, TTL,
  and provider diagnostics. Explicit fallback events include a bounded
  provider/model/latency/token/cost attempt chain. They never contain a
  complete title, frame/fingerprint, evidence key, rejected label, or raw
  vision response.
- `logs/translations_YYYYMMDD.txt` is the human-readable translation history.
- `logs/live_translate.db` is the persistent cache/corpus. Live DB cache is
  disabled by default because measured live hit rate was low; clip mode may
  still use it.
- `logs/audio_dump/<run_id>/<utterance_id>.wav` exists only when
  `LIVE_TRANSLATE_DUMP_AUDIO=1`.
- `scratch/analysis/` is the normal destination for generated offline reports;
  do not commit generated artifacts unless explicitly requested.

### Optional desktop and OCR surfaces

Tauri v2 exposes config, cache, process, and system-stat commands under
`src-tauri/src/handlers/`. Vue calls them through
`src-frontend/src/api/client.ts`. Python exports config on startup; dashboard
changes take effect on the next Python restart when dashboard overrides are
enabled. API keys never enter the JSON bridge.

The OCR work has two distinct surfaces:

- `modules/scene_context.py`: context-only activity classification and optional
  translation metadata publication; never a spoken-text source or automatic
  STT-term source.
- `donation_ocr/`: separate ROI OCR translation prototype and process, with
  session logs under `scratch/donation_panel/`.

Do not merge OCR text into spoken STT source or silently treat a Tier-4 OCR
proposal as implementation-ready.
