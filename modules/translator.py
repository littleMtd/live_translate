import hashlib
import queue
import re
import threading
import time
from contextlib import nullcontext
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
    effective_system_prompt_for_engine,
    engine_chain_config_key,
    get_last_engine_api_diagnostics,
    get_last_engine_diagnostics,
    get_last_token_usage,
    get_last_token_usage_engine,
    reset_last_engine_diagnostics,
    reset_last_token_usage,
)
from modules.translation_runtime import (
    FallbackState,
    active_engine,
    call_with_fallback,
    probe_primary_recovery,
)
from modules.translation_memory import MemoryLookup, TranslationMemory
from modules.translation_policy import TranslationPolicy
from modules.translation_corrections import (
    NameRenderingRule as _NameRenderingRule,
    load_translation_corrections,
)

log = get_logger("translator")

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_MIN_TRANSLATE_CHARS = 2    # skip STT fragments shorter than this
_CACHE_MAX_SIZE = 500       # max entries in per-session translation cache
_FALLBACK_PROBE_EVERY = 3    # legacy call-count probe interval; user-path probes are disabled
_FALLBACK_PROBE_INTERVAL_SEC = 30.0
_FALLBACK_PROBE_TEXT = "안녕하세요"
_FALLBACK_THRESHOLD = 3      # consecutive primary failures before hard-switching to fallback
_TRANSLATION_WORKERS = 2
_MAX_PENDING_TRANSLATIONS = 4
_TRANSLATION_LOOP_POLL_SEC = 0.05
_API_EVENT_DEFAULTS = {
    "api_attempt_count": 0,
    "api_timeout_count": 0,
    "api_total_wall_ms": None,
    "api_final_attempt_ms": None,
    "api_first_attempt_ms": None,
    "api_retry_attempt_ms": None,
    "retry_sleep_ms": 0.0,
    "timeout_config_ms": None,
    "api_attempt_timeout_ms": None,
    "api_attempt_index": 0,
    "api_inflight_count_at_start": None,
    "source_text_char_count": None,
    "prompt_char_count": None,
    "request_body_char_count": None,
    "message_count": None,
    "context_item_count": None,
    "api_error_type": None,
    "api_error_message_class": None,
}
_CACHE_HIT_STATUSES = {"memory_hit", "db_hit"}

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

_SHARED_NAME_SCOPE = "__shared__"
_STELLIVE_HINA_PROFILE_ID = "stellive_hina"
_HADES_PROFILE_ID = "hades_chxxnnx"
_MWMEU_PROFILE_ID = "mwmeu"

_CORRECTION_TABLES = load_translation_corrections()
_SOURCE_NORM_SHARED = _CORRECTION_TABLES.source_norm_shared
_SOURCE_NORM_BY_PROFILE = _CORRECTION_TABLES.source_norm_by_profile
_SOURCE_AWARE_TARGET_REPLACEMENTS = tuple(
    (group.source_terms, group.replacements, group.match_all)
    for group in _CORRECTION_TABLES.source_aware_target_replacements
)
_PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS = {
    profile: tuple(
        (group.source_terms, group.replacements, group.match_all) for group in groups
    )
    for profile, groups in _CORRECTION_TABLES.profile_source_aware_target_replacements.items()
}
_KOREAN_NAME_SUFFIXES = _CORRECTION_TABLES.korean_name_suffixes
_NAME_RENDERING_RULES = _CORRECTION_TABLES.name_rendering_rules


def _sorted_norm_items(norm: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(norm.items(), key=lambda item: len(item[0]), reverse=True))


# L4: pre-sorted normalization tables (shared, and shared+profile merged per
# profile) so the hot path doesn't rebuild + sort a dict per translation.
_SOURCE_NORM_SHARED_SORTED = _sorted_norm_items(_SOURCE_NORM_SHARED)
_SOURCE_NORM_WITH_PROFILE_SORTED = {
    profile: _sorted_norm_items({**_SOURCE_NORM_SHARED, **profile_norm})
    for profile, profile_norm in _SOURCE_NORM_BY_PROFILE.items()
}


