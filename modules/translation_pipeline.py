"""The translation pipeline around Translator: provisional and final workers, ordered
emission, the fallback recovery probe and start(). Moved out of translator.py
(2026-10-28); translator.py still answers translator.start and the other names here.

Names that translator.py owns, and imported names the tests patch on it
(runtime_events, cfg, ...), are read as core.<name>, so those patches still apply.
"""
import hashlib
import json
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from datetime import datetime, timezone
from modules.activity_context import (
    ActivitySnapshot,
    activity_snapshot_metadata,
    bind_activity_snapshot,
    bind_profile_id,
    capture_effective_activity_snapshot,
    effective_profile_id,
)
from modules.profile_context import ProfileSnapshot, bind_profile_snapshot
from modules.live_status import live_status
from utils.metrics import metrics
from utils.pipeline import start_daemon_thread
from utils.queue_utils import put_latest
from modules.pipeline_events import sentence_incomplete, sentence_metadata, sentence_text
from modules.provisional_subtitles import (
    ProvisionalCandidate,
    ProvisionalRequest,
    ProvisionalStore,
    SubtitlePayload,
    provisional_fingerprint,
)
from modules.translation_engines import (
    TranslationEngine,
    engine_chain_config_key,
    reset_last_engine_diagnostics,
    reset_last_token_usage,
    reset_translation_call_trace,
    translation_route_id,
)
from modules.forensics_contract import (
    ADJUDICATION_POLICY_VERSION,
    REQUEST_CONTRACT_SCHEMA_VERSION,
    history_manifest,
    message_manifest,
    sha256_text,
    stable_identity,
)
from modules.translation_runtime import active_engine
from modules.translation_request import TranslationRequest, freeze_route_request
from modules.translation_policy import RepetitionEvidence
from modules.request_protection import PROTECTION_POLICY_VERSION
from modules.translation_normalization import (
    _canonical_obligations_for_request,
    _dependency_marker,
    _request_protection_for,
    _resolve_entity_request_context,
)
from modules.translation_guard import _final_script_rejection_reason, _looks_untranslated

from modules import translator as core

_DEDUP_SUBTITLE_SEC = 5.0   # suppress identical subtitle within this window
_MAX_COMPLETED_BACKLOG = 64  # completed-but-unemitted results before warning (L8)
_STOP_DRAIN_TIMEOUT_MARGIN_SEC = 0.5


def _translation_max_output_delay_ms() -> float:
    value = core.cfg.translation.max_subtitle_output_delay_ms
    return float(value) if isinstance(value, (int, float)) else 30000.0



def _stop_drain_timeout_sec() -> float:
    try:
        join_timeout = float(getattr(core.cfg, "thread_join_timeout", 5.0) or 5.0)
    except (TypeError, ValueError):
        join_timeout = 5.0
    return max(core._TRANSLATION_LOOP_POLL_SEC, join_timeout - _STOP_DRAIN_TIMEOUT_MARGIN_SEC)



def _build_probe_system_prompt(_shared_state: core._TranslatorSharedState) -> str:
    return core._compose_system_prompt()



def _start_fallback_probe_thread(
    shared_state: core._TranslatorSharedState,
    stop_event: threading.Event,
    *,
    interval_seconds: float = core._FALLBACK_PROBE_INTERVAL_SEC,
) -> threading.Thread:
    def run() -> None:
        engines: list[TranslationEngine] | None = None
        engines_key: tuple | None = None
        while not stop_event.wait(interval_seconds):
            with shared_state.lock:
                if shared_state.fallback.active_idx <= 0:
                    continue
                probe_state = core._copy_fallback_state(shared_state.fallback)
                before_state = core._copy_fallback_state(probe_state)
                # Provider-health probes are not subtitles and have no event
                # cohort. Conversation history would only risk cross-scene
                # contamination, so probes deliberately run without it.
                probe_history: list[tuple[str, str]] = []
            # L7: rebuild the probe chain when the engine selection changes
            # mid-run instead of caching the first build forever.
            chain_key = engine_chain_config_key()
            if engines is None or chain_key != engines_key:
                engines = core._build_engine_chain()
                engines_key = chain_key
            probe_observations: list[dict[str, object]] = []
            probe_started = time.monotonic()
            activity_snapshot = capture_effective_activity_snapshot(
                core.cfg.translation.current_activity,
                automatic_enabled=bool(
                    core.cfg.scene.publish_translation_activity
                ),
            )
            with bind_activity_snapshot(activity_snapshot), bind_profile_snapshot(core.profile_state.current()):
                system_prompt = _build_probe_system_prompt(shared_state)
                try:
                    core.probe_primary_recovery(
                        engines,
                        probe_state,
                        core._FALLBACK_PROBE_TEXT,
                        system_prompt,
                        _looks_untranslated,
                        core.log,
                        circuit_breaker_enabled=(
                            core._translation_circuit_breaker_enabled()
                        ),
                        recovery_cooldown_seconds=(
                            core.cfg.translation.circuit_recovery_cooldown_sec
                        ),
                        required_consecutive_successes=(
                            core.cfg.translation.circuit_recovery_success_threshold
                        ),
                        history=probe_history,
                        observation_sink=probe_observations.append,
                        deadline_at=core._translation_deadline_at(),
                        max_route_inflight=(
                            core.cfg.translation.live_route_max_inflight
                        ),
                        output_guard=core._translation_output_guard,
                    )
                except Exception:
                    core.log.exception("Fallback primary probe failed unexpectedly")
                    continue
            if stop_event.is_set():
                return
            probe_elapsed_ms = round((time.monotonic() - probe_started) * 1000, 2)
            with shared_state.lock:
                if stop_event.is_set():
                    return
                committed_before = core._copy_fallback_state(shared_state.fallback)
                core._merge_fallback_state(shared_state.fallback, before_state, probe_state)
                if stop_event.is_set():
                    core._replace_fallback_state(shared_state.fallback, committed_before)
                    return
                committed_after = core._copy_fallback_state(shared_state.fallback)

            observation = probe_observations[-1] if probe_observations else {}
            probe_status = str(observation.get("status") or "unknown")
            state_applied = committed_after == probe_state
            active_before = active_engine(engines, committed_before.active_idx)
            active_after = active_engine(engines, committed_after.active_idx)
            common_fields = {
                "primary_engine": str(getattr(active_engine(engines, 0), "engine_name", "") or ""),
                "primary_route": translation_route_id(active_engine(engines, 0)),
                "active_engine": str(getattr(active_after, "engine_name", "") or ""),
                "active_route": translation_route_id(active_after),
                "active_engine_before": str(getattr(active_before, "engine_name", "") or ""),
                "active_route_before": translation_route_id(active_before),
                "active_idx_before": committed_before.active_idx,
                "active_idx": committed_after.active_idx,
                "probe_status": probe_status,
                "probe_elapsed_ms": probe_elapsed_ms,
                "probe_history_items": len(probe_history),
                "probe_history_source_chars": sum(len(source) for source, _ in probe_history),
                "probe_history_target_chars": sum(len(target) for _, target in probe_history),
                "probe_success_streak": observation.get("success_streak", 0),
                "committed_probe_success_streak": committed_after.consecutive_probe_successes,
                "required_probe_successes": (
                    core.cfg.translation.circuit_recovery_success_threshold
                ),
                "cooldown_seconds": (
                    core.cfg.translation.circuit_recovery_cooldown_sec
                ),
                "cooldown_remaining_ms": round(
                    max(0.0, committed_after.primary_cooldown_until - time.monotonic()) * 1000,
                    2,
                ),
                "state_applied": state_applied,
            }
            if observation.get("exception_type"):
                common_fields["exception_type"] = observation["exception_type"]
            for diagnostic_field in (
                "api_error_type",
                "api_error_message_class",
                "api_total_wall_ms",
                "api_timeout_count",
                "deadline_exceeded",
                "deadline_scope",
                "deadline_budget_ms",
            ):
                if diagnostic_field in observation:
                    common_fields[diagnostic_field] = observation[
                        diagnostic_field
                    ]
            if probe_status == "cooldown_skipped":
                action = "probe_cooldown_skipped"
            elif probe_status == "success":
                action = "probe_succeeded"
            else:
                action = "probe_failed"
            if stop_event.is_set():
                return
            core._send_fallback_event(shared_state, action, **common_fields)
            if stop_event.is_set():
                return
            if (
                bool(observation.get("recovered"))
                and committed_before.active_idx > 0
                and committed_after.active_idx == 0
            ):
                core._send_fallback_event(shared_state, "circuit_closed", **common_fields)

    return start_daemon_thread("TranslationFallbackProbe", run)



