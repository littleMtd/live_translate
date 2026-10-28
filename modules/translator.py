import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path


from config import cfg
from modules.activity_context import (
    activity_prompt_capsule,
    activity_snapshot_metadata,
    bind_activity_snapshot,
    bound_activity_snapshot,
    capture_effective_activity_snapshot,
    effective_activity_value,
    effective_profile_id,
    normalize_activity,
)
from modules.conversation_history import ConversationHistory
from modules.profile_context import (
    bound_profile_snapshot,
    effective_profile_applied,
    profile_state,
)
from modules.session_context import HistoryCohort, LiveSessionSnapshot
from utils.logger import get_logger
from utils.metrics import metrics
from utils.pipeline import poll_queue, start_daemon_thread
from utils.runtime_events import (
    runtime_events,
    translation_quality,
)
from modules.provisional_subtitles import (
    ProvisionalCandidate,
    deepseek_provisional_eligible,
    provisional_fingerprint,
)
from modules.db import _get_db
from modules.translation_prompts import (
    _BASE_PROMPT,
    _BASE_PROMPT_TAIL,
    get_translation_profile,
)
from modules.translation_engines import (
    TranslationEngine,
    DeepSeekTranslationEngine,
    GroqTranslationEngine,
    _build_engine_chain,
    build_effective_deepseek_messages,
    build_effective_groq_messages,
    effective_system_prompt_for_engine,
    _request_entity_capsule,
    engine_chain_config_key,
    get_last_engine_api_diagnostics,
    get_last_engine_diagnostics,
    get_last_token_usage,
    get_selected_token_usage,
    get_selected_translation_attempt,
    get_translation_attempts,
    record_translation_attempt,
    translation_route_id,
)
from modules.forensics_contract import (
    ADJUDICATION_POLICY_VERSION,
    REQUEST_CONTRACT_SCHEMA_VERSION,
    artifact_hashes,
    history_manifest,
    message_manifest,
    sha256_text,
    stable_identity,
)
from modules.translation_runtime import (
    FallbackResult,
    FallbackState,
    active_engine,
    call_with_fallback,
    probe_primary_recovery,
)
from modules.translation_memory import MemoryLookup, TranslationMemory
from modules.translation_request import TranslationRequest, freeze_route_request
from modules.translation_policy import RepetitionEvidence, TranslationPolicy
from modules.request_protection import (
    PROTECTION_POLICY_VERSION,
    RequestProtection,
)
from modules.translation_corrections import (
    CanonicalObligation,
    CanonicalObligationEvaluation,
    evaluate_canonical_obligations,
)

# Split out of this file (2026-10-27); names stay importable from here.
from modules.translation_normalization import (
    _BOUNDARY_SOURCE_NORM_BY_PROFILE,
    _CONDITIONAL_SOURCE_NORM_BY_PROFILE,
    _CONDITIONAL_SOURCE_NORM_SHARED,
    _CORRECTION_CAPTURE,
    _NAME_RENDERING_RULES,
    _PROFILE_SOURCE_AWARE_TARGET_REPLACEMENTS,
    _SOURCE_AWARE_TARGET_REPLACEMENTS,
    _SOURCE_NORM_BY_PROFILE,
    _apply_source_aware_corrections,
    _canonical_obligations_for_request,
    _normalize_deepseek_taiwan_script,
    _normalize_source_before_matching,
    _publication_approved_terms,
    _record_correction,
    _request_protection_for,
    _resolve_active_canonical_obligations,
    _resolve_entity_request_context,
    _source_activated_name_canonicals,
    get_corrections,
    reset_corrections,
)
from modules.translation_guard import (
    GUARD_RESCUE_POLICY,
    _GUARD_RESCUE_HANGUL_RUN,
    _adjudicate_translation_candidate,
    _choose_guard_rescue,
    _final_script_rejection_reason,
    _guard_rescue_eligible,
    _guard_rescue_text_ok,
    _is_legitimate_preserve_as_is,
    _looks_like_meta_garbage_output,
    _looks_like_model_refusal,
    _looks_untranslated,
    _profile_translation_qa,
    _quality_telemetry_approved_terms,
    _source_honorific_name_terms,
    _translation_output_guard,
)

log = get_logger("translator")

_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

_MIN_TRANSLATE_CHARS = 2    # skip STT fragments shorter than this
_CACHE_MAX_SIZE = 500       # max entries in per-session translation cache
_FALLBACK_PROBE_INTERVAL_SEC = 30.0
_FALLBACK_PROBE_TEXT = "안녕하세요"
_FALLBACK_THRESHOLD = 3      # consecutive primary failures before hard-switching to fallback
_LIVE_FALLBACK_THRESHOLD = 1
_TRANSLATION_WORKERS = 2
_MAX_PENDING_TRANSLATIONS = 4
_TRANSLATION_LOOP_POLL_SEC = 0.05
# A preview waits at most this long for earlier sentences' finals before it is dropped
# (its own final follows); finals take ~1 s, a stuck one would otherwise hold previews.
_PREVIEW_ORDER_WAIT_SEC = 5.0
_PREVIEW_ORDER_POLL_SEC = 0.05
_API_EVENT_DEFAULTS = {
    "request_contract_id": "",
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
    "api_cost_usd": None,
    "api_cost_basis": "",
    "api_pricing_revision": "",
    "provider_options": {},
    "system_fingerprint": "",
    "deadline_exceeded": False,
    "deadline_scope": "",
    "deadline_budget_ms": None,
}
_CACHE_HIT_STATUSES = {"memory_hit", "db_hit"}
FallbackEventSink = Callable[..., None]


_STELLIVE_HINA_PROFILE_ID = "stellive_hina"
_HADES_PROFILE_ID = "hades_chxxnnx"
_MWMEU_PROFILE_ID = "mwmeu"

_CANONICAL_PUBLICATION_POLICY_VERSION = "canonical-obligations-v1"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FORENSIC_ARTIFACT_PATHS = (
    "data/entity_registry.json",
    "data/streamer_profiles.json",
    "data/translation_profiles.json",
    "data/translation_corrections.json",
    "data/unknown_name_escrow.json",
)


@lru_cache(maxsize=8)
def _artifact_hash_manifest(
    signature: tuple[tuple[str, int, int], ...],
) -> dict[str, str]:
    del signature
    return artifact_hashes(_PROJECT_ROOT, _FORENSIC_ARTIFACT_PATHS)


