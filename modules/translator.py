import hashlib
import queue
import threading
import time
from collections import deque, OrderedDict
from datetime import datetime
from pathlib import Path

from config import cfg
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.queue_utils import put_latest
from modules.pipeline_events import sentence_incomplete, sentence_text
from modules.prompt_evolver import PromptEvolver  # noqa: E402
from modules.db import _get_db  # noqa: E402

log = get_logger("translator")

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_MIN_TRANSLATE_CHARS = 2    # skip STT fragments shorter than this
_CACHE_MAX_SIZE = 500       # max entries in per-session translation cache
_FALLBACK_PROBE_EVERY = 50  # after this many fallback calls, probe engines[0] once

_HANGUL_RATIO_THRESHOLD = 0.50  # reject result if >50 % of chars are Hangul syllables


def _looks_untranslated(result: str, source: str) -> bool:
    if result == source:
        return True
    chars = [c for c in result if not c.isspace()]
    if not chars:
        return False
    if len(chars) < 6:
        return False  # too short for ratio to be meaningful (single Korean name is OK)
    hangul = sum(1 for c in chars if "가" <= c <= "힣")
    if (hangul / len(chars)) > _HANGUL_RATIO_THRESHOLD:
        return True
    # Japanese hiragana/katakana should never appear in zh-TW output
    japanese = sum(1 for c in chars if "぀" <= c <= "ゟ" or "゠" <= c <= "ヿ")
    if japanese > 2:
        return True
    # Result much longer than source likely means hallucinated continuation
    src_chars = len([c for c in source if not c.isspace()])
    if len(chars) > src_chars * 3 and len(chars) > 40:
        return True
    return False


def _write_history(ko: str, zh: str) -> None:
    path = _LOG_DIR / f"translations_{datetime.now().strftime('%Y%m%d')}.txt"
    ts = datetime.now().strftime("%H:%M:%S")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {ko}\n        → {zh}\n")

from modules.translation_prompts import (
    _BASE_PROMPT,
    _QWEN_PROMPT,
    _is_qwen_model,
    _build_base_prompt,
    _build_qwen_optimized_prompt,
    get_translation_profile,
)
from modules.translation_engines import (
    TranslationEngine,
    GeminiEngine,
    ClaudeEngine,
    GoogleTranslateEngine,
    OllamaEngine,
    NvidiaEngine,
    _build_user_message,
    _build_engine_chain,
)
from modules.translation_runtime import (
    FallbackState,
    active_engine,
    call_with_fallback,
)
from modules.translation_memory import TranslationMemory
from modules.translation_policy import TranslationPolicy


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

