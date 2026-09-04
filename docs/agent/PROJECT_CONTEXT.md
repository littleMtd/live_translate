# Project Context and Architecture

This is a binding routed extension of `AGENTS.md`. Read it completely before
code changes, architecture review, diagnosis, or claims about current runtime
behavior. Code remains authoritative when this snapshot is stale.

## Current Project Context

This section is working memory for future sessions. Treat it as a snapshot and
verify against code/runtime data before making decisions.

## Repository Architecture Map (verified 2026-08-26)

Code is authoritative when prose disagrees with it. `README.md` and `system.md`
were synchronized with the current runtime on 2026-08-26. `frontend-design.md`
remains a detailed hybrid tutorial/spec, so verify component-level claims in
the Tauri/Vue source before copying them.

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
Windows input endpoint / sounddevice (exact-format preflighted)
  -> audio_queue
  -> STTEngine (ElevenLabs Scribe v2 batch primary; Groq same-chunk fallback;
     SenseVoice optional)
  -> text_queue
  -> SentenceBuffer / sentence_splitter
  -> optional one-shot provisional request
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

- `modules/audio_capture.py`: capability-filtered loopback-device resolution,
  bounded actual-stream readiness, stereo-to-mono, normalization metadata,
  Silero/RMS VAD, discontinuity resets, overlap, adaptive chunking, and
  `audio` / `audio_startup` runtime events.
- `modules/stt.py`: ElevenLabs/Groq/SenseVoice clients, provider fallback,
  key selection/retry, timestamp
  overlap dedupe, prompt/context state, transcription events, optional WAV
  dump, and STT runtime events. The ElevenLabs adapter uses Scribe v2 batch on
  each completed VAD chunk; it is not the realtime WebSocket model because the
  current capture boundary does not publish frame-level audio to STT. Profile
  and manual-activity keyterms are bounded at 100 and only their count is
  logged. Each Groq request freezes text-free provenance
  for any included recent transcript context: source utterance ID, request-time
  age, normalized/truncated context length, source engine, and available source
  confidence. Response filtering may clear future context eligibility but does
  not rewrite the snapshot for the request already sent. The raw context text
  and any reversible hash are not emitted. For ElevenLabs, timing-based overlap
  removal is enabled only when the copied prefix byte-matches the last audio
  chunk that produced a successful transcription event; failed, filtered, or
  skipped predecessor audio therefore remains recoverable. Scribe language
  probability and provider-native word log-probability/type metadata are kept
  on the typed event, while runtime telemetry emits aggregates rather than word
  text or transcription IDs. These fields are measurement evidence only and do
  not rewrite or select the transcript.
- `modules/stt_policy.py`: segment confidence/language rejection, Whisper
  hallucination checks, overlap dedupe, and Groq prompt-budget construction.
- `modules/pipeline_events.py`: typed `AudioChunk`, `TranscriptionEvent`,
  `SentenceEvent`, segment metadata, and source-confidence aggregation.

#### Silero upstream-version policy (user decision, 2026-08-01)

- Do not pin the Silero VAD repository/model version by default. The user
  prefers receiving upstream model improvements over exact cross-device model
  reproducibility. Do not introduce a pin as a maintenance cleanup or implicit
  reliability fix without a new explicit user decision.
- The current `torch.hub.load("snakers4/silero-vad", force_reload=False)`
  behavior is intentional for now: a fresh environment or cleared/refreshed
  Torch Hub cache may obtain the then-current upstream `master`, while repeated
  starts reuse the local cache. This decision does not require downloading on
  every application start.
- The 2026-08-01 fresh-device run used upstream Silero VAD 6.2.1 and showed
  encouraging Korean sung-vocal capture. Treat that as a runtime observation,
  not proof that the upstream model revision caused the improvement; device
  routing, source mix, and VB-CABLE capture also changed.
- If the VAD loader or its telemetry is changed later, record the loaded
  upstream version/revision or model checksum in startup/runtime diagnostics so
  regressions can be attributed and a known-good revision can be selected if
  needed. This observability requirement does not itself pin or auto-rollback
  the model.

#### Windows audio endpoint policy (user decision, 2026-08-02)

- `audio.device_name` represents the intended endpoint family and therefore
  the VB-CABLE isolation boundary. When one or more names match, select the
  first enumerated endpoint that passes the exact requested sample-rate,
  channel-count, and dtype check. If all matching endpoints fail, stop with
  their bounded rejection reasons; do not silently capture Stereo Mix.
- When the configured name has no match, retain warning-plus-auto-detection.
  Auto-detection is also capability-filtered. Do not prefer WASAPI merely by
  host-API name: on the 2026-08-02 host, CABLE Output MME accepted
  16 kHz/2ch float32 while the WASAPI endpoint rejected 16 kHz with
  `PaErrorCode -9997`. Device indices are installation-local and must not be
  persisted as stable identity.
