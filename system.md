# System Architecture

## Project Goal

Real-time Traditional Chinese subtitle translation for Korean live streams.
The pipeline captures system audio, converts speech to Korean text, splits text into translation-ready sentences, translates to zh-TW, and displays subtitles in a floating window.

## Runtime Pipeline

```text
System audio capture (sounddevice + WASAPI)
        ↓
STT engine: Groq whisper-large-v3 primary, SenseVoice-Small fallback (configurable)
        ↓
Sentence splitter: Korean ending rules + time window
        ↓
Translator: engine chain (default: Claude → Gemini → Google Translate)
        ↓
Subtitle display: tkinter overlay on the main thread
        ↓
runtime_events.jsonl: per-event log — `audio` (VAD), `stt`, `sentence` (assembly), `translation`
```

The stages communicate through `queue.Queue` objects.
The pipeline is controlled by `stop_event` and `pause_event`.

## Development Environment

| Item | Path |
|---|---|
| Python virtual environment | `live_translate/live-subtitle-env/` |
| Python interpreter | `live_translate/live-subtitle-env/Scripts/python.exe` |
| Run tests | `live-subtitle-env\Scripts\python.exe -m unittest discover -s tests` |
| Run app | `live-subtitle-env\Scripts\python.exe main.py` |

## Core Files

- `main.py`: entry point, starts threads, handles signals, validates config
- `config.py`: all tunable parameters and API keys

### Pipeline stages (`modules/`)

- `modules/audio_capture.py`: system audio capture and VAD chunking
- `modules/stt.py`: speech-to-text engine (Groq primary by default, SenseVoice optional local engine, configurable via `cfg.stt.primary_engine`)
- `modules/stt_policy.py`: STT output quality filtering (no_speech, logprob, hallucination detection)
- `modules/sentence_splitter.py`: sentence segmentation orchestration
- `modules/sentence_buffer.py`: sentence accumulation, timing windows, force-cut logic
- `modules/pipeline_events.py`: typed pipeline events — `TranscriptionEvent`, `SentenceEvent`
- `modules/translator.py`: translation coordinator (facade)
- `modules/translation_engines.py`: `TranslationEngine` ABC + Gemini, Claude, GoogleTranslate, Ollama, Nvidia implementations
- `modules/translation_runtime.py`: fallback state machine and cache operation functions
- `modules/translation_memory.py`: two-level cache — in-memory LRU → SQLite write-through
- `modules/translation_policy.py`: input pre-processing, STT garbage detection, slang lookup
- `modules/translation_prompts.py`: system prompt construction, Qwen variant, streamer profile injection
- `modules/streamer_profiles.py`: JSON-driven streamer profiles and STT glossary builder
- `modules/db.py`: SQLite persistent translation cache, LRU eviction, WAL mode, schema migration
- `modules/subtitle_display.py`: tkinter subtitle window and pause/resume UI

### Utilities (`utils/`)

- `utils/pipeline.py`: `start_daemon_thread`, `poll_queue`, pause helpers
- `utils/queue_utils.py`: `put_latest()` — drain-all-keep-latest strategy for pipeline queues
- `utils/api_retry.py`: `classify_error()` — shared API error classification
- `utils/metrics.py`: `PipelineMetrics` — thread-safe counters and latency tracking, 60 s summary log
- `utils/runtime_events.py`: `RuntimeEventWriter` + `translation_quality()` — JSONL event log written under `logs/runtime_events_YYYYMMDD.jsonl`; quality flags (`empty_target`, `low_target_cjk`, `long_target_ratio`, plus reference-free `repetitive_target` (degeneration via distinct-bigram ratio), `target_meta_leak` (leaked label/English-refusal scaffolding), `unbalanced_brackets`) accompany every translation outcome and collapse into a single `quality_score` (0.0–1.0) + `quality_severity` (`ok`/`warn`/`bad`) for trend/comparison; `bad`/`warn` outcomes also bump `translation.quality.*` counters in the 60 s metrics summary. Translation events also carry `profile_id` / `profile_applied` (active streamer profile + whether profile-specific rules were applied) and, when the engine returns usage, `token_prompt` / `token_output` / `token_total` / `token_cache_read` / `token_cache_write` (captured uniformly via `translation_engines._log_token_usage`, surfaced by `get_last_token_usage()`). A `utterance_id` (minted once per STT transcription, e.g. `utt-7`) rides the `TranscriptionEvent` → `SentenceEvent` → translation-metadata channel so `stt` and `translation` events for the same utterance can be joined; merged sentence cuts carry the latest source's id. The `sentence` event (emitted by `sentence_splitter`) records how a subtitle line was assembled — `cut_reason` (`natural` / `forced_prefix` / `forced_blob` / `merged:…`), `chunk_count`, total `audio_seconds`, and `incomplete` — so "STT couldn't hear it" is distinguishable from "we force-cut a half sentence too early". On a sent Groq request the `stt` event also carries prompt-budget fields (`prompt_bytes`, `prompt_max_bytes`, `glossary_present`, `glossary_truncated`, `context_present`, `context_included`) from `stt_policy.build_groq_prompt_budget`, so a long glossary crowding out the recent-transcript context is visible. When source-normalization or source-aware target corrections actually fire, the `translation` event lists them as `corrections` (each `{stage, rule, before, after}`, stages `source_norm` / `target_correction` / `name_render`) plus `correction_count`, so you can tell whether `海洞 → 해둥이`-style rescues are routine or rarely needed now. Because a subtitle is usually built from several STT chunks, `sentence` and `translation` events also carry `source_utterance_ids` (every contributing chunk, not just the latest), so a mistranslation can be traced back to each chunk's audio + confidence — essential for separating STT mishearing from translation errors. All events are stamped `schema_version` (currently `2`); filter `== 2` to get only records with the full modern schema and ignore older mixed data. For collecting STT-vs-translation labeling data, `cfg.stt.dump_audio` (off by default) writes each transcribed chunk's audio to `logs/audio_dump/<session>/<utterance_id>.wav` (`utils.audio.write_wav`, stdlib only) so the original speech can be replayed to confirm whether STT misheard
- `utils/text_heuristics.py`: Korean NLP constants (sentence endings, STT garbage patterns, regex)
- `utils/logger.py`: UTF-8 logger for Windows CJK output
- `utils/audio.py`: audio utility functions
- `utils/config_export.py`: exports runtime config to JSON for Tauri dashboard

