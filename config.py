import os
import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from dotenv import load_dotenv
from modules.streamer_profiles import canonical_profile_id, known_profile_ids

load_dotenv()


@dataclass(frozen=True)
class _Keys:
    anthropic:        str = os.environ.get("ANTHROPIC_API_KEY", "")
    groq:             str = os.environ.get("GROQ_API_KEY", "")
    groq_fallback:    str = os.environ.get("GROQ_API_KEY_fall_back", "")
    # Prefer ElevenLabs' documented spelling, while accepting the spelling
    # already used by this workspace for a migration-safe rollout.
    elevenlabs:       str = (
        os.environ.get("ELEVENLABS_API_KEY", "")
        or os.environ.get("ElevenLabs_API_KEY", "")
    )
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
    primary_engine:    str = "elevenlabs"      # "elevenlabs", "groq", or "sensevoice"
    sensevoice_model:  str = "iic/SenseVoiceSmall"
    sensevoice_device: str = "cuda"
    groq_model:        str = "whisper-large-v3"
    elevenlabs_model:  str = "scribe_v2"
    elevenlabs_timeout: float = 15.0
    elevenlabs_failure_cooldown_sec: float = 30.0
    # Keep this at or below 100: ElevenLabs applies a 20-second minimum
    # billable duration to requests containing more than 100 keyterms.
    elevenlabs_max_keyterms: int = 100
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


_VALID_SEMANTIC_EARLY_CUT_MODES = {"off", "shadow"}


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
    provisional_enabled: bool = True
    provisional_hold_seconds: float = 1.75
    provisional_queue_maxsize: int = 4
    # T20's shadow gate did not clear the frozen activation criteria. Keep the
    # record-only diagnostic explicit; no "active" value is accepted.
    semantic_early_cut_mode: str = "off"

    def __post_init__(self):
        if self.semantic_early_cut_mode not in _VALID_SEMANTIC_EARLY_CUT_MODES:
            raise ValueError(
                "cfg.splitter.semantic_early_cut_mode invalid: "
                f"{self.semantic_early_cut_mode!r} "
                f"(must be one of {_VALID_SEMANTIC_EARLY_CUT_MODES})"
            )
        if (
            isinstance(self.provisional_hold_seconds, bool)
            or not isinstance(self.provisional_hold_seconds, (int, float))
            or not math.isfinite(self.provisional_hold_seconds)
            or self.provisional_hold_seconds <= 0
        ):
            raise ValueError("cfg.splitter.provisional_hold_seconds must be positive")
        if (
            isinstance(self.provisional_queue_maxsize, bool)
            or not isinstance(self.provisional_queue_maxsize, int)
            or self.provisional_queue_maxsize <= 0
        ):
            raise ValueError("cfg.splitter.provisional_queue_maxsize must be positive")


_DEFAULT_SLANG_PATH = Path(__file__).resolve().parent / "data" / "default_slang.json"


def _load_default_slang(path: Path = _DEFAULT_SLANG_PATH) -> MappingProxyType:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"default slang data must be a non-empty object: {path}")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
        raise ValueError(f"default slang data must map strings to strings: {path}")
    return MappingProxyType(data)


_DEFAULT_SLANG: MappingProxyType = _load_default_slang()


_VALID_STREAMER_PROFILES = known_profile_ids(include_aliases=True)
_VALID_TRANSLATION_MODES = {"live", "clip"}
_VALID_DEEPSEEK_ROUTES = {"primary", "off"}
_VALID_ENGINE_NAMES      = {"claude", "google_translate", "deepl", "ollama", "nvidia", "groq", "openrouter"}
_VALID_BACKEND_MODES     = {"anthropic", "ollama", "nvidia"}
_VALID_SCENE_VISION_PROVIDERS = {"groq", "openrouter"}
_SCENE_VISION_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}")