def _submitted_now() -> tuple[float, str]:
    return time.monotonic(), datetime.now(timezone.utc).isoformat()



def _failed_completion(seq: int) -> core._CompletedTranslation:
    """Synthetic failed result so a crashed future never leaves a gap in
    the sequence — a gap would stall the in-order emit loop forever."""
    now = time.monotonic()
    return core._CompletedTranslation(
        seq,
        core.TranslationOutcome(
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
                core.cfg.translation.translation_mode or ""
            ),
        },
        now,
        now,
        now,
        "",
        0,
        "",
        dict(core._API_EVENT_DEFAULTS),
    )



class _ProvisionalWorker:
    """One-shot DeepSeek provisional previews (formerly start()'s closure).

    Owns the publication gate that shutdown closes before draining finals.
    """

    def __init__(self, shared_state, store, stop_event, subtitle_queue):
        self._shared_state = shared_state
        self._store = store
        self._stop_event = stop_event
        self._subtitle_queue = subtitle_queue
        self._publication_lock = threading.Lock()
        self._publication_open = True
        self._earlier_finals_shown: Callable[[], bool] | None = None

    def _wait_for_earlier_finals(self, provisional_id: str) -> bool:
        """Hold a preview until the finals of earlier sentences are out.

        Previews bypass the ordered emitter, so a preview for sentence N+1 could reach
        the screen while sentence N's final was still translating, and the screen read
        N+1, N, N+1 (about 1 % of finals in older runs). Returns False when the wait is
        abandoned: this sentence's own final already closed it, shutdown, or timeout.
        """
        shown = self._earlier_finals_shown
        if shown is None:
            return True
        deadline = time.monotonic() + core._PREVIEW_ORDER_WAIT_SEC
        while not shown():
            if (
                self._stop_event.is_set()
                or self._store.is_closed(provisional_id)
                or time.monotonic() >= deadline
            ):
                return False
            self._stop_event.wait(core._PREVIEW_ORDER_POLL_SEC)
        return True

    def close_publication(self) -> None:
        with self._publication_lock:
            self._publication_open = False

    def translate(
        self,
        request: ProvisionalRequest,
        earlier_finals_shown: Callable[[], bool] | None = None,
    ) -> None:
        """``earlier_finals_shown`` reports whether every final submitted before this
        preview's sentence has been emitted; the preview waits for it (see
        ``_wait_for_earlier_finals``)."""
        self._earlier_finals_shown = earlier_finals_shown
        started = time.monotonic()
        request_contract_id = ""
        if not core.deepseek_provisional_eligible():
            return
        if (
            request.profile_snapshot is not None
            and request.profile_snapshot.generation
            != core.profile_state.current().generation
        ):
            self._store.close(request.provisional_id)
            core.runtime_events.emit(
                "provisional_translation",
                action="retired_profile_generation",
                provisional_id=request.provisional_id,
                **request.profile_snapshot.as_metadata(),
            )
            return
        engine = core.DeepSeekTranslationEngine()
        if not engine.available or self._store.is_closed(
            request.provisional_id
        ):
            return
        reset_last_engine_diagnostics()
        reset_last_token_usage()
        try:
            request_profile_snapshot = request.profile_snapshot or core.profile_state.current()
            with bind_profile_snapshot(request_profile_snapshot):
              with bind_profile_id(request.profile_id):
                with bind_activity_snapshot(request.activity_snapshot):
                    preview_translator = core.Translator(shared_state=self._shared_state)
                    preview_policy = core._new_translation_policy()
                    repetition_evidence = RepetitionEvidence(
                        min_avg_logprob=request.min_avg_logprob,
                        max_no_speech_prob=request.max_no_speech_prob,
                        cut_reason=request.cut_reason,
                        forced=request.forced,
                        incomplete=request.incomplete,
                    )
                    filter_reason = preview_policy.rejection_reason(
                        request.text.strip(),
                        repetition_evidence=repetition_evidence,
                    )
                    prepared_source = preview_policy.prepare_input(
                        request.text.strip(),
                        initial_rejection_reason=filter_reason,
                        repetition_evidence=repetition_evidence,
                    )
                    if prepared_source is None:
                        core.runtime_events.emit(
                            "provisional_translation",
                            action="source_filtered",
                            provisional_id=request.provisional_id,
                            filter_reason=filter_reason or "source_policy",
                        )
                        return
                    prepared_source = core._normalize_source_before_matching(
                        prepared_source
                    )
                    entity_context = _resolve_entity_request_context(prepared_source)
                    obligations = _canonical_obligations_for_request(entity_context)
                    request_protection = _request_protection_for(
                        prepared_source,
                        entity_context,
                        obligations,
                    )
                    system_prompt = preview_translator._build_system_prompt(
                        entity_context.capsule
                    )
                    history_cohort = self._shared_state.session.history_cohort(
                        request.activity_snapshot
                    )
                    with self._shared_state.lock:
                        history = self._shared_state.history.context(history_cohort)
                    route_request = freeze_route_request(
                        engine, request_protection.provider_source,
                        system_prompt, request.incomplete, history,
                    )
                    messages = route_request.messages
                    fingerprint = provisional_fingerprint(
                        prepared_source=prepared_source,
                        source_utterance_ids=request.source_utterance_ids,
                        evidence_source_utterance_ids=(
                            request.evidence_source_utterance_ids
                        ),
                        profile_id=effective_profile_id(request.profile_id),
                        profile_cache_identity=request_profile_snapshot.cache_identity,
                        activity_cache_identity=(
                            request.activity_snapshot.cache_identity
                        ),
                        history_cohort=history_cohort,
                        messages=messages,
                        incomplete=request.incomplete,
                        protection_identity=request_protection.fingerprint_identity,
                        provider_options=route_request.options_dict(),
                        timeout_seconds=route_request.timeout_seconds,
                    )
                    translation_request = TranslationRequest(
                        original_source=prepared_source,
                        provider_source=request_protection.provider_source,
                        incomplete=request.incomplete,
                        history=tuple(history),
                        history_cohort=history_cohort,
                        profile_cache_identity=request_profile_snapshot.cache_identity,
                        activity_cache_identity=request.activity_snapshot.cache_identity,
                        request_protection_identity=request_protection.identity,
                        canonical_obligations=tuple(
                            json.dumps(item.as_dict(), ensure_ascii=False, sort_keys=True)
                            for item in obligations
                        ),
                        routes=(route_request,),
                        history_context_enabled=int(
                            core.cfg.translation.context_window or 0
                        ) > 0,
                        artifact_hashes=tuple(sorted(core._current_artifact_hashes().items())),
                        phase="provisional",
                        provisional_id=request.provisional_id,
                    )
                    request_contract_id = translation_request.route_contract_id(route_request)
                    core.runtime_events.emit_once(
                        "translation_request_contract",
                        request_contract_id,
                        request_contract_id=request_contract_id,
                        request_contract_schema_version=REQUEST_CONTRACT_SCHEMA_VERSION,
                        translation_request_id=translation_request.request_id,
                        request_phase="provisional",
                        route_index=0,
                        route_count=1,
                        route_body_json_style="compact_utf8",
                        contract_role="provisional_request",
                        phase="provisional",
                        provisional_id=request.provisional_id,
                        route_id=translation_route_id(engine),
                        engine=engine.engine_name,
                        model=engine.model_name,
                        original_source=prepared_source,
                        provider_source=request_protection.provider_source,
                        provider_source_sha256=sha256_text(request_protection.provider_source),
                        effective_system_prompt=messages[0][1],
                        messages=list(message_manifest(messages)),
                        retry_messages=[],
                        provider_options=route_request.options_dict(),
                        timeout_seconds=route_request.timeout_seconds,
                        body_sha256=hashlib.sha256(route_request.body).hexdigest(),
                        retry_body_sha256="",
                        messages_sha256=stable_identity({"messages": list(messages)}),
                        history=list(history_manifest(history)),
                        history_cohort=list(history_cohort),
                        history_context_enabled=translation_request.history_context_enabled,
                        profile_cache_identity=translation_request.profile_cache_identity,
                        activity_cache_identity=translation_request.activity_cache_identity,
                        request_protection_identity=translation_request.request_protection_identity,
                        history_cohort_id=f"{history_cohort[0]}:{history_cohort[1]}:{history_cohort[2]}",
                        incomplete=request.incomplete,
                        request_protection={
                            "identity": request_protection.identity,
                            "spans": [span.identity_row() for span in request_protection.spans],
                        },
                        canonical_obligations=[item.as_dict() for item in obligations],
                        profile=request_profile_snapshot.as_metadata(),
                        activity=activity_snapshot_metadata(request.activity_snapshot),
                        artifact_hashes=dict(translation_request.artifact_hashes),
                        policy_versions={
                            "adjudication": ADJUDICATION_POLICY_VERSION,
                            "canonical_publication": core._CANONICAL_PUBLICATION_POLICY_VERSION,
                            "request_protection": PROTECTION_POLICY_VERSION,
                        },
                    )
                    # Re-check at the actual call boundary so stale queued work
                    # cannot reach DeepSeek after the emergency route is disabled.
                    if not core.deepseek_provisional_eligible():
                        return
                    if (
                        request.profile_snapshot is not None
                        and request.profile_snapshot.generation
                        != core.profile_state.current().generation
                    ):
                        self._store.close(request.provisional_id)
                        core.runtime_events.emit(
                            "provisional_translation",
                            action="retired_profile_generation",
                            provisional_id=request.provisional_id,
                            **request.profile_snapshot.as_metadata(),
                        )
                        return
                    if not self._store.admit_call(request.provisional_id):
                        core.runtime_events.emit(
                            "provisional_translation",
                            action="cancelled_before_call",
                            provisional_id=request.provisional_id,
                            request_contract_id=request_contract_id,
                        )
                        return
                    raw_target, engine_result = self._call_provider(
                        request, engine, route_request, messages, request_contract_id
                    )
                    if self._stop_event.is_set() or not core.deepseek_provisional_eligible():
                        self._store.close(request.provisional_id)
                        return
                    if (
                        request.profile_snapshot is not None
                        and request.profile_snapshot.generation
                        != core.profile_state.current().generation
                    ):
                        self._store.close(request.provisional_id)
                        core.runtime_events.emit(
                            "provisional_translation",
                            action="retired_profile_generation",
                            provisional_id=request.provisional_id,
                            **request.profile_snapshot.as_metadata(),
                        )
                        return
                    diagnostics = (
                        engine_result.diagnostics_dict() if engine_result
                        else core.get_last_engine_api_diagnostics()
                    )
                    usage = (
                        engine_result.usage_dict() if engine_result
                        else core.get_last_token_usage()
                    )
                    if not raw_target:
                        core.runtime_events.emit(
                            "provisional_translation",
                            action="failed",
                            provisional_id=request.provisional_id,
                            latency_ms=round((time.monotonic() - started) * 1000, 2),
                            engine=engine.engine_name,
                            model=engine.model_name,
                            request_contract_id=request_contract_id,
                        )
                        return
                    guard = core._translation_output_guard(
                        engine,
                        raw_target,
                        prepared_source,
                        obligations=obligations,
                        request_protection=request_protection,
                    )
                    if guard.get("reason"):
                        core.runtime_events.emit(
                            "provisional_translation",
                            action="guard_rejected",
                            provisional_id=request.provisional_id,
                            guard_rejection=str(guard.get("reason") or ""),
                            latency_ms=round((time.monotonic() - started) * 1000, 2),
                            engine=engine.engine_name,
                            model=engine.model_name,
                            request_contract_id=request_contract_id,
                        )
                        return
                    display_target = str(
                        guard.get("candidate_output") or raw_target
                    )
                    completed = time.monotonic()
                    candidate = ProvisionalCandidate(
                        provisional_id=request.provisional_id,
                        raw_target=raw_target,
                        display_target=display_target,
                        fingerprint=fingerprint,
                        engine=engine.engine_name,
                        model=engine.model_name,
                        request_contract_id=request_contract_id,
                        requested_at_monotonic=request.requested_at_monotonic,
                        completed_at_monotonic=completed,
                        usage=usage,
                        diagnostics=diagnostics,
                        provider_options=route_request.provider_options,
                        system_fingerprint=(
                            engine_result.system_fingerprint if engine_result else ""
                        ),
                    )
                    preview_payload = SubtitlePayload(
                        text=display_target,
                        subtitle_id=request.provisional_id,
                        revision=0,
                        phase="provisional",
                    )
                    self._publish_candidate(
                        request, candidate, preview_payload, engine, route_request,
                        engine_result, display_target, started, completed, usage,
                        diagnostics, request_contract_id,
                    )
        except Exception:
            if self._stop_event.is_set():
                self._store.close(request.provisional_id)
                return
            core.log.exception("Provisional translation failed")
            core.runtime_events.emit(
                "provisional_translation",
                action="failed",
                provisional_id=request.provisional_id,
                request_contract_id=request_contract_id,
                latency_ms=round((time.monotonic() - started) * 1000, 2),
            )

    def _publish_candidate(
        self, request, candidate, preview_payload, engine, route_request,
        engine_result, display_target, started, completed, usage, diagnostics,
        request_contract_id,
    ) -> None:
        if not self._wait_for_earlier_finals(request.provisional_id):
            self._store.close(request.provisional_id)
            metrics.increment("translation.provisional.order_wait_abandoned")
            core.runtime_events.emit(
                "provisional_translation",
                action="cancelled_late",
                provisional_id=request.provisional_id,
                reason="earlier_final_pending",
            )
            return
        with self._publication_lock:
            if (
                self._stop_event.is_set()
                or not self._publication_open
                or not core.deepseek_provisional_eligible()
            ):
                self._store.close(request.provisional_id)
                return
            if not self._store.publish_and_enqueue(
                candidate,
                lambda: put_latest(
                    self._subtitle_queue,
                    preview_payload,
                    core.log,
                    "subtitle_queue",
                ),
            ):
                core.runtime_events.emit(
                    "provisional_translation",
                    action="cancelled_late",
                    provisional_id=request.provisional_id,
                )
                return
            core.runtime_events.emit(
                "provisional_translation",
                action="succeeded",
                provisional_id=request.provisional_id,
                target_text=display_target,
                latency_ms=round((completed - started) * 1000, 2),
                stt_ready_to_subtitle_ms=round(
                    max(
                        0.0,
                        completed
                        - request.first_stt_ready_at_monotonic,
                    )
                    * 1000,
                    2,
                ),
                engine=engine.engine_name,
                model=engine.model_name,
                request_contract_id=request_contract_id,
                input_tokens=usage.get("prompt"),
                provider_options=route_request.options_dict(),
                system_fingerprint=(
                    engine_result.system_fingerprint if engine_result else ""
                ),
                output_tokens=usage.get("output"),
                cache_hit_tokens=usage.get("cache_read"),
                cache_miss_tokens=usage.get("cache_write"),
                cost_usd=diagnostics.get("api_cost_usd"),
                api_cost_basis=diagnostics.get("api_cost_basis"),
                api_pricing_revision=diagnostics.get("api_pricing_revision"),
            )

    def _call_provider(self, request, engine, route_request, messages, request_contract_id):
        api_call_started = time.monotonic()
        api_call_outcome = "exception"
        engine_result = None
        try:
            if route_request.body:
                engine_result = engine.translate_messages_result(
                    route_request
                )
                raw_target = engine_result.text
            else:
                raw_target = engine.translate_messages(messages)
            api_call_outcome = "returned" if raw_target else "empty"
        finally:
            # Record the API call before any post-call lifecycle or
            # content guard can discard this provisional result.
            api_diagnostics = (
                engine_result.diagnostics_dict() if engine_result
                else core.get_last_engine_api_diagnostics()
            )
            api_usage = (
                engine_result.usage_dict() if engine_result
                else core.get_last_token_usage()
            )
            core.runtime_events.emit(
                "provisional_translation",
                action="api_attempt_completed",
                provisional_id=request.provisional_id,
                request_contract_id=request_contract_id,
                engine=engine.engine_name,
                model=engine.model_name,
                provider_options=route_request.options_dict(),
                system_fingerprint=(
                    engine_result.system_fingerprint if engine_result
                    else ""
                ),
                call_outcome=api_call_outcome,
                latency_ms=round(
                    (time.monotonic() - api_call_started) * 1000,
                    2,
                ),
                api_cost_usd=api_diagnostics.get("api_cost_usd"),
                api_cost_basis=api_diagnostics.get("api_cost_basis"),
                api_pricing_revision=api_diagnostics.get("api_pricing_revision"),
                input_tokens=api_usage.get("prompt"),
                output_tokens=api_usage.get("output"),
                cache_hit_tokens=api_usage.get("cache_read"),
                cache_miss_tokens=api_usage.get("cache_write"),
            )
        return raw_target, engine_result