def _db_cache_enabled() -> bool:
    """SQLite cache layer is for clip mode (replays repeat); live mode showed a
    ~0.45% hit rate so it is disabled by default (cfg.database.live_db_cache)."""
    if cfg.translation.translation_mode == "clip":
        return True
    return bool(getattr(cfg.database, "live_db_cache", False))


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


# Per-worker record of source-normalization / target-correction rules that
# actually fired on the current translation, so the runtime event can show
# whether "海洞 -> 해둥이"-style rescues are routine or rarely needed anymore.
_LAST_CORRECTIONS = threading.local()


def reset_corrections() -> None:
    _LAST_CORRECTIONS.value = []


def _record_correction(stage: str, rule: str, before: str, after: str) -> None:
    bucket = getattr(_LAST_CORRECTIONS, "value", None)
    if not isinstance(bucket, list):
        bucket = []
        _LAST_CORRECTIONS.value = bucket
    bucket.append({"stage": stage, "rule": rule, "before": before, "after": after})


def get_corrections() -> list[dict]:
    value = getattr(_LAST_CORRECTIONS, "value", None)
    return list(value) if isinstance(value, list) else []


def _replace_recording(text: str, wrong: str, right: str, *, stage: str, rule_id: str) -> str:
    """Apply text.replace(wrong, right), recording the rule iff it changed text."""
    if wrong and wrong in text:
        new = text.replace(wrong, right)
        if new != text:
            _record_correction(stage, rule_id, wrong, right)
            return new
    return text


def _replace_wrong_name_forms(result: str, rule: _NameRenderingRule) -> str:
    if not rule.wrong_forms:
        return result

    alternatives = sorted({rule.canonical, *rule.wrong_forms}, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(alternative) for alternative in alternatives))
    corrected = pattern.sub(rule.canonical, result)
    if corrected != result:
        present = "|".join(form for form in rule.wrong_forms if form in result)
        _record_correction("name_render", f"name:{rule.canonical}", present, rule.canonical)
    return corrected


def _normalize_source_before_matching(text: str) -> str:
    """Replace known unambiguous STT noise forms with their canonical source alias.

    Runs before slang lookup, cache lookup, LLM call, and source-aware corrections.
    Operates on prepared text only; raw_text stored in TranslationOutcome is untouched.
    Profile-gated: normalization only applies when the matching profile is active.
    """
    profile_id = cfg.active_streamer_profile
    if profile_id and bool(cfg.translation.use_profile):
        items = _SOURCE_NORM_WITH_PROFILE_SORTED.get(profile_id, _SOURCE_NORM_SHARED_SORTED)
    else:
        items = _SOURCE_NORM_SHARED_SORTED
    for noisy, canonical in items:
        text = _replace_recording(
            text, noisy, canonical, stage="source_norm", rule_id=f"{noisy}->{canonical}"
        )
    return text


def _source_terms_match(source: str, source_terms: tuple[str, ...], match_all: bool) -> bool:
    if match_all:
        return all(term in source for term in source_terms)
    return any(term in source for term in source_terms)


def _apply_source_aware_corrections(source: str, result: str) -> str:
    corrected = result
    for source_terms, replacements, match_all in _SOURCE_AWARE_TARGET_REPLACEMENTS:
        if not _source_terms_match(source, source_terms, match_all):
            continue
        for wrong, right in replacements:
            corrected = _replace_recording(
                corrected, wrong, right, stage="target_correction", rule_id=f"{wrong}->{right}"
            )

    if cfg.translation.use_profile:
        profile_replacements = _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS.get(cfg.active_streamer_profile, ())
        for source_terms, replacements, match_all in profile_replacements:
            if not _source_terms_match(source, source_terms, match_all):
                continue
            for wrong, right in replacements:
                corrected = _replace_recording(
                    corrected, wrong, right,
                    stage="target_correction", rule_id=f"profile:{wrong}->{right}",
                )

    for rule in _NAME_RENDERING_RULES:
        if not _name_rendering_rule_enabled(rule):
            continue
        if not _source_has_name_alias(source, rule.source_aliases):
            continue
        corrected = _replace_wrong_name_forms(corrected, rule)

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