@dataclass(frozen=True)
class _Translation:
    # -------------------------------------------------------------------------
    # Configurable fallback list. In ordinary live ``anthropic`` mode the
    # protected route is assembled separately by translation_engines.py:
    # DeepSeek -> OpenRouter Qwen -> DeepL -> Groq (or Qwen -> DeepL -> Groq
    # when deepseek_route="off"). Dashboard edits cannot reorder that route.
    # This tuple remains configurable for NVIDIA/clip/other applicable paths.
    #
    # Supported names (must match keys in _make_engine() in translator.py):
    #   "claude"           — Anthropic Claude     (needs ANTHROPIC_API_KEY)
    #   "google_translate" — Google Translate v2  (needs GOOGLE_TRANSLATE_API_KEY)
    #   "deepseek"         — DeepSeek V4 Flash    (needs DEEPSEEK_API_KEY;
    #                           protected live route only, not dashboard chain)
    #   "deepl"            — DeepL API v2         (needs DEEPL_API_KEY)
    #
    # To add a new configurable-chain engine:
    #   1. Add its name to engine_chain below and to the validated name set.
    #   2. Add its model/config field(s) in this class (see examples below).
    #   3. Implement a TranslationEngine subclass in modules/translator.py.
    #   4. Register the name in _make_engine() in translator.py.
    # -------------------------------------------------------------------------
    # Configured fallback chain for NVIDIA/clip/other applicable paths.
    # OpenRouter uses the benchmarked Qwen3-Next capsule; DeepL is the fast
    # non-LLM safety net and Groq remains last.
    engine_chain:   tuple        = ("openrouter", "deepl", "groq")

    # --- Model / API settings (one block per engine) -------------------------
    # Claude model selection (change to switch modes):
    #   "claude-sonnet-4-6"          — quality mode  (cache kicks in at ≥ 2048 sys-tokens)
    #   "claude-haiku-4-5-20251001"  — economy mode  (cache kicks in at ≥ 4096 sys-tokens)
    model:                    str = "claude-sonnet-4-6"
    claude_timeout:           float = 5.0   # per-request timeout (seconds) for ClaudeEngine
    # Groq fallback (uses GROQ_API_KEY_fall_back). Qwen3-32B is scheduled
    # for removal by Groq; use the production GPT-OSS model instead.
    groq_translation_model:   str = "openai/gpt-oss-120b"
    # GPT-OSS supports low / medium / high reasoning effort. Live subtitles
    # use low to avoid spending latency on hidden reasoning.
    groq_translation_reasoning_effort: str = "low"
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
    openrouter_model: str = "qwen/qwen3-next-80b-a3b-instruct"
    openrouter_timeout: int = 8
    openrouter_compact_prompt: bool = True
    openrouter_max_tokens: int = 160
    openrouter_context_window: int = 2
    openrouter_history_source_chars: int = 160
    openrouter_history_target_chars: int = 220
    openrouter_http_referer: str = "http://localhost/live_translate"
    openrouter_app_name: str = "live_translate"
    # Google Translate v2 — target lang uses BCP-47 (zh-TW is supported)
    google_translate_lang:    str = "zh-TW"
    google_translate_timeout: float = 5.0
    # Owner-authorized protected live route. ``primary`` selects the fixed
    # Flash -> Qwen -> DeepL -> Groq chain; ``off`` restores the exact fixed
    # Qwen -> DeepL -> Groq chain. Dashboard engine ordering cannot alter it.
    deepseek_route: str = os.environ.get(
        "LIVE_TRANSLATE_DEEPSEEK_ROUTE", "primary"
    ).strip().lower()
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout: float = 4.0
    deepseek_max_tokens: int = 160
    # Pricing snapshot verified from DeepSeek's official pricing page on
    # 2026-08-15. Keeping rates explicit makes every recorded cost auditable.
    deepseek_cache_hit_usd_per_million: float = 0.0028
    deepseek_cache_miss_usd_per_million: float = 0.14
    deepseek_output_usd_per_million: float = 0.28
    deepseek_pricing_revision: str = "2026-08-15"
    # DeepL     (target lang uses a different code from target_lang below)
    # DeepL API v2. Traditional Chinese must be requested as ZH-HANT.
    # Free keys (ending in :fx) use api-free.deepl.com automatically; all
    # other keys use the Pro endpoint.
    deepl_target_lang: str = "ZH-HANT"
    deepl_timeout: float = 4.0
    deepl_context_window: int = 2
    deepl_history_source_chars: int = 160
    deepl_history_target_chars: int = 220
    deepl_context_max_chars: int = 1400

    # --- Shared translation settings -----------------------------------------
    # Live reliability is route-neutral: every configured provider/model route
    # uses the same circuit policy and shares one end-to-end API deadline.
    # Individual adapter timeouts remain per-route caps inside this budget.
    circuit_breaker_enabled: bool = True
    circuit_recovery_cooldown_sec: float = 60.0
    circuit_recovery_success_threshold: int = 2
    live_total_deadline_sec: float = 10.0
    live_route_max_inflight: int = 2
    target_lang:    str          = "zh-TW"
    max_tokens:     int          = 200
    temperature:    float        = 0.1
    queue_maxsize:  int          = 8
    context_window: int          = 10  # retained recent translations; adaptive windows stay <= this
    adaptive_history_enabled: bool = True
    adaptive_history_base_window: int = 5
    adaptive_history_dependency_window: int = 10
    adaptive_history_dependency_markers: tuple = (
        "근데", "그런데", "그래서", "그러니까", "그리고", "아니", "맞아",
        "그러면", "그럼", "그게", "그러네", "그렇지",
    )
    # Latent multilingual policy. Production Groq STT currently pins
    # language="ko", so Japanese detection cannot be reached honestly yet.
    # Keep disabled until the separate KO-vs-auto-detect replay gate passes;
    # confidence/repetition filtering remains mandatory if later enabled.
    translate_coherent_foreign_speech: bool = False
    # Safety cap on per-input length. Oversized inputs are almost always STT
    # hallucinations (repeated phrases, chunk-boundary glitches). Rejecting them
    # at the policy layer prevents a single bad input from burning a day's
    # token budget. Counted in characters, not tokens.
    max_translate_chars: int     = 500
    # Let high-confidence, naturally completed multi-syllable emotional
    # repetition bypass only the excessive-repetition STT garbage branch.
    # Missing confidence, forced cuts and short/Jamo loops remain fail-closed.
    repetition_confidence_exempt_enabled: bool = True
    max_subtitle_output_delay_ms: int = 30000
    # Translation mode — controls the STT correction section in the system prompt.
    # Options: "live" (default, real-time STT noise handling), "clip" (conservative, preserves structure)
    translation_mode: str        = "live"
    # Streamer-specific few-shot profile appended to base prompt.
    # Options: "" (general only), "stellive_hina", "isegye_lilpa", "hades_chxxnnx", "mwmeu", "irise", "url"
    streamer_profile: str        = "isegye_lilpa"
    use_profile:      bool       = True   # set False to strip profile regardless of streamer_profile
    profile_mode:     str        = "auto"  # auto content override or manual hard lock
    # Manual session state: what the streamer is doing right now (e.g.
    # "StarCraft", "tier list talk"). Injected into the system prompt as one
    # labeled background line to disambiguate game/context terms — never as
    # text to translate. Empty = omitted. This field remains manual-only; the
    # automatic resolver publishes a separate canonical snapshot and never
    # writes this value.
    current_activity: str        = ""
    slang:          MappingProxyType = field(default_factory=lambda: _DEFAULT_SLANG)

    def __post_init__(self):
        if self.translation_mode not in _VALID_TRANSLATION_MODES:
            raise ValueError(
                f"cfg.translation.translation_mode invalid: {self.translation_mode!r} "
                f"(must be one of {_VALID_TRANSLATION_MODES})"
            )
        if self.deepseek_route not in _VALID_DEEPSEEK_ROUTES:
            raise ValueError(
                "cfg.translation.deepseek_route invalid: "
                f"{self.deepseek_route!r} "
                f"(must be one of {_VALID_DEEPSEEK_ROUTES})"
            )
        canonical_streamer_profile = canonical_profile_id(self.streamer_profile)
        if self.profile_mode not in {"auto", "manual"}:
            raise ValueError("cfg.translation.profile_mode must be auto or manual")
        if self.streamer_profile and not canonical_streamer_profile:
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
        if len(set(self.engine_chain)) != len(self.engine_chain):
            raise ValueError("cfg.translation.engine_chain entries must be unique")
        for field_name in (
            "circuit_recovery_cooldown_sec",
            "live_total_deadline_sec",
            "claude_timeout",
            "google_translate_timeout",
            "deepseek_timeout",
            "deepseek_cache_hit_usd_per_million",
            "deepseek_cache_miss_usd_per_million",
            "deepseek_output_usd_per_million",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    f"cfg.translation.{field_name} must be positive and finite"
                )
        for field_name in (
            "deepseek_max_tokens",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"cfg.translation.{field_name} must be a positive integer")
        if not isinstance(self.circuit_breaker_enabled, bool):
            raise ValueError(
                "cfg.translation.circuit_breaker_enabled must be boolean"
            )
        if (
            isinstance(self.circuit_recovery_success_threshold, bool)
            or not isinstance(self.circuit_recovery_success_threshold, int)
            or self.circuit_recovery_success_threshold < 1
        ):
            raise ValueError(
                "cfg.translation.circuit_recovery_success_threshold must be at least 1"
            )
        if (
            isinstance(self.live_route_max_inflight, bool)
            or not isinstance(self.live_route_max_inflight, int)
            or self.live_route_max_inflight < 1
            or self.live_route_max_inflight > 8
        ):
            raise ValueError(
                "cfg.translation.live_route_max_inflight must be between 1 and 8"
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
    # Legacy compatibility fields. Live fallback policy is provider-neutral
    # and is configured by cfg.translation.circuit_*.
    circuit_breaker_enabled: bool = True
    recovery_cooldown_sec: float = 60.0
    recovery_success_threshold: int = 2


@dataclass(frozen=True)
class _Scene:
    """Automatic scene-context updater (modules/scene_context.py).

    Samples a safe livestream player crop and records bounded activity
    evidence a few times per hour. Capture and translation-only publication
    have separate switches; automatic activity never reaches STT hot terms.
    """
    # Enabled after the complete activity/correctness runtime gate passed.
    enabled:              bool  = True
    # T13-B activation switch. Manual activity remains authoritative, and a
    # fresh confirmed automatic snapshot may affect translation context only.
    publish_translation_activity: bool = True
    # T15/T17 kill switch. The open-set runtime gate passed, so direct pipeline
    # runs publish by default; an explicit dashboard false still disables it.
    publish_open_set_activity: bool = True
    check_interval_sec:   float = 5.0     # bounded fast profile discovery cadence
    min_call_gap_sec:     float = 180.0   # at most one vision call per gap
    refresh_interval_sec: float = 600.0   # re-ask even without a scene change
    change_threshold:     float = 12.0    # mean abs diff on 64x64 grayscale
    # Capture only the livestream browser window. Full-screen capture polluted
    # current_activity when the local coding workspace was visible; matching
    # "Google Chrome" then failed the same way once the stream shared a window
    # with other tabs — a Chrome window's title IS its active tab's title, so
    # browsing ChatGPT/Sheets in that window relabeled the scene (20260707-08:
    # ChatGPT x175, "Google Sheets", "selling a product page").
    #
    # capture_mode options:
    #   "chrome_window"  — require exactly one visible, non-minimized,
    #                      owner-approved browser window whose active title
    #                      matches window_title_keywords; otherwise fail
    #                      closed. Keep the stream in its own browser window.
    #   "window"         — legacy title-keyword match per scan (stream
    #                      platform name must stay in the active tab title).
    #   "primary_screen" — full-screen grab (pollution-prone, debug only).
    capture_mode:         str   = "chrome_window"
    window_title_keywords: tuple = ("SOOP", "치지직", "CHZZK")
    # Chrome/Edge/Brave share the "Chrome_WidgetWin_1" class. Legacy title
    # markers cannot establish browser identity and are retained only for
    # configuration compatibility; executable basenames below are authoritative.
    browser_title_markers: tuple = ("google chrome", "brave")
    # Exact executable basenames form the security boundary. Window titles are
    # page-controlled and therefore cannot prove browser identity.
    browser_process_names: tuple = ("chrome.exe", "brave.exe")
    chrome_title_marker:  str   = "google chrome"
    window_fallback_fullscreen: bool = False
    # Explicit provider/model routes. Groq remains primary; the owner-approved
    # OpenRouter route is used only after a retryable Groq transport/provider
    # failure, never after valid unknown/noncanonical output.
    vision_provider:      str   = "groq"
    vision_model:         str   = "qwen/qwen3.6-27b"
    vision_fallback_routes: tuple[tuple[str, str], ...] = (
        ("openrouter", "qwen/qwen3-vl-32b-instruct"),
    )
    vision_timeout:       float = 20.0
    vision_max_retries:   int   = 0
    max_activity_chars:   int   = 40
    max_open_set_identities_per_window: int = 8
    # A second bounded classifier reuses the validated player crop to resolve
    # reviewed content profiles. It never emits free-form identities.
    resolve_content_profile: bool = True
    # Adaptive profile sampling: unresolved/recovering scenes sample quickly;
    # stable confirmed content backs off to the normal cadence.
    profile_identity_fast_call_gap_sec: float = 5.0
    profile_identity_stable_call_gap_sec: float = 15.0
    profile_identity_schema_retry_limit: int = 1
    profile_identity_max_attempts_per_minute: int = 12
    profile_identity_recovery_clear_sec: float = 15.0
    profile_identity_expiry_sec: float = 300.0

    def __post_init__(self):
        routes: list[tuple[str, str]] = [
            (self.vision_provider, self.vision_model)
        ]
        if not isinstance(self.vision_fallback_routes, tuple):
            raise ValueError("cfg.scene.vision_fallback_routes must be a tuple")
        for route in self.vision_fallback_routes:
            if (
                not isinstance(route, tuple)
                or len(route) != 2
                or not all(isinstance(value, str) for value in route)
            ):
                raise ValueError(
                    "cfg.scene.vision_fallback_routes contains malformed route"
                )
            routes.append(route)
        for provider, model in routes:
            if provider not in _VALID_SCENE_VISION_PROVIDERS:
                raise ValueError(
                    f"cfg.scene vision provider invalid: {provider!r}"
                )
            if not _SCENE_VISION_MODEL_RE.fullmatch(model):
                raise ValueError(
                    f"cfg.scene vision model invalid: {model!r}"
                )
        if len(routes) > 3:
            raise ValueError("cfg.scene supports at most three vision routes")
        if len(set(routes)) != len(routes):
            raise ValueError("cfg.scene vision routes must be unique")
        if self.vision_max_retries != 0:
            raise ValueError("cfg.scene.vision_max_retries must remain zero")
        for field_name in (
            "publish_translation_activity",
            "publish_open_set_activity",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"cfg.scene.{field_name} must be boolean")
        if (
            isinstance(self.max_activity_chars, bool)
            or not isinstance(self.max_activity_chars, int)
            or not 1 <= self.max_activity_chars <= 80
        ):
            raise ValueError(
                "cfg.scene.max_activity_chars must be between 1 and 80"
            )
        if (
            isinstance(self.max_open_set_identities_per_window, bool)
            or not isinstance(self.max_open_set_identities_per_window, int)
            or not 1 <= self.max_open_set_identities_per_window <= 32
        ):
            raise ValueError(
                "cfg.scene.max_open_set_identities_per_window must be "
                "between 1 and 32"
            )
        if (
            isinstance(self.vision_timeout, bool)
            or not isinstance(self.vision_timeout, (int, float))
            or not math.isfinite(self.vision_timeout)
            or self.vision_timeout <= 0
        ):
            raise ValueError("cfg.scene.vision_timeout must be positive")
        for field_name in (
            "check_interval_sec",
            "min_call_gap_sec",
            "refresh_interval_sec",
            "profile_identity_fast_call_gap_sec",
            "profile_identity_stable_call_gap_sec",
            "profile_identity_expiry_sec",
            "profile_identity_recovery_clear_sec",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"cfg.scene.{field_name} must be positive and finite")
        if self.profile_identity_fast_call_gap_sec > self.profile_identity_stable_call_gap_sec:
            raise ValueError("cfg.scene profile fast cadence cannot exceed stable cadence")
        if self.profile_identity_schema_retry_limit not in {0, 1}:
            raise ValueError("cfg.scene.profile_identity_schema_retry_limit must be 0 or 1")
        if (
            isinstance(self.profile_identity_max_attempts_per_minute, bool)
            or not isinstance(self.profile_identity_max_attempts_per_minute, int)
            or not 1 <= self.profile_identity_max_attempts_per_minute <= 30
        ):
            raise ValueError("cfg.scene.profile_identity_max_attempts_per_minute must be 1..30")
        if (
            isinstance(self.change_threshold, bool)
            or not isinstance(self.change_threshold, (int, float))
            or not math.isfinite(self.change_threshold)
            or self.change_threshold < 0
        ):
            raise ValueError("cfg.scene.change_threshold must be finite and non-negative")


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
    # Ordinary live "anthropic" uses the fixed protected route described
    # above; other applicable anthropic/clip and NVIDIA paths may use
    # translation.engine_chain. Ollama bypasses it.
    live_engine:         str          = "anthropic"
    clip_engine:         str          = "anthropic"
    ollama:              _Ollama      = field(default_factory=_Ollama)
    nvidia:              _Nvidia      = field(default_factory=_Nvidia)
    scene:               _Scene       = field(default_factory=_Scene)
    thread_join_timeout: int          = 5

    @property
    def active_streamer_profile(self) -> str:
        return canonical_profile_id(self.translation.streamer_profile)

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
                    "current_activity", "streamer_profile", "use_profile", "profile_mode"),
    "scene": ("publish_open_set_activity",),
    "subtitle": ("idle_hide_ms", "alpha"),
}
_DASHBOARD_OVERRIDE_TOP = ("live_engine",)


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _is_typed_enum(value: object, allowed: set[str]) -> bool:
    """JSON enum validation that rejects non-strings without invoking hashing."""
    return isinstance(value, str) and value in allowed


