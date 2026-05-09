# live_translate

Real-time Korean → Traditional Chinese subtitle overlay for live streams.

Captures system audio, transcribes Korean speech, and displays a floating subtitle window — no OBS plugin, no browser extension required.

---

## Features

- **Floating subtitle overlay** — transparent tkinter window, always on top, draggable
- **Multiple STT engines** — SenseVoice-Small (local GPU) or Groq Whisper (cloud fallback)
- **Multiple translation backends** — Gemini, Claude, Google Translate, Ollama (local), NVIDIA NIM
- **Per-mode engine selection** — configure different backends for live vs. clip mode
- **Streamer profiles** — built-in few-shot prompt sets for specific VTubers (스텔라이브 히나, 릴파, 챈나, MW:MEU)
- **Persistent translation cache** — SQLite with LRU eviction; repeat sentences cost zero API tokens
- **Prompt caching** — Anthropic prompt cache enabled in live mode (reduces token cost ~90%)
- **Pause / resume** — spacebar or toggle button freezes the pipeline without closing the window
- **Tauri dashboard** — optional desktop UI for live config editing and cache stats

---

## Requirements

| Requirement | Notes |
|-------------|-------|
| Windows 10/11 | WASAPI loopback capture; macOS/Linux not supported |
| Python 3.11+ | Uses `str \| None` union syntax |
| Virtual audio cable | [VB-Cable](https://vb-audio.com/Cable/) (free) or NVIDIA RTX Voice |
| GPU (optional) | Required for SenseVoice local STT; ~2 GB VRAM minimum |
| Rust + Node.js | Required only to build the Tauri dashboard |

---

## Setup

### 1. Clone and create virtual environment

```bash
git clone https://github.com/littleMtd/live_translate.git
cd live_translate
python -m venv live-subtitle-env
live-subtitle-env\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

> For SenseVoice local STT, install PyTorch with CUDA separately:
> ```bash
> pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
> ```

### 3. Set up API keys

Copy `.env.example` to `.env` and fill in the keys you plan to use:

```bash
copy .env.example .env
```

```env
ANTHROPIC_API_KEY=...
GROQ_API_KEY=...
GEMINI_API_KEY=...
GOOGLE_TRANSLATE_API_KEY=...
NVIDIA_API_KEY=...
```

You only need keys for the engines you actually use. At minimum, one translation engine key is required.

### 4. Configure audio input

Open `config.py` and set `device_name` to your virtual audio cable output:

```python
@dataclass(frozen=True)
class _Audio:
    device_name: str = "CABLE Output"   # match your virtual cable name exactly
```

To list available devices, run:

```bash
python -c "import sounddevice; print(sounddevice.query_devices())"
```

---

## Usage

```bash
# Full pipeline — STT + translation + subtitle overlay
live-subtitle-env\Scripts\python.exe main.py

# STT-only mode — print recognised sentences without translation (useful for tuning)
live-subtitle-env\Scripts\python.exe main.py --stt-only
```

### Subtitle window controls

| Action | Effect |
|--------|--------|
| `Space` or toggle button | Pause / resume the pipeline |
| Drag | Move the window anywhere on screen |
| `Esc` or double-click | Quit |

---

## Tauri Dashboard (Optional)

The Tauri dashboard is a standalone desktop app that lets you control the pipeline and inspect the cache without touching the terminal.

### What you can do

| Feature | Details |
|---------|---------|
| Start / Stop pipeline | Launches or terminates `main.py` as a subprocess |
| Config editor | Edit subtitle appearance, translation settings, and STT options; changes are written to `logs/live_translate_config.json` |
| Cache stats | Shows total entries, total cache hits, last-used timestamp, and DB file size |
| Clear cache | Deletes all rows from the translation DB |

> **Note:** Config changes made in the dashboard take effect on the **next Python restart** — they write to the JSON export file, not directly to `config.py`.

> **Note:** The dashboard reads config from `logs/live_translate_config.json`, which Python writes on startup. Open the dashboard **after** running `main.py` at least once, otherwise the config panel will show a "Config not found — run Python first" error.

### Prerequisites

| Tool | Notes |
|------|-------|
| [Rust](https://rustup.rs/) | Stable toolchain; install via `rustup` |
| [Node.js](https://nodejs.org/) | v18+ recommended |

### Run in development mode

```bash
# 1. Install frontend dependencies (first time only)
cd src-frontend
npm install
cd ..

# 2. Launch dashboard (starts Vite dev server + Tauri window)
cd src-tauri
cargo tauri dev
```

The Tauri window opens at `http://localhost:5173`. Hot-reload is active for Vue component changes.

### Build a distributable binary

```bash
cd src-tauri
cargo tauri build
```

The installer and standalone `.exe` are placed in `src-tauri/target/release/bundle/`.

---

## Configuration

All settings live in [`config.py`](config.py). The most commonly changed options:

### Choose translation engine

```python
# Per-mode engine: "anthropic" (Gemini/Claude chain) | "ollama" | "nvidia"
live_engine: str = "anthropic"   # used when translation_mode = "live"
clip_engine: str = "nvidia"      # used when translation_mode = "clip"
```

### Choose translation mode

```python
translation_mode: str = "live"   # "live" (real-time, less conservative)
                                 # "clip" (preserves structure, better for subtitled clips)
```

### Choose STT engine

```python
primary_engine: str = "groq"          # "groq" (cloud) | "sensevoice" (local GPU)
groq_model:     str = "whisper-large-v3"
```

### Enable streamer profile

Streamer-specific few-shot examples improve translation accuracy for fandom vocabulary:

```python
streamer_profile: str = "stellive_hina"   # see table below
use_profile:      bool = True
```

| Profile key | Streamer |
|-------------|----------|
| `"stellive_hina"` | 스텔라이브 시라유키 히나 |
| `"isegye_lilpa"` | 이세계아이돌 / 릴파 |
| `"hades_chxxnnx"` | HADES / 챈나 |
| `"mwmeu"` | MW:MEU |
| `""` | General (no profile) |

### Use a local model (Ollama)

```bash
ollama pull qwen2.5:3b   # or any model from ollama.com/library
ollama serve
```

```python
live_engine: str = "ollama"
# In _Ollama:
model: str = "qwen2.5:3b"
```

---

## Translation Engine Reference

| Engine | Key required | Notes |
|--------|-------------|-------|
| Claude (Haiku / Sonnet) | `ANTHROPIC_API_KEY` | Prompt caching in live mode |
| Gemini Flash | `GEMINI_API_KEY` | Fast, cost-effective |
| Google Translate v2 | `GOOGLE_TRANSLATE_API_KEY` | No LLM context; fastest fallback |
| Ollama | — | Fully local; needs `ollama serve` running |
| NVIDIA NIM | `NVIDIA_API_KEY` | Cloud-hosted open models; free tier available |

The `engine_chain` in `config.py` controls fallback order when using `live_engine = "anthropic"`.

---

## Project Structure

```
live_translate/
├── main.py                  # Entry point
├── config.py                # All runtime settings
├── .env                     # API keys (not committed)
├── .env.example             # Template
├── modules/
│   ├── audio_capture.py     # WASAPI loopback + VAD
│   ├── stt.py               # Speech-to-text (SenseVoice / Groq)
│   ├── sentence_splitter.py # Korean sentence segmentation
│   ├── translator.py        # Translation engines + cache
│   ├── db.py                # SQLite persistent cache
│   ├── subtitle_display.py  # Floating tkinter overlay
│   └── prompt_evolver.py    # Optional live prompt enrichment
├── utils/
│   ├── logger.py            # UTF-8 logger for Windows
│   ├── queue_utils.py       # drain_put helper
│   ├── api_retry.py         # Error classification + backoff
│   └── config_export.py     # Export config to JSON for Tauri
├── src-tauri/               # Tauri Rust backend (optional dashboard)
├── src-frontend/            # Vue.js dashboard frontend (optional)
├── tests/                   # Unit + integration tests
└── logs/                    # Translation history + DB (auto-created)
```

---

## Running Tests

```bash
live-subtitle-env\Scripts\python.exe -m pytest tests/ -q
```

---

## License

MIT