- `audio_capture.start()` returns only after actual `InputStream` entry or a
  bounded error/timeout. Silero loads afterward behind a callback gate, so a
  first-run model load cannot masquerade as a stream-open timeout or build a
  stale audio backlog. PortAudio input overflow and internal callback backlog
  are capture discontinuities and reset VAD/fixed buffers before later audio.
- `python modules/audio_capture.py` is a read-only device/host-API/format
  diagnostic. It must not open a stream, load Silero, record audio, or emit a
  runtime event.

Sentence assembly:

- `modules/sentence_buffer.py`: completeness rules, prefix/blob force cuts,
  pending fragments, cut metadata, and bounded holds for unpunctuated Korean
  embedded-question/subordinate endings such as `받을지`; punctuated standalone
  questions remain complete and the existing hard force-cut is unchanged.
  Ordinary Scribe full stops are active completion evidence through the shared
  safe-terminal classifier; decimal/version, URL, and ellipsis tails remain
  unsafe and do not gain early-release authority.
- `modules/sentence_splitter.py`: queue-thread orchestration, bounded two-cut
  merge, sentence runtime events, and sentence-queue backpressure.
- `utils/text_heuristics.py`: shared Korean endings, template/garbage/song
  markers, regexes, and language-character helpers.

Selective secondary-ASR source replacement remains disabled. Retained evidence
does not establish trigger precision or a safe reconciliation rule: SenseVoice
is latency-viable only as evidence, faster-whisper is too slow for the live
path, and majority agreement selects the wrong surface form in a known
context-supported lexical case. Groq therefore remains a separately owned
same-chunk provider-failure fallback, not a semantic second-opinion route.

Translation:

- `modules/translator.py`: facade and pipeline worker pool; deterministic
  policy, source normalization, slang, cache, prompt, fallback, source-aware
  target corrections, deterministic publication guards, ordered emission,
  translation events, and the background recovery-probe thread.
- `modules/translation_engines.py`: engine registry and API adapters for
  Claude, Google Translate, DeepL, Ollama, NVIDIA, OpenRouter, DeepSeek, and Groq;
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
- `modules/unknown_name_escrow.py`: exact reviewed source-grounded unknown-name
  placeholder mapping, restoration, and cardinality validation. Known
  canonical source spans are excluded before detection.
- `modules/semantic_terminology.py`: exact/narrow source-grounded semantic
  terminology placeholder mapping, restoration, and cardinality validation.
- `modules/provisional_subtitles.py`: one-shot provisional candidate storage,
  closure, and exact fingerprint contract.
- `modules/translation_corrections.py`: validates and loads deterministic
  source-normalization (including separately declared Korean name-boundary
  aliases), replacement, conditional, and name-rendering tables.
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
- `modules/profile_context.py`: immutable source/content/effective profile and
  registry snapshots. Exact reviewed member-name markers in one validated safe
  player crop are strong one-frame evidence when every marker belongs to the
  same family; cross-family markers conflict. Reviewed branding markers are
  medium evidence and retain two-frame consensus. The resolver uses JSON-mode
  output, a serialization-only bounded retry, five-second seeking/recovery and
  fifteen-second stable cadence, a rolling call-attempt cap, and bounded stale
  profile recovery. A hidden player tab suspends observation while its locked
  browser HWND/PID/class remains valid, retaining confirmed profile ownership
  and pausing profile expiry without generation churn. Provider diagnostics and
  the latest privacy-safe resolver
  observation feed runtime telemetry and dashboard status. Manual is a complete
  effective-profile hard lock. Profile generation is part of STT, sentence,
  provisional, history, request, and cache isolation.
- `modules/profile_control.py`: validates and atomically hot-reloads dashboard
  profile selection and `data/streamer_profiles.json`; invalid reloads retain
  the prior valid generation and a privacy-safe status file feeds the dashboard.
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
- `anthropic`: historical backend name. Ordinary live use follows the fixed
  protected DeepSeek/Qwen route described below; other applicable/clip paths
  may use `engine_chain`. It does not mean “Claude only”.
- `ollama`: local Ollama only.

All translation workers share `TranslationPolicy`, `TranslationMemory`,
recent history, and `FallbackState`; engine diagnostics remain thread-local per
call. In live NVIDIA mode, primary failure opens a circuit and sends user
traffic to fallback engines. A background probe waits for cooldown and requires
consecutive valid responses before restoring NVIDIA. Probe calls copy recent
production history. Circuit/probe actions are emitted as
`translation_fallback` events.

Production quality-retry and Japanese shadow/active experiment execution were
retired in August 2026. Japanese residue detection remains part of the normal
translation-quality telemetry and Kana publication guard; no second-opinion or
record-only translation call is made. Historical analyzers may still parse old
retry/shadow fields.

