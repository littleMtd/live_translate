import os
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from dotenv import load_dotenv
from modules.streamer_profiles import known_profile_ids

load_dotenv()


@dataclass(frozen=True)
class _Keys:
    anthropic:        str = os.environ.get("ANTHROPIC_API_KEY", "")
    groq:             str = os.environ.get("GROQ_API_KEY", "")
    groq_fallback:    str = os.environ.get("GROQ_API_KEY_fall_back", "")
    openrouter:       str = os.environ.get("OPENROUTER_API_KEY", "")
    deepseek:         str = os.environ.get("DEEPSEEK_API_KEY", "")
    deepl:            str = os.environ.get("DEEPL_API_KEY", "")
    google_translate: str = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    nvidia:           str = os.environ.get("NVIDIA_API_KEY", "")


@dataclass(frozen=True)
class _Audio:
    sample_rate:      int   = 16000
    channels:         int   = 1        # mono after downmix, as sent to VAD/STT
    capture_channels: int   = 2        # capture stereo when available, then downmix
    chunk_seconds:    int   = 3        # used only when vad_enabled=False
    volume_threshold: float = 0.01     # RMS threshold — speech vs silence
    device_name:      str   = "CABLE Output"
    queue_maxsize:    int   = 10
    # VAD settings
    vad_enabled:           bool  = True
    vad_silence_sec:       float = 0.90  # silence duration that triggers a cut
    vad_min_speech_sec:    float = 0.8   # discard chunks shorter than this
    vad_near_miss_min_speech_sec: float = 0.3  # retain overlap for short speech below STT length
    # Prefer complete speech turns over minimum latency. Runtime logs showed
    # 7-10s chunks keep better sentence coherence for chaotic livestream speech.
    vad_max_speech_sec:    float = 6.5
    vad_hard_max_speech_sec: float = 9.0
    vad_overlap_sec:       float = 1.0
    vad_near_miss_overlap_sec: float = 1.5
    vad_silence_overlap_sec: float = 0.4
    vad_adaptive_enabled:   bool  = True
    vad_adaptive_after_boundary_cuts: int = 1
    vad_adaptive_silence_sec: float = 1.1
    vad_adaptive_max_speech_sec: float = 7.5
    vad_adaptive_hard_max_speech_sec: float = 10.0
    vad_adaptive_overlap_sec: float = 1.2
    stt_normalize_enabled: bool  = True
    stt_target_rms:       float = 0.08
    stt_max_gain:         float = 4.0
    stt_peak_limit:       float = 0.95
    # Silero VAD — used when vad_enabled=True and torch is available
    # Falls back to RMS automatically if torch.hub download fails.
    vad_silero_threshold:  float = 0.5   # speech probability threshold (0–1)


@dataclass(frozen=True)
class _STT:
    primary_engine:    str = "groq"            # "sensevoice" or "groq"
    sensevoice_model:  str = "iic/SenseVoiceSmall"
    sensevoice_device: str = "cuda"
    groq_model:        str = "whisper-large-v3"
    language:          str = "ko"
    groq_prompt:            str   = (
        "Korean gaming livestream speech. Transcribe spoken Korean in Hangul only; "
        "do not translate, summarize, or add captions/outro/ad boilerplate."
    )
    groq_timeout:           float = 10.0
    groq_max_retries:       int   = 0
    groq_rate_limit_cooldown_sec: float = 60.0
    groq_daily_request_limit: int = 2000
    use_profile_glossary:   bool  = True
    # Collection-mode only: dump each transcribed chunk's audio to
    # logs/audio_dump/<session>/<utterance_id>.wav so STT-vs-translation error
    # attribution can be verified by replaying the original speech. Off by
    # default; enable a labeling run with env LIVE_TRANSLATE_DUMP_AUDIO=1 (no
    # code edit needed). Writes one wav per utterance.
    dump_audio:             bool  = os.environ.get("LIVE_TRANSLATE_DUMP_AUDIO", "") == "1"
    batch_size_s:           int   = 60
    queue_maxsize:          int   = 20
    # Groq verbose_json confidence filters
    no_speech_threshold:    float = 0.6    # reject if avg no_speech_prob exceeds this
    avg_logprob_threshold:  float = -1.0   # reject if avg_logprob below this
    context_avg_logprob_threshold: float = -0.7
    context_no_speech_threshold:   float = 0.3
    context_max_age_sec:           float = 30.0
    context_min_chars:             int   = 4
    dedupe_by_timestamp:           bool  = True
    # Listen mode keeps STT-only output usable for songs/music, where Whisper
    # confidence is usually lower than normal speech.
    listen_no_speech_threshold:    float = 0.8
    listen_avg_logprob_threshold:  float = -2.0
    listen_groq_prompt:            str   = (
        "Korean song lyrics or speech. Transcribe in Korean Hangul only; "
        "keep lyric lines exactly; do not translate."
    )
    # Post-transcription sanity checks
    max_japanese_chars:     int   = 2      # reject if Japanese kana chars exceed this
    max_repeat_ratio:       float = 0.7    # reject if a repeated phrase fills > this fraction