_HISTORY_WRITE_LOCK = threading.Lock()


def _write_history(ko: str, zh: str) -> None:
    path = _LOG_DIR / f"translations_{datetime.now().strftime('%Y%m%d')}.txt"
    ts = datetime.now().strftime("%H:%M:%S")
    # Serialize appends: history is written outside the shared state lock (M1),
    # so two workers may reach this concurrently.
    with _HISTORY_WRITE_LOCK:
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


@dataclass
class _TranslatorSharedState:
    memory: TranslationMemory
    policy: TranslationPolicy
    fallback: FallbackState
    lock: object


def _new_translation_memory() -> TranslationMemory:
    try:
        recent_window = int(getattr(cfg.translation, "context_window", 0) or 0)
    except (TypeError, ValueError):
        recent_window = 0
    recent_window = max(recent_window, 0)
    return TranslationMemory(
        recent_window=recent_window,
        max_cache_size=_CACHE_MAX_SIZE,
        db_factory=_get_db,
        history_writer=_write_history,
    )


def _new_translation_policy() -> TranslationPolicy:
    return TranslationPolicy(
        slang=cfg.translation.slang,
        min_translate_chars=_MIN_TRANSLATE_CHARS,
        max_translate_chars=cfg.translation.max_translate_chars,
    )


def _new_translator_shared_state() -> _TranslatorSharedState:
    return _TranslatorSharedState(
        memory=_new_translation_memory(),
        policy=_new_translation_policy(),
        fallback=FallbackState(),
        lock=threading.RLock(),
    )


def _merge_fallback_state(shared: FallbackState, before: FallbackState, after: FallbackState) -> None:
    if (
        shared.active_idx == before.active_idx
        and shared.probe_counter == before.probe_counter
        and shared.consecutive_primary_failures == before.consecutive_primary_failures
    ):
        shared.active_idx = after.active_idx
        shared.probe_counter = after.probe_counter
        shared.consecutive_primary_failures = after.consecutive_primary_failures
        return

    if after.active_idx == 0 and before.active_idx > 0 and shared.active_idx == before.active_idx:
        shared.active_idx = 0
        shared.probe_counter = 0
        shared.consecutive_primary_failures = 0
        return

    if after.active_idx > before.active_idx and after.active_idx >= shared.active_idx:
        shared.active_idx = after.active_idx
        shared.probe_counter = after.probe_counter
        shared.consecutive_primary_failures = after.consecutive_primary_failures
        return

    if shared.active_idx == after.active_idx:
        shared.probe_counter = max(shared.probe_counter, after.probe_counter)
        shared.consecutive_primary_failures = max(
            shared.consecutive_primary_failures,
            after.consecutive_primary_failures,
        )


def _outcome_used_api(outcome: TranslationOutcome) -> bool:
    if outcome.result_source == "api":
        return True
    if outcome.status == "failed" and outcome.result_source == "none":
        return True
    if outcome.result_source == "post_policy" and outcome.cache_status not in _CACHE_HIT_STATUSES:
        return True
    return False


def _api_event_fields(
    outcome: TranslationOutcome,
    diagnostics: dict[str, int | float | str | None],
) -> dict:
    fields = dict(_API_EVENT_DEFAULTS)
    engine = str(diagnostics.get("engine") or "")
    if not engine or engine != outcome.engine or not _outcome_used_api(outcome):
        return fields
    if int(diagnostics.get("api_attempt_count") or 0) <= 0:
        return fields
    for key in fields:
        fields[key] = diagnostics.get(key, fields[key])
    return fields


