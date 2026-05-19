import socket
import threading
import time
from abc import ABC, abstractmethod
from urllib.error import URLError

from config import cfg
from utils.api_retry import classify_error
from utils.logger import get_logger

log = get_logger("translation_engines")

_GEMINI_HTTP_TIMEOUT_MS = 12000
_NVIDIA_MAX_ATTEMPTS = 2
_NVIDIA_RETRY_DELAY_SEC = 0.5
_ENGINE_DIAGNOSTICS = threading.local()


def _set_last_engine_diagnostics(engine: str = "", retry_count: int = 0, retry_reason: str = "") -> None:
    _ENGINE_DIAGNOSTICS.value = {
        "engine": engine,
        "retry_count": retry_count,
        "retry_reason": retry_reason,
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


def _build_user_message(text: str, incomplete: bool) -> str:
    if incomplete:
        return f"input (incomplete sentence, translate as best as possible): {text}"
    return f"input: {text}"


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


def _log_token_usage(engine: str, usage) -> None:
    prompt_tokens = _usage_value(usage, "prompt_token_count", "promptTokenCount", "input_tokens")
    output_tokens = _usage_value(usage, "candidates_token_count", "candidatesTokenCount", "output_tokens", "response_token_count")
    total_tokens = _usage_value(usage, "total_token_count", "totalTokenCount")
    cache_write = _usage_value(usage, "cache_creation_input_tokens")
    cache_read = _usage_value(usage, "cache_read_input_tokens")

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
        """Short identifier stored in the DB (e.g. 'gemini', 'claude', 'deepl')."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Model/version string for DB cache keying (e.g. 'gemini-2.5-flash')."""
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


class GeminiEngine(TranslationEngine):
    def __init__(self):
        self._client = None
        if not cfg.keys.gemini:
            log.error("GEMINI_API_KEY not set")
            return
        try:
            import google.genai as genai
            from google.genai import types as genai_types
            self._client = genai.Client(
                api_key=cfg.keys.gemini,
                http_options=genai_types.HttpOptions(timeout=_GEMINI_HTTP_TIMEOUT_MS),
            )
            log.info("GeminiEngine ready (model=%s)", cfg.translation.gemini_model)
        except Exception as e:
            log.error("Failed to init Gemini: %s", e)

    @property
    def engine_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return cfg.translation.gemini_model

    @property
    def available(self) -> bool:
        return self._client is not None

    def translate(self, text: str, system_prompt: str, incomplete: bool,
                  history: list[tuple[str, str]] | None = None) -> str | None:
        if self._client is None:
            return None
        try:
            from google.genai import types as genai_types
            contents = []
            for ko, zh in (history or []):
                contents.append(genai_types.Content(
                    role="user", parts=[genai_types.Part(text=f"input: {ko}")]
                ))
                contents.append(genai_types.Content(
                    role="model", parts=[genai_types.Part(text=zh)]
                ))
            contents.append(genai_types.Content(
                role="user",
                parts=[genai_types.Part(text=_build_user_message(text, incomplete))],
            ))
            config = genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=cfg.translation.max_tokens,
                temperature=cfg.translation.temperature,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
            )
            _t0 = time.monotonic()
            resp = self._client.models.generate_content(
                model=cfg.translation.gemini_model,
                contents=contents,
                config=config,
            )
            log.info("Gemini translate: %.0fms", (time.monotonic() - _t0) * 1000)
            _log_token_usage("Gemini", getattr(resp, "usage_metadata", None))
            result = resp.text.strip()
            log.debug("Gemini: %.30s → %s", text, result)
            return result
        except Exception as e:
            kind = classify_error(e)
            if kind == "auth":
                log.error("Gemini auth error (check GEMINI_API_KEY): %s", e)
            elif kind == "rate_limit":
                log.warning("Gemini rate-limit: %s", e)
            elif kind == "network":
                log.warning("Gemini network error: %s", e)
            else:
                log.error("Gemini error: %s", e)
            return None


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
            for ko, zh in (history or []):
                messages.append({"role": "user", "content": f"input: {ko}"})
                messages.append({"role": "assistant", "content": zh})
            messages.append({"role": "user", "content": _build_user_message(text, incomplete)})
            resp = self._client.messages.create(
                model=cfg.translation.model,
                max_tokens=cfg.translation.max_tokens,
                temperature=cfg.translation.temperature,
                system=[system_content],
                messages=messages,
                timeout=5.0,
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
        for ko, zh in (history or []):
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
            usage = data.get("usage", {})
            log.info("Ollama tokens | prompt=%s output=%s",
                     usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
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
        _set_last_engine_diagnostics("nvidia", 0, "")
        if not self._api_key:
            return None
        import urllib.request
        import urllib.error
        import json as _json

        messages = [{"role": "system", "content": system_prompt}]
        for ko, zh in (history or []):
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
        payload = _json.dumps(body).encode()

        for attempt in range(_NVIDIA_MAX_ATTEMPTS):
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
                with urllib.request.urlopen(req, timeout=self._timeout) as r:
                    data = _json.loads(r.read())
                log.info("Nvidia translate: %.0fms", (time.monotonic() - _t0) * 1000)
                usage = data.get("usage", {})
                log.info("Nvidia tokens | prompt=%s output=%s",
                         usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
                msg = data["choices"][0]["message"]
                content = (msg.get("content") or "").strip()
                if self._strip_think:
                    import re as _re
                    content = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL).strip()
                log.debug("Nvidia: %.30s → %s", text, content)
                if content:
                    return content
                if attempt + 1 < _NVIDIA_MAX_ATTEMPTS:
                    _set_last_engine_diagnostics("nvidia", attempt + 1, "empty_response")
                    log.warning("Nvidia returned empty response; retrying once")
                    time.sleep(_NVIDIA_RETRY_DELAY_SEC)
                    continue
                return None
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode()
                except Exception:
                    pass
                if e.code == 401:
                    log.error("Nvidia auth error — NVIDIA_API_KEY 無效或已過期")
                elif e.code == 404:
                    log.error("Nvidia 模型 %r 不存在 — 請確認 build.nvidia.com 上的模型名稱", self._model)
                elif e.code == 429:
                    log.warning("Nvidia rate-limit (429) — 請求過於頻繁，略過此句")
                else:
                    log.error("Nvidia HTTP %d: %s", e.code, body or e)
                return None
            except urllib.error.URLError as e:
                if attempt + 1 < _NVIDIA_MAX_ATTEMPTS:
                    reason = "timeout" if _is_timeout_exception(e) else "network"
                    _set_last_engine_diagnostics("nvidia", attempt + 1, reason)
                    log.warning("Nvidia network error; retrying once: %s", e)
                    time.sleep(_NVIDIA_RETRY_DELAY_SEC)
                    continue
                log.error("Nvidia network error: %s", e)
                return None
            except Exception as e:
                reason = "timeout" if _is_timeout_exception(e) else classify_error(e)
                if reason in ("timeout", "network") and attempt + 1 < _NVIDIA_MAX_ATTEMPTS:
                    _set_last_engine_diagnostics("nvidia", attempt + 1, reason)
                    log.warning("Nvidia transient error; retrying once: %s", e)
                    time.sleep(_NVIDIA_RETRY_DELAY_SEC)
                    continue
                log.error("Nvidia error: %s", e)
                return None
        return None


def _make_engine(name: str) -> "TranslationEngine | None":
    """Instantiate an engine by name. Returns None if unavailable or unknown."""
    if name == "gemini":
        e = GeminiEngine()
        return e if e.available else None
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
    log.warning("Unknown engine %r in engine_chain — skipping", name)
    return None


def _build_engine_chain() -> "list[TranslationEngine]":
    """Build an ordered list of available engines.

    Picks cfg.live_engine or cfg.clip_engine based on current translation_mode.
    "ollama"/"nvidia" bypass engine_chain entirely — no fallback.
    "anthropic" (default) uses engine_chain with ordered fallback.
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