@dataclass(frozen=True)
class _Splitter:
    min_wait_seconds:  int = 3
    force_cut_seconds: int = 8
    max_merge_source_count: int = 2
    max_merge_text_chars: int = 120
    segment_gap_split_enabled: bool = True
    segment_gap_seconds: float = 0.6
    silence_complete_enabled: bool = True
    pending_incomplete_timeout_seconds: float = 8.0


_DEFAULT_SLANG_PATH = Path(__file__).resolve().parent / "data" / "default_slang.json"


def _load_default_slang(path: Path = _DEFAULT_SLANG_PATH) -> MappingProxyType:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"default slang data must be a non-empty object: {path}")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
        raise ValueError(f"default slang data must map strings to strings: {path}")
    return MappingProxyType(data)


_DEFAULT_SLANG: MappingProxyType = _load_default_slang()


_VALID_STREAMER_PROFILES = known_profile_ids()
_VALID_TRANSLATION_MODES = {"live", "clip"}
_VALID_ENGINE_NAMES      = {"claude", "google_translate", "ollama", "nvidia", "groq", "openrouter"}
_VALID_BACKEND_MODES     = {"anthropic", "ollama", "nvidia"}


@dataclass(frozen=True)
class _Translation:
    # -------------------------------------------------------------------------
    # Engine chain — ordered fallback list; first available engine is primary.
    #
    # Supported names (must match keys in _make_engine() in translator.py):
    #   "claude"           — Anthropic Claude     (needs ANTHROPIC_API_KEY)
    #   "google_translate" — Google Translate v2  (needs GOOGLE_TRANSLATE_API_KEY)
    #   "deepseek"         — DeepSeek Chat        (needs DEEPSEEK_API_KEY)   [not yet impl.]
    #   "deepl"            — DeepL                (needs DEEPL_API_KEY)       [not yet impl.]
    #
    # To add a new engine:
    #   1. Add its name to engine_chain below.
    #   2. Add its model/config field(s) in this class (see examples below).
    #   3. Implement a TranslationEngine subclass in modules/translator.py.
    #   4. Register the name in _make_engine() in translator.py.
    # -------------------------------------------------------------------------
    # Fallback chain when live_engine="nvidia" times out.
    # groq first: runtime data (0620-0626) shows openrouter completes at
    # p50 ~12s every day it is used — useless for live subtitles — while
    # groq completes at ~1s. openrouter stays as the last resort.
    engine_chain:   tuple        = ("groq", "openrouter")

    # --- Model / API settings (one block per engine) -------------------------
    # Claude model selection (change to switch modes):
    #   "claude-sonnet-4-6"          — quality mode  (cache kicks in at ≥ 2048 sys-tokens)
    #   "claude-haiku-4-5-20251001"  — economy mode  (cache kicks in at ≥ 4096 sys-tokens)
    model:                    str = "claude-sonnet-4-6"
    claude_timeout:           float = 5.0   # per-request timeout (seconds) for ClaudeEngine
    # Groq fallback (uses GROQ_API_KEY_fall_back)
    groq_translation_model:   str = "qwen/qwen3-32b"
    groq_translation_timeout: int = 12
    # Keep Groq fallback below on-demand TPM limits. NVIDIA remains the quality path.
    groq_translation_compact_prompt: bool = True
    groq_translation_max_tokens: int = 512
    groq_translation_retry_max_tokens: int = 256
    groq_translation_context_window: int = 2
    groq_translation_history_source_chars: int = 160
    groq_translation_history_target_chars: int = 220
    # OpenRouter fallback (uses OPENROUTER_API_KEY). Paid model, called only
    # after higher-priority engines in the active chain fail.
    openrouter_model: str = "qwen/qwen3-30b-a3b-instruct-2507"
    openrouter_timeout: int = 8
    openrouter_compact_prompt: bool = True
    openrouter_max_tokens: int = 512
    openrouter_context_window: int = 2
    openrouter_history_source_chars: int = 160
    openrouter_history_target_chars: int = 220
    openrouter_http_referer: str = "http://localhost/live_translate"
    openrouter_app_name: str = "live_translate"
    # Google Translate v2 — target lang uses BCP-47 (zh-TW is supported)
    google_translate_lang:    str = "zh-TW"
    # DeepSeek  (uncomment when DeepSeekEngine is implemented)
    # deepseek_model: str        = "deepseek-chat"
    # DeepL     (target lang uses a different code from target_lang below)
    # deepl_target_lang: str     = "ZH"   # DeepL uses "ZH" not "zh-TW"

    # --- Shared translation settings -----------------------------------------
    target_lang:    str          = "zh-TW"
    max_tokens:     int          = 200
    temperature:    float        = 0.1
    queue_maxsize:  int          = 8
    context_window: int          = 10  # recent translations passed as context to LLM
    # Safety cap on per-input length. Oversized inputs are almost always STT
    # hallucinations (repeated phrases, chunk-boundary glitches). Rejecting them
    # at the policy layer prevents a single bad input from burning a day's
    # token budget. Counted in characters, not tokens.
    max_translate_chars: int     = 500
    max_subtitle_output_delay_ms: int = 30000
    # Translation mode — controls the STT correction section in the system prompt.
    # Options: "live" (default, real-time STT noise handling), "clip" (conservative, preserves structure)
    translation_mode: str        = "live"
    # Streamer-specific few-shot profile appended to base prompt.
    # Options: "" (general only), "stellive_hina", "isegye_lilpa", "hades_chxxnnx", "mwmeu","url"
    streamer_profile: str        = "hades_chxxnnx"
    use_profile:      bool       = True   # set False to strip profile regardless of streamer_profile
    # Manual session state: what the streamer is doing right now (e.g.
    # "StarCraft", "tier list talk"). Injected into the system prompt as one
    # labeled background line to disambiguate game/context terms — never as
    # text to translate. Empty = omitted. Low-frequency by design: set it by
    # hand (config or dashboard JSON); no screen text is ever fed as context.
    current_activity: str        = ""
    # Act on the QE signal at runtime: when the reference-free heuristics rate
    # an API result "bad" (Hangul leak / repetition / meta shapes), ask one
    # different engine for a second opinion and keep whichever scores better.
    # Detectable-bad is ~0.2% of sentences, so the retry cost is negligible.
    quality_retry_enabled: bool  = True
    slang:          MappingProxyType = field(default_factory=lambda: _DEFAULT_SLANG)

    def __post_init__(self):
        if self.translation_mode not in _VALID_TRANSLATION_MODES:
            raise ValueError(
                f"cfg.translation.translation_mode invalid: {self.translation_mode!r} "
                f"(must be one of {_VALID_TRANSLATION_MODES})"
            )
        if self.streamer_profile not in _VALID_STREAMER_PROFILES:
            raise ValueError(
                f"cfg.translation.streamer_profile invalid: {self.streamer_profile!r} "
                f"(must be one of {_VALID_STREAMER_PROFILES})"
            )
        for name in self.engine_chain:
            if name not in _VALID_ENGINE_NAMES:
                raise ValueError(
                    f"cfg.translation.engine_chain contains unknown engine {name!r} "
                    f"(must be one of {_VALID_ENGINE_NAMES})"
                )


