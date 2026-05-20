import hashlib
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config import cfg
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.queue_utils import put_latest
from utils.runtime_events import runtime_events, translation_quality
from modules.pipeline_events import sentence_incomplete, sentence_metadata, sentence_text
from modules.prompt_evolver import PromptEvolver
from modules.db import _get_db
from modules.translation_prompts import (
    _BASE_PROMPT,
    _QWEN_PROMPT,
    _is_qwen_model,
    get_translation_profile,
)
from modules.translation_engines import (
    TranslationEngine,
    _build_engine_chain,
    get_last_engine_diagnostics,
)
from modules.translation_runtime import (
    FallbackState,
    active_engine,
    call_with_fallback,
)
from modules.translation_memory import MemoryLookup, TranslationMemory
from modules.translation_policy import TranslationPolicy

log = get_logger("translator")

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_MIN_TRANSLATE_CHARS = 2    # skip STT fragments shorter than this
_CACHE_MAX_SIZE = 500       # max entries in per-session translation cache
_FALLBACK_PROBE_EVERY = 50  # after this many fallback calls, probe engines[0] once
_TRANSLATION_WORKERS = 2
_MAX_PENDING_TRANSLATIONS = 4
_TRANSLATION_LOOP_POLL_SEC = 0.05

_HANGUL_RATIO_THRESHOLD = 0.50  # reject result if >50 % of chars are Hangul syllables
_DEPENDENCY_MARKERS = (
    "그러니까",
    "그런데",
    "그러면",
    "그러네",
    "그렇지",
    "그래서",
    "근데",
    "아니",
    "맞아",
    "그게",
    "그럼",
    "그리고",
)
_DEPENDENCY_MARKER_BOUNDARY_RE = re.compile(r"^[\s\.,!?~…。？！,，、:;；]|$")


_META_GARBAGE_MARKERS = (
    "無法理解",
    "无法理解",
    "無明確語義",
    "无明确语义",
    "STT亂碼",
    "STT乱碼",
    "STT 垃圾",
    "亂碼",
    "乱码",
    "無意義詞",
    "无意义词",
    "無意義",
    "无意义",
    "省略",
)

_SOURCE_AWARE_TARGET_REPLACEMENTS = (
    (("하데스", "하덱스"), (("哈迪斯", "HADES"), ("哈德克斯", "HADES"))),
    (("마가 뜨", "마가뜨"), (("瑪加特", "冷場"), ("馬嘎", "冷場"), ("魔嘎", "冷場"))),
    (("붕 뜨",), (("飄起來的時間", "空掉的時間"), ("浮起來的時間", "空掉的時間"))),
    (("개복치",), (("鯛魚燒", "玻璃心"), ("翻車魚風格", "玻璃心風格"))),
    (("끼윤",), (("끼윤", "Kkiyun"),)),
    (("예난",), (("예난", "Yenan"), ("藝蘭", "Yenan"))),
    (("히나",), (("希娜", "Hina"),)),
    (("철구",), (("哲求", "Chulgu"), ("鐵球", "Chulgu"))),
    (("신빨",), (("更懂鞋", "神力更強"), ("鞋比較好", "神力比較強"))),
    (
        ("만신",),
        (
            ("幾乎都滿了，滿了", "幾乎是大神巫"),
            ("都滿了，滿了", "簡直是大神巫"),
            ("滿了，滿了", "大神巫，大神巫"),
        ),
    ),
)

_SHARED_NAME_SCOPE = "__shared__"
_HADES_PROFILE_ID = "hades_chxxnnx"
_KOREAN_NAME_SUFFIXES = frozenset(
    (
        "이에요",
        "입니다",
        "에게",
        "한테",
        "이랑",
        "하고",
        "예요",
        "이다",
        "누나",
        "언니",
        "오빠",
        "님",
        "씨",
        "형",
        "가",
        "이",
        "은",
        "는",
        "을",
        "를",
        "의",
        "도",
        "만",
        "에",
        "께",
        "랑",
        "과",
        "와",
        "야",
        "아",
    )
)


@dataclass(frozen=True)
class _NameRenderingRule:
    scope: str
    source_aliases: tuple[str, ...]
    wrong_forms: tuple[str, ...]
    canonical: str


_NAME_RENDERING_RULES = (
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("챈나",),
        ("챈나", "-chan", "-Chan", "－chan", "－Chan", "–chan", "–Chan", "—chan", "—Chan"),
        "Chxxnnx",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("김봉준", "봉준"),
        ("김봉준", "봉준", "Bongjun", "奉俊", "奉主"),
        "Kim Bongjun",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("성태",),
        ("성태", "Sungtae老師", "Sungtae哥", "Sungtae", "成泰", "狀態哥"),
        "KimSungtae",
    ),
    _NameRenderingRule(
        _HADES_PROFILE_ID,
        ("키마",),
        ("키마", "Kima", "基馬"),
        "Kyma",
    ),
    _NameRenderingRule(
        _SHARED_NAME_SCOPE,
        ("고세구",),
        ("高世久",),
        "Gosegu",
    ),
)