def _retry_diagnostics_apply(outcome: TranslationOutcome, diagnostics: dict[str, int | str]) -> bool:
    engine = str(diagnostics.get("engine") or "")
    return bool(engine and engine == outcome.engine and _outcome_used_api(outcome))


def _token_usage_for_outcome(outcome: TranslationOutcome) -> dict[str, int | None]:
    if not _outcome_used_api(outcome):
        return {}
    usage_engine = get_last_token_usage_engine()
    outcome_engine = str(outcome.engine or "").strip().lower()
    if usage_engine and usage_engine != outcome_engine:
        return {}
    return get_last_token_usage()


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
    api_event_fields: dict


class Translator:
    def __init__(self, shared_state: _TranslatorSharedState | None = None):
        self._shared_state = shared_state or _new_translator_shared_state()
        self._state_lock = self._shared_state.lock
        self._fallback_state_obj = self._shared_state.fallback
        self._engines: list[TranslationEngine] = _build_engine_chain()
        self._engines_key = engine_chain_config_key()
        self._memory = self._shared_state.memory
        self._policy = self._shared_state.policy
        self._last_input: str = ""

    def _state_guard(self):
        return getattr(self, "_state_lock", None) or nullcontext()

    def _fallback_state(self) -> FallbackState:
        state = getattr(self, "_fallback_state_obj", None)
        if state is None:
            state = FallbackState()
            self._fallback_state_obj = state
        return state

    def _refresh_engines_if_needed(self) -> None:
        current_key = engine_chain_config_key()
        previous_key = getattr(self, "_engines_key", current_key)
        if previous_key == current_key:
            self._engines_key = current_key
            return
        with self._state_guard():
            if getattr(self, "_engines_key", previous_key) == current_key:
                return
            self._engines = _build_engine_chain()
            self._engines_key = current_key
            state = self._fallback_state()
            state.active_idx = 0
            state.probe_counter = 0
            state.consecutive_primary_failures = 0

    @property
    def _active_idx(self) -> int:
        return self._fallback_state().active_idx

    @_active_idx.setter
    def _active_idx(self, value: int) -> None:
        self._fallback_state().active_idx = value

    @property
    def _probe_counter(self) -> int:
        return self._fallback_state().probe_counter

    @_probe_counter.setter
    def _probe_counter(self, value: int) -> None:
        self._fallback_state().probe_counter = value

    @property
    def _consecutive_primary_failures(self) -> int:
        return self._fallback_state().consecutive_primary_failures

    @_consecutive_primary_failures.setter
    def _consecutive_primary_failures(self, value: int) -> None:
        self._fallback_state().consecutive_primary_failures = value

    def translate(self, text: str, incomplete: bool = False) -> str | None:
        return self.translate_event(text, incomplete).target_text

    def translate_event(self, text: str, incomplete: bool = False) -> TranslationOutcome:
        raw_text = (text or "").strip()
        policy = self._policy_state()
        # L1: rejection_reason, prepare_input and the sanitize-rejection read
        # must share one lock section — the policy instance is shared across
        # workers, so split reads raced and produced wrong filter_reason values.
        with self._state_guard():
            filter_reason = policy.rejection_reason(raw_text)
            text = policy.prepare_input(raw_text, initial_rejection_reason=filter_reason)
            self._last_input = policy.last_input
            sanitize_rejection = policy._last_sanitize_rejection
        if text is None:
            return TranslationOutcome(
                source_text=raw_text,
                target_text=None,
                status="filtered",
                result_source="policy",
                cache_status="skipped",
                incomplete=incomplete,
                filter_reason=filter_reason or sanitize_rejection or "unknown",
            )

        text = _normalize_source_before_matching(text)

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
        self._refresh_engines_if_needed()
        system_prompt = self._build_system_prompt()
        engine = self._active_engine()
        prompt_ver = self._prompt_version_for_engine(engine, system_prompt)
        self._log_prompt_mode_once()

        lookup = self._lookup_existing_translation_event(text, incomplete, prompt_ver, engine)
        if lookup.result:
            target_text = _apply_source_aware_corrections(text, lookup.result)
            if _looks_like_meta_garbage_output(target_text):
                self._invalidate_cached_translation(text, incomplete, prompt_ver, engine, lookup.result)
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

        with self._state_guard():
            history = self._memory_state().context()
        result, used_engine = self._call_with_fallback(text, system_prompt, incomplete, history)
        # Attribute the outcome to the engine that actually produced it: on a
        # soft fallback the active engine stays primary, so reading
        # _active_engine() here mislabeled the result source, API diagnostics
        # and the DB cache row.
        engine = used_engine or self._active_engine()
        prompt_ver = self._prompt_version_for_engine(engine, system_prompt)
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
            self._record_success(text, result, incomplete, prompt_ver, engine)
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
            # API failure — allow next identical input to retry rather than
            # staying suppressed. Under the state lock, and only if last_input
            # is still ours: with two workers, a slow failing request must not
            # clear the duplicate-suppression state another worker just wrote.
            with self._state_guard():
                policy = self._policy_state()
                if policy.last_input == self._last_input:
                    policy.reset_last_input()
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
        with self._state_guard():
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
        with self._state_guard():
            self._memory_state().record_direct_memory(text, slang_result, incomplete)
        # File I/O outside the shared lock (M1).
        self._memory_state().write_history(text, slang_result)
        return slang_result

    def _lookup_existing_translation_event(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        engine: TranslationEngine | None = None,
    ) -> MemoryLookup:
        # Memory cache under the lock; the SQLite read happens outside it (M1)
        # so a slow DB never blocks the other worker.
        if engine is None:
            engine = self._active_engine()
        with self._state_guard():
            lookup = self._memory_state().lookup_memory_event(
                text, incomplete, prompt_ver, engine
            )
        if lookup.result:
            log.debug("Cache hit: %s", text[:20])
            return lookup
        if incomplete or engine is None or not _db_cache_enabled():
            metrics.increment("translation.cache.skipped")
            return MemoryLookup(None, "skipped")
        db_result = self._memory_state().db_lookup(text, engine, prompt_ver)
        if db_result:
            with self._state_guard():
                lookup = self._memory_state().record_db_hit(
                    text, incomplete, prompt_ver, db_result, engine
                )
            log.debug("Cache hit: %s", text[:20])
            return lookup
        metrics.increment("translation.cache.miss")
        return MemoryLookup(None, "miss")

    def _record_success(self, text: str, result: str, incomplete: bool,
                        prompt_ver: str,
        engine: TranslationEngine | None = None) -> None:
        with self._state_guard():
            if engine is None:
                engine = self._active_engine()
            self._memory_state().record_success_memory(
                text, result, incomplete, prompt_ver, engine
            )
        # File/DB I/O outside the shared lock (M1).
        self._memory_state().write_history(text, result)
        if not incomplete and engine is not None and _db_cache_enabled():
            self._memory_state().db_store(text, result, engine, prompt_ver)

    def _invalidate_cached_translation(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
        result: str | None,
    ) -> None:
        with self._state_guard():
            self._memory_state().invalidate_memory(
                text, incomplete, prompt_ver, active_engine, result
            )
        # DB delete outside the shared lock (M1). The delete is NOT gated on
        # _db_cache_enabled(): stale rows must be purged even if the cache layer
        # was enabled earlier in the session and disabled since.
        if not incomplete and active_engine is not None:
            self._memory_state().invalidate_db(text, active_engine, prompt_ver)

    def _active_engine(self) -> TranslationEngine | None:
        return active_engine(self._engines, self._active_idx)

    def _log_prompt_mode_once(self) -> None:
        if _is_qwen_model() and not hasattr(self, '_qwen_log_once'):
            log.info("Using Qwen-optimized system prompt (shorter, more direct)")
            self._qwen_log_once = True

    def _build_system_prompt(self) -> str:
        return _compose_system_prompt()

    @staticmethod
    def _prompt_version(system_prompt: str) -> str:
        return hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    def _prompt_version_for_engine(
        self,
        engine: TranslationEngine | None,
        system_prompt: str,
    ) -> str:
        return self._prompt_version(effective_system_prompt_for_engine(engine, system_prompt))

    def _call_with_fallback(
        self, text: str, system_prompt: str, incomplete: bool,
        history: list[tuple[str, str]] | None = None,
    ) -> tuple[str | None, TranslationEngine | None]:
        """Returns (result, engine_used). engine_used is the engine that
        actually produced the result — on a soft fallback this differs from
        the active engine, which intentionally stays on primary."""
        fallback_state = self._fallback_state()
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            state = fallback_state
        else:
            with lock:
                before_state = FallbackState(
                    fallback_state.active_idx,
                    fallback_state.probe_counter,
                    fallback_state.consecutive_primary_failures,
                )
                state = FallbackState(
                    before_state.active_idx,
                    before_state.probe_counter,
                    before_state.consecutive_primary_failures,
                )
        result, used_idx = call_with_fallback(
            self._engines,
            state,
            text,
            system_prompt,
            incomplete,
            history,
            _FALLBACK_PROBE_EVERY,
            _FALLBACK_THRESHOLD,
            _looks_untranslated,
            log,
        )
        if lock is not None:
            with lock:
                _merge_fallback_state(fallback_state, before_state, state)
        used_engine = active_engine(self._engines, used_idx)
        return result, used_engine

    def _get_prompt_version_hash(self) -> str:
        return self._prompt_version_for_engine(self._active_engine(), self._build_system_prompt())