## Configuration Layout

`config.py` is the single source of truth for all runtime parameters.

- `cfg.keys`: API keys loaded from `.env`
- `cfg.audio`: sample rate, channels, chunking, VAD, queue size
- `cfg.stt`: STT engine, model, device, language, batch size, queue size
- `cfg.splitter`: sentence timing windows
- `cfg.translation`: engine chain, model names per engine, target language, max tokens, queue size, slang table
- `cfg.subtitle`: overlay UI settings, timing, queue size

## Engine Strategy

### STT

- Primary (default): Groq `whisper-large-v3` cloud API — set via `cfg.stt.primary_engine = "groq"`
- Optional local engine: `SenseVoice-Small` (FunASR) — set `cfg.stt.primary_engine = "sensevoice"` to prefer local on a GPU host
- Recovery probe: when running on Groq with a previously-loaded SenseVoice, the engine retries SenseVoice every `_SENSEVOICE_PROBE_EVERY` calls so the host can recover from a transient local failure
- Unavailable propagation: if both engines fail to initialize, the STT thread sets `stop_event` instead of polling forever — `main.py` then unwinds the pipeline cleanly

### Translation

Two layers of selection:

1. **Backend mode** — `cfg.live_engine` / `cfg.clip_engine` (default `"nvidia"`):
   - `"nvidia"` — NIM-hosted Qwen3 as primary, with `engine_chain` as fallback
   - `"ollama"` — local Ollama only, no fallback
   - `"anthropic"` — bypass NIM and run the full `engine_chain` directly
2. **Engine chain** — `cfg.translation.engine_chain`, the ordered fallback list used by the `nvidia` and `anthropic` modes.

Default chain (`config.py`):