def _looks_like_meta_garbage_output(result: str) -> bool:
    normalized = result.strip()
    if not normalized:
        return False
    if "STT" in normalized.upper() and any(marker in normalized for marker in _META_GARBAGE_MARKERS):
        return True
    if normalized.startswith(("(", "（", "[", "【")) and any(
        marker in normalized for marker in _META_GARBAGE_MARKERS
    ):
        return True
    return False


def _is_hangul_syllable(char: str) -> bool:
    return "\uac00" <= char <= "\ud7a3"


def _is_name_suffix_boundary(char: str) -> bool:
    return char.isspace() or not char.isalnum()


def _source_alias_matches_at(source: str, alias: str, start: int) -> bool:
    if start > 0 and _is_hangul_syllable(source[start - 1]):
        return False

    end = start + len(alias)
    if end >= len(source):
        return True

    next_char = source[end]
    if not _is_hangul_syllable(next_char):
        return True

    suffix_end = end
    while suffix_end < len(source) and _is_hangul_syllable(source[suffix_end]):
        suffix_end += 1

    suffix = source[end:suffix_end]
    if suffix not in _KOREAN_NAME_SUFFIXES:
        return False

    return suffix_end >= len(source) or _is_name_suffix_boundary(source[suffix_end])


def _source_has_name_alias(source: str, aliases: tuple[str, ...]) -> bool:
    for alias in aliases:
        if not alias:
            continue
        start = source.find(alias)
        while start >= 0:
            if _source_alias_matches_at(source, alias, start):
                return True
            start = source.find(alias, start + 1)
    return False


def _name_rendering_rule_enabled(rule: _NameRenderingRule) -> bool:
    if rule.scope == _SHARED_NAME_SCOPE:
        return True
    return bool(cfg.translation.use_profile) and cfg.active_streamer_profile == rule.scope


def _replace_wrong_name_forms(result: str, rule: _NameRenderingRule) -> str:
    if rule.canonical in result:
        return result

    wrong_forms = tuple(sorted(rule.wrong_forms, key=len, reverse=True))
    if not wrong_forms:
        return result

    pattern = re.compile("|".join(re.escape(wrong) for wrong in wrong_forms))
    return pattern.sub(rule.canonical, result)


def _apply_source_aware_corrections(source: str, result: str) -> str:
    corrected = result
    for source_terms, replacements in _SOURCE_AWARE_TARGET_REPLACEMENTS:
        if not any(term in source for term in source_terms):
            continue
        for wrong, right in replacements:
            corrected = corrected.replace(wrong, right)

    for rule in _NAME_RENDERING_RULES:
        if not _name_rendering_rule_enabled(rule):
            continue
        if not _source_has_name_alias(source, rule.source_aliases):
            continue
        corrected = _replace_wrong_name_forms(corrected, rule)

    if "무당" in source and "신발" in source:
        corrected = corrected.replace("更懂鞋", "神力更強")
        corrected = corrected.replace("更懂鞋子", "神力更強")

    return corrected


