import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class _Keys:
    anthropic:        str = os.environ.get("ANTHROPIC_API_KEY", "")
    groq:             str = os.environ.get("GROQ_API_KEY", "")
    gemini:           str = os.environ.get("GEMINI_API_KEY", "")
    deepseek:         str = os.environ.get("DEEPSEEK_API_KEY", "")
    deepl:            str = os.environ.get("DEEPL_API_KEY", "")
    google_translate: str = os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
    nvidia:           str = os.environ.get("NVIDIA_API_KEY", "")


@dataclass(frozen=True)
class _Audio:
    sample_rate:      int   = 16000
    channels:         int   = 1
    chunk_seconds:    int   = 3        # used only when vad_enabled=False
    volume_threshold: float = 0.01     # RMS threshold — speech vs silence
    device_name:      str   = "CABLE Output"
    queue_maxsize:    int   = 10
    # VAD settings
    vad_enabled:           bool  = True
    vad_silence_sec:       float = 0.6   # silence duration that triggers a cut
    vad_min_speech_sec:    float = 0.4   # discard chunks shorter than this
    vad_max_speech_sec:    float = 8.0   # force cut even without silence
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
    groq_prompt:       str = ""   # hint for Whisper: streamer names, slang, etc.
    batch_size_s:      int = 60
    queue_maxsize:     int = 20


@dataclass(frozen=True)
class _Splitter:
    min_wait_seconds:  int = 3
    force_cut_seconds: int = 8


_DEFAULT_SLANG: MappingProxyType = MappingProxyType({
    # Laughs / chat shorthand
    "ㅋㅋ":    "哈哈",
    "ㅋㅋㅋㅋ": "哈哈哈",
    "ㅎㅎ":    "呵呵",
    "ㄱㄱ":    "好嘞",
    "ㅇㅋ":    "OK",

    # Common exclamations / reactions
    "대박":    "太狂了",
    "대애박":  "超級狂",
    "헐":      "天啊",
    "진짜":    "真的",
    "아이구":  "哎呀",
    "어머":    "天哪",
    "와우":    "哇喔",
    "우와":    "哇",

    # Casual confirmation / encouragement
    "화이팅":  "加油",
    "파이팅":  "加油",
    "잘하자":  "好好加油",

    # Slang for surprise / disbelief
    "헐 대박": "我的天，太誇張了",

    # Game/chat terms often left untranslated but mapped for readability
    "방송":    "直播",
    "선물":    "禮物",
    "구독":    "訂閱",
    "좋아요":  "按讚",

    # Shortened words / casual forms
    "ㄷㄷ":    "震驚",
    "ㅂㅂ":    "掰掰",
    "ㄴㄴ":    "不要",

    # Emphasis / intensifiers
    "완전":    "完全",
    "완전 짱": "超棒",

    # Common memetic phrases
    "아싸":    "耶（成功）",
    "대박이네": "太猛了",

    # Placeholder for romanized phrases often used by streamers
    "jjajang": "炸醬麵",
    "annyeong": "你好",

    # Add more as needed — evolve or extract from DB later
})


_VALID_STREAMER_PROFILES = {"", "stellive_hina", "isegye_lilpa", "hades_chxxnnx", "mwmeu"}
_VALID_TRANSLATION_MODES = {"live", "clip"}
_VALID_ENGINE_NAMES      = {"gemini", "claude", "google_translate", "ollama", "nvidia"}
_VALID_BACKEND_MODES     = {"anthropic", "ollama", "nvidia"}


@dataclass(frozen=True)
class _Translation:
    # -------------------------------------------------------------------------
    # Engine chain — ordered fallback list; first available engine is primary.
    #
    # Supported names (must match keys in _make_engine() in translator.py):
    #   "gemini"           — Google Gemini        (needs GEMINI_API_KEY)
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
    engine_chain:   tuple        = ("claude", "gemini", "google_translate")

    # --- Model / API settings (one block per engine) -------------------------
    # Claude model selection (change to switch modes):
    #   "claude-sonnet-4-6"          — quality mode  (cache kicks in at ≥ 2048 sys-tokens)
    #   "claude-haiku-4-5-20251001"  — economy mode  (cache kicks in at ≥ 4096 sys-tokens)
    model:                    str = "claude-haiku-4-5-20251001"
    # Gemini
    gemini_model:             str = "gemini-2.5-flash"
    # Google Translate v2 — target lang uses BCP-47 (zh-TW is supported)
    google_translate_lang:    str = "zh-TW"
    # DeepSeek  (uncomment when DeepSeekEngine is implemented)
    # deepseek_model: str        = "deepseek-chat"
    # DeepL     (target lang uses a different code from target_lang below)
    # deepl_target_lang: str     = "ZH"   # DeepL uses "ZH" not "zh-TW"

    # --- Shared translation settings -----------------------------------------
    target_lang:    str          = "zh-TW"
    max_tokens:     int          = 150
    temperature:    float        = 0.0
    queue_maxsize:  int          = 2
    context_window: int          = 3     # recent translations passed as context to LLM
    # Translation mode — controls the STT correction section in the system prompt.
    # Options: "live" (default, real-time STT noise handling), "clip" (conservative, preserves structure)
    translation_mode: str        = "clip"
    # Streamer-specific few-shot profile appended to base prompt.
    # Options: "" (general only), "stellive_hina", "isegye_lilpa", "hades_chxxnnx", "mwmeu"
    streamer_profile: str        = "stellive_hina"
    use_profile:      bool       = True   # set False to strip profile regardless of streamer_profile
    evolve_enabled: bool         = False
    evolve_every:   int          = 20
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
    queue_maxsize:    int   = 10


@dataclass(frozen=True)
class _Database:
    db_path:           str = str(Path(__file__).resolve().parent / "logs" / "live_translate.db")
    db_cache_max_rows: int = 50_000   # LRU eviction threshold (by last_used_at)


@dataclass(frozen=True)
class _Ollama:
    base_url: str = "http://localhost:11434"
    model:    str = "qwen2.5:3b"
    timeout:  int = 60


@dataclass(frozen=True)
class _Nvidia:
    # Model name from build.nvidia.com — click any model → "API" tab for exact name
    model:   str = "qwen/qwen3.5-122b-a10b"
    timeout: int = 30


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
    live_engine:         str          = "anthropic"
    clip_engine:         str          = "nvidia"
    ollama:              _Ollama      = field(default_factory=_Ollama)
    nvidia:              _Nvidia      = field(default_factory=_Nvidia)
    thread_join_timeout: int          = 5

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


cfg = _Config()
