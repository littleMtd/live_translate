"""
PromptEvolver — evolves the translation system prompt in real-time during a stream.

Every `cfg.translation.evolve_every` completed translations, it sends a batch of
recent (ko → zh) pairs to Gemini and asks it to:
  1. Identify new stream-specific slang / names / game terms
  2. Note any consistent translation errors

The result is merged back into the live system prompt used by Translator.
Thread-safe: analysis runs in a background thread so it never blocks translation.

If GEMINI_API_KEY is missing the evolver disables itself at construction time —
record() becomes a no-op so we don't trigger a repeating auth error every
evolve_every translations.
"""
import json
import threading
import time
from collections import deque

from config import cfg
from utils.logger import get_logger
from utils.api_retry import classify_error, _RETRY_DELAYS

log = get_logger("prompt_evolver")

# Hard cap on evolved slang entries. The evolved prompt feeds the prompt_ver
# hash that keys both the in-memory and DB translation caches, so every prompt
# change invalidates all cached translations (intentional: a new prompt may
# yield different output). An unbounded slang dict would make the prompt grow
# for the whole stream — more tokens per call AND a cache flush on every
# evolve cycle. Oldest entries are evicted first (insertion order ≈ LRU-add).
_MAX_EXTRA_SLANG = 30

_META_SYSTEM = (
    "你是翻譯品質分析師。你會收到一批韓語→繁中的字幕翻譯記錄，"
    "請分析並找出這場直播特有的用語、主播名字、遊戲名稱、或需要修正的翻譯。"
    "只輸出 JSON，格式如下，不要加任何說明：\n"
    '{"new_slang": {"韓文詞": "中文譯", ...}, '
    '"stream_context": "一句話描述這場直播的主題與主播風格", '
    '"corrections": ["發現的翻譯問題描述（可空陣列）"]}'
)


class PromptEvolver:
    def __init__(self):
        self._lock = threading.Lock()
        self._buffer: deque[tuple[str, str]] = deque(maxlen=cfg.translation.evolve_every * 3)
        self._count = 0
        self._extra_slang: dict[str, str] = {}
        self._stream_context: str = ""
        self._analyzing = False
        self._gemini_client = None
        # Disable evolver if the operator turned it on without supplying a key,
        # otherwise every evolve_every translations would log a fresh auth error.
        self._disabled = bool(cfg.translation.evolve_enabled) and not cfg.keys.gemini
        if self._disabled:
            log.warning(
                "evolve_enabled=True but GEMINI_API_KEY not set — PromptEvolver disabled",
            )

    def record(self, ko: str, zh: str):
        """Call after every successful translation."""
        if self._disabled or not cfg.translation.evolve_enabled:
            return
        # Set _analyzing=True atomically inside the lock to prevent two threads
        # from both passing the `not self._analyzing` guard before either sets it.
        with self._lock:
            self._buffer.append((ko, zh))
            self._count += 1
            if self._count % cfg.translation.evolve_every == 0 and not self._analyzing:
                self._analyzing = True
                should_trigger = True
            else:
                should_trigger = False

        if should_trigger:
            threading.Thread(target=self._analyze, daemon=True,
                             name="PromptEvolver").start()

    def build_system_prompt(self, base_prompt: str) -> str:
        """Return the current evolved system prompt."""
        with self._lock:
            parts = [base_prompt]
            if self._stream_context:
                parts.append(f"[本場直播背景]: {self._stream_context}")
            if self._extra_slang:
                additions = "、".join(f"{k}→{v}" for k, v in self._extra_slang.items())
                parts.append(f"[本場新增俚語]: {additions}")
            return "\n".join(parts)

    def _call_gemini(self, system: str, user: str) -> str:
        if self._gemini_client is None:
            import google.genai as genai
            self._gemini_client = genai.Client(api_key=cfg.keys.gemini)
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                from google.genai import types as genai_types
                config = genai_types.GenerateContentConfig(
                    system_instruction=system,
                )
                resp = self._gemini_client.models.generate_content(
                    model=cfg.translation.gemini_model,
                    contents=user,
                    config=config,
                )
                return resp.text.strip()
            except Exception as e:
                kind = classify_error(e)
                if kind == "auth":
                    log.error("Evolver Gemini auth error: %s", e)
                    raise
                if kind in ("rate_limit", "network") and attempt < len(_RETRY_DELAYS):
                    delay = _RETRY_DELAYS[attempt]
                    log.warning("Evolver Gemini %s error (attempt %d/%d), retrying in %ds",
                                kind, attempt + 1, len(_RETRY_DELAYS) + 1, delay)
                    time.sleep(delay)
                    continue
                raise
        raise RuntimeError(f"Evolver Gemini failed after {len(_RETRY_DELAYS)} retries")

    def _analyze(self):
        with self._lock:
            pairs = list(self._buffer)

        try:
            sample = "\n".join(f"{i+1}. {ko} → {zh}" for i, (ko, zh) in enumerate(pairs[-30:]))
            user_msg = f"以下是最近 {len(pairs[-30:])} 句翻譯記錄：\n{sample}"

            raw = self._call_gemini(_META_SYSTEM, user_msg)
            data = json.loads(raw)

            new_slang = data.get("new_slang", {})
            context = data.get("stream_context", "")
            corrections = data.get("corrections", [])

            with self._lock:
                if new_slang:
                    self._extra_slang.update(new_slang)
                    overflow = len(self._extra_slang) - _MAX_EXTRA_SLANG
                    if overflow > 0:
                        for key in list(self._extra_slang)[:overflow]:
                            del self._extra_slang[key]
                        log.info("Evolved slang capped at %d entries (%d evicted)",
                                 _MAX_EXTRA_SLANG, overflow)
                    log.info("Evolved slang: %s", new_slang)
                if context:
                    self._stream_context = context
                    log.info("Stream context: %s", context)
                if corrections:
                    for c in corrections:
                        log.warning("Translation issue detected: %s", c)

        except json.JSONDecodeError as e:
            log.warning("Evolver: invalid JSON response — %s", e)
        except Exception as e:
            log.error("Evolver analysis failed: %s", e)
        finally:
            with self._lock:
                self._analyzing = False
