import socket
import threading
import time
from abc import ABC, abstractmethod
from urllib.error import URLError

from config import cfg
from utils.api_retry import classify_error
from utils.logger import get_logger

log = get_logger("translation_engines")

_NVIDIA_MAX_ATTEMPTS = 2
_NVIDIA_RETRY_DELAY_SEC = 0.5
_GROQ_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_USER_AGENT = "live_translate/1.0"
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_USER_AGENT = "live_translate/1.0"
# Shared invariants for the compact (TPM-budget) prompts. These engines now
# carry most traffic on nvidia-degradation days, so the systematic error
# classes observed in runtime logs must be covered even without the full
# prompt: invented Chinese phonetic names (랑코→朗子/Lanco), Japanese kana
# escapes (지노와아→ジノワァ), and Korean number-unit drops (만 5천원→五千).
_COMPACT_INVARIANTS = (
    "Rules: "
    "(1) Names: keep streamer/fan/nickname Korean names in Hangul exactly as "
    "written (e.g. 랑코 stays 랑코) while still translating the rest of the sentence normally; never invent Chinese phonetic renderings "
    "or romanizations; only nationally famous people keep conventional "
    "Chinese names. "
    "(2) Never output Japanese kana; unknown Korean sound-words stay in "
    "Hangul. "
    "(3) Korean number units are exact: 만=10,000 and 억=100,000,000 — "
    "만 5천원 is 15,000元 (never 五千), 10만원 is 10萬元 (never 一萬); when "
    "unsure write the amount in digits. "
    "(4) Output Traditional Chinese (zh-TW) only, never Simplified."
)
_GROQ_COMPACT_SYSTEM_PROMPT = (
    "You are a Korean to Traditional Chinese live subtitle translator. "
    "Translate only the provided Korean input into natural zh-TW. "
    "Output only the translation, with no labels or explanations. "
    "If the input is empty, noise, or unreadable, output an empty string. "
    "Keep uncertain names and brands as names; do not invent facts. "
    + _COMPACT_INVARIANTS
)
_OPENROUTER_COMPACT_SYSTEM_PROMPT = (
    "You are a Korean to Traditional Chinese live subtitle translator. "
    "Translate only the provided Korean livestream speech into natural zh-TW. "
    "Output only the translation, with no labels, explanations, romanization, or source text. "
    "Preserve gaming/anime terms and uncertain person names as names; do not force Chinese phonetic names. "
    "If the input is empty, noise, or unreadable, output an empty string. "
    "If the source is uncertain, prefer a short conservative translation over invented detail. "
    + _COMPACT_INVARIANTS
)
_ENGINE_DIAGNOSTICS = threading.local()
_TOKEN_USAGE = threading.local()
_NVIDIA_INFLIGHT_LOCK = threading.Lock()
_NVIDIA_INFLIGHT_COUNT = 0