_DEDUP_SUBTITLE_SEC = 5.0   # suppress identical subtitle within this window
_MAX_COMPLETED_BACKLOG = 64  # completed-but-unemitted results before warning (L8)
_STOP_DRAIN_TIMEOUT_MARGIN_SEC = 0.5


def _translation_max_output_delay_ms() -> float:
    value = getattr(cfg.translation, "max_subtitle_output_delay_ms", 30000)
    return float(value) if isinstance(value, (int, float)) else 30000.0


def _stop_drain_timeout_sec() -> float:
    try:
        join_timeout = float(getattr(cfg, "thread_join_timeout", 5.0) or 5.0)
    except (TypeError, ValueError):
        join_timeout = 5.0
    return max(_TRANSLATION_LOOP_POLL_SEC, join_timeout - _STOP_DRAIN_TIMEOUT_MARGIN_SEC)


def _compose_system_prompt() -> str:
    """Shared between Translator._build_system_prompt and the probe thread (L5).

    Pure function of config since PromptEvolver was removed (2026-06-12):
    prompt_ver is now stable for a given base prompt + profile selection.
    """
    is_qwen = _is_qwen_model()
    system_prompt = _QWEN_PROMPT if is_qwen else _BASE_PROMPT

    if not cfg.translation.use_profile:
        return system_prompt

    streamer_profile = get_translation_profile(cfg.active_streamer_profile, qwen=is_qwen)
    if streamer_profile:
        system_prompt += "\n\n" + streamer_profile
        log.debug("Appended streamer profile: %s", cfg.active_streamer_profile)
    return system_prompt