class _FinalWorker:
    """Final translation for one ordered sentence on a pool worker thread."""

    def __init__(self, shared_state, provisional_store):
        self._shared_state = shared_state
        self._provisional_store = provisional_store
        self._worker_state = threading.local()

    def translate_item(
        self, seq: int, item, submitted_at: float, submitted_at_utc: str
    ) -> core._CompletedTranslation:
        started = time.monotonic()
        worker_started_at_utc = datetime.now(timezone.utc).isoformat()
        worker_id = threading.current_thread().name
        # Reset before translating so cache hits / failures (which never reach an
        # engine) don't inherit the previous call's token usage / corrections.
        reset_last_engine_diagnostics()
        reset_last_token_usage()
        reset_translation_call_trace()
        core.reset_corrections()
        # Everything that touches `item` runs inside the try: a malformed item
        # must yield a synthetic failed outcome, never a lost sequence number
        # (a gap would stall the in-order emit loop forever).
        text = ""
        incomplete = False
        policy_input = ""
        worker_translator: core.Translator | None = None
        translation_mode = str(
            core.cfg.translation.translation_mode or ""
        )
        metadata: dict = {"translation_mode": translation_mode}
        try:
            text = sentence_text(item)
            incomplete = sentence_incomplete(item)
            metadata = sentence_metadata(item).copy()
            event_snapshot = metadata.pop("activity_snapshot", None)
            event_profile_snapshot = metadata.pop("profile_snapshot", None)
            activity_snapshot_fallback_used = not isinstance(
                event_snapshot, ActivitySnapshot
            )
            activity_snapshot = (
                event_snapshot
                if isinstance(event_snapshot, ActivitySnapshot)
                else capture_effective_activity_snapshot(
                    core.cfg.translation.current_activity,
                    automatic_enabled=bool(
                        core.cfg.scene.publish_translation_activity
                    ),
                    source_text=text,
                )
            )
            worker_observed_snapshot = capture_effective_activity_snapshot(
                core.cfg.translation.current_activity,
                automatic_enabled=bool(
                    core.cfg.scene.publish_translation_activity
                ),
            )
            profile_snapshot = (
                event_profile_snapshot
                if isinstance(event_profile_snapshot, ProfileSnapshot)
                else core.profile_state.current()
            )
            profile_id = profile_snapshot.effective_profile_id
            marker = _dependency_marker(text)
            metadata.update(
                {
                    "sequence_id": seq,
                    "starts_with_dependency_marker": bool(marker),
                    "dependency_marker": marker,
                    "profile_id": profile_id,
                    **profile_snapshot.as_metadata(),
                    # QE must be able to check whether background context
                    # helped or polluted; record it per event.
                    "current_activity": activity_snapshot.display_label,
                    **activity_snapshot_metadata(activity_snapshot),
                    "activity_snapshot_stage": (
                        "sentence_enqueue"
                        if not activity_snapshot_fallback_used
                        else "worker_fallback"
                    ),
                    "activity_bound_snapshot_used": (
                        not activity_snapshot_fallback_used
                    ),
                    "activity_snapshot_fallback_used": (
                        activity_snapshot_fallback_used
                    ),
                    "activity_snapshot_fallback_reason": (
                        "legacy_event_without_snapshot"
                        if activity_snapshot_fallback_used
                        else ""
                    ),
                    "activity_local_cue_applied": (
                        activity_snapshot.source == "local_source"
                    ),
                    "worker_observed_activity_id": (
                        worker_observed_snapshot.activity_id
                    ),
                    "worker_observed_activity_source": (
                        worker_observed_snapshot.source
                    ),
                    "worker_observed_effective_generation": (
                        worker_observed_snapshot.effective_generation
                    ),
                    "activity_capsule_applied": bool(
                        activity_snapshot.display_label
                    ),
                    "activity_capsule_activity_id": (
                        activity_snapshot.activity_id
                        if activity_snapshot.display_label
                        else ""
                    ),
                    "worker_started_at_utc": worker_started_at_utc,
                    "translation_submitted_at_utc": submitted_at_utc,
                    # Diagnostic-only: distinguishes the 5s live timeout path
                    # from the 60s clip/offline path in latency artifacts.
                    "translation_mode": translation_mode,
                }
            )
            worker_translator = getattr(self._worker_state, "translator", None)
            if worker_translator is None:
                worker_translator = core.Translator(shared_state=self._shared_state)
                worker_translator._defer_success_record = True
                self._worker_state.translator = worker_translator
            repetition_evidence = RepetitionEvidence(
                min_avg_logprob=metadata.get("min_avg_logprob"),
                max_no_speech_prob=metadata.get("max_no_speech_prob"),
                cut_reason=str(metadata.get("cut_reason") or ""),
                forced=bool(metadata.get("forced", False)),
                incomplete=incomplete,
            )
            source_key = (text or "").strip()
            provisional_id = str(metadata.get("provisional_id") or "")
            provisional_candidate = self._provisional_store.candidate(provisional_id)
            provisional_not_ready = bool(
                provisional_id and provisional_candidate is None
            )
            if provisional_id and provisional_candidate is None:
                self._provisional_store.close(provisional_id)
            with self._shared_state.lock:
                inflight_count = self._shared_state.inflight_sources.get(source_key, 0)
                if inflight_count and self._shared_state.policy.last_input == source_key:
                    # The previous identical input has not succeeded yet.
                    # Let this worker attempt it; ordered output dedupe still
                    # suppresses two successful visible subtitles.
                    self._shared_state.policy.reset_last_input()
                self._shared_state.inflight_sources[source_key] = inflight_count + 1
            outcome_for_state: core.TranslationOutcome | None = None
            try:
                worker_translator._current_sequence_id = seq
                outcome, policy_input = self._translate_bound(
                    seq, text, incomplete, metadata, worker_translator,
                    repetition_evidence, source_key, provisional_id,
                    provisional_candidate, provisional_not_ready, profile_snapshot,
                    profile_id, activity_snapshot,
                )
            finally:
                worker_translator._current_sequence_id = None
                with self._shared_state.lock:
                    remaining = self._shared_state.inflight_sources.get(source_key, 1) - 1
                    if remaining > 0:
                        self._shared_state.inflight_sources[source_key] = remaining
                    else:
                        self._shared_state.inflight_sources.pop(source_key, None)
        except Exception:
            core.log.exception("Translation worker failed for: %.40s", text)
            failed_policy_input = policy_input or str(
                getattr(worker_translator, "_last_input", "") or ""
            )
            if failed_policy_input:
                with self._shared_state.lock:
                    if self._shared_state.policy.last_input == failed_policy_input:
                        self._shared_state.policy.reset_last_input()
                policy_input = failed_policy_input
            metadata.setdefault("sequence_id", seq)
            outcome = core.TranslationOutcome(
                source_text=(text or "").strip(),
                target_text=None,
                status="failed",
                result_source="none",
                cache_status="skipped",
                incomplete=incomplete,
            )
        return self._complete_item(
            seq, outcome, metadata, submitted_at, started, worker_id,
            worker_translator, policy_input,
        )


    def _translate_bound(
        self, seq, text, incomplete, metadata, worker_translator,
        repetition_evidence, source_key, provisional_id,
        provisional_candidate, provisional_not_ready, profile_snapshot,
        profile_id, activity_snapshot,
    ) -> tuple[core.TranslationOutcome, str]:
        with bind_profile_snapshot(profile_snapshot):
          with bind_profile_id(profile_id):
            with bind_activity_snapshot(activity_snapshot):
                history_cohort = self._shared_state.session.history_cohort(
                    activity_snapshot
                )
                with self._shared_state.lock:
                    history_items, cross_cohort_items = (
                        self._shared_state.history.cohort_stats(history_cohort)
                    )
                metadata.update(
                    {
                        "history_profile_id": "",
                        "history_session_id": history_cohort[0],
                        "history_activity_id": history_cohort[1],
                        "history_cohort_epoch": history_cohort[2],
                        "history_cohort_id": (
                            f"{history_cohort[0]}:"
                            f"{history_cohort[1]}:"
                            f"{history_cohort[2]}"
                        ),
                        "history_candidate_count": history_items,
                        "history_cross_cohort_excluded_count": (
                            cross_cohort_items
                        ),
                    }
                )
                if provisional_candidate is None:
                    # Preserve the long-standing worker contract for
                    # ordinary sentences and lightweight test doubles.
                    outcome = worker_translator.translate_event(
                        text,
                        incomplete,
                        repetition_evidence=repetition_evidence,
                    )
                else:
                    outcome = worker_translator.translate_event(
                        text,
                        incomplete,
                        repetition_evidence=repetition_evidence,
                        provisional_candidate=provisional_candidate,
                        source_utterance_ids=tuple(
                            metadata.get("source_utterance_ids") or ()
                        ),
                        evidence_source_utterance_ids=tuple(
                            metadata.get(
                                "evidence_source_utterance_ids"
                            )
                            or ()
                        ),
                    )
                outcome_for_state = outcome
                if provisional_id:
                    self._provisional_store.close(provisional_id)
                    provisional_trace = {
                        "provisional_id": provisional_id,
                        **worker_translator._last_provisional_trace,
                    }
                    if provisional_not_ready and not worker_translator._last_provisional_trace:
                        provisional_trace.update(
                            {
                                "promotion_attempted": False,
                                "promotion_passed": False,
                                "candidate_not_ready": True,
                                "final_retranslation": True,
                            }
                        )
                    metadata["provisional"] = provisional_trace
                    metadata.update(
                        {
                            "provisional_id": provisional_id,
                            "provisional_promotion_attempted": bool(
                                provisional_trace.get(
                                    "promotion_attempted"
                                )
                            ),
                            "provisional_promotion_passed": bool(
                                provisional_trace.get("promotion_passed")
                            ),
                            "provisional_fingerprint_mismatch": bool(
                                provisional_trace.get(
                                    "fingerprint_mismatch"
                                )
                            ),
                            "provisional_guard_rejection": str(
                                provisional_trace.get(
                                    "guard_rejection"
                                )
                                or ""
                            ),
                            "provisional_final_retranslation": bool(
                                provisional_trace.get(
                                    "final_retranslation"
                                )
                            ),
                            "provisional_final_revision": 1,
                        }
                    )
                policy_input = (
                    getattr(worker_translator, "_last_input", "")
                    or source_key
                )
        return outcome, policy_input

    def _complete_item(
        self, seq, outcome, metadata, submitted_at, started, worker_id,
        worker_translator, policy_input,
    ) -> core._CompletedTranslation:
        completed_at = time.monotonic()
        metadata["worker_completed_at_utc"] = datetime.now(
            timezone.utc
        ).isoformat()
        snapshot_monotonic = float(
            metadata.get("activity_snapshot_captured_at_monotonic") or 0.0
        )
        if snapshot_monotonic > 0:
            metadata["activity_snapshot_to_worker_ms"] = round(
                max(0.0, started - snapshot_monotonic) * 1000, 2
            )
        elapsed = completed_at - started
        fallback_details = getattr(worker_translator, "_last_fallback_details", None)
        selected_attempt = (
            fallback_details.selected_attempt if fallback_details else {}
        )
        attempts = list(fallback_details.attempts) if fallback_details else []
        diagnostics = (
            selected_attempt or (attempts[-1] if attempts else None)
            or core.get_last_engine_diagnostics()
        )
        api_diagnostics = (
            selected_attempt or (attempts[-1] if attempts else None)
            or core.get_last_engine_api_diagnostics()
        )
        if attempts:
            metadata["attempts"] = attempts
        for usage_key, usage_value in core._token_usage_for_outcome(
            outcome, selected_attempt
        ).items():
            if usage_value is not None:
                metadata[f"token_{usage_key}"] = usage_value
        corrections = list(outcome.corrections)
        if corrections:
            metadata["corrections"] = corrections
            metadata["correction_count"] = len(corrections)
        retry_count = 0
        retry_reason = ""
        if core._retry_diagnostics_apply(outcome, diagnostics):
            retry_count = int(diagnostics.get("retry_count") or 0)
            retry_reason = str(diagnostics.get("retry_reason") or "")
        api_event_fields = core._api_event_fields(outcome, api_diagnostics)
        return core._CompletedTranslation(
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
            policy_input,
        )