| Priority | Engine | Model | Key |
|---|---|---|---|
| 1 | Claude | `claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| 2 | Gemini | `gemini-2.5-flash` | `GEMINI_API_KEY` |
| 3 | Google Translate v2 | `google-translate-v2` | `GOOGLE_TRANSLATE_API_KEY` |

The translator tracks `active_idx` across failures and probes engines[0] every `_FALLBACK_PROBE_EVERY` calls to recover from transient primary outages.

To add a new engine: see the step-by-step guide in `config.py` (`_Translation.engine_chain` comment block).

## Important Behavior

- All pipeline stages should remain non-blocking where possible.
- API failures should not crash the whole app.
- The subtitle overlay must stay responsive even if translation fails temporarily.
- `pause_event` should freeze the pipeline and clear queued stale output.

### Pipeline health

The pipeline relies on each stage either making forward progress or shutting the whole graph down — silent zombie threads are a bug. Current guarantees:

- `audio_capture.start()` resolves the loopback device **synchronously**; a missing device raises out of `start()` so `main.py` can `sys.exit(1)` with a clear error instead of leaving a dead daemon and empty `audio_queue`. Any exception inside the daemon `run()` also sets `stop_event` before returning.
- `STTEngine.available` is `False` when both SenseVoice and Groq failed to initialize. The STT thread checks it on entry and sets `stop_event` rather than spinning on `audio_queue` forever.
- `RuntimeEventWriter.emit()` normalizes non-JSON-native values (numpy scalars, NaN/Inf, custom objects) before serialization and writes a fallback record if `json.dumps` still fails. The daily log filename is derived from the injected `clock` so tests and clock-skewed hosts route events into the right file.

## Test Coverage

The repository includes unit and integration tests for:

- config validation
- audio capture behavior
- sentence splitting
- STT behavior
- translation behavior
- retry logic
- integration pipeline flow

## Current Phase

Phase 1 (persistent cache) is **complete**. Current focus is stability, observability, and Phase 2 dashboard UI.

### Documentation Organization

- **sql.md**: Single source of truth for database schema, behavior, and persistence layer design
- **frontend-design.md**: Single source of truth for desktop dashboard UI (Tauri + Rust + Vue.js)
- **system.md** (this file): Runtime architecture and pipeline design only

## Phase Planning

### Phase 1: Persistent Translation Cache — ✅ Complete

Database layer with SQLite. **Implemented.**

- SQLite schema with WAL mode, `RLock` thread safety, schema migration (v1→v2)
- `modules/db.py` with `TranslationDB` class, LRU eviction
- Integration with `translator.py` (read-through / write-through, `prompt_version` keyed)
- Falls back to in-memory cache if DB unavailable

**Spec:** See `sql.md`

---

### Phase 2: Desktop Dashboard (Frontend UI)

Interactive configuration and monitoring dashboard using Tauri (Rust backend) + Vue.js (frontend).

**Spec:** See `frontend-design.md`

**Architecture:**
- Rust handlers for config, cache stats, Python process control
- Vue.js components for settings, cache analytics, system stats
- WebSocket for real-time updates
- Auto-save configuration to `config.json`

**Deliverables:**
- Tauri app skeleton with Rust handlers
- Vue.js dashboard with 3+ tabs
- Live config editing without restart
- Cache statistics visualization
- Python process lifecycle control

**Timeline:** ~2-3 weeks

---

### Phase 3: Usage Tracking & Statistics

Add session-level and long-term usage tracking.

**Planned Features:**
- `stream_sessions` table for session metadata
- Per-session cost tracking
- API call counting and rate analysis
- Performance metrics (latency, accuracy)
- Dashboard charts and export

---

### Phase 4: Backend Abstraction (Optional)

Prepare for future expansion: server deployment, multi-user, mobile clients.

**Planned Changes:**
- Separate Python core logic from desktop/tkinter UI
- RESTful API layer (Flask) for config and cache access
- gRPC or WebSocket for real-time subtitle streaming
- Docker containerization for cloud deployment

---

## Deployment Targets

### Current (Phase 1-2)

**Windows Standalone:**
- Desktop app (Tauri .exe)
- Python runtime bundled
- WASAPI audio capture
- tkinter overlay UI (supplemented by Tauri dashboard)

### Future Considerations

These are **not** on the immediate roadmap but architecturally supported:

#### Web Browser (Phase 3+)

Replace Tauri with Flask + web dashboard:
- Backend: Python server on localhost
- Frontend: HTML5 + Vue.js (no Tauri needed)
- Audio: Web Audio API from browser (limited, mainly for testing)
- Advantage: works on any OS with browser

#### Cross-Platform Desktop (Phase 4+)

Generalize Tauri app to run on macOS and Linux:
- Audio capture: abstract from WASAPI → support CoreAudio (macOS), PulseAudio (Linux)
- Requires testing on target platforms
- Estimated effort: 2-3 weeks

#### Mobile (Phase 4+, Long-term)

Deploy as server with mobile clients:

**Architecture:**
```
Backend (Python server on Cloud/VPS)
  ├─ STT, Translator, Cache
  └─ REST/WebSocket API

Mobile Client (Flutter)
  ├─ Audio capture (microphone)
  ├─ Send to backend
  └─ Display subtitle overlay
```

**Challenges:**
- Microphone audio quality (vs. loopback capture)
- Network latency (50-200ms added)
- Battery/data usage
- iOS restrictions on background audio capture

**Estimated effort:** 4-6 weeks (Flutter) + server ops

---

## Roadmap

- Phase 1 (1 week): SQLite persistent cache
- Phase 2 (2-3 weeks): Tauri dashboard UI
- Phase 3 (2-3 weeks): Usage tracking and analytics
- Phase 4 (4+ weeks): Backend abstraction and cloud readiness
- Future: Cross-platform desktop, mobile clients (not scheduled)
## Queue backpressure strategy (intentional asymmetry)

| Queue | Strategy | Why |
|-------|----------|-----|
| `audio_queue` | `put_latest` (drain all, newest wins) | Stale audio is worthless for live subtitles; transcribing it would only delay fresh speech. |
| `text_queue` | `put_latest` | Same: an STT fragment that the splitter could not consume in time is already stale. |
| `sentence_queue` | `put_drop_oldest` (drop one) | Sentences are the most valuable unit (already filtered + assembled); keep as much backlog as possible and shed only the oldest. |
| `subtitle_queue` | `put_latest` | Display shows one subtitle at a time; only the newest matters. |

Do not "unify" these — the difference is deliberate (review L12).