def _build_probe_system_prompt(_shared_state: _TranslatorSharedState) -> str:
    return _compose_system_prompt()


def _start_fallback_probe_thread(
    shared_state: _TranslatorSharedState,
    stop_event: threading.Event,
    *,
    interval_seconds: float = _FALLBACK_PROBE_INTERVAL_SEC,
) -> threading.Thread:
    def run() -> None:
        engines: list[TranslationEngine] | None = None
        engines_key: tuple | None = None
        while not stop_event.wait(interval_seconds):
            with shared_state.lock:
                if shared_state.fallback.active_idx <= 0:
                    continue
                probe_state = FallbackState(
                    shared_state.fallback.active_idx,
                    shared_state.fallback.probe_counter,
                    shared_state.fallback.consecutive_primary_failures,
                )
            # L7: rebuild the probe chain when the engine selection changes
            # mid-run instead of caching the first build forever.
            chain_key = engine_chain_config_key()
            if engines is None or chain_key != engines_key:
                engines = _build_engine_chain()
                engines_key = chain_key
            system_prompt = _build_probe_system_prompt(shared_state)
            try:
                recovered = probe_primary_recovery(
                    engines,
                    probe_state,
                    _FALLBACK_PROBE_TEXT,
                    system_prompt,
                    _looks_untranslated,
                    log,
                )
            except Exception:
                log.exception("Fallback primary probe failed unexpectedly")
                continue
            if not recovered:
                continue
            with shared_state.lock:
                if shared_state.fallback.active_idx > 0:
                    shared_state.fallback.active_idx = 0
                    shared_state.fallback.probe_counter = 0
                    shared_state.fallback.consecutive_primary_failures = 0

    return start_daemon_thread("TranslationFallbackProbe", run)