def _dependency_marker(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped:
        return ""
    for marker in _DEPENDENCY_MARKERS:
        if not stripped.startswith(marker):
            continue
        suffix = stripped[len(marker):]
        if _DEPENDENCY_MARKER_BOUNDARY_RE.match(suffix):
            return marker
    return ""


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


# ---------------------------------------------------------------------------
# Translator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TranslationOutcome:
    source_text: str
    target_text: str | None
    status: str
    result_source: str
    cache_status: str
    incomplete: bool
    engine: str = ""
    model: str = ""
    prompt_version: str = ""
    filter_reason: str = ""

    def as_event_fields(self, latency_ms: float, metadata: dict) -> dict:
        return {
            "source_text": self.source_text,
            "target_text": self.target_text,
            "status": self.status,
            "result_source": self.result_source,
            "cache_status": self.cache_status,
            "incomplete": self.incomplete,
            "engine": self.engine,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "filter_reason": self.filter_reason,
            "latency_ms": round(latency_ms, 2),
            **metadata,
            **translation_quality(self.source_text, self.target_text),
        }


@dataclass(frozen=True)
class _CompletedTranslation:
    seq: int
    outcome: TranslationOutcome
    elapsed: float
    metadata: dict
    submitted_at: float
    started_at: float
    completed_at: float
    worker_id: str
    retry_count: int
    retry_reason: str


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
        self._policy = TranslationPolicy(
            slang=cfg.translation.slang,
            min_translate_chars=_MIN_TRANSLATE_CHARS,
            max_translate_chars=cfg.translation.max_translate_chars,
        )
        self._last_input: str = ""

    def translate(self, text: str, incomplete: bool = False) -> str | None:
        return self.translate_event(text, incomplete).target_text

    def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
        raw_text = (text or "").strip()
        policy = self._policy_state()
        filter_reason = policy.rejection_reason(raw_text)
        text = self._prepare_input(text)
        if text is None:
            return TranslationOutcome(
                source_text=raw_text,
                target_text=None,
                status="filtered",
                result_source="policy",
                cache_status="skipped",
                incomplete=incomplete,
                filter_reason=filter_reason or policy._last_sanitize_rejection or "unknown",
            )

        if slang_result := self._translate_slang(text, incomplete):
            return TranslationOutcome(
                source_text=raw_text,
                target_text=slang_result,
                status="success",
                result_source="slang",
                cache_status="skipped",
                incomplete=incomplete,
            )

        # 根据当前模型选择对应的 prompt
        system_prompt = self._build_system_prompt()
        prompt_ver = self._prompt_version(system_prompt)
        self._log_prompt_mode_once()

        lookup = self._lookup_existing_translation_event(text, incomplete, prompt_ver)
        engine = self._active_engine()
        if lookup.result:
            target_text = _apply_source_aware_corrections(text, lookup.result)
            if _looks_like_meta_garbage_output(target_text):
                return TranslationOutcome(
                    source_text=raw_text,
                    target_text=None,
                    status="filtered",
                    result_source="post_policy",
                    cache_status=lookup.source,
                    incomplete=incomplete,
                    filter_reason="meta_garbage_output",
                    engine=engine.engine_name if engine else "",
                    model=engine.model_name if engine else "",
                    prompt_version=prompt_ver,
                )
            return TranslationOutcome(
                source_text=raw_text,
                target_text=target_text,
                status="success",
                result_source=lookup.source,
                cache_status=lookup.source,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_ver,
            )

        result = self._call_with_fallback(text, system_prompt, incomplete, self._memory_state().context())
        engine = self._active_engine()
        if result:
            result = _apply_source_aware_corrections(text, result)
            if _looks_like_meta_garbage_output(result):
                log.debug("Filtering meta garbage translation output: %.40s -> %.40s", text, result)
                return TranslationOutcome(
                    source_text=raw_text,
                    target_text=None,
                    status="filtered",
                    result_source="post_policy",
                    cache_status=lookup.source,
                    incomplete=incomplete,
                    engine=engine.engine_name if engine else "",
                    model=engine.model_name if engine else "",
                    prompt_version=prompt_ver,
                    filter_reason="meta_garbage_output",
                )
            self._record_success(text, result, incomplete, prompt_ver)
            return TranslationOutcome(
                source_text=raw_text,
                target_text=result,
                status="success",
                result_source="api",
                cache_status=lookup.source,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_ver,
            )
        else:
            # API failure — allow next identical input to retry rather than staying suppressed
            self._policy_state().reset_last_input()
            self._last_input = ""
        return TranslationOutcome(
            source_text=raw_text,
            target_text=None,
            status="failed",
            result_source="none",
            cache_status=lookup.source,
            incomplete=incomplete,
            engine=engine.engine_name if engine else "",
            model=engine.model_name if engine else "",
            prompt_version=prompt_ver,
        )

    def _prepare_input(self, text: str) -> str | None:
        prepared = self._policy_state().prepare_input(text)
        self._last_input = self._policy_state().last_input
        return prepared

    def _policy_state(self) -> TranslationPolicy:
        return self._policy

    def _memory_state(self) -> TranslationMemory:
        return self._memory

    def _translate_slang(self, text: str, incomplete: bool) -> str | None:
        slang_result = self._policy_state().slang_result(text)
        if not slang_result:
            return None

        log.debug("Slang hit: %s → %s", text, slang_result)
        self._evolver.record(text, slang_result)
        self._memory_state().record_direct(text, slang_result, incomplete)
        return slang_result

    def _lookup_existing_translation_event(self, text: str, incomplete: bool,
                                           prompt_ver: str) -> MemoryLookup:
        lookup = self._memory_state().lookup_existing_event(
            text,
            incomplete,
            prompt_ver,
            self._active_engine(),
        )
        if lookup.result:
            log.debug("Cache hit: %s", text[:20])
        return lookup

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


_DEDUP_SUBTITLE_SEC = 5.0   # suppress identical subtitle within this window


def start(sentence_queue: queue.Queue, subtitle_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        worker_state = threading.local()
        executor = ThreadPoolExecutor(
            max_workers=_TRANSLATION_WORKERS,
            thread_name_prefix="TranslationWorker",
        )
        pending: dict[int, Future[_CompletedTranslation]] = {}
        completed: dict[int, _CompletedTranslation] = {}
        next_seq = 0
        next_emit_seq = 0
        last_result = ""
        last_result_time = 0.0

        def translate_item(seq: int, item, submitted_at: float) -> _CompletedTranslation:
            text = sentence_text(item)
            incomplete = sentence_incomplete(item)
            metadata = sentence_metadata(item).copy()
            marker = _dependency_marker(text)
            metadata.update(
                {
                    "sequence_id": seq,
                    "starts_with_dependency_marker": bool(marker),
                    "dependency_marker": marker,
                }
            )
            started = time.monotonic()
            worker_id = threading.current_thread().name
            worker_translator = getattr(worker_state, "translator", None)
            if worker_translator is None:
                worker_translator = Translator()
                worker_state.translator = worker_translator
            try:
                outcome = worker_translator.translate_event(text, incomplete)
            except Exception:
                log.exception("Translation worker failed for: %.40s", text)
                outcome = TranslationOutcome(
                    source_text=(text or "").strip(),
                    target_text=None,
                    status="failed",
                    result_source="none",
                    cache_status="skipped",
                    incomplete=incomplete,
                )
            completed_at = time.monotonic()
            elapsed = completed_at - started
            diagnostics = get_last_engine_diagnostics()
            retry_count = 0
            retry_reason = ""
            if (
                outcome.result_source == "api"
                and outcome.engine == "nvidia"
                and diagnostics.get("engine") == "nvidia"
            ):
                retry_count = int(diagnostics.get("retry_count") or 0)
                retry_reason = str(diagnostics.get("retry_reason") or "")
            return _CompletedTranslation(
                seq,
                outcome,
                elapsed,
                metadata,
                submitted_at,
                started,
                completed_at,
                worker_id,
                retry_count,
                retry_reason,
            )

        def collect_finished() -> None:
            for seq, future in list(pending.items()):
                if not future.done():
                    continue
                pending.pop(seq)
                try:
                    completed[seq] = future.result()
                except Exception:
                    log.exception("Translation future failed")

        def emit_completed(item: _CompletedTranslation) -> None:
            nonlocal last_result, last_result_time
            outcome = item.outcome
            elapsed = item.elapsed
            emitted_at = time.monotonic()
            event_metadata = item.metadata.copy()
            event_metadata.update(
                {
                    "engine_latency_ms": round(elapsed * 1000, 2),
                    "queue_wait_ms": round(max(0.0, item.started_at - item.submitted_at) * 1000, 2),
                    "output_delay_ms": round(max(0.0, emitted_at - item.submitted_at) * 1000, 2),
                    "predecessor_stall_ms": round(max(0.0, emitted_at - item.completed_at) * 1000, 2),
                    "translation_worker_id": item.worker_id,
                    "retry_count": item.retry_count,
                    "retry_reason": item.retry_reason,
                }
            )
            metrics.observe_latency("translation", elapsed)
            event_fields = outcome.as_event_fields(elapsed * 1000, event_metadata)
            result = outcome.target_text
            if result:
                metrics.increment("translation.success")
                now = time.monotonic()
                if result == last_result and (now - last_result_time) < _DEDUP_SUBTITLE_SEC:
                    log.debug("Suppressing duplicate subtitle: %s", result[:30])
                    runtime_events.emit(
                        "translation",
                        **event_fields,
                        subtitle_emitted=False,
                        subtitle_suppressed_reason="duplicate",
                    )
                    return
                last_result = result
                last_result_time = now
                put_latest(subtitle_queue, result, log, "subtitle_queue")
                runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=True,
                    subtitle_suppressed_reason="",
                )
            else:
                metrics.increment("translation.empty")
                runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=False,
                    subtitle_suppressed_reason="",
                )
            metrics.log_summary_if_due()

        try:
            while not stop_event.is_set():
                collect_finished()
                while next_emit_seq in completed:
                    emit_completed(completed.pop(next_emit_seq))
                    next_emit_seq += 1

                if len(pending) >= _MAX_PENDING_TRANSLATIONS:
                    stop_event.wait(_TRANSLATION_LOOP_POLL_SEC)
                    continue

                has_item, item = poll_queue(
                    sentence_queue,
                    stop_event,
                    pause_event,
                    timeout=_TRANSLATION_LOOP_POLL_SEC,
                )
                if has_item:
                    submitted_at = time.monotonic()
                    pending[next_seq] = executor.submit(translate_item, next_seq, item, submitted_at)
                    next_seq += 1
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
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