@dataclass(frozen=True)
class _Subtitle:
    idle_hide_ms:     int   = 30000 # hide after this ms if no new translation arrives
    font:             tuple = ("Microsoft JhengHei", 22, "bold")
    bg:               str   = "#010101"   # transparent-key colour (near-black); do not use #000000
    ctrl_bg:          str   = "#1a1a1a"   # control bar background (stays visible)
    fg:               str   = "#FFFFFF"
    outline_color:    str   = "#000000"
    outline_width:    int   = 2           # pixels; 1 = thin, 2 = standard, 3 = thick
    alpha:            float = 1.0         # full window opacity; bg transparency via -transparentcolor
    max_width_chars:  int   = 36
    wraplength:       int   = 700
    padx:             int   = 16
    pady:             int   = 8
    init_offset_x:    int   = 400   # distance from screen centre
    init_offset_y:    int   = 160   # distance from screen bottom
    poll_interval_ms: int   = 100
    min_display_ms:   int   = 1500  # minimum ms a subtitle stays before being replaced
    ms_per_char:      int   = 80    # additional ms per character (reading speed guard)
    queue_maxsize:    int   = 10


@dataclass(frozen=True)
class _Database:
    db_path:           str = str(Path(__file__).resolve().parent / "logs" / "live_translate.db")
    db_cache_max_rows: int = 50_000   # LRU eviction threshold (by last_used_at)
    # Live speech almost never repeats verbatim: 17 days of live data showed a
    # 0.45% DB hit rate (45 reuses / ~10k rows), all trivial interjections.
    # Disable the SQLite cache layer for live mode by default; clip mode
    # (replayed segments genuinely repeat) keeps using it. Existing rows are
    # kept on disk as a ko->zh corpus.
    live_db_cache: bool = False