def _dashboard_value_is_valid(
    section: str,
    name: str,
    value: object,
    base: "_Config",
) -> bool:
    """Validate the JSON values that bypass dataclass type annotations.

    Tauri validates normal UI submissions, but this file is persisted between
    runs and may also be edited manually.  Keep malformed overrides from
    creating a config that only fails after a pipeline thread has started.
    """
    if section == "audio":
        if name == "vad_enabled":
            return isinstance(value, bool)
        if name == "vad_silence_sec":
            return _is_finite_number(value) and 0.0 < float(value) <= 5.0
        if name == "vad_max_speech_sec":
            return _is_finite_number(value) and base.audio.vad_min_speech_sec < float(value) <= 30.0
    if section == "stt" and name == "primary_engine":
        return _is_typed_enum(value, {"elevenlabs", "groq", "sensevoice"})
    if section == "translation":
        if name == "engine_chain":
            return (
                isinstance(value, list)
                and bool(value)
                and all(isinstance(engine, str) and engine in _VALID_ENGINE_NAMES for engine in value)
                and len(set(value)) == len(value)
            )
        if name == "translation_mode":
            return _is_typed_enum(value, _VALID_TRANSLATION_MODES)
        if name == "max_tokens":
            return isinstance(value, int) and not isinstance(value, bool) and 10 <= value <= 500
        if name == "target_lang":
            return isinstance(value, str) and bool(value.strip()) and len(value) <= 50
        if name == "current_activity":
            return isinstance(value, str) and len(value) <= 200
        if name == "streamer_profile":
            return isinstance(value, str) and canonical_profile_id(value) is not None
        if name == "use_profile":
            return isinstance(value, bool)
        if name == "profile_mode":
            return _is_typed_enum(value, {"auto", "manual"})
    if section == "scene" and name == "publish_open_set_activity":
        return isinstance(value, bool)
    if section == "subtitle":
        if name == "idle_hide_ms":
            return isinstance(value, int) and not isinstance(value, bool) and 1000 <= value <= 120000
        if name == "alpha":
            return _is_finite_number(value) and 0.1 <= float(value) <= 1.0
    return False