class _OrderedEmitter:
    """Sequence-ordered completion and publication; coordinator thread only."""

    def __init__(self, shared_state, subtitle_queue):
        self._shared_state = shared_state
        self._subtitle_queue = subtitle_queue
        self.pending: dict[int, Future[core._CompletedTranslation]] = {}
        self.completed: dict[int, core._CompletedTranslation] = {}
        self.next_seq = 0
        self.next_emit_seq = 0
        self.last_result = ""
        self.last_result_time = 0.0

    def submit(self, executor, worker: Callable, item) -> None:
        submitted_at, submitted_at_utc = _submitted_now()
        self.pending[self.next_seq] = executor.submit(
            worker,
            self.next_seq,
            item,
            submitted_at,
            submitted_at_utc,
        )
        self.next_seq += 1

    def pending_count(self) -> int:
        return len(self.pending)

    def completed_count(self) -> int:
        return len(self.completed)

    def idle(self) -> bool:
        return not self.pending and not self.completed

    def cancel_pending(self) -> None:
        for future in self.pending.values():
            future.cancel()

    def emit_ready(self) -> None:
        while self.next_emit_seq in self.completed:
            self.emit_completed(self.completed.pop(self.next_emit_seq))
            self.next_emit_seq += 1

    def collect_finished(self) -> None:
        for seq, future in list(self.pending.items()):
            if not future.done():
                continue
            self.pending.pop(seq)
            try:
                self.completed[seq] = future.result()
            except Exception:
                core.log.exception("Translation future failed")
                metrics.increment("translation.future_failed")
                self.completed[seq] = _failed_completion(seq)
        if len(self.completed) > _MAX_COMPLETED_BACKLOG:
            # L8: _MAX_PENDING_TRANSLATIONS only bounds in-flight futures;
            # an emit-loop stall would let this dict grow unboundedly.
            metrics.increment("translation.completed_backlog_high")
            core.log.warning(
                "Completed-translation backlog at %d (next_emit_seq=%d) — emit loop may be stalled",
                len(self.completed),
                self.next_emit_seq,
            )
    def _withdraw_preview(self, event_metadata: dict, reason: str) -> None:
        """The final will not be shown: replace this sentence's preview with nothing.

        Without this an unchecked preview stayed up until the next subtitle or the
        30 s idle hide, and never reached history. The empty final revision is a no-op
        on screen when no preview was displayed.
        """
        provisional_id = str(event_metadata.get("provisional_id") or "")
        if not provisional_id:
            return
        put_latest(
            self._subtitle_queue,
            SubtitlePayload(text="", subtitle_id=provisional_id, revision=1, phase="final"),
            core.log,
            "subtitle_queue",
        )
        core.runtime_events.emit(
            "provisional_translation",
            action="withdrawn",
            provisional_id=provisional_id,
            reason=reason,
        )

    def emit_completed(self, item: core._CompletedTranslation) -> None:
        outcome = item.outcome
        elapsed = item.elapsed
        emitted_at = time.monotonic()
        output_delay_ms = round(max(0.0, emitted_at - item.submitted_at) * 1000, 2)
        event_metadata = item.metadata.copy()
        sentence_enqueued_at = float(
            event_metadata.get("sentence_enqueued_at_monotonic") or 0.0
        )
        event_metadata.update(
            {
                "engine_latency_ms": round(elapsed * 1000, 2),
                "queue_wait_ms": round(max(0.0, item.started_at - item.submitted_at) * 1000, 2),
                "sentence_queue_wait_ms": (
                    round(
                        max(0.0, item.started_at - sentence_enqueued_at) * 1000,
                        2,
                    )
                    if sentence_enqueued_at > 0
                    else None
                ),
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
        final_script_rejection = (
            _final_script_rejection_reason(event_fields) if result else ""
        )
        # Status indicator. Only real failures count: policy skips (laughter,
        # filtered noise) record nothing, and stale/duplicate suppression is a
        # display decision after a successful translation.
        if result and not final_script_rejection:
            live_status.record_translation(True)
        elif final_script_rejection or outcome.status == "failed":
            live_status.record_translation(False)
        if final_script_rejection:
            metrics.increment("translation.final_script_rejected")
            with self._shared_state.lock:
                if (
                    item.policy_input
                    and self._shared_state.policy.last_input == item.policy_input
                ):
                    self._shared_state.policy.reset_last_input()
            core.runtime_events.emit(
                "translation",
                **{
                    **event_fields,
                    "target_text": None,
                    "status": "failed",
                    "filter_reason": final_script_rejection,
                },
                subtitle_emitted=False,
                subtitle_suppressed_reason="final_script_invariant",
            )
            self._withdraw_preview(event_metadata, "final_script_invariant")
            return
        with self._shared_state.lock:
            if result:
                self._shared_state.policy.last_input = (
                    item.policy_input or outcome.source_text.strip()
                )
            elif (
                outcome.status == "failed"
                and item.policy_input
                and self._shared_state.policy.last_input == item.policy_input
            ):
                self._shared_state.policy.reset_last_input()
        if result:
            max_output_delay_ms = _translation_max_output_delay_ms()
            if max_output_delay_ms > 0 and output_delay_ms > max_output_delay_ms:
                metrics.increment("translation.subtitle.stale_skipped")
                with self._shared_state.lock:
                    if (
                        item.policy_input
                        and self._shared_state.policy.last_input == item.policy_input
                    ):
                        self._shared_state.policy.reset_last_input()
                core.log.warning(
                    "Skipping stale subtitle after %.0fms output delay: %s",
                    output_delay_ms,
                    result[:30],
                )
                core.runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=False,
                    subtitle_suppressed_reason="stale_output_delay",
                )
                self._withdraw_preview(event_metadata, "stale_output_delay")
                return
        if outcome.deferred_success is not None:
            try:
                # Commit conversation state in sequence order, not provider
                # completion order. Subtitle/event ordering already uses
                # this same coordinator boundary.
                outcome.deferred_success()
            except Exception:
                metrics.increment("translation.memory_commit_failed")
                core.log.exception(
                    "Ordered translation memory commit failed for sequence %s",
                    item.seq,
                )
        if result:
            metrics.increment("translation.success")
            # Surface low-quality output in the 60 s metrics summary so a
            # degrading stretch is visible without scraping the JSONL.
            severity = event_fields.get("quality_severity")
            if severity in ("bad", "warn"):
                metrics.increment(f"translation.quality.{severity}")
            now = time.monotonic()
            if result == self.last_result and (now - self.last_result_time) < _DEDUP_SUBTITLE_SEC:
                core.log.debug("Suppressing duplicate subtitle: %s", result[:30])
                core.runtime_events.emit(
                    "translation",
                    **event_fields,
                    subtitle_emitted=False,
                    subtitle_suppressed_reason="duplicate",
                )
                self._withdraw_preview(event_metadata, "duplicate")
                return
            self.last_result = result
            self.last_result_time = now
            provisional_id = str(event_metadata.get("provisional_id") or "")
            subtitle_item: str | SubtitlePayload = result
            if provisional_id:
                subtitle_item = SubtitlePayload(
                    text=result,
                    subtitle_id=provisional_id,
                    revision=1,
                    phase="final",
                )
            put_latest(
                self._subtitle_queue,
                subtitle_item,
                core.log,
                "subtitle_queue",
            )
            live_status.record_subtitle(outcome.source_text, result)
            core.runtime_events.emit(
                "translation",
                **event_fields,
                subtitle_emitted=True,
                subtitle_suppressed_reason="",
            )
        else:
            metrics.increment("translation.empty")
            core.runtime_events.emit(
                "translation",
                **event_fields,
                subtitle_emitted=False,
                subtitle_suppressed_reason="",
            )
            self._withdraw_preview(event_metadata, outcome.status or "empty")
        metrics.log_summary_if_due()



def start(sentence_queue: queue.Queue, subtitle_queue: queue.Queue,
          stop_event: threading.Event,
          pause_event: threading.Event | None = None,
          *,
          provisional_queue: queue.Queue | None = None,
          upstream_done_event: threading.Event | None = None,
          on_fatal=None) -> threading.Thread:
    def run_pipeline():
        shared_state = core._new_translator_shared_state(
            fallback_event_sink=core._emit_fallback_runtime_event,
        )
        executor = core._DaemonWorkerPool(
            max_workers=core._TRANSLATION_WORKERS,
            thread_name_prefix="TranslationWorker",
        )
        provisional_executor = core._DaemonWorkerPool(
            max_workers=1,
            thread_name_prefix="ProvisionalTranslation",
        )
        provisional_store = ProvisionalStore()
        provisional_future: Future[None] | None = None
        emitter = _OrderedEmitter(shared_state, subtitle_queue)
        final_worker = _FinalWorker(shared_state, provisional_store)
        provisional_worker = _ProvisionalWorker(
            shared_state, provisional_store, stop_event, subtitle_queue
        )
        fallback_probe_thread = _start_fallback_probe_thread(shared_state, stop_event)

        try:
            while not stop_event.is_set():
                if provisional_future is not None and provisional_future.done():
                    provisional_future = None
                if provisional_queue is not None and provisional_future is None:
                    try:
                        request = provisional_queue.get_nowait()
                    except queue.Empty:
                        request = None
                    if isinstance(request, ProvisionalRequest):
                        # Finals already submitted, plus sentences still queued under
                        # backpressure, belong to earlier sentences; the preview may
                        # show only once they have all been emitted. If the count
                        # includes this preview's own sentence, its final closes the
                        # provisional and the wait gives up.
                        barrier = emitter.next_seq + sentence_queue.qsize()
                        provisional_future = provisional_executor.submit(
                            provisional_worker.translate,
                            request,
                            lambda barrier=barrier: emitter.next_emit_seq >= barrier,
                        )
                emitter.collect_finished()
                emitter.emit_ready()

                if emitter.pending_count() >= core._MAX_PENDING_TRANSLATIONS:
                    stop_event.wait(core._TRANSLATION_LOOP_POLL_SEC)
                    continue

                has_item, item = core.poll_queue(
                    sentence_queue,
                    stop_event,
                    pause_event,
                    timeout=core._TRANSLATION_LOOP_POLL_SEC,
                )
                if has_item:
                    emitter.submit(executor, final_worker.translate_item, item)
        except Exception:
            # Wake the rest of the graph before bounded drain begins. Without
            # this, a translator-loop failure could wait for the splitter's
            # done signal while the splitter was still waiting on stop_event.
            stop_event.set()
            raise
        finally:
            provisional_worker.close_publication()
            provisional_executor.shutdown(wait=False, cancel_futures=True)
            # Keep accepting the splitter's final flushed sentence until its
            # done_event is set, then drain all accepted work in order. This is
            # bounded so a stuck engine call still cannot hang shutdown.
            deadline = time.monotonic() + _stop_drain_timeout_sec()
            discard_queued_sentences = bool(pause_event and pause_event.is_set())
            try:
                while True:
                    while (
                        discard_queued_sentences
                        or emitter.pending_count() < core._MAX_PENDING_TRANSLATIONS
                    ):
                        try:
                            item = sentence_queue.get_nowait()
                        except queue.Empty:
                            break
                        if discard_queued_sentences:
                            continue
                        emitter.submit(executor, final_worker.translate_item, item)
                    emitter.collect_finished()
                    emitter.emit_ready()
                    upstream_done = (
                        upstream_done_event is None or upstream_done_event.is_set()
                    )
                    if (
                        upstream_done
                        and sentence_queue.empty()
                        and emitter.idle()
                    ):
                        break
                    if time.monotonic() >= deadline:
                        core.log.warning(
                            "Stop drain timed out with %d queued / %d pending / %d completed translations",
                            sentence_queue.qsize(),
                            emitter.pending_count(),
                            emitter.completed_count(),
                        )
                        emitter.cancel_pending()
                        break
                    time.sleep(core._TRANSLATION_LOOP_POLL_SEC)
            finally:
                executor.shutdown(wait=False, cancel_futures=False)
            fallback_probe_thread.join(
                timeout=max(0.0, deadline - time.monotonic())
            )
            if fallback_probe_thread.is_alive():
                core.log.warning("Fallback recovery probe did not stop before drain deadline")
            core.log.info("Translator stopped")

    def run():
        try:
            run_pipeline()
        except Exception as exc:
            core.log.error("Translator worker aborted: %s", exc, exc_info=True)
            if callable(on_fatal):
                on_fatal(exc)
            stop_event.set()

    return start_daemon_thread("Translator", run)