def _current_artifact_hashes() -> dict[str, str]:
    signature: list[tuple[str, int, int]] = []
    for relative in _FORENSIC_ARTIFACT_PATHS:
        path = _PROJECT_ROOT / relative
        try:
            stat = path.stat()
            signature.append((relative, stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((relative, 0, 0))
    return _artifact_hash_manifest(tuple(signature))


def _fallback_failure_threshold() -> int:
    # Live subtitles should stop paying primary timeout cost after the first
    # miss; clip/offline mode keeps the more conservative retry-before-switch.
    if cfg.translation.translation_mode == "live":
        return _LIVE_FALLBACK_THRESHOLD
    return _FALLBACK_THRESHOLD


def _translation_circuit_breaker_enabled() -> bool:
    if cfg.translation.translation_mode != "live":
        return False
    return bool(cfg.translation.circuit_breaker_enabled)


def _translation_deadline_at() -> float | None:
    if cfg.translation.translation_mode != "live":
        return None
    return time.monotonic() + float(cfg.translation.live_total_deadline_sec)


def _emit_fallback_runtime_event(action: str, **fields) -> None:
    runtime_events.emit(
        "translation_fallback",
        action=action,
        translation_mode=cfg.translation.translation_mode,
        profile_id=effective_profile_id(cfg.active_streamer_profile),
        circuit_breaker_enabled=_translation_circuit_breaker_enabled(),
        **fields,
    )


def _db_cache_enabled() -> bool:
    """SQLite cache layer is for clip mode (replays repeat); live mode showed a
    ~0.45% hit rate so it is disabled by default (cfg.database.live_db_cache)."""
    if cfg.translation.translation_mode == "clip":
        return True
    return bool(cfg.database.live_db_cache)


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
    prepared_source_text: str = ""
    request_contract_id: str = ""
    engine: str = ""
    model: str = ""
    prompt_version: str = ""
    filter_reason: str = ""
    canonical_obligation_evaluation: CanonicalObligationEvaluation | None = None
    unknown_name_approved_terms: tuple[str, ...] = ()
    corrections: tuple[dict, ...] = ()
    provider_options: tuple[tuple[str, str], ...] = ()
    system_fingerprint: str = ""
    guard_rescue: dict | None = None
    deferred_success: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @property
    def route_id(self) -> str:
        provider = str(self.engine or "").strip().lower()
        model = str(self.model or "").strip()
        if not provider:
            return ""
        return f"{provider}:{model}" if model else provider

    def as_event_fields(self, latency_ms: float, metadata: dict) -> dict:
        profile_id = str(metadata.get("profile_id") or "")
        profile_applied = bool(metadata.get("profile_applied"))
        obligation_evaluation = self.canonical_obligation_evaluation
        if obligation_evaluation is None:
            entity_context = _resolve_entity_request_context(self.source_text)
            obligations = _canonical_obligations_for_request(entity_context)
            obligation_evaluation = evaluate_canonical_obligations(
                self.target_text, obligations
            )
        approved_terms = _quality_telemetry_approved_terms(
            profile_id if profile_applied else "",
            self.source_text,
            (
                obligation_evaluation.obligations
            ),
        )
        approved_terms = frozenset(
            set(approved_terms) | set(self.unknown_name_approved_terms)
        )
        quality = translation_quality(
            self.source_text,
            self.target_text,
            approved_terms=approved_terms,
        )
        if self.guard_rescue:
            quality = {**quality, "quality_flags": [
                *(quality.get("quality_flags") or []), "published_with_source_hangul"]}
        profile_qa = _profile_translation_qa(
            source_text=self.source_text,
            target_text=self.target_text,
            status=self.status,
            incomplete=self.incomplete,
            profile_id=profile_id,
            profile_applied=profile_applied,
            metadata=metadata,
            quality=quality,
            obligation_evaluation=obligation_evaluation,
        )
        obligation_fields = {
            "canonical_publication_policy_version": (
                _CANONICAL_PUBLICATION_POLICY_VERSION
            ),
            "canonical_obligation_evaluation": obligation_evaluation.as_dict(),
        }
        cache_key_payload = {
            "prepared_source_text": self.prepared_source_text,
            "incomplete": self.incomplete,
            "prompt_version": self.prompt_version,
            "request_contract_id": self.request_contract_id,
            "route_id": self.route_id,
            "history_cohort_id": str(metadata.get("history_cohort_id") or ""),
        }
        cache_contract = {
            **cache_key_payload,
            "cache_key_sha256": stable_identity(cache_key_payload),
            "lookup_result": self.cache_status,
            "result_source": self.result_source,
        }
        return {
            "source_text": self.source_text,
            "prepared_source_text": self.prepared_source_text,
            "target_text": self.target_text,
            "status": self.status,
            "result_source": self.result_source,
            "cache_status": self.cache_status,
            "incomplete": self.incomplete,
            "engine": self.engine,
            "model": self.model,
            "route_id": self.route_id,
            "prompt_version": self.prompt_version,
            "request_contract_id": self.request_contract_id,
            "cache_lookup_contract": cache_contract,
            "filter_reason": self.filter_reason,
            "target_unknown_name_escrow_terms": list(
                self.unknown_name_approved_terms
            ),
            "guard_rescue": dict(self.guard_rescue) if self.guard_rescue else None,
            "latency_ms": round(latency_ms, 2),
            **metadata,
            **quality,
            **profile_qa,
            **obligation_fields,
            "provider_options": {
                key: json.loads(value) for key, value in self.provider_options
            },
            "system_fingerprint": self.system_fingerprint,
        }


@dataclass
class _TranslatorSharedState:
    memory: TranslationMemory
    history: ConversationHistory
    policy: TranslationPolicy
    fallback: FallbackState
    lock: object
    fallback_event_sink: FallbackEventSink | None = None
    inflight_sources: dict[str, int] = field(default_factory=dict)
    session: LiveSessionSnapshot = field(default_factory=LiveSessionSnapshot.create)

    @property
    def history_session_id(self) -> str:
        """Compatibility name for request contracts created before session ownership."""
        return self.session.session_id


def _conversation_window() -> int:
    try:
        recent_window = int(cfg.translation.context_window or 0)
    except (TypeError, ValueError):
        recent_window = 0
    return max(recent_window, 0)


def _new_translation_memory(
    history: ConversationHistory | None = None,
) -> TranslationMemory:
    recent_window = _conversation_window()
    return TranslationMemory(
        recent_window=recent_window,
        max_cache_size=_CACHE_MAX_SIZE,
        conversation_history=history,
        db_factory=_get_db,
        history_writer=_write_history,
    )


def _new_translation_policy() -> TranslationPolicy:
    return TranslationPolicy(
        slang=cfg.translation.slang,
        min_translate_chars=_MIN_TRANSLATE_CHARS,
        max_translate_chars=cfg.translation.max_translate_chars,
        repetition_confidence_exempt_enabled=(
            cfg.translation.repetition_confidence_exempt_enabled
        ),
        repetition_avg_logprob_threshold=cfg.stt.context_avg_logprob_threshold,
        repetition_no_speech_threshold=cfg.stt.context_no_speech_threshold,
    )


def _new_translator_shared_state(
    *,
    fallback_event_sink: FallbackEventSink | None = None,
) -> _TranslatorSharedState:
    history = ConversationHistory(
        recent_window=_conversation_window(),
        max_cohorts=8,
    )
    return _TranslatorSharedState(
        memory=_new_translation_memory(history),
        history=history,
        policy=_new_translation_policy(),
        fallback=FallbackState(),
        lock=threading.RLock(),
        fallback_event_sink=fallback_event_sink,
    )


def _copy_fallback_state(state: FallbackState) -> FallbackState:
    return FallbackState(
        state.active_idx,
        state.consecutive_primary_failures,
        state.primary_cooldown_until,
        state.consecutive_probe_successes,
    )


def _replace_fallback_state(target: FallbackState, source: FallbackState) -> None:
    target.active_idx = source.active_idx
    target.consecutive_primary_failures = source.consecutive_primary_failures
    target.primary_cooldown_until = source.primary_cooldown_until
    target.consecutive_probe_successes = source.consecutive_probe_successes


def _send_fallback_event(
    shared_state: _TranslatorSharedState | None,
    action: str,
    **fields,
) -> None:
    if shared_state is None:
        return
    sink = shared_state.fallback_event_sink
    if sink is None:
        return
    try:
        sink(action, **fields)
    except Exception:
        metrics.increment("translation.fallback.runtime_event_error")
        log.exception("Failed to persist translation fallback event")


def _merge_fallback_state(shared: FallbackState, before: FallbackState, after: FallbackState) -> None:
    if (
        shared.active_idx == before.active_idx
        and shared.consecutive_primary_failures == before.consecutive_primary_failures
        and shared.primary_cooldown_until == before.primary_cooldown_until
        and shared.consecutive_probe_successes == before.consecutive_probe_successes
    ):
        shared.active_idx = after.active_idx
        shared.consecutive_primary_failures = after.consecutive_primary_failures
        shared.primary_cooldown_until = after.primary_cooldown_until
        shared.consecutive_probe_successes = after.consecutive_probe_successes
        return

    if (
        after.active_idx == 0
        and before.active_idx > 0
        and shared.active_idx == before.active_idx
        and shared.primary_cooldown_until == before.primary_cooldown_until
        and shared.consecutive_probe_successes == before.consecutive_probe_successes
    ):
        shared.active_idx = 0
        shared.consecutive_primary_failures = 0
        shared.primary_cooldown_until = 0.0
        shared.consecutive_probe_successes = 0
        return

    if (
        shared.active_idx == before.active_idx
        and after.active_idx > before.active_idx
        and after.active_idx >= shared.active_idx
    ):
        previous_cooldown_until = shared.primary_cooldown_until
        shared.active_idx = after.active_idx
        shared.consecutive_primary_failures = after.consecutive_primary_failures
        shared.primary_cooldown_until = max(previous_cooldown_until, after.primary_cooldown_until)
        if after.primary_cooldown_until >= previous_cooldown_until:
            shared.consecutive_probe_successes = after.consecutive_probe_successes
        return

    if shared.active_idx == after.active_idx:
        shared.consecutive_primary_failures = max(
            shared.consecutive_primary_failures,
            after.consecutive_primary_failures,
        )
        if after.primary_cooldown_until > shared.primary_cooldown_until:
            shared.primary_cooldown_until = after.primary_cooldown_until
            shared.consecutive_probe_successes = after.consecutive_probe_successes
        elif after.primary_cooldown_until == shared.primary_cooldown_until:
            shared.consecutive_probe_successes = max(
                shared.consecutive_probe_successes,
                after.consecutive_probe_successes,
            )


def _outcome_used_api(outcome: TranslationOutcome) -> bool:
    if outcome.result_source in ("api", "guard_rescue"):
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
    diagnostic_route = str(diagnostics.get("route_id") or "")
    if (
        not engine
        or engine != outcome.engine
        or (
            diagnostic_route
            and diagnostic_route != outcome.route_id
        )
        or not _outcome_used_api(outcome)
    ):
        return fields
    if int(diagnostics.get("api_attempt_count") or 0) <= 0:
        return fields
    for key in fields:
        fields[key] = diagnostics.get(key, fields[key])
    return fields


def _retry_diagnostics_apply(outcome: TranslationOutcome, diagnostics: dict[str, int | str]) -> bool:
    engine = str(diagnostics.get("engine") or "")
    diagnostic_route = str(diagnostics.get("route_id") or "")
    return bool(
        engine
        and engine == outcome.engine
        and (not diagnostic_route or diagnostic_route == outcome.route_id)
        and _outcome_used_api(outcome)
    )


def _token_usage_for_outcome(
    outcome: TranslationOutcome,
    selected_attempt: dict[str, object] | None = None,
) -> dict[str, int | None]:
    if not _outcome_used_api(outcome):
        return {}
    selected = (
        selected_attempt if selected_attempt is not None
        else get_selected_translation_attempt()
    )
    selected_route = str(selected.get("route_id") or "")
    if selected_route:
        if selected_route != outcome.route_id:
            return {}
    elif str(selected.get("engine") or "").strip().lower() != str(outcome.engine or "").strip().lower():
        return {}
    if selected_attempt is None:
        return get_selected_token_usage()
    return {
        key: selected[f"token_{key}"]
        for key in ("prompt", "output", "total", "cache_read", "cache_write")
        if f"token_{key}" in selected
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
    api_event_fields: dict
    policy_input: str = ""


class _DaemonWorkerPool:
    """Small fixed-size pool whose workers cannot keep the process alive.

    ``ThreadPoolExecutor`` uses non-daemon threads and registers them for an
    interpreter-exit join.  A provider call stuck below Python's timeout layer
    could therefore outlive the bounded translator drain and prevent process
    exit.  This pool keeps the same ``Future`` interface used by the coordinator
    while making the worker lifetime subordinate to the pipeline process.
    """

    def __init__(self, max_workers: int, *, thread_name_prefix: str):
        self._tasks: queue.Queue[tuple[Future, Callable, tuple, dict] | None] = queue.Queue()
        self._lock = threading.Lock()
        self._shutdown = False
        self._threads = [
            start_daemon_thread(f"{thread_name_prefix}_{index}", self._worker)
            for index in range(max_workers)
        ]

    def _worker(self) -> None:
        while True:
            task = self._tasks.get()
            if task is None:
                return
            future, fn, args, kwargs = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def submit(self, fn: Callable, /, *args, **kwargs) -> Future:
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            future: Future = Future()
            self._tasks.put((future, fn, args, kwargs))
            return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        task = self._tasks.get_nowait()
                    except queue.Empty:
                        break
                    if task is not None:
                        task[0].cancel()
            for _thread in self._threads:
                self._tasks.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


class Translator:
    def __init__(
        self,
        shared_state: _TranslatorSharedState | None = None,
        *,
        defer_success_record: bool = False,
    ):
        self._shared_state = shared_state or _new_translator_shared_state()
        self._state_lock = self._shared_state.lock
        self._fallback_state_obj = self._shared_state.fallback
        self._engines: list[TranslationEngine] = _build_engine_chain()
        self._engines_key = engine_chain_config_key()
        self._memory = self._shared_state.memory
        self._policy = self._shared_state.policy
        self._last_input: str = ""
        self._defer_success_record = defer_success_record
        self._session = self._shared_state.session
        self._last_provisional_trace: dict = {}

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
            state.consecutive_primary_failures = 0
            state.primary_cooldown_until = 0.0
            state.consecutive_probe_successes = 0

    @property
    def _active_idx(self) -> int:
        return self._fallback_state().active_idx

    @_active_idx.setter
    def _active_idx(self, value: int) -> None:
        self._fallback_state().active_idx = value

    @property
    def _consecutive_primary_failures(self) -> int:
        return self._fallback_state().consecutive_primary_failures

    @_consecutive_primary_failures.setter
    def _consecutive_primary_failures(self, value: int) -> None:
        self._fallback_state().consecutive_primary_failures = value

    def translate(self, text: str, incomplete: bool = False) -> str | None:
        return self.translate_event(text, incomplete).target_text

    def translate_event(
        self,
        text: str,
        incomplete: bool = False,
        *,
        repetition_evidence: RepetitionEvidence | None = None,
        provisional_candidate: ProvisionalCandidate | None = None,
        source_utterance_ids: tuple[str, ...] = (),
        evidence_source_utterance_ids: tuple[str, ...] = (),
    ) -> TranslationOutcome:
        snapshot = bound_activity_snapshot() or capture_effective_activity_snapshot(
            cfg.translation.current_activity,
            automatic_enabled=bool(
                cfg.scene.publish_translation_activity
            ),
            source_text=text,
        )
        with bind_activity_snapshot(snapshot):
            corrections: list[dict] = []
            token = _CORRECTION_CAPTURE.set(corrections)
            try:
                outcome = self._translate_event_with_snapshot(
                    text,
                    incomplete,
                    repetition_evidence=repetition_evidence,
                    provisional_candidate=provisional_candidate,
                    source_utterance_ids=source_utterance_ids,
                    evidence_source_utterance_ids=evidence_source_utterance_ids,
                )
            finally:
                _CORRECTION_CAPTURE.reset(token)
            return replace(outcome, corrections=tuple(corrections))

    def _translate_event_with_snapshot(
        self,
        text: str,
        incomplete: bool = False,
        *,
        repetition_evidence: RepetitionEvidence | None = None,
        provisional_candidate: ProvisionalCandidate | None = None,
        source_utterance_ids: tuple[str, ...] = (),
        evidence_source_utterance_ids: tuple[str, ...] = (),
    ) -> TranslationOutcome:
        raw_text = (text or "").strip()
        self._last_provisional_trace = {}
        self._last_fallback_details = None
        history_cohort = self._history_cohort()
        if repetition_evidence is not None:
            # The call argument is authoritative.  A stale/malformed evidence
            # object must never relabel an incomplete sentence as complete.
            repetition_evidence = RepetitionEvidence(
                min_avg_logprob=repetition_evidence.min_avg_logprob,
                max_no_speech_prob=repetition_evidence.max_no_speech_prob,
                cut_reason=repetition_evidence.cut_reason,
                forced=repetition_evidence.forced,
                incomplete=incomplete,
            )
        policy = self._policy_state()
        # L1: rejection_reason, prepare_input and the sanitize-rejection read
        # must share one lock section — the policy instance is shared across
        # workers, so split reads raced and produced wrong filter_reason values.
        with self._state_guard():
            filter_reason = policy.rejection_reason(
                raw_text,
                repetition_evidence=repetition_evidence,
            )
            text = policy.prepare_input(
                raw_text,
                initial_rejection_reason=filter_reason,
                repetition_evidence=repetition_evidence,
            )
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
        entity_context = _resolve_entity_request_context(text)
        canonical_obligations = _canonical_obligations_for_request(entity_context)
        request_protection = _request_protection_for(
            text,
            entity_context,
            canonical_obligations,
        )
        provider_text = request_protection.provider_source

        # Direct paths predate escrow and cannot preserve its provider token.
        # Keep them unavailable for the narrowly escrowed sentences rather than
        # allowing a cache/slang result to bypass the final identity invariant.
        slang_result = (
            None
            if request_protection.requires_provider_protection
            else self._translate_slang(text, incomplete)
        )
        if slang_result:
            slang_guard = _translation_output_guard(
                None,
                slang_result,
                text,
                obligations=canonical_obligations,
            )
            if not slang_guard.get("reason"):
                slang_result = str(
                    slang_guard.get("candidate_output") or slang_result
                )
                slang_evaluation = evaluate_canonical_obligations(
                    slang_result, canonical_obligations
                )
                success_commit = lambda: self._record_direct_success(
                    text,
                    slang_result,
                    incomplete,
                    history_cohort,
                )
                if not getattr(self, "_defer_success_record", False):
                    success_commit()
                    success_commit = None
                return TranslationOutcome(
                    source_text=raw_text,
                    prepared_source_text=text,
                    target_text=slang_result,
                    status="success",
                    result_source="slang",
                    cache_status="skipped",
                    incomplete=incomplete,
                    canonical_obligation_evaluation=slang_evaluation,
                    deferred_success=success_commit,
                )
            metrics.increment(
                "translation.publication_invariant.slang_rejected"
            )

        # 根据当前模型选择对应的 prompt
        self._refresh_engines_if_needed()
        system_prompt = self._build_system_prompt(entity_context.capsule)
        engine = self._active_engine()
        with self._state_guard():
            history = self._history_state().context(history_cohort)
        translation_request = self._freeze_translation_request(
            request_protection, system_prompt, incomplete, history,
            canonical_obligations, history_cohort=history_cohort,
        )
        prompt_ver = self._prompt_version_for_engine(
            engine,
            system_prompt,
            protection_identity=request_protection.fingerprint_identity,
            history=history,
            route_request=(
                translation_request.route(translation_route_id(engine))
                if engine is not None else None
            ),
            translation_request=translation_request,
        )

        lookup = (
            MemoryLookup(None, "miss")
            if request_protection.requires_provider_protection
            else self._lookup_existing_translation_event(
                text, incomplete, prompt_ver, engine, history_cohort
            )
        )
        if lookup.result:
            cache_guard = _translation_output_guard(
                None,
                lookup.result,
                text,
                obligations=canonical_obligations,
            )
            if cache_guard.get("reason"):
                metrics.increment(
                    "translation.publication_invariant.cache_rejected"
                )
                self._invalidate_cached_translation(
                    text,
                    incomplete,
                    prompt_ver,
                    engine,
                    lookup.result,
                    history_cohort,
                )
                lookup = MemoryLookup(None, "miss")
            else:
                target_text = str(
                    cache_guard.get("candidate_output") or lookup.result
                )
                cache_evaluation = evaluate_canonical_obligations(
                    target_text, canonical_obligations
                )
        if lookup.result:
            if (
                _looks_like_meta_garbage_output(target_text)
                or _looks_like_model_refusal(target_text)
            ):
                self._invalidate_cached_translation(
                    text,
                    incomplete,
                    prompt_ver,
                    engine,
                    lookup.result,
                    history_cohort,
                )
                self._reset_failed_input()
                return TranslationOutcome(
                    source_text=raw_text,
                    prepared_source_text=text,
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
            success_commit = lambda: self._record_lookup_context(
                text,
                target_text,
                incomplete,
                history_cohort,
            )
            if not getattr(self, "_defer_success_record", False):
                success_commit()
                success_commit = None
            return TranslationOutcome(
                source_text=raw_text,
                prepared_source_text=text,
                target_text=target_text,
                status="success",
                result_source=lookup.source,
                cache_status=lookup.source,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_ver,
                canonical_obligation_evaluation=cache_evaluation,
                deferred_success=success_commit,
            )

        deepseek_route_request = next(
            (route for route in translation_request.routes if route.engine == "deepseek"),
            None,
        )
        deadline_at = _translation_deadline_at()
        promoted = False
        result = None
        used_engine = None
        if provisional_candidate is not None and deepseek_provisional_eligible():
            snapshot = bound_activity_snapshot()
            profile_snapshot = bound_profile_snapshot()
            if profile_snapshot is None:
                profile_snapshot = profile_state.current()
            assert snapshot is not None
            fingerprint_route = (
                deepseek_route_request if deepseek_route_request is not None
                and deepseek_route_request.body else None
            ) or freeze_route_request(
                DeepSeekTranslationEngine(), provider_text, system_prompt,
                incomplete, history,
            )
            fingerprint = provisional_fingerprint(
                prepared_source=text,
                source_utterance_ids=source_utterance_ids,
                evidence_source_utterance_ids=evidence_source_utterance_ids,
                profile_id=profile_snapshot.effective_profile_id,
                profile_cache_identity=profile_snapshot.cache_identity,
                activity_cache_identity=snapshot.cache_identity,
                history_cohort=history_cohort,
                messages=fingerprint_route.messages,
                incomplete=incomplete,
                protection_identity=request_protection.fingerprint_identity,
                provider_options=fingerprint_route.options_dict(),
                timeout_seconds=fingerprint_route.timeout_seconds,
            )
            if fingerprint == provisional_candidate.fingerprint:
                provisional_engine = DeepSeekTranslationEngine()
                guard = _translation_output_guard(
                    provisional_engine,
                    provisional_candidate.raw_target,
                    text,
                    obligations=canonical_obligations,
                    request_protection=request_protection,
                )
                if not guard.get("reason"):
                    # Keep the provider candidate intact. The authoritative
                    # final adjudication owns restore/correction for promoted
                    # output exactly as it does for primary and fallback.
                    result = provisional_candidate.raw_target
                    used_engine = provisional_engine
                    promoted = True
                    self._last_provisional_trace = {
                        "promotion_attempted": True,
                        "promotion_passed": True,
                        "fingerprint_match": True,
                    }
                else:
                    self._last_provisional_trace = {
                        "promotion_attempted": True,
                        "promotion_passed": False,
                        "fingerprint_match": True,
                        "guard_rejection": str(guard.get("reason") or ""),
                        "final_retranslation": True,
                    }
            else:
                self._last_provisional_trace = {
                    "promotion_attempted": True,
                    "promotion_passed": False,
                    "fingerprint_match": False,
                    "fingerprint_mismatch": True,
                    "final_retranslation": True,
                }
        if not promoted:
            result, used_engine = self._call_with_fallback(
                request_protection,
                system_prompt,
                incomplete,
                history,
                deadline_at=deadline_at,
                canonical_obligations=canonical_obligations,
                translation_request=translation_request,
            )
        # Attribute the outcome to the engine that actually produced it: on a
        # soft fallback the active engine stays primary, so reading
        # _active_engine() here mislabeled the result source, API diagnostics
        # and the DB cache row.
        engine = used_engine or self._active_engine()
        prompt_ver = self._prompt_version_for_engine(
            engine,
            system_prompt,
            protection_identity=request_protection.fingerprint_identity,
            history=history,
            route_request=(
                translation_request.route(translation_route_id(engine))
                if engine is not None else None
            ),
            translation_request=translation_request,
        )
        request_contract_id = (
            provisional_candidate.request_contract_id
            if promoted and provisional_candidate is not None
            else str(
                (getattr(self, "_last_fallback_details", None).selected_attempt
                 if getattr(self, "_last_fallback_details", None) else {}).get(
                    "request_contract_id", ""
                )
            )
        )
        selected_route = (
            translation_request.route(translation_route_id(engine))
            if engine is not None else None
        )
        selected_attempt = (
            getattr(self, "_last_fallback_details", None).selected_attempt
            if getattr(self, "_last_fallback_details", None) else {}
        )
        return self._finalize_translation_result(
            raw_text=raw_text,
            prepared_text=text,
            provider_result=result,
            promoted=promoted,
            engine=engine,
            prompt_version=prompt_ver,
            cache_status=lookup.source,
            incomplete=incomplete,
            canonical_obligations=canonical_obligations,
            request_protection=request_protection,
            history_cohort=history_cohort,
            request_contract_id=request_contract_id,
            provider_options=(
                provisional_candidate.provider_options
                if promoted and provisional_candidate is not None
                else selected_route.provider_options if selected_route else ()
            ),
            system_fingerprint=(
                provisional_candidate.system_fingerprint
                if promoted and provisional_candidate is not None
                else str(selected_attempt.get("system_fingerprint") or "")
            ),
            guard_rescue=(
                None if promoted
                else selected_attempt.get("guard_rescue") if isinstance(selected_attempt.get("guard_rescue"), dict)
                else None
            ),
        )

    def _reset_failed_input(self) -> None:
        """Release duplicate suppression after a retryable final failure."""
        with self._state_guard():
            policy = self._policy_state()
            if policy.last_input == self._last_input:
                policy.reset_last_input()
            self._last_input = ""

    def _finalize_translation_result(
        self,
        *,
        raw_text: str,
        prepared_text: str,
        provider_result: str | None,
        promoted: bool,
        engine: TranslationEngine | None,
        prompt_version: str,
        cache_status: str,
        incomplete: bool,
        canonical_obligations: tuple[CanonicalObligation, ...],
        request_protection: RequestProtection,
        history_cohort: HistoryCohort,
        request_contract_id: str = "",
        provider_options: tuple[tuple[str, str], ...] = (),
        system_fingerprint: str = "",
        guard_rescue: dict | None = None,
    ) -> TranslationOutcome:
        """Sole finalizer for primary, fallback, and provisional candidates."""
        if not provider_result:
            self._reset_failed_input()
            return TranslationOutcome(
                source_text=raw_text,
                prepared_source_text=prepared_text,
                target_text=None,
                status="failed",
                result_source="none",
                cache_status=cache_status,
                incomplete=incomplete,
                engine=engine.engine_name if engine else "",
                model=engine.model_name if engine else "",
                prompt_version=prompt_version,
                request_contract_id=request_contract_id,
                provider_options=provider_options,
                system_fingerprint=system_fingerprint,
                canonical_obligation_evaluation=evaluate_canonical_obligations(
                    None, canonical_obligations
                ),
            )

        adjudication = _adjudicate_translation_candidate(
            engine,
            provider_result,
            prepared_text,
            obligations=canonical_obligations,
            request_protection=request_protection,
            record_corrections=True,
            publication=True,
        )
        result = adjudication.candidate_output
        final_obligation_evaluation = adjudication.canonical_evaluation
        common_failure = {
            "source_text": raw_text,
            "prepared_source_text": prepared_text,
            "target_text": None,
            "result_source": "post_policy",
            "cache_status": cache_status,
            "incomplete": incomplete,
            "engine": engine.engine_name if engine else "",
            "model": engine.model_name if engine else "",
            "prompt_version": prompt_version,
            "request_contract_id": request_contract_id,
            "provider_options": provider_options,
            "system_fingerprint": system_fingerprint,
        }
        # A rescue hint is never trusted: the publication-time adjudication must itself fail only on the
        # copied-name Hangul checks, with both stages passing the same rule.
        rescued = bool(
            guard_rescue
            and adjudication.reason
            and _guard_rescue_eligible(adjudication.evidence, prepared_text)
        )
        if adjudication.reason and not rescued:
            reason = adjudication.reason
            status = "filtered" if reason == "meta_garbage_output" else "failed"
            log.debug(
                "Rejecting translation candidate (%s/%s): %.40s -> %.40s",
                adjudication.rejection_owner,
                reason,
                prepared_text,
                result,
            )
            metric_by_owner = {
                "canonical_obligation": "translation.canonical_obligation.final_rejected",
                "entity_provenance": "translation.entity_target.final_rejected",
                "script_safety": "translation.target_script.final_rejected",
                "model_output": "translation.model_output.final_rejected",
            }
            metric = (
                "translation.unknown_name_escrow.final_rejected"
                if reason.startswith("unknown_name_")
                else "translation.semantic_terminology.final_rejected"
                if reason.startswith("semantic_terminology_")
                else metric_by_owner.get(
                    adjudication.rejection_owner,
                    "translation.publication_invariant.final_rejected",
                )
            )
            metrics.increment(metric)
            self._reset_failed_input()
            return TranslationOutcome(
                **common_failure,
                status=status,
                filter_reason=reason,
                canonical_obligation_evaluation=final_obligation_evaluation,
            )

        if rescued:
            # Viewers saw it, so it stays in history/context, but it is never cached: an identical later
            # sentence takes the normal path again.
            success_commit = lambda: self._record_rescue(prepared_text, result, incomplete, history_cohort)
        else:
            success_commit = lambda: self._record_success(
                prepared_text,
                result,
                incomplete,
                prompt_version,
                engine,
                history_cohort,
            )
        if not getattr(self, "_defer_success_record", False):
            success_commit()
            success_commit = None
        return TranslationOutcome(
            source_text=raw_text,
            prepared_source_text=prepared_text,
            target_text=result,
            status="success",
            result_source=(
                "guard_rescue" if rescued else "provisional_promotion" if promoted else "api"
            ),
            cache_status=cache_status,
            incomplete=incomplete,
            engine=engine.engine_name if engine else "",
            model=engine.model_name if engine else "",
            prompt_version=prompt_version,
            request_contract_id=request_contract_id,
            provider_options=provider_options,
            system_fingerprint=system_fingerprint,
            canonical_obligation_evaluation=final_obligation_evaluation,
            unknown_name_approved_terms=request_protection.approved_hangul_terms,
            guard_rescue=(
                {**guard_rescue, "kept_hangul": sorted(set(_GUARD_RESCUE_HANGUL_RUN.findall(result or "")))}
                if rescued else None
            ),
            deferred_success=success_commit,
        )

    def _policy_state(self) -> TranslationPolicy:
        return self._policy

    def _memory_state(self) -> TranslationMemory:
        return self._memory

    def _history_state(self) -> ConversationHistory:
        shared = getattr(self, "_shared_state", None)
        history = getattr(shared, "history", None)
        return history if history is not None else self._memory_state().history

    def _history_cohort(self) -> HistoryCohort:
        return self._session_snapshot().history_cohort(bound_activity_snapshot())

    def _history_session(self) -> str:
        return self._session_snapshot().session_id

    def _session_snapshot(self) -> LiveSessionSnapshot:
        session = getattr(self, "_session", None)
        if session is None:
            shared = getattr(self, "_shared_state", None)
            session = getattr(shared, "session", None)
        if session is None:
            session = LiveSessionSnapshot.create()
        self._session = session
        return session

    def _translate_slang(self, text: str, incomplete: bool) -> str | None:
        slang_result = self._policy_state().slang_result(text)
        if not slang_result:
            return None

        log.debug("Slang hit: %s → %s", text, slang_result)
        return slang_result

    def _record_direct_success(self, text: str, result: str, incomplete: bool,
                               cohort: HistoryCohort) -> None:
        with self._state_guard():
            self._history_state().remember(text, result, incomplete, cohort)
        # File I/O outside the shared lock (M1).
        self._memory_state().write_history(text, result)

    def _record_lookup_context(self, text: str, result: str, incomplete: bool,
                               cohort: HistoryCohort) -> None:
        with self._state_guard():
            self._history_state().remember(text, result, incomplete, cohort)

    def _lookup_existing_translation_event(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        engine: TranslationEngine | None = None,
        cohort: HistoryCohort | None = None,
    ) -> MemoryLookup:
        # Memory cache under the lock; the SQLite read happens outside it (M1)
        # so a slow DB never blocks the other worker.
        if engine is None:
            engine = self._active_engine()
        with self._state_guard():
            lookup = self._memory_state().lookup_memory_event(
                text,
                incomplete,
                prompt_ver,
                engine,
                remember_recent=False,
                cohort=cohort,
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
                    text,
                    incomplete,
                    prompt_ver,
                    db_result,
                    engine,
                    remember_recent=False,
                    cohort=cohort,
                )
            log.debug("Cache hit: %s", text[:20])
            return lookup
        metrics.increment("translation.cache.miss")
        return MemoryLookup(None, "miss")

    def _record_success(self, text: str, result: str, incomplete: bool,
                        prompt_ver: str,
        engine: TranslationEngine | None = None,
        cohort: HistoryCohort | None = None) -> None:
        with self._state_guard():
            if engine is None:
                engine = self._active_engine()
            self._memory_state().cache_store(
                text, incomplete, result, prompt_ver, engine
            )
            self._history_state().remember(text, result, incomplete, cohort)
        # File/DB I/O outside the shared lock (M1).
        self._memory_state().write_history(text, result)
        if not incomplete and engine is not None and _db_cache_enabled():
            self._memory_state().db_store(text, result, engine, prompt_ver)

    def _record_rescue(self, text: str, result: str, incomplete: bool,
                       cohort: HistoryCohort | None = None) -> None:
        """History and transcript only; a rescued subtitle is never cached (memory or DB)."""
        metrics.increment("translation.guard_rescue.published")
        with self._state_guard():
            self._history_state().remember(text, result, incomplete, cohort)
        self._memory_state().write_history(text, result)

    def _invalidate_cached_translation(
        self,
        text: str,
        incomplete: bool,
        prompt_ver: str,
        active_engine: TranslationEngine | None,
        result: str | None,
        cohort: HistoryCohort | None = None,
    ) -> None:
        with self._state_guard():
            self._memory_state().invalidate_memory(
                text,
                incomplete,
                prompt_ver,
                active_engine,
                result,
                cohort,
            )
            self._history_state().forget(text, result, cohort)
        # DB delete outside the shared lock (M1). The delete is NOT gated on
        # _db_cache_enabled(): stale rows must be purged even if the cache layer
        # was enabled earlier in the session and disabled since.
        if not incomplete and active_engine is not None:
            self._memory_state().invalidate_db(text, active_engine, prompt_ver)

    def _active_engine(self) -> TranslationEngine | None:
        return active_engine(self._engines, self._active_idx)

    def _build_system_prompt(self, entity_capsule: str = "") -> str:
        return _compose_system_prompt(entity_capsule)

    @staticmethod
    def _prompt_version(system_prompt: str) -> str:
        return hashlib.md5(system_prompt.encode()).hexdigest()[:8]

    def _prompt_version_for_engine(
        self,
        engine: TranslationEngine | None,
        system_prompt: str,
        *,
        protection_identity: str = "",
        history: list[tuple[str, str]] | None = None,
        route_request=None,
        translation_request: TranslationRequest | None = None,
    ) -> str:
        snapshot = bound_activity_snapshot()
        if snapshot is None:
            snapshot = capture_effective_activity_snapshot(
                cfg.translation.current_activity,
                automatic_enabled=bool(
                    cfg.scene.publish_translation_activity
                ),
            )
        if route_request is not None and route_request.body:
            effective_prompt = route_request.messages[0][1]
        elif bound_activity_snapshot() is None:
            with bind_activity_snapshot(snapshot):
                effective_prompt = effective_system_prompt_for_engine(
                    engine,
                    system_prompt,
                )
        else:
            effective_prompt = effective_system_prompt_for_engine(
                engine,
                system_prompt,
            )
        cohort = (
            translation_request.history_cohort
            if translation_request is not None else self._history_cohort()
        )
        selected_history = (
            translation_request.history
            if translation_request is not None else tuple(history or ())
        )
        activity_cache_identity = (
            translation_request.activity_cache_identity
            if translation_request is not None else snapshot.cache_identity
        )
        history_context_enabled = (
            translation_request.history_context_enabled
            if translation_request is not None else
            int(cfg.translation.context_window or 0) > 0
        )
        # Cache entries can be context-shaped, so the episode boundary is part
        # of the identity even when activity is unknown. Resolver refreshes do
        # not change cohort_epoch; actual scene/profile transitions do.
        return self._prompt_version(
            effective_prompt
            + (
                "\n[request-entity-capsule] " + _request_entity_capsule(system_prompt)
                if _request_entity_capsule(system_prompt)
                and _request_entity_capsule(system_prompt) not in effective_prompt
                else ""
            )
            + "\n[canonical-publication-policy] "
            + _CANONICAL_PUBLICATION_POLICY_VERSION
            + (
                "\n[request-protection] " + protection_identity
                if protection_identity
                else ""
            )
            + "\n[request-cache-cohort] "
            + f"{cohort[0]}:{cohort[1]}:{cohort[2]}"
            + (
                "\n[history-session] " + (
                    cohort[0] if translation_request is not None
                    else self._history_session()
                )
                if history_context_enabled
                else ""
            )
            + (
                "\n[selected-history] "
                + stable_identity({"history": list(selected_history)})
                if history_context_enabled
                else ""
            )
            + (
                "\n[activity-cache-identity] " + activity_cache_identity
                if activity_cache_identity
                else ""
            )
            + (
                "\n[provider-options] "
                + stable_identity({
                    "options": route_request.options_dict(),
                    "timeout_seconds": route_request.timeout_seconds,
                })
                if route_request is not None and route_request.provider_options else ""
            )
        )

    def _freeze_translation_request(
        self,
        request_protection: RequestProtection,
        system_prompt: str,
        incomplete: bool,
        history: list[tuple[str, str]] | None,
        canonical_obligations: tuple[CanonicalObligation, ...],
        *,
        history_cohort: tuple[str, str, int] | None = None,
    ) -> TranslationRequest:
        profile_snapshot = bound_profile_snapshot()
        activity_snapshot = bound_activity_snapshot()
        return TranslationRequest(
            original_source=request_protection.original_source,
            provider_source=request_protection.provider_source,
            incomplete=incomplete,
            history=tuple(history or ()),
            history_cohort=history_cohort or self._history_cohort(),
            profile_cache_identity=(
                profile_snapshot.cache_identity if profile_snapshot else ""
            ),
            activity_cache_identity=(
                activity_snapshot.cache_identity if activity_snapshot else ""
            ),
            request_protection_identity=request_protection.identity,
            canonical_obligations=tuple(
                json.dumps(item.as_dict(), ensure_ascii=False, sort_keys=True)
                for item in canonical_obligations
            ),
            routes=tuple(
                freeze_route_request(
                    engine, request_protection.provider_source, system_prompt,
                    incomplete, history,
                ) for engine in self._engines
            ),
            history_context_enabled=int(
                cfg.translation.context_window or 0
            ) > 0,
            artifact_hashes=tuple(sorted(_current_artifact_hashes().items())),
        )

    def _call_with_fallback(
        self,
        request_protection: RequestProtection,
        system_prompt: str,
        incomplete: bool,
        history: list[tuple[str, str]] | None = None,
        *,
        deadline_at: float | None = None,
        canonical_obligations: tuple[CanonicalObligation, ...] | None = None,
        translation_request: TranslationRequest | None = None,
    ) -> tuple[str | None, TranslationEngine | None]:
        """Returns (result, engine_used). engine_used is the engine that
        actually produced the result — on a soft fallback this differs from
        the active engine, which intentionally stays on primary."""
        source_text = request_protection.original_source
        provider_source = request_protection.provider_source
        if canonical_obligations is None:
            entity_context = _resolve_entity_request_context(source_text)
            canonical_obligations = _canonical_obligations_for_request(entity_context)
        if translation_request is None:
            translation_request = self._freeze_translation_request(
                request_protection, system_prompt, incomplete, history,
                canonical_obligations,
            )
        profile_snapshot = bound_profile_snapshot()
        activity_snapshot = bound_activity_snapshot()
        history_cohort = translation_request.history_cohort
        frozen_messages_by_engine = {
            route.engine: route.messages for engine, route in zip(
                self._engines, translation_request.routes
            ) if route.engine in {"deepseek", "groq"}
        }
        route_requests_by_engine = {
            route.engine: route for engine, route in zip(
                self._engines, translation_request.routes
            ) if isinstance(engine, (DeepSeekTranslationEngine, GroqTranslationEngine))
        }
        request_contract_ids: dict[str, str] = {}
        for route_index, candidate_engine in enumerate(self._engines):
            engine_name = str(candidate_engine.engine_name or "").lower()
            route_id = translation_route_id(candidate_engine)
            route_request = translation_request.routes[route_index]
            messages = route_request.messages
            effective_prompt = (
                route_request.messages[0][1] if route_request.body else
                effective_system_prompt_for_engine(candidate_engine, system_prompt)
            )
            exact_messages_available = bool(route_request.body)
            contract_id = translation_request.route_contract_id(route_request)
            request_contract_ids[route_id] = contract_id
            runtime_events.emit_once(
                "translation_request_contract",
                contract_id,
                request_contract_id=contract_id,
                request_contract_schema_version=REQUEST_CONTRACT_SCHEMA_VERSION,
                translation_request_id=translation_request.request_id,
                request_phase=translation_request.phase,
                route_index=route_index,
                route_count=len(translation_request.routes),
                route_body_json_style=(
                    "compact_utf8" if engine_name == "deepseek" else "python_default"
                ),
                route_id=route_id,
                engine=engine_name,
                model=str(candidate_engine.model_name or ""),
                original_source=source_text,
                provider_source=provider_source,
                provider_source_sha256=sha256_text(provider_source),
                effective_system_prompt=effective_prompt,
                effective_system_prompt_sha256=sha256_text(effective_prompt),
                exact_messages_available=exact_messages_available,
                contract_role="available_route_request",
                messages=list(message_manifest(messages)),
                retry_messages=list(message_manifest(route_request.retry_messages)),
                provider_options=route_request.options_dict(),
                timeout_seconds=route_request.timeout_seconds,
                body_sha256=hashlib.sha256(route_request.body).hexdigest(),
                retry_body_sha256=(
                    hashlib.sha256(route_request.retry_body).hexdigest()
                    if route_request.retry_body is not None else ""
                ),
                messages_sha256=stable_identity({"messages": list(messages)}),
                history=list(history_manifest(history)),
                history_cohort=list(translation_request.history_cohort),
                history_context_enabled=translation_request.history_context_enabled,
                profile_cache_identity=translation_request.profile_cache_identity,
                activity_cache_identity=translation_request.activity_cache_identity,
                request_protection_identity=translation_request.request_protection_identity,
                history_cohort_id=(
                    f"{history_cohort[0]}:{history_cohort[1]}:{history_cohort[2]}"
                ),
                incomplete=incomplete,
                request_protection={
                    "identity": request_protection.identity,
                    "spans": [span.identity_row() for span in request_protection.spans],
                },
                canonical_obligations=[
                    obligation.as_dict() for obligation in canonical_obligations
                ],
                profile=(profile_snapshot.as_metadata() if profile_snapshot else {}),
                activity=(
                    activity_snapshot_metadata(activity_snapshot)
                    if activity_snapshot else {}
                ),
                artifact_hashes=dict(translation_request.artifact_hashes),
                policy_versions={
                    "adjudication": ADJUDICATION_POLICY_VERSION,
                    "canonical_publication": _CANONICAL_PUBLICATION_POLICY_VERSION,
                    "request_protection": PROTECTION_POLICY_VERSION,
                },
            )
        fallback_state = self._fallback_state()
        lock = getattr(self, "_state_lock", None)
        if lock is None:
            before_state = _copy_fallback_state(fallback_state)
            state = fallback_state
        else:
            with lock:
                before_state = _copy_fallback_state(fallback_state)
                state = _copy_fallback_state(before_state)
        result, used_idx, fallback_details = call_with_fallback(
            self._engines,
            state,
            provider_source,
            system_prompt,
            incomplete,
            history,
            _fallback_failure_threshold(),
            _looks_untranslated,
            log,
            circuit_breaker_enabled=_translation_circuit_breaker_enabled(),
            recovery_cooldown_seconds=(
                cfg.translation.circuit_recovery_cooldown_sec
            ),
            deadline_at=deadline_at,
            max_route_inflight=cfg.translation.live_route_max_inflight,
            frozen_messages_by_engine=frozen_messages_by_engine,
            request_contract_ids=request_contract_ids,
            route_requests_by_engine=route_requests_by_engine,
            return_details=True,
            output_rescue=lambda attempts: _choose_guard_rescue(attempts, source_text),
            output_guard=lambda candidate_engine, candidate, _provider_source: (
                _translation_output_guard(
                    candidate_engine,
                    candidate,
                    source_text,
                    obligations=canonical_obligations,
                    request_protection=request_protection,
                )
            ),
        )
        self._last_fallback_details = fallback_details
        if lock is not None:
            with lock:
                committed_before = _copy_fallback_state(fallback_state)
                _merge_fallback_state(fallback_state, before_state, state)
                committed_after = _copy_fallback_state(fallback_state)
        else:
            committed_before = before_state
            committed_after = _copy_fallback_state(fallback_state)

        if (
            _translation_circuit_breaker_enabled()
            and committed_after.active_idx != committed_before.active_idx
        ):
            from_engine = active_engine(self._engines, committed_before.active_idx)
            to_engine = active_engine(self._engines, committed_after.active_idx)
            attempts = list(fallback_details.attempts)
            failed_attempt = next(
                (
                    attempt
                    for attempt in reversed(attempts)
                    if str(attempt.get("route_id") or "")
                    == translation_route_id(from_engine)
                ),
                {},
            )
            _send_fallback_event(
                getattr(self, "_shared_state", None),
                (
                    "fallback_recovered"
                    if committed_after.active_idx < committed_before.active_idx
                    else "circuit_opened"
                    if committed_before.active_idx == 0
                    else "fallback_advanced"
                ),
                primary_engine=str(getattr(active_engine(self._engines, 0), "engine_name", "") or ""),
                primary_route=translation_route_id(active_engine(self._engines, 0)),
                from_engine=str(getattr(from_engine, "engine_name", "") or ""),
                from_route=translation_route_id(from_engine),
                active_engine=str(getattr(to_engine, "engine_name", "") or ""),
                active_route=translation_route_id(to_engine),
                from_active_idx=committed_before.active_idx,
                active_idx=committed_after.active_idx,
                sequence_id=getattr(self, "_current_sequence_id", None),
                attempt_chain=[
                    {
                        key: attempt.get(key)
                        for key in (
                            "route_id",
                            "status",
                            "failure_scope",
                            "api_error_type",
                            "api_error_message_class",
                        )
                    }
                    for attempt in attempts
                ],
                failure_status=str(failed_attempt.get("status") or ""),
                failure_scope=str(failed_attempt.get("failure_scope") or ""),
                api_error_type=failed_attempt.get("api_error_type"),
                api_error_message_class=failed_attempt.get("api_error_message_class"),
                cooldown_seconds=cfg.translation.circuit_recovery_cooldown_sec,
                cooldown_remaining_ms=round(
                    max(0.0, committed_after.primary_cooldown_until - time.monotonic()) * 1000,
                    2,
                ),
                required_probe_successes=(
                    cfg.translation.circuit_recovery_success_threshold
                ),
            )
        used_engine = active_engine(self._engines, used_idx)
        return result, used_engine


    def _get_prompt_version_hash(self) -> str:
        snapshot = bound_activity_snapshot() or capture_effective_activity_snapshot(
            cfg.translation.current_activity,
            automatic_enabled=bool(
                cfg.scene.publish_translation_activity
            ),
        )
        with bind_activity_snapshot(snapshot):
            return self._prompt_version_for_engine(
                self._active_engine(),
                self._build_system_prompt(),
            )


def _compose_system_prompt(entity_capsule: str = "") -> str:
    """Shared between Translator._build_system_prompt and the probe thread (L5).

    Pure function of config since PromptEvolver was removed (2026-06-12):
    prompt_ver is now stable for a given base prompt + profile selection.
    """
    system_prompt = _BASE_PROMPT

    if effective_profile_applied(cfg.translation.use_profile):
        profile_id = effective_profile_id(cfg.active_streamer_profile)
        streamer_profile = get_translation_profile(profile_id)
        if streamer_profile:
            system_prompt += "\n\n" + streamer_profile
            log.debug("Appended streamer profile: %s", profile_id)

    # Manual session state (orthogonal to profiles, applies even with
    # use_profile=False): one labeled background line, never source text.
    activity_capsule = activity_prompt_capsule(
        effective_activity_value(cfg.translation.current_activity)
    )
    if activity_capsule:
        system_prompt += "\n\n" + activity_capsule

    if entity_capsule:
        system_prompt += "\n\n" + entity_capsule

    # Output rules go last so profile/background sections never sit after the
    # final instruction the model is supposed to obey.
    system_prompt += _BASE_PROMPT_TAIL
    return system_prompt


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


# The pipeline (workers, ordered emission, recovery probe, start) lives in
# translation_pipeline.py, which reads this module as `core`. Some imports above
# look unused here but are read as core.X there (and patched on this module by
# tests): do not remove them; tests/test_translation_pipeline_split.py checks this. Its names are answered
# lazily here, so `translator.start(...)` and old imports keep working and either
# module can be imported first.
_PIPELINE_NAMES = frozenset({
    "_DEDUP_SUBTITLE_SEC",
    "_FinalWorker",
    "_MAX_COMPLETED_BACKLOG",
    "_OrderedEmitter",
    "_ProvisionalWorker",
    "_STOP_DRAIN_TIMEOUT_MARGIN_SEC",
    "_build_probe_system_prompt",
    "_failed_completion",
    "_start_fallback_probe_thread",
    "_stop_drain_timeout_sec",
    "_submitted_now",
    "_translation_max_output_delay_ms",
    "start",
})


def __getattr__(name):
    if name in _PIPELINE_NAMES:
        from modules import translation_pipeline

        return getattr(translation_pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