def _dashboard_font_is_valid(sub: dict[str, object], base_font: tuple) -> bool:
    family = sub.get("font_family", base_font[0] if len(base_font) > 0 else "Microsoft JhengHei")
    size = sub.get("font_size", base_font[1] if len(base_font) > 1 else 22)
    style = sub.get("font_style", base_font[2] if len(base_font) > 2 else "bold")
    return (
        isinstance(family, str) and bool(family.strip()) and len(family) <= 100
        and isinstance(size, int) and not isinstance(size, bool) and 8 <= size <= 48
        and isinstance(style, str) and bool(style.strip()) and len(style) <= 50
    )


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
        changes = {
            name: sub[name]
            for name in fields_
            if name in sub and _dashboard_value_is_valid(section, name, sub[name], base)
        }
        if isinstance(changes.get("engine_chain"), list):
            changes["engine_chain"] = tuple(changes["engine_chain"])
        if section == "subtitle" and any(
            key in sub for key in ("font_family", "font_size", "font_style")
        ) and _dashboard_font_is_valid(sub, base.subtitle.font):
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
    top_changes = {
        name: data[name]
        for name in _DASHBOARD_OVERRIDE_TOP
        if name in data
        and _is_typed_enum(data[name], _VALID_BACKEND_MODES)
    }
    if not section_updates and not top_changes:
        return base
    try:
        return replace(base, **section_updates, **top_changes)
    except (TypeError, ValueError):
        return base


cfg = _Config()
if os.environ.get(_DASHBOARD_OVERRIDE_ENV):
    cfg = _apply_dashboard_overrides(cfg)