class Translator:
    def __init__(self):
        self._evolver = PromptEvolver()
        self._engines: list[TranslationEngine] = _build_engine_chain()
        self._active_idx = 0
        self._probe_counter = 0
        # 保留最近的翻译历史以提供上下文；至少保留 30 条以增强上下文感知
        recent_window = max(getattr(cfg.translation, 'context_window', 0) or 0, 30)
        self._memory = TranslationMemory(
            recent_window=recent_window,
            max_cache_size=_CACHE_MAX_SIZE,
            db_factory=_get_db,
            history_writer=_write_history,
        )
        self._cache = self._memory.cache
        self._recent = self._memory.recent
        self._policy = TranslationPolicy(
            slang=cfg.translation.slang,
            min_translate_chars=_MIN_TRANSLATE_CHARS,
        )
        self._last_input: str = ""

    @staticmethod
    def _is_stt_garbage(text: str) -> bool:
        return TranslationPolicy.is_stt_garbage(text)

    def translate(self, text: str, incomplete: bool = False) -> str | None:
        text = self._prepare_input(text)
        if text is None:
            return None

        if slang_result := self._translate_slang(text, incomplete):
            return slang_result

        # 根据当前模型选择对应的 prompt
        system_prompt = self._build_system_prompt()
        prompt_ver = self._prompt_version(system_prompt)
        self._log_prompt_mode_once()

        if existing := self._lookup_existing_translation(text, incomplete, prompt_ver):
            return existing

        result = self._call_with_fallback(text, system_prompt, incomplete, self._memory_state().context())
        if result:
            self._record_success(text, result, incomplete, prompt_ver)
        else:
            # API failure — allow next identical input to retry rather than staying suppressed
            self._policy_state().reset_last_input()
            self._last_input = ""
        return result

    def _prepare_input(self, text: str) -> str | None:
        prepared = self._policy_state().prepare_input(text)
        self._last_input = self._policy_state().last_input
        return prepared

    def _policy_state(self) -> TranslationPolicy:
        policy = getattr(self, "_policy", None)
        if policy is not None:
            return policy

        policy = TranslationPolicy(
            slang=cfg.translation.slang,
            min_translate_chars=_MIN_TRANSLATE_CHARS,
            last_input=getattr(self, "_last_input", ""),
        )
        self._policy = policy
        self._last_input = policy.last_input
        return policy

    def _memory_state(self) -> TranslationMemory:
        memory = getattr(self, "_memory", None)
        if memory is not None:
            return memory

        recent_window = max(getattr(cfg.translation, 'context_window', 0) or 0, 30)
        cache = getattr(self, "_cache", OrderedDict())
        recent = getattr(self, "_recent", deque(maxlen=recent_window))
        memory = TranslationMemory(
            cache=cache,
            recent=recent,
            recent_window=recent_window,
            max_cache_size=_CACHE_MAX_SIZE,
            db_factory=_get_db,
            history_writer=_write_history,
        )
        self._memory = memory
        self._cache = memory.cache
        self._recent = memory.recent
        return memory

    def _translate_slang(self, text: str, incomplete: bool) -> str | None:
        slang_result = self._policy_state().slang_result(text)
        if not slang_result:
            return None

        log.debug("Slang hit: %s → %s", text, slang_result)
        self._evolver.record(text, slang_result)
        self._memory_state().record_direct(text, slang_result, incomplete)
        return slang_result

    def _lookup_existing_translation(self, text: str, incomplete: bool,
                                     prompt_ver: str) -> str | None:
        existing = self._memory_state().lookup_existing(
            text,
            incomplete,
            prompt_ver,
            self._active_engine(),
        )
        if existing:
            log.debug("Cache hit: %s", text[:20])
        return existing

    def _record_success(self, text: str, result: str, incomplete: bool,
                        prompt_ver: str) -> None:
        self._evolver.record(text, result)
        self._memory_state().record_success(
            text,
            result,
            incomplete,
            prompt_ver,
            self._active_engine(),
        )

    def _active_engine(self) -> TranslationEngine | None:
        return active_engine(self._engines, self._active_idx)

    def _log_prompt_mode_once(self) -> None:
        if _is_qwen_model() and not hasattr(self, '_qwen_log_once'):
            log.info("Using Qwen-optimized system prompt (shorter, more direct)")
            self._qwen_log_once = True

    def _build_system_prompt(self) -> str:
        is_qwen = _is_qwen_model()
        base_prompt = _QWEN_PROMPT if is_qwen else _BASE_PROMPT
        system_prompt = self._evolver.build_system_prompt(base_prompt)

        if not cfg.translation.use_profile:
            return system_prompt

        streamer_profile = get_translation_profile(cfg.active_streamer_profile, qwen=is_qwen)
        if streamer_profile:
            system_prompt += "\n\n" + streamer_profile
            log.debug("Appended streamer profile: %s", cfg.active_streamer_profile)

        return system_prompt

    @staticmethod
    def _prompt_version(system_prompt: str) -> str:
        return hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    def _call_with_fallback(self, text: str, system_prompt: str, incomplete: bool,
                            history: list[tuple[str, str]] | None = None) -> str | None:
        state = FallbackState(self._active_idx, self._probe_counter)
        result = call_with_fallback(
            self._engines,
            state,
            text,
            system_prompt,
            incomplete,
            history,
            _FALLBACK_PROBE_EVERY,
            _looks_untranslated,
            log,
        )
        self._active_idx = state.active_idx
        self._probe_counter = state.probe_counter
        return result

    def _get_prompt_version_hash(self) -> str:
        return self._prompt_version(self._build_system_prompt())

    def _cache_store(self, text: str, incomplete: bool, value: str, prompt_ver: str) -> None:
        self._memory_state().cache_store(text, incomplete, value, prompt_ver)

    def _cache_lookup(self, text: str, incomplete: bool, prompt_ver: str) -> str | None:
        return self._memory_state().cache_lookup(text, incomplete, prompt_ver)

    def _db_lookup(self, text: str, engine: TranslationEngine, prompt_ver: str) -> str | None:
        return self._memory_state().db_lookup(text, engine, prompt_ver)

    def _db_store(self, text: str, result: str, engine: TranslationEngine, prompt_ver: str) -> None:
        self._memory_state().db_store(text, result, engine, prompt_ver)


_DEDUP_SUBTITLE_SEC = 5.0   # suppress identical subtitle within this window


def start(sentence_queue: queue.Queue, subtitle_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        translator = Translator()
        last_result = ""
        last_result_time = 0.0
        while not stop_event.is_set():
            has_item, item = poll_queue(sentence_queue, stop_event, pause_event)
            if not has_item:
                continue

            text = sentence_text(item)
            incomplete = sentence_incomplete(item)
            started = time.monotonic()
            result = translator.translate(text, incomplete)
            metrics.observe_latency("translation", time.monotonic() - started)
            if result:
                metrics.increment("translation.success")
                now = time.monotonic()
                if result == last_result and (now - last_result_time) < _DEDUP_SUBTITLE_SEC:
                    log.debug("Suppressing duplicate subtitle: %s", result[:30])
                    continue
                last_result = result
                last_result_time = now
                put_latest(subtitle_queue, result, log, "subtitle_queue")
            else:
                metrics.increment("translation.empty")
            metrics.log_summary_if_due()

        log.info("Translator stopped")

    return start_daemon_thread("Translator", run)


if __name__ == "__main__":
    translator = Translator()
    tests = [
        ("안녕하세요, 오늘 방송에 오신 걸 환영해요!", False),
        ("진짜 대박이다 ㅋㅋㅋ", False),
        ("지금 게임 하고", True),
    ]
    for text, incomplete in tests:
        result = translator.translate(text, incomplete)
        print(f"{text!r} → {result!r}")