@dataclass(frozen=True)
class _Ollama:
    base_url: str = "http://localhost:11434"
    model:    str = "qwen2.5:3b"
    timeout:  int = 60


@dataclass(frozen=True)
class _Nvidia:
    # Model name from build.nvidia.com — click any model → "API" tab for exact name
    model:   str = "qwen/qwen3-next-80b-a3b-instruct"
    # Clip/offline timeout; live mode uses live_timeout below when set.
    timeout: int = 10
    # Live override: fail fast so fallback engines can take over when NIM is degraded.
    live_timeout: int = 5


@dataclass(frozen=True)
class _Scene:
    """Automatic scene-context updater (modules/scene_context.py).

    Distills the screen into cfg.translation.current_activity by asking a
    vision model "what game/activity is this" on a sampled frame, a few times
    per hour. Only that short conclusion ever reaches the prompt — screen
    text (chat, donations) is never fed as context.
    """
    enabled:              bool  = True
    check_interval_sec:   float = 20.0    # cheap fingerprint check cadence
    min_call_gap_sec:     float = 180.0   # at most one vision call per gap
    refresh_interval_sec: float = 600.0   # re-ask even without a scene change
    change_threshold:     float = 12.0    # mean abs diff on 64x64 grayscale
    # Groq OpenAI-compatible endpoint; uses cfg.keys.groq (fallback key as backup).
    vision_model:         str   = "meta-llama/llama-4-scout-17b-16e-instruct"
    vision_timeout:       float = 20.0
    max_activity_chars:   int   = 40