def start(sentence_queue: queue.Queue, subtitle_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None) -> threading.Thread:
    def run():
        shared_state = _new_translator_shared_state()
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
        _start_fallback_probe_thread(shared_state, stop_event)

        def translate_item(seq: int, item, submitted_at: float) -> _CompletedTranslation:
            started = time.monotonic()
            worker_id = threading.current_thread().name
            # Reset before translating so cache hits / failures (which never reach an
            # engine) don't inherit the previous call's token usage / corrections.
            reset_last_engine_diagnostics()
            reset_last_token_usage()
            reset_corrections()
            # Everything that touches `item` runs inside the try: a malformed item
            # must yield a synthetic failed outcome, never a lost sequence number
            # (a gap would stall the in-order emit loop forever).
            text = ""
            incomplete = False
            translation_mode = str(
                getattr(cfg.translation, "translation_mode", "") or ""
            )
            metadata: dict = {"translation_mode": translation_mode}
            try:
                text = sentence_text(item)
                incomplete = sentence_incomplete(item)
                metadata = sentence_metadata(item).copy()
                marker = _dependency_marker(text)
                metadata.update(
                    {
                        "sequence_id": seq,
                        "starts_with_dependency_marker": bool(marker),
                        "dependency_marker": marker,
                        "profile_id": cfg.active_streamer_profile,
                        "profile_applied": bool(getattr(cfg.translation, "use_profile", False)),
                        # Diagnostic-only: distinguishes the 5s live timeout path
                        # from the 60s clip/offline path in latency artifacts.
                        "translation_mode": translation_mode,
                    }
                )
                worker_translator = getattr(worker_state, "translator", None)
                if worker_translator is None:
                    worker_translator = Translator(shared_state=shared_state)
                    worker_state.translator = worker_translator
                outcome = worker_translator.translate_event(text, incomplete)
            except Exception:
                log.exception("Translation worker failed for: %.40s", text)
                metadata.setdefault("sequence_id", seq)
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
            api_diagnostics = get_last_engine_api_diagnostics()
            for usage_key, usage_value in _token_usage_for_outcome(outcome).items():
                if usage_value is not None:
                    metadata[f"token_{usage_key}"] = usage_value
            corrections = get_corrections()
            if corrections:
                metadata["corrections"] = corrections
                metadata["correction_count"] = len(corrections)
            retry_count = 0
            retry_reason = ""
            if _retry_diagnostics_apply(outcome, diagnostics):
                retry_count = int(diagnostics.get("retry_count") or 0)
                retry_reason = str(diagnostics.get("retry_reason") or "")
            api_event_fields = _api_event_fields(outcome, api_diagnostics)
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
                api_event_fields,
            )

        def _failed_completion(seq: int) -> _CompletedTranslation:
            """Synthetic failed result so a crashed future never leaves a gap in
            the sequence — a gap would stall the in-order emit loop forever."""
            now = time.monotonic()
            return _CompletedTranslation(
                seq,
                TranslationOutcome(
                    source_text="",
                    target_text=None,
                    status="failed",
                    result_source="none",
                    cache_status="skipped",
                    incomplete=False,
                ),
                0.0,
                {
                    "sequence_id": seq,
                    "translation_mode": str(
                        getattr(cfg.translation, "translation_mode", "") or ""
                    ),
                },
                now,
                now,
                now,
                "",
                0,
                "",
                dict(_API_EVENT_DEFAULTS),
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
                    metrics.increment("translation.future_failed")
                    completed[seq] = _failed_completion(seq)
            if len(completed) > _MAX_COMPLETED_BACKLOG:
                # L8: _MAX_PENDING_TRANSLATIONS only bounds in-flight futures;
                # an emit-loop stall would let this dict grow unboundedly.
                metrics.increment("translation.completed_backlog_high")
                log.warning(
                    "Completed-translation backlog at %d (next_emit_seq=%d) — emit loop may be stalled",
                    len(completed),
                    next_emit_seq,
                )

        def emit_completed(item: _CompletedTranslation) -> None:
            nonlocal last_result, last_result_time
            outcome = item.outcome
            elapsed = item.elapsed
            emitted_at = time.monotonic()
            output_delay_ms = round(max(0.0, emitted_at - item.submitted_at) * 1000, 2)
            event_metadata = item.metadata.copy()
            event_metadata.update(
                {
                    "engine_latency_ms": round(elapsed * 1000, 2),
                    "queue_wait_ms": round(max(0.0, item.started_at - item.submitted_at) * 1000, 2),
                    "output_delay_ms": output_delay_ms,
                    "predecessor_stall_ms": round(max(0.0, emitted_at - item.completed_at) * 1000, 2),
                    "translation_worker_id": item.worker_id,
                    "retry_count": item.retry_count,
                    "retry_reason": item.retry_reason,
                    **item.api_event_fields,
                }
            )
            metrics.observe_latency("translation", elapsed)
            event_fields = outcome.as_event_fields(elapsed * 1000, event_metadata)
            result = outcome.target_text
            if result:
                metrics.increment("translation.success")
                # Surface low-quality output in the 60 s metrics summary so a
                # degrading stretch is visible without scraping the JSONL.
                severity = event_fields.get("quality_severity")
                if severity in ("bad", "warn"):
                    metrics.increment(f"translation.quality.{severity}")
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
                max_output_delay_ms = _translation_max_output_delay_ms()
                if max_output_delay_ms > 0 and output_delay_ms > max_output_delay_ms:
                    metrics.increment("translation.subtitle.stale_skipped")
                    log.warning(
                        "Skipping stale subtitle after %.0fms output delay: %s",
                        output_delay_ms,
                        result[:30],
                    )
                    runtime_events.emit(
                        "translation",
                        **event_fields,
                        subtitle_emitted=False,
                        subtitle_suppressed_reason="stale_output_delay",
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
            # Stop accepting work but let already-submitted translations finish:
            # cancel_futures=True dropped completed-but-unemitted and in-flight
            # subtitles at stop. Drain in order with a bounded wait so shutdown
            # can't hang on a stuck engine call.
            executor.shutdown(wait=False, cancel_futures=False)
            deadline = time.monotonic() + _stop_drain_timeout_sec()
            while True:
                collect_finished()
                while next_emit_seq in completed:
                    emit_completed(completed.pop(next_emit_seq))
                    next_emit_seq += 1
                if not pending and not completed:
                    break
                if time.monotonic() >= deadline:
                    log.warning(
                        "Stop drain timed out with %d pending / %d completed translations",
                        len(pending),
                        len(completed),
                    )
                    for future in pending.values():
                        future.cancel()
                    break
                time.sleep(_TRANSLATION_LOOP_POLL_SEC)
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
