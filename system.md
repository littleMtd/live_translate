# System Architecture

## Project Goal

Real-time Traditional Chinese subtitle translation for Korean live streams.
The pipeline captures system audio, converts speech to Korean text, splits text into translation-ready sentences, translates to zh-TW, and displays subtitles in a floating window.

## Runtime Pipeline

```text
System audio capture (sounddevice + WASAPI)
        ↓
STT engine: SenseVoice-Small primary, Groq fallback
        ↓
Sentence splitter: Korean ending rules + time window
        ↓
Translator: engine chain (default: Google Translate → Gemini → Claude)
        ↓
Subtitle display: tkinter overlay on the main thread
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
- `modules/audio_capture.py`: system audio capture and VAD chunking
- `modules/stt.py`: speech-to-text engine with fallback support
- `modules/sentence_splitter.py`: sentence segmentation and force-cut logic
- `modules/translator.py`: translation engine, cache, slang mapping, fallback logic
- `modules/db.py`: SQLite persistent translation cache (Phase 1), LRU eviction, WAL mode
- `modules/subtitle_display.py`: tkinter subtitle window and pause/resume UI
- `modules/prompt_evolver.py`: optional live prompt enrichment
- `utils/logger.py`: UTF-8 logger for Windows CJK output
- `utils/queue_utils.py`: `drain_put()` — unified drain-all-keep-latest strategy for all pipeline queues
- `utils/api_retry.py`: `classify_error()` + `_RETRY_DELAYS` — shared API error classification and backoff constants

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

- Primary: `SenseVoice-Small` local engine
- Fallback: Groq `whisper-large-v3`
- Goal: keep STT mostly local to control latency and cost

### Translation

Ordered engine chain configured in `cfg.translation.engine_chain` (`config.py`).

| Priority | Engine | Model | Key |
|---|---|---|---|
| 1 | Google Translate v2 | google-translate-v2 | `GOOGLE_TRANSLATE_API_KEY` |
| 2 | Gemini | gemini-2.5-flash | `GEMINI_API_KEY` |
| 3 | Claude | claude-haiku-4-5 | `ANTHROPIC_API_KEY` |

To add a new engine: see the step-by-step guide in `config.py` (`_Translation.engine_chain` comment block).

## Important Behavior

- All pipeline stages should remain non-blocking where possible.
- API failures should not crash the whole app.
- The subtitle overlay must stay responsive even if translation fails temporarily.
- `pause_event` should freeze the pipeline and clear queued stale output.

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