@dataclass(frozen=True)
class _Config:
    keys:                _Keys        = field(default_factory=_Keys)
    audio:               _Audio       = field(default_factory=_Audio)
    stt:                 _STT         = field(default_factory=_STT)
    splitter:            _Splitter    = field(default_factory=_Splitter)
    translation:         _Translation = field(default_factory=_Translation)
    subtitle:            _Subtitle    = field(default_factory=_Subtitle)
    database:            _Database    = field(default_factory=_Database)
    # Translation backend per mode — options: "anthropic" | "ollama" | "nvidia"
    # "anthropic" uses engine_chain (with fallback); "ollama"/"nvidia" bypass it entirely.
    live_engine:         str          = "nvidia"
    clip_engine:         str          = "nvidia"
    ollama:              _Ollama      = field(default_factory=_Ollama)
    nvidia:              _Nvidia      = field(default_factory=_Nvidia)
    scene:               _Scene       = field(default_factory=_Scene)
    thread_join_timeout: int          = 5

    @property
    def active_streamer_profile(self) -> str:
        return self.translation.streamer_profile

    def __post_init__(self):
        if self.live_engine not in _VALID_BACKEND_MODES:
            raise ValueError(
                f"cfg.live_engine invalid: {self.live_engine!r} "
                f"(must be one of {_VALID_BACKEND_MODES})"
            )
        if self.clip_engine not in _VALID_BACKEND_MODES:
            raise ValueError(
                f"cfg.clip_engine invalid: {self.clip_engine!r} "
                f"(must be one of {_VALID_BACKEND_MODES})"
            )


_DASHBOARD_CONFIG_JSON = Path(__file__).parent / "logs" / "live_translate_config.json"
_DASHBOARD_OVERRIDE_ENV = "LIVE_TRANSLATE_APPLY_DASHBOARD_CONFIG"

# Only these dashboard-editable fields (src-frontend ConfigPanel.vue) may override
# config.py. config.py stays the single source of truth for everything else; the JSON
# cannot reach any field outside this whitelist.
# subtitle font is stored as a `font` tuple in config.py but flattened into
# font_family/font_size/font_style by config_export; the override reverses that.
_DASHBOARD_OVERRIDE_FIELDS = {
    "audio": ("vad_enabled", "vad_silence_sec", "vad_max_speech_sec"),
    "stt": ("primary_engine",),
    "translation": ("engine_chain", "translation_mode", "max_tokens", "target_lang",
                    "current_activity"),
    "subtitle": ("idle_hide_ms", "alpha"),
}
_DASHBOARD_OVERRIDE_TOP = ("live_engine",)


def _apply_dashboard_overrides(base: "_Config", json_path: Path = _DASHBOARD_CONFIG_JSON) -> "_Config":
    """Merge dashboard-edited fields from live_translate_config.json onto ``base``.

    Scoped to the ConfigPanel-editable whitelist. Any read/parse/validation failure
    falls back to the unmodified ``base`` so a stale or malformed JSON never breaks
    startup. Applied only when the launching process opts in via the env var (the Tauri
    "Start Python" button sets it); manual ``python main.py`` runs use pure config.py.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return base
    if not isinstance(data, dict):
        return base

    section_updates: dict[str, object] = {}
    for section, fields_ in _DASHBOARD_OVERRIDE_FIELDS.items():
        sub = data.get(section)
        if not isinstance(sub, dict):
            continue
        changes = {name: sub[name] for name in fields_ if name in sub}
        if isinstance(changes.get("engine_chain"), list):
            changes["engine_chain"] = tuple(changes["engine_chain"])
        if section == "subtitle" and any(
            key in sub for key in ("font_family", "font_size", "font_style")
        ):
            base_font = base.subtitle.font
            changes["font"] = (
                sub.get("font_family", base_font[0] if len(base_font) > 0 else "Microsoft JhengHei"),
                sub.get("font_size", base_font[1] if len(base_font) > 1 else 22),
                sub.get("font_style", base_font[2] if len(base_font) > 2 else "bold"),
            )
        if changes:
            try:
                section_updates[section] = replace(getattr(base, section), **changes)
            except (TypeError, ValueError):
                return base
    top_changes = {name: data[name] for name in _DASHBOARD_OVERRIDE_TOP if name in data}
    if not section_updates and not top_changes:
        return base
    try:
        return replace(base, **section_updates, **top_changes)
    except (TypeError, ValueError):
        return base


cfg = _Config()
if os.environ.get(_DASHBOARD_OVERRIDE_ENV):
    cfg = _apply_dashboard_overrides(cfg)