DeepSeek record-only shadow/model-comparison and `source_fuzzy_shadow` are also
retired and have no production producer, queue, state, config, or request path.
The current deterministic source normalizer is unrelated and remains active.

The current request/finalization contract is singular: a frozen effective
request supplies provider/fallback consumers, and primary success, fallback
success, and exact provisional promotion converge on the same restoration,
correction, invariant, event, cache/history, and ordered-publication owner.
Provisional mismatch closes the candidate and follows the ordinary final route.

Quality flags and their T04 classifications are diagnostic candidate signals,
not an automatic subtitle-error label or an error-rate denominator. Profile
canonical outputs and narrowly source-proven acronyms may improve approved vs.
unexpected telemetry classification without changing the legacy flag, score,
retry, route, or shipped subtitle.

For the `irise` profile, exact Korean source aliases for KIIRI, TIZ,
Heart Crush, and IRISÉ also gate deterministic target canonicalization. A
successful completed translation records the expected and any missing
canonical terms, plus a narrowly cued `파트` rendered as `部門` semantic
candidate. `translation_qa_disposition` separates clean, deterministically
normalized, and suspicious outcomes. These additive QA fields do not enter
legacy quality flags, score, severity, retry, provider selection, or routing;
the existing correction trace remains the attribution for actual repairs.

### Configuration and data ownership

- `.env`: secrets only; never print, export, or commit.
- `config.py`: runtime defaults and validation.
- `logs/live_translate_config.json`: dashboard bridge; not a second unrestricted
  config source. Only `_DASHBOARD_OVERRIDE_FIELDS` /
  `_DASHBOARD_OVERRIDE_TOP` can override Python settings on restart.
- `data/default_slang.json`: exact direct translations for short slang.
- `modules/semantic_terminology.py`: production semantic terminology rules;
  intentionally separate from canonical names and unknown-name escrow.
- `data/streamer_profiles.json`: profile IDs/aliases and STT glossary terms.
- `data/translation_profiles.json`: standard and Qwen prompt-profile text.
- `data/translation_corrections.json`: deterministic source/target corrections,
  profile-scoped boundary-aware source aliases, and name-rendering rules,
  including exact IRISÉ canonical output rules.
- `data/scene_stt_terms.json`: activity-specific STT vocabulary.
- `data/fan_terms.json`: reviewed terminology/reference inventory.
- `data/eval_cases.json`: small offline output-quality fixtures.
- `data/replay_eval_snapshot.jsonl`: frozen real-runtime deterministic golden
  set; changes require explicit replay review.
- `data/translation_prompt_benchmark_20260822.json`: current 75-case
  production-derived semantic regression suite. Its maintained offline scorer
  is `scripts/evaluate_translation_prompt_benchmark.py`.

The user's local `config.py` profile selection may be intentionally dirty.
Preserve it and do not stage/commit it unless the user asks.

### Runtime observability and storage

- `logs/runtime_events_YYYYMMDD.jsonl` uses runtime schema v5. Event types
  currently include `audio_startup`, `audio`, `stt`, `sentence`,
  `translation`, `scene`, and `translation_fallback`.
- Every event carries `run_id`, UTC `created_at`, `run_kind`, `git_sha`, and
  `git_dirty`. Use `(run_id, sequence_id)` rather than `sequence_id` alone.
- `run_kind` is `live`, `test`, `replay`, or `benchmark`; analysis should
  normally filter live data explicitly.
- Translation events include attempt chains, selected engine/model, queue and
  API latency, source evidence, profile/activity, correction trace, quality
  flags/classifications, additive profile QA evidence, and subtitle
  emission/suppression.
- Ordinary live-chain mode derives `deepseek-v4-flash -> OpenRouter Qwen ->
  DeepL -> Groq` when `deepseek_route=primary`; `off` restores the exact prior
  Qwen chain. Flash uses its dedicated compact production contract while Qwen
  retains its compact Qwen capsule; both use the same immutable profile,
  activity, history, and current-input message structure. A Flash
  script/meta guard is a sentence-local content rejection, so rejected output
  cannot enter subtitle/cache/history or provider-health state. Attempt rows
  retain the guard reason, corrected candidate preview, candidate-only
  correction trace, and QA classifications; the selected route remains Qwen.
- The runtime analyzer retains read-only support for historical
  `translation_shadow` events, but current production no longer emits them.
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
- Normal runtime teardown automatically exports the current `run_id` to
  `scratch/chatgpt_bundles/` without WAV copies after emitting a terminal
  lifecycle event. Dashboard-forced Stop/close cannot execute Python teardown,
  so Tauri exports the exact launcher-assigned run ID after the child exits.
  Both paths are fail-soft; manual dashboard exports and optional WAV inclusion
  remain available separately.

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