def _int_diagnostic(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int_diagnostic(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_diagnostic(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return default


def _timeout_config_ms(timeout) -> float | None:
    try:
        return _float_diagnostic(float(timeout) * 1000)
    except (TypeError, ValueError):
        return None


def _elapsed_ms(start: float | None, end: float | None = None) -> float | None:
    if start is None:
        return None
    end = time.monotonic() if end is None else end
    return _float_diagnostic((end - start) * 1000)


def _nvidia_inflight_started() -> int:
    global _NVIDIA_INFLIGHT_COUNT
    with _NVIDIA_INFLIGHT_LOCK:
        inflight_at_start = _NVIDIA_INFLIGHT_COUNT
        _NVIDIA_INFLIGHT_COUNT += 1
        return inflight_at_start


def _nvidia_inflight_finished() -> None:
    global _NVIDIA_INFLIGHT_COUNT
    with _NVIDIA_INFLIGHT_LOCK:
        _NVIDIA_INFLIGHT_COUNT = max(0, _NVIDIA_INFLIGHT_COUNT - 1)


def _set_last_engine_diagnostics(
    engine: str = "",
    retry_count: int = 0,
    retry_reason: str = "",
    **api_fields,
) -> None:
    _ENGINE_DIAGNOSTICS.value = {
        "engine": engine,
        "retry_count": retry_count,
        "retry_reason": retry_reason,
        "api_attempt_count": _int_diagnostic(api_fields.get("api_attempt_count")),
        "api_timeout_count": _int_diagnostic(api_fields.get("api_timeout_count")),
        "api_total_wall_ms": _float_diagnostic(api_fields.get("api_total_wall_ms")),
        "api_final_attempt_ms": _float_diagnostic(api_fields.get("api_final_attempt_ms")),
        "api_first_attempt_ms": _float_diagnostic(api_fields.get("api_first_attempt_ms")),
        "api_retry_attempt_ms": _float_diagnostic(api_fields.get("api_retry_attempt_ms")),
        "retry_sleep_ms": _float_diagnostic(api_fields.get("retry_sleep_ms"), 0.0),
        "timeout_config_ms": _float_diagnostic(api_fields.get("timeout_config_ms")),
        "api_attempt_timeout_ms": _float_diagnostic(api_fields.get("api_attempt_timeout_ms")),
        "api_attempt_index": _int_diagnostic(api_fields.get("api_attempt_index")),
        "api_inflight_count_at_start": _optional_int_diagnostic(
            api_fields.get("api_inflight_count_at_start")
        ),
        "source_text_char_count": _optional_int_diagnostic(api_fields.get("source_text_char_count")),
        "prompt_char_count": _optional_int_diagnostic(api_fields.get("prompt_char_count")),
        "request_body_char_count": _optional_int_diagnostic(
            api_fields.get("request_body_char_count")
        ),
        "message_count": _optional_int_diagnostic(api_fields.get("message_count")),
        "context_item_count": _optional_int_diagnostic(api_fields.get("context_item_count")),
        "api_error_type": api_fields.get("api_error_type"),
        "api_error_message_class": api_fields.get("api_error_message_class"),
    }


def get_last_engine_diagnostics() -> dict[str, int | str]:
    """Return retry diagnostics for the last engine call in this thread."""
    value = getattr(_ENGINE_DIAGNOSTICS, "value", None)
    if not isinstance(value, dict):
        return {"engine": "", "retry_count": 0, "retry_reason": ""}
    return {
        "engine": str(value.get("engine") or ""),
        "retry_count": int(value.get("retry_count") or 0),
        "retry_reason": str(value.get("retry_reason") or ""),
    }


def get_last_engine_api_diagnostics() -> dict[str, int | float | str | None]:
    """Return retry/API timing diagnostics for the last engine call in this thread."""
    value = getattr(_ENGINE_DIAGNOSTICS, "value", None)
    if not isinstance(value, dict):
        value = {}
    return {
        "engine": str(value.get("engine") or ""),
        "retry_count": _int_diagnostic(value.get("retry_count")),
        "retry_reason": str(value.get("retry_reason") or ""),
        "api_attempt_count": _int_diagnostic(value.get("api_attempt_count")),
        "api_timeout_count": _int_diagnostic(value.get("api_timeout_count")),
        "api_total_wall_ms": _float_diagnostic(value.get("api_total_wall_ms")),
        "api_final_attempt_ms": _float_diagnostic(value.get("api_final_attempt_ms")),
        "api_first_attempt_ms": _float_diagnostic(value.get("api_first_attempt_ms")),
        "api_retry_attempt_ms": _float_diagnostic(value.get("api_retry_attempt_ms")),
        "retry_sleep_ms": _float_diagnostic(value.get("retry_sleep_ms"), 0.0),
        "timeout_config_ms": _float_diagnostic(value.get("timeout_config_ms")),
        "api_attempt_timeout_ms": _float_diagnostic(value.get("api_attempt_timeout_ms")),
        "api_attempt_index": _int_diagnostic(value.get("api_attempt_index")),
        "api_inflight_count_at_start": _optional_int_diagnostic(
            value.get("api_inflight_count_at_start")
        ),
        "source_text_char_count": _optional_int_diagnostic(value.get("source_text_char_count")),
        "prompt_char_count": _optional_int_diagnostic(value.get("prompt_char_count")),
        "request_body_char_count": _optional_int_diagnostic(value.get("request_body_char_count")),
        "message_count": _optional_int_diagnostic(value.get("message_count")),
        "context_item_count": _optional_int_diagnostic(value.get("context_item_count")),
        "api_error_type": value.get("api_error_type"),
        "api_error_message_class": value.get("api_error_message_class"),
    }


def reset_last_engine_diagnostics() -> None:
    """Clear per-thread engine diagnostics before a new translation attempt."""
    _ENGINE_DIAGNOSTICS.value = {}


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        reason_text = str(reason or exc).lower()
        return "timed out" in reason_text or "timeout" in reason_text
    text = str(exc).lower()
    return "timed out" in text or "timeout" in text


def _http_status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def _timeout_message_class(exc: BaseException) -> str:
    reason = getattr(exc, "reason", "")
    text = f"{type(exc).__name__} {reason} {exc}".lower()
    if "connect" in text:
        return "connect_timeout"
    return "read_timeout"


def _http_message_class(status_code: int) -> str:
    if 400 <= status_code < 500:
        return "http_4xx"
    if 500 <= status_code < 600:
        return "http_5xx"
    return "unknown"


def _classify_api_error(exc: BaseException) -> tuple[str, str]:
    if _is_timeout_exception(exc):
        return "timeout", _timeout_message_class(exc)

    status_code = _http_status_code(exc)
    if status_code is not None:
        return "api_error", _http_message_class(status_code)

    if isinstance(exc, URLError):
        return "connection_error", "connection_error"

    name = type(exc).__name__
    if name == "JSONDecodeError":
        return "parse_error", "json_parse_error"
    if isinstance(exc, (KeyError, IndexError)):
        return "parse_error", "unknown"

    kind = classify_error(exc)
    if kind == "network":
        return "connection_error", "connection_error"
    if kind in ("auth", "rate_limit"):
        return "api_error", "unknown"
    return "unknown", "unknown"


def _strip_think_tags(content: str) -> str:
    import re as _re

    stripped = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
    stripped = _re.sub(r"<think>.*$", "", stripped, flags=_re.DOTALL).strip()
    return stripped


def _build_user_message(text: str, incomplete: bool) -> str:
    if incomplete:
        return f"input (incomplete sentence, translate as best as possible): {text}"
    return f"input: {text}"


def _build_groq_user_message(text: str, incomplete: bool) -> str:
    return "/no_think\n" + _build_user_message(text, incomplete)


def _clamp_int(value, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, parsed)


def _truncate_for_groq(value: str, max_chars: int) -> str:
    text = (value or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _limited_history(
    history: list[tuple[str, str]] | None,
    *,
    config_prefix: str,
) -> list[tuple[str, str]]:
    """Shared history limiter for OpenAI-compatible fallback engines (L5).

    Reads `{prefix}_context_window` / `{prefix}_history_source_chars` /
    `{prefix}_history_target_chars` from cfg.translation.
    """
    limit = _clamp_int(getattr(cfg.translation, f"{config_prefix}_context_window", 2), 2)
    if limit <= 0:
        return []

    source_chars = _clamp_int(
        getattr(cfg.translation, f"{config_prefix}_history_source_chars", 160),
        160,
    )
    target_chars = _clamp_int(
        getattr(cfg.translation, f"{config_prefix}_history_target_chars", 220),
        220,
    )

    limited: list[tuple[str, str]] = []
    for source, target in (history or [])[-limit:]:
        source_text = _truncate_for_groq(source, source_chars)
        target_text = _truncate_for_groq(target, target_chars)
        if source_text and target_text:
            limited.append((source_text, target_text))
    return limited


def _primary_context_window() -> int:
    return _clamp_int(getattr(cfg.translation, "context_window", 0), 0)


def _limited_primary_history(history: list[tuple[str, str]] | None) -> list[tuple[str, str]]:
    limit = _primary_context_window()
    if limit <= 0:
        return []
    return list(history or [])[-limit:]


def _limited_groq_history(history: list[tuple[str, str]] | None) -> list[tuple[str, str]]:
    return _limited_history(history, config_prefix="groq_translation")


def _limited_openrouter_history(history: list[tuple[str, str]] | None) -> list[tuple[str, str]]:
    return _limited_history(history, config_prefix="openrouter")


def _groq_system_prompt(system_prompt: str) -> str:
    if not bool(getattr(cfg.translation, "groq_translation_compact_prompt", True)):
        return system_prompt
    profile_id = getattr(cfg, "active_streamer_profile", "")
    if profile_id and bool(getattr(cfg.translation, "use_profile", False)):
        return f"{_GROQ_COMPACT_SYSTEM_PROMPT} Active streamer profile: {profile_id}."
    return _GROQ_COMPACT_SYSTEM_PROMPT


def _openrouter_system_prompt(system_prompt: str) -> str:
    if not bool(getattr(cfg.translation, "openrouter_compact_prompt", True)):
        return system_prompt
    profile_id = getattr(cfg, "active_streamer_profile", "")
    if profile_id and bool(getattr(cfg.translation, "use_profile", False)):
        return f"{_OPENROUTER_COMPACT_SYSTEM_PROMPT} Active streamer profile: {profile_id}."
    return _OPENROUTER_COMPACT_SYSTEM_PROMPT


def effective_system_prompt_for_engine(
    engine: "TranslationEngine | str | None",
    system_prompt: str,
) -> str:
    engine_name = getattr(engine, "engine_name", engine) or ""
    if engine_name == "groq":
        return _groq_system_prompt(system_prompt)
    if engine_name == "openrouter":
        return _openrouter_system_prompt(system_prompt)
    return system_prompt


def _is_groq_token_limit_error(status_code: int, body: str) -> bool:
    if status_code != 413:
        return False
    text = (body or "").lower()
    return (
        "token" in text
        or "tpm" in text
        or "rate_limit_exceeded" in text
        or "request too large" in text
    )


def _usage_value(usage, *names: str):
    if usage is None:
        return None
    for name in names:
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if value is not None:
            return value
    return None


def reset_last_token_usage() -> None:
    """Clear per-thread token usage so a cache hit/failure can't inherit a stale count."""
    _TOKEN_USAGE.value = {}


def get_last_token_usage() -> dict[str, int | None]:
    """Token usage parsed from the last engine response in this thread (empty if none)."""
    value = getattr(_TOKEN_USAGE, "value", None)
    if not isinstance(value, dict):
        return {}
    return {
        key: value.get(key)
        for key in ("prompt", "output", "total", "cache_read", "cache_write")
        if key in value
    }


def get_last_token_usage_engine() -> str:
    value = getattr(_TOKEN_USAGE, "value", None)
    if not isinstance(value, dict):
        return ""
    return str(value.get("engine") or "")


def _log_token_usage(engine: str, usage) -> None:
    # OpenAI-compatible (prompt_tokens/completion_tokens), Gemini (*_token_count),
    # and Anthropic (input_tokens/output_tokens) field names are all accepted so a
    # single chokepoint captures usage regardless of which engine produced it.
    prompt_tokens = _usage_value(usage, "prompt_token_count", "promptTokenCount", "input_tokens", "prompt_tokens")
    output_tokens = _usage_value(usage, "candidates_token_count", "candidatesTokenCount", "output_tokens", "response_token_count", "completion_tokens")
    total_tokens = _usage_value(usage, "total_token_count", "totalTokenCount", "total_tokens")
    cache_write = _usage_value(usage, "cache_creation_input_tokens")
    cache_read = _usage_value(usage, "cache_read_input_tokens")

    _TOKEN_USAGE.value = {
        "engine": str(engine or "").strip().lower(),
        "prompt": _optional_int_diagnostic(prompt_tokens),
        "output": _optional_int_diagnostic(output_tokens),
        "total": _optional_int_diagnostic(total_tokens),
        "cache_read": _optional_int_diagnostic(cache_read),
        "cache_write": _optional_int_diagnostic(cache_write),
    }

    parts = [f"{engine} tokens"]
    if prompt_tokens is not None:
        parts.append(f"prompt={prompt_tokens}")
    if output_tokens is not None:
        parts.append(f"output={output_tokens}")
    if total_tokens is not None:
        parts.append(f"total={total_tokens}")
    if cache_write:
        parts.append(f"cache_write={cache_write}")
    if cache_read:
        parts.append(f"cache_read={cache_read}")

    log.info(" | ".join(parts))


# ---------------------------------------------------------------------------
# Engine abstraction
# ---------------------------------------------------------------------------

class TranslationEngine(ABC):
    """
    Common interface for all translation backends.

    To add a new engine: see the step-by-step guide in config.py (_Translation.engine_chain).
    """

    @property
    @abstractmethod
    def engine_name(self) -> str:
        """Short identifier stored in the DB (e.g. 'nvidia', 'claude', 'openrouter')."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model/version string for DB cache keying (e.g. 'qwen/qwen3-32b')."""
        ...

    @property
    @abstractmethod
    def available(self) -> bool:
        """True if the engine initialised successfully and can accept calls."""
        ...

    @abstractmethod
    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        """
        Translate text. Return None on any failure.

        text:          raw source text (Korean)
        system_prompt: evolved prompt — LLM engines use it;
                       direct-translation engines (Google Translate) may ignore it.
        incomplete:    True if the sentence is a fragment.
        history:       recent (ko, zh) pairs; LLM engines prepend as multi-turn messages.
                       Direct-translation engines ignore this.
        """
        ...


class ClaudeEngine(TranslationEngine):
    def __init__(self):
        self._client = None
        if not cfg.keys.anthropic:
            log.error("ANTHROPIC_API_KEY not set")
            return
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=cfg.keys.anthropic)
            log.info("ClaudeEngine ready (model=%s)", cfg.translation.model)
        except Exception as e:
            log.error("Failed to init Anthropic: %s", e)

    @property
    def engine_name(self) -> str:
        return "claude"

    @property
    def model_name(self) -> str:
        return cfg.translation.model

    @property
    def available(self) -> bool:
        return self._client is not None

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        if self._client is None:
            return None
        try:
            _t0 = time.monotonic()
            system_content: dict = {"type": "text", "text": system_prompt}
            if cfg.translation.translation_mode == "live":
                system_content["cache_control"] = {"type": "ephemeral"}
            messages = []
            for ko, zh in _limited_primary_history(history):
                messages.append({"role": "user", "content": f"input: {ko}"})
                messages.append({"role": "assistant", "content": zh})
            messages.append({"role": "user", "content": _build_user_message(text, incomplete)})
            resp = self._client.messages.create(
                model=cfg.translation.model,
                max_tokens=cfg.translation.max_tokens,
                temperature=cfg.translation.temperature,
                system=[system_content],
                messages=messages,
                timeout=float(getattr(cfg.translation, "claude_timeout", 5.0) or 5.0),
            )
            log.info("Claude translate: %.0fms", (time.monotonic() - _t0) * 1000)
            _log_token_usage("Claude", getattr(resp, "usage", None))
            result = resp.content[0].text.strip()
            log.debug("Claude: %.30s → %s", text, result)
            return result
        except Exception as e:
            kind = classify_error(e)
            if kind == "auth":
                log.error("Claude auth error (check ANTHROPIC_API_KEY): %s", e)
            elif kind == "rate_limit":
                log.warning("Claude rate-limit: %s", e)
            elif kind == "network":
                log.warning("Claude network error: %s", e)
            else:
                log.error("Claude error: %s", e)
            return None


class GoogleTranslateEngine(TranslationEngine):
    """Google Cloud Translation API v2 (Basic). No LLM — ignores system_prompt."""

    _URL = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self):
        self._api_key = cfg.keys.google_translate
        self._target_lang = cfg.translation.google_translate_lang
        if not self._api_key:
            log.error("GOOGLE_TRANSLATE_API_KEY not set")

    @property
    def engine_name(self) -> str:
        return "google_translate"

    @property
    def model_name(self) -> str:
        return "google-translate-v2"

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def translate(self, text: str, _system_prompt: str, _incomplete: bool,
                  _history: list[tuple[str, str]] | None = None) -> str | None:  # pyright: ignore[reportUnusedParameter]
        if not self._api_key:
            return None
        try:
            import urllib.request
            import urllib.parse
            import json as _json
            payload = _json.dumps({
                "q": text,
                "source": "ko",
                "target": self._target_lang,
                "format": "text",
            }).encode()
            url = f"{self._URL}?key={urllib.parse.quote(self._api_key, safe='')}"
            req = urllib.request.Request(url, data=payload,
                                         headers={"Content-Type": "application/json"})
            _t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=5) as r:
                data = _json.loads(r.read())
            result = data["data"]["translations"][0]["translatedText"].strip()
            log.info("GoogleTranslate translate: %.0fms", (time.monotonic() - _t0) * 1000)
            log.debug("GoogleTranslate: %.30s → %s", text, result)
            return result
        except Exception as e:
            safe = str(e).replace(self._api_key, "***") if self._api_key else str(e)
            kind = classify_error(e)
            if kind == "auth":
                log.error("GoogleTranslate auth error (check GOOGLE_TRANSLATE_API_KEY): %s", safe)
            elif kind == "rate_limit":
                log.warning("GoogleTranslate rate-limit: %s", safe)
            elif kind == "network":
                log.warning("GoogleTranslate network error: %s", safe)
            else:
                log.error("GoogleTranslate error: %s", safe)
            return None


class OllamaEngine(TranslationEngine):
    """Ollama local model via OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(self):
        self._base_url = cfg.ollama.base_url.rstrip("/")
        self._model = cfg.ollama.model
        self._timeout = cfg.ollama.timeout
        log.info("OllamaEngine ready (model=%s, base_url=%s)", self._model, self._base_url)

    @property
    def engine_name(self) -> str:
        return "ollama"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return True

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        import urllib.request
        import urllib.error
        import json as _json

        messages = [{"role": "system", "content": system_prompt}]
        for ko, zh in _limited_primary_history(history):
            messages.append({"role": "user", "content": f"input: {ko}"})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": _build_user_message(text, incomplete)})

        payload = _json.dumps({
            "model": self._model,
            "messages": messages,
            "stream": False,
            "temperature": cfg.translation.temperature,
            "max_tokens": cfg.translation.max_tokens,
        }).encode()

        url = f"{self._base_url}/v1/chat/completions"
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        try:
            _t0 = time.monotonic()
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                data = _json.loads(r.read())
            log.info("Ollama translate: %.0fms", (time.monotonic() - _t0) * 1000)
            _log_token_usage("Ollama", data.get("usage"))
            result = data["choices"][0]["message"]["content"].strip()
            log.debug("Ollama: %.30s → %s", text, result)
            return result
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log.error("模型 %r 不存在，請先執行 `ollama pull %s`", self._model, self._model)
            else:
                log.error("Ollama HTTP %d: %s", e.code, e)
            return None
        except urllib.error.URLError as e:
            reason = str(e).lower()
            # WinError 10061 = connection refused on Windows
            if "refused" in reason or "10061" in reason or "connect" in reason:
                log.error("Ollama 未啟動或 base_url 設定錯誤 (%s) — 請先執行 `ollama serve`", self._base_url)
            else:
                log.error("Ollama network error: %s", e)
            return None
        except Exception as e:
            log.error("Ollama error: %s", e)
            return None


class NvidiaEngine(TranslationEngine):
    """NVIDIA NIM hosted models via OpenAI-compatible endpoint."""

    _BASE_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

    def __init__(self):
        self._api_key = cfg.keys.nvidia
        self._model = cfg.nvidia.model
        self._timeout = (
            cfg.nvidia.live_timeout
            if cfg.translation.translation_mode == "live" and cfg.nvidia.live_timeout
            else cfg.nvidia.timeout
        )
        self._retry_transient_errors = cfg.translation.translation_mode != "live"
        _m = self._model.lower()
        self._is_qwen3    = "qwen3" in _m or "qwen-3" in _m
        self._strip_think = self._is_qwen3 or any(x in _m for x in ("deepseek-v4", "deepseek-r1", "deepseek-v3"))
        if not self._api_key:
            log.error("NVIDIA_API_KEY not set")
        else:
            log.info("NvidiaEngine ready (model=%s, qwen3=%s, strip_think=%s)",
                     self._model, self._is_qwen3, self._strip_think)

    @property
    def engine_name(self) -> str:
        return "nvidia"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        timeout_config_ms = _timeout_config_ms(self._timeout)
        api_attempt_count = 0
        api_timeout_count = 0
        api_total_start: float | None = None
        api_final_attempt_ms: float | None = None
        api_first_attempt_ms: float | None = None
        api_retry_attempt_ms: float | None = None
        api_attempt_index = 0
        api_inflight_count_at_start: int | None = None
        api_attempt_timeout_ms = timeout_config_ms
        retry_sleep_ms = 0.0
        retry_count = 0
        retry_reason = ""
        source_text_char_count = len(text or "")
        prompt_char_count = len(system_prompt or "")
        history = _limited_primary_history(history)
        context_item_count = len(history)
        request_body_char_count: int | None = None
        message_count: int | None = None
        retry_transient_errors = bool(getattr(self, "_retry_transient_errors", True))

        def record_diagnostics(
            api_error_type: str | None = None,
            api_error_message_class: str | None = None,
        ) -> None:
            _set_last_engine_diagnostics(
                "nvidia",
                retry_count,
                retry_reason,
                api_attempt_count=api_attempt_count,
                api_timeout_count=api_timeout_count,
                api_total_wall_ms=_elapsed_ms(api_total_start),
                api_final_attempt_ms=api_final_attempt_ms,
                api_first_attempt_ms=api_first_attempt_ms,
                api_retry_attempt_ms=api_retry_attempt_ms,
                retry_sleep_ms=retry_sleep_ms,
                timeout_config_ms=timeout_config_ms,
                api_attempt_timeout_ms=api_attempt_timeout_ms,
                api_attempt_index=api_attempt_index,
                api_inflight_count_at_start=api_inflight_count_at_start,
                source_text_char_count=source_text_char_count,
                prompt_char_count=prompt_char_count,
                request_body_char_count=request_body_char_count,
                message_count=message_count,
                context_item_count=context_item_count,
                api_error_type=api_error_type,
                api_error_message_class=api_error_message_class,
            )

        def record_attempt_duration(attempt_index: int, attempt_started_at: float) -> None:
            nonlocal api_attempt_index, api_final_attempt_ms
            nonlocal api_first_attempt_ms, api_retry_attempt_ms
            elapsed_ms = _elapsed_ms(attempt_started_at)
            api_attempt_index = attempt_index
            api_final_attempt_ms = elapsed_ms
            if attempt_index == 1:
                api_first_attempt_ms = elapsed_ms
            elif attempt_index == 2:
                api_retry_attempt_ms = elapsed_ms

        record_diagnostics()
        if not self._api_key:
            return None
        import urllib.request
        import urllib.error
        import json as _json

        messages = [{"role": "system", "content": system_prompt}]
        for ko, zh in history:
            messages.append({"role": "user", "content": f"input: {ko}"})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": _build_user_message(text, incomplete)})

        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": cfg.translation.temperature,
            "max_tokens": cfg.translation.max_tokens,
        }
        if self._is_qwen3:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        message_count = len(messages)
        payload_text = _json.dumps(body)
        request_body_char_count = len(payload_text)
        payload = payload_text.encode()

        for attempt in range(_NVIDIA_MAX_ATTEMPTS):
            current_attempt_index = attempt + 1
            req = urllib.request.Request(
                self._BASE_URL,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
            )
            try:
                _t0 = time.monotonic()
                if api_total_start is None:
                    api_total_start = _t0
                api_attempt_count += 1
                inflight_at_start = _nvidia_inflight_started()
                if api_inflight_count_at_start is None:
                    api_inflight_count_at_start = inflight_at_start
                try:
                    with urllib.request.urlopen(req, timeout=self._timeout) as r:
                        data = _json.loads(r.read())
                finally:
                    _nvidia_inflight_finished()
                _api_response_loaded = time.monotonic()
                log.info("Nvidia translate: %.0fms", (_api_response_loaded - _t0) * 1000)
                _log_token_usage("Nvidia", data.get("usage"))
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if self._strip_think:
                    content = _strip_think_tags(content)
                record_attempt_duration(current_attempt_index, _t0)
                log.debug("Nvidia: %.30s → %s", text, content)
                if content:
                    record_diagnostics()
                    return content
                if attempt + 1 < _NVIDIA_MAX_ATTEMPTS:
                    retry_count = attempt + 1
                    retry_reason = "empty_response"
                    record_diagnostics("api_error", "empty_response")
                    log.warning("Nvidia returned empty response; retrying once")
                    _sleep_t0 = time.monotonic()
                    time.sleep(_NVIDIA_RETRY_DELAY_SEC)
                    retry_sleep_ms += _elapsed_ms(_sleep_t0) or 0.0
                    continue
                record_diagnostics("api_error", "empty_response")
                return None
            except urllib.error.HTTPError as e:
                record_attempt_duration(current_attempt_index, _t0)
                error_body = ""  # L6: don't shadow the request `body` dict
                try:
                    error_body = e.read().decode()
                except Exception:
                    pass
                if e.code == 401:
                    log.error("Nvidia auth error — NVIDIA_API_KEY 無效或已過期")
                elif e.code == 404:
                    log.error("Nvidia 模型 %r 不存在 — 請確認 build.nvidia.com 上的模型名稱", self._model)
                elif e.code == 429:
                    log.warning("Nvidia rate-limit (429) — 請求過於頻繁，略過此句")
                else:
                    log.error("Nvidia HTTP %d: %s", e.code, error_body or e)
                error_type, message_class = _classify_api_error(e)
                record_diagnostics(error_type, message_class)
                return None
            except urllib.error.URLError as e:
                record_attempt_duration(current_attempt_index, _t0)
                if _is_timeout_exception(e):
                    api_timeout_count += 1
                if retry_transient_errors and attempt + 1 < _NVIDIA_MAX_ATTEMPTS:
                    reason = "timeout" if _is_timeout_exception(e) else "network"
                    retry_count = attempt + 1
                    retry_reason = reason
                    error_type, message_class = _classify_api_error(e)
                    record_diagnostics(error_type, message_class)
                    log.warning("Nvidia network error; retrying once: %s", e)
                    _sleep_t0 = time.monotonic()
                    time.sleep(_NVIDIA_RETRY_DELAY_SEC)
                    retry_sleep_ms += _elapsed_ms(_sleep_t0) or 0.0
                    continue
                log.error("Nvidia network error: %s", e)
                error_type, message_class = _classify_api_error(e)
                record_diagnostics(error_type, message_class)
                return None
            except Exception as e:
                record_attempt_duration(current_attempt_index, _t0)
                if _is_timeout_exception(e):
                    api_timeout_count += 1
                reason = "timeout" if _is_timeout_exception(e) else classify_error(e)
                if (
                    retry_transient_errors
                    and reason in ("timeout", "network")
                    and attempt + 1 < _NVIDIA_MAX_ATTEMPTS
                ):
                    retry_count = attempt + 1
                    retry_reason = reason
                    error_type, message_class = _classify_api_error(e)
                    record_diagnostics(error_type, message_class)
                    log.warning("Nvidia transient error; retrying once: %s", e)
                    _sleep_t0 = time.monotonic()
                    time.sleep(_NVIDIA_RETRY_DELAY_SEC)
                    retry_sleep_ms += _elapsed_ms(_sleep_t0) or 0.0
                    continue
                log.error("Nvidia error: %s", e)
                error_type, message_class = _classify_api_error(e)
                record_diagnostics(error_type, message_class)
                return None
        return None


class OpenRouterTranslationEngine(TranslationEngine):
    """OpenRouter hosted OpenAI-compatible models. Used as paid live fallback."""

    def __init__(self):
        self._api_key = cfg.keys.openrouter
        self._model = cfg.translation.openrouter_model
        self._timeout = cfg.translation.openrouter_timeout
        self._max_tokens = min(
            cfg.translation.max_tokens,
            _clamp_int(getattr(cfg.translation, "openrouter_max_tokens", 256), 256, 1),
        )
        _m = self._model.lower()
        self._strip_think = "qwen3" in _m or "qwen-3" in _m
        if not self._api_key:
            log.error("OPENROUTER_API_KEY not set - OpenRouterTranslationEngine unavailable")
        else:
            log.info("OpenRouterTranslationEngine ready (model=%s)", self._model)

    @property
    def engine_name(self) -> str:
        return "openrouter"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        timeout_config_ms = _timeout_config_ms(self._timeout)
        source_text_char_count = len(text or "")
        system_prompt = _openrouter_system_prompt(system_prompt)
        prompt_char_count = len(system_prompt or "")
        history = _limited_openrouter_history(history)

        def record_diagnostics(
            started_at: float | None = None,
            api_error_type: str | None = None,
            api_error_message_class: str | None = None,
            request_body_char_count: int | None = None,
            message_count: int | None = None,
        ) -> None:
            _set_last_engine_diagnostics(
                "openrouter",
                0,
                "",
                api_attempt_count=1 if started_at is not None else 0,
                api_timeout_count=1 if api_error_type == "timeout" else 0,
                api_total_wall_ms=_elapsed_ms(started_at),
                api_final_attempt_ms=_elapsed_ms(started_at),
                timeout_config_ms=timeout_config_ms,
                api_attempt_timeout_ms=timeout_config_ms,
                api_attempt_index=1 if started_at is not None else 0,
                source_text_char_count=source_text_char_count,
                prompt_char_count=prompt_char_count,
                request_body_char_count=request_body_char_count,
                message_count=message_count,
                context_item_count=len(history or []),
                api_error_type=api_error_type,
                api_error_message_class=api_error_message_class,
            )

        record_diagnostics()
        if not self._api_key:
            return None
        import urllib.request
        import urllib.error
        import json as _json

        messages = [{"role": "system", "content": system_prompt}]
        for ko, zh in history:
            messages.append({"role": "user", "content": f"input: {ko}"})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": _build_user_message(text, incomplete)})

        body = {
            "model": self._model,
            "messages": messages,
            "temperature": cfg.translation.temperature,
            "max_tokens": self._max_tokens,
            "reasoning": {"exclude": True},
        }
        payload_text = _json.dumps(body)
        payload = payload_text.encode()
        request_body_char_count = len(payload_text)
        message_count = len(messages)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _OPENROUTER_USER_AGENT,
            "Authorization": f"Bearer {self._api_key}",
        }
        referer = str(getattr(cfg.translation, "openrouter_http_referer", "") or "").strip()
        app_name = str(getattr(cfg.translation, "openrouter_app_name", "") or "").strip()
        if referer:
            headers["HTTP-Referer"] = referer
        if app_name:
            headers["X-Title"] = app_name

        req = urllib.request.Request(_OPENROUTER_BASE_URL, data=payload, headers=headers)
        started_at: float | None = None
        try:
            started_at = time.monotonic()
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                data = _json.loads(r.read())
            log.info("OpenRouter translate: %.0fms", (time.monotonic() - started_at) * 1000)
            _log_token_usage("OpenRouter", data.get("usage"))
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if self._strip_think:
                content = _strip_think_tags(content)
            record_diagnostics(
                started_at=started_at,
                request_body_char_count=request_body_char_count,
                message_count=message_count,
            )
            log.debug("OpenRouter: %.30s -> %s", text, content)
            return content or None
        except urllib.error.HTTPError as e:
            error_body = ""  # L6: don't shadow the request `body` dict
            try:
                error_body = e.read().decode()
            except Exception:
                pass
            if e.code == 401:
                log.error("OpenRouter auth error - OPENROUTER_API_KEY invalid or expired")
            elif e.code == 402:
                log.error("OpenRouter credits exhausted or payment required")
            elif e.code == 429:
                log.warning("OpenRouter rate-limit (429): %s", error_body or e)
            else:
                log.error("OpenRouter HTTP %d: %s", e.code, error_body or e)
            error_type, message_class = _classify_api_error(e)
            record_diagnostics(
                started_at=started_at,
                api_error_type=error_type,
                api_error_message_class=message_class,
                request_body_char_count=request_body_char_count,
                message_count=message_count,
            )
            return None
        except Exception as e:
            kind = classify_error(e)
            if _is_timeout_exception(e) or kind == "network":
                log.warning("OpenRouter network/timeout error: %s", e)
            else:
                log.error("OpenRouter error: %s", e)
            error_type, message_class = _classify_api_error(e)
            record_diagnostics(
                started_at=started_at,
                api_error_type=error_type,
                api_error_message_class=message_class,
                request_body_char_count=request_body_char_count,
                message_count=message_count,
            )
            return None


class GroqTranslationEngine(TranslationEngine):
    """Groq hosted models via OpenAI-compatible endpoint. Used as nvidia fallback."""

    def __init__(self):
        self._api_key = cfg.keys.groq_fallback
        self._model = cfg.translation.groq_translation_model
        self._timeout = cfg.translation.groq_translation_timeout
        self._max_tokens = min(
            cfg.translation.max_tokens,
            _clamp_int(getattr(cfg.translation, "groq_translation_max_tokens", 128), 128, 1),
        )
        self._retry_max_tokens = min(
            self._max_tokens,
            _clamp_int(getattr(cfg.translation, "groq_translation_retry_max_tokens", 96), 96, 1),
        )
        _m = self._model.lower()
        self._strip_think = "qwen3" in _m or "qwen-3" in _m
        if not self._api_key:
            log.error("GROQ_API_KEY_fall_back not set — GroqTranslationEngine unavailable")
        else:
            log.info("GroqTranslationEngine ready (model=%s)", self._model)

    @property
    def engine_name(self) -> str:
        return "groq"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        timeout_config_ms = _timeout_config_ms(self._timeout)
        system_prompt = _groq_system_prompt(system_prompt)
        history = _limited_groq_history(history)
        api_attempt_count = 0
        api_timeout_count = 0
        api_total_start: float | None = None
        api_final_attempt_ms: float | None = None
        api_first_attempt_ms: float | None = None
        api_retry_attempt_ms: float | None = None
        api_attempt_index = 0
        retry_sleep_ms = 0.0
        retry_count = 0
        retry_reason = ""
        source_text_char_count = len(text or "")
        prompt_char_count = len(system_prompt or "")
        request_body_char_count: int | None = None
        message_count: int | None = None

        def record_diagnostics(
            api_error_type: str | None = None,
            api_error_message_class: str | None = None,
        ) -> None:
            _set_last_engine_diagnostics(
                "groq",
                retry_count,
                retry_reason,
                api_attempt_count=api_attempt_count,
                api_timeout_count=api_timeout_count,
                api_total_wall_ms=_elapsed_ms(api_total_start),
                api_final_attempt_ms=api_final_attempt_ms,
                api_first_attempt_ms=api_first_attempt_ms,
                api_retry_attempt_ms=api_retry_attempt_ms,
                retry_sleep_ms=retry_sleep_ms,
                timeout_config_ms=timeout_config_ms,
                api_attempt_timeout_ms=timeout_config_ms,
                api_attempt_index=api_attempt_index,
                source_text_char_count=source_text_char_count,
                prompt_char_count=prompt_char_count,
                request_body_char_count=request_body_char_count,
                message_count=message_count,
                context_item_count=len(history),
                api_error_type=api_error_type,
                api_error_message_class=api_error_message_class,
            )

        def record_attempt_duration(attempt_index: int, attempt_started_at: float) -> None:
            nonlocal api_attempt_index, api_final_attempt_ms
            nonlocal api_first_attempt_ms, api_retry_attempt_ms
            elapsed_ms = _elapsed_ms(attempt_started_at)
            api_attempt_index = attempt_index
            api_final_attempt_ms = elapsed_ms
            if attempt_index == 1:
                api_first_attempt_ms = elapsed_ms
            elif attempt_index == 2:
                api_retry_attempt_ms = elapsed_ms

        record_diagnostics()
        if not self._api_key:
            return None
        import urllib.request
        import urllib.error
        import json as _json

        messages = [{"role": "system", "content": system_prompt}]
        for ko, zh in (history or []):
            messages.append({"role": "user", "content": f"input: {ko}"})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": _build_groq_user_message(text, incomplete)})

        payload_text = _json.dumps({
            "model": self._model,
            "messages": messages,
            "temperature": cfg.translation.temperature,
            "max_tokens": self._max_tokens,
        })
        request_body_char_count = len(payload_text)
        message_count = len(messages)
        payload = payload_text.encode()

        req = urllib.request.Request(
            _GROQ_BASE_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _GROQ_USER_AGENT,
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        attempt_started_at: float | None = None
        try:
            attempt_started_at = time.monotonic()
            if api_total_start is None:
                api_total_start = attempt_started_at
            api_attempt_count += 1
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                data = _json.loads(r.read())
            record_attempt_duration(1, attempt_started_at)
            log.info("Groq translate: %.0fms", (time.monotonic() - attempt_started_at) * 1000)
            _log_token_usage("Groq", data.get("usage"))
            content = (data["choices"][0]["message"].get("content") or "").strip()
            if self._strip_think:
                content = _strip_think_tags(content)
            log.debug("Groq: %.30s → %s", text, content)
            record_diagnostics()
            return content or None
        except urllib.error.HTTPError as e:
            if attempt_started_at is not None:
                record_attempt_duration(1, attempt_started_at)
            body = ""
            try:
                body = e.read().decode()
            except Exception:
                pass
            if _is_groq_token_limit_error(e.code, body) and history:
                log.warning("Groq request exceeded token budget; retrying once without history")
                retry_count = 1
                retry_reason = "token_limit_without_history"
                record_diagnostics("http_error", "token_limit")
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": _build_groq_user_message(text, incomplete)},
                ]
                payload_text = _json.dumps({
                    "model": self._model,
                    "messages": messages,
                    "temperature": cfg.translation.temperature,
                    "max_tokens": self._retry_max_tokens,
                })
                request_body_char_count = len(payload_text)
                message_count = len(messages)
                payload = payload_text.encode()
                req = urllib.request.Request(
                    _GROQ_BASE_URL,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": _GROQ_USER_AGENT,
                        "Authorization": f"Bearer {self._api_key}",
                    },
                )
                retry_started_at: float | None = None
                try:
                    retry_started_at = time.monotonic()
                    if api_total_start is None:
                        api_total_start = retry_started_at
                    api_attempt_count += 1
                    with urllib.request.urlopen(req, timeout=self._timeout) as r:
                        data = _json.loads(r.read())
                    record_attempt_duration(2, retry_started_at)
                    log.info("Groq translate: %.0fms", (time.monotonic() - retry_started_at) * 1000)
                    _log_token_usage("Groq", data.get("usage"))
                    content = (data["choices"][0]["message"].get("content") or "").strip()
                    if self._strip_think:
                        content = _strip_think_tags(content)
                    log.debug("Groq: %.30s → %s", text, content)
                    record_diagnostics()
                    return content or None
                except urllib.error.HTTPError as retry_error:
                    if retry_started_at is not None:
                        record_attempt_duration(2, retry_started_at)
                    body = ""
                    try:
                        body = retry_error.read().decode()
                    except Exception:
                        pass
                    log.error("Groq HTTP %d: %s", retry_error.code, body or retry_error)
                    error_type, message_class = _classify_api_error(retry_error)
                    record_diagnostics(error_type, message_class)
                    return None
                except Exception as retry_error:
                    # This block runs inside an except handler: an uncaught
                    # timeout/URLError here would escape translate() and break
                    # the None-on-failure engine contract, skipping the rest of
                    # the fallback chain. Catch everything and fail soft.
                    if _is_timeout_exception(retry_error) or classify_error(retry_error) == "network":
                        if _is_timeout_exception(retry_error):
                            api_timeout_count += 1
                        log.warning("Groq retry network/timeout error: %s", retry_error)
                    else:
                        log.error("Groq retry error: %s", retry_error)
                    if retry_started_at is not None:
                        record_attempt_duration(2, retry_started_at)
                    error_type, message_class = _classify_api_error(retry_error)
                    record_diagnostics(error_type, message_class)
                    return None
            if e.code == 401:
                log.error("Groq auth error — GROQ_API_KEY_fall_back 無效或已過期")
            elif e.code == 429:
                log.warning("Groq rate-limit (429)")
            else:
                log.error("Groq HTTP %d: %s", e.code, body or e)
            error_type, message_class = _classify_api_error(e)
            record_diagnostics(error_type, message_class)
            return None
        except Exception as e:
            kind = classify_error(e)
            if attempt_started_at is not None:
                record_attempt_duration(1, attempt_started_at)
            if _is_timeout_exception(e) or kind == "network":
                if _is_timeout_exception(e):
                    api_timeout_count += 1
                log.warning("Groq network/timeout error: %s", e)
            else:
                log.error("Groq error: %s", e)
            error_type, message_class = _classify_api_error(e)
            record_diagnostics(error_type, message_class)
            return None


def _make_engine(name: str) -> "TranslationEngine | None":
    """Instantiate an engine by name. Returns None if unavailable or unknown."""
    if name == "claude":
        e = ClaudeEngine()
        return e if e.available else None
    if name == "google_translate":
        e = GoogleTranslateEngine()
        return e if e.available else None
    if name == "ollama":
        return OllamaEngine()
    if name == "nvidia":
        e = NvidiaEngine()
        return e if e.available else None
    if name == "openrouter":
        e = OpenRouterTranslationEngine()
        return e if e.available else None
    if name == "groq":
        e = GroqTranslationEngine()
        return e if e.available else None
    log.warning("Unknown engine %r in engine_chain — skipping", name)
    return None


def engine_chain_config_key() -> tuple:
    mode = cfg.translation.translation_mode
    backend = cfg.clip_engine if mode == "clip" else cfg.live_engine
    return (
        mode,
        backend,
        tuple(getattr(cfg.translation, "engine_chain", ())),
        getattr(cfg.translation, "model", ""),
        getattr(cfg.ollama, "base_url", ""),
        getattr(cfg.ollama, "model", ""),
        getattr(cfg.ollama, "timeout", ""),
        getattr(cfg.nvidia, "model", ""),
        getattr(cfg.nvidia, "timeout", ""),
        getattr(cfg.nvidia, "live_timeout", ""),
        getattr(cfg.translation, "openrouter_model", ""),
        getattr(cfg.translation, "openrouter_timeout", ""),
        getattr(cfg.translation, "groq_translation_model", ""),
        getattr(cfg.translation, "groq_translation_timeout", ""),
        getattr(cfg.translation, "google_translate_lang", ""),
    )


def _build_engine_chain() -> "list[TranslationEngine]":
    """Build an ordered list of available engines.

    Picks cfg.live_engine or cfg.clip_engine based on current translation_mode.
    "ollama" bypasses engine_chain entirely.
    "nvidia" uses NvidiaEngine first, then appends available engines from engine_chain.
    "anthropic" (default) uses engine_chain directly as ordered fallback.
    """
    mode = cfg.translation.translation_mode
    engine_name = cfg.clip_engine if mode == "clip" else cfg.live_engine
    log.info("Engine selection: mode=%s → engine=%s", mode, engine_name)
    if engine_name == "ollama":
        return [OllamaEngine()]
    if engine_name == "nvidia":
        e = NvidiaEngine()
        if not e.available:
            log.error("NvidiaEngine unavailable — check NVIDIA_API_KEY")
            return []
        fallbacks = [fb for name in cfg.translation.engine_chain
                     if (fb := _make_engine(name)) is not None]
        if fallbacks:
            log.info("NvidiaEngine ready with fallback chain: %s",
                     [fb.engine_name for fb in fallbacks])
        return [e] + fallbacks
    engines = [e for name in cfg.translation.engine_chain
               if (e := _make_engine(name)) is not None]
    if not engines:
        log.error("No translation engines available — all engines failed to initialise")
    return engines
