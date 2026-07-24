from __future__ import annotations

import time
from collections.abc import Callable, MutableMapping, Sequence
from dataclasses import dataclass

from modules.translation_engines import (
    TranslationEngine,
    record_translation_attempt,
    reset_last_engine_diagnostics,
    reset_last_token_usage,
    select_translation_attempt,
)
from utils.metrics import metrics


CacheKey = tuple[str, bool, str, str, str]
UntranslatedCheck = Callable[[str, str], bool]
ProbeObservationSink = Callable[[dict[str, object]], None]


@dataclass
class FallbackState:
    active_idx: int = 0
    probe_counter: int = 0
    consecutive_primary_failures: int = 0
    primary_cooldown_until: float = 0.0
    consecutive_probe_successes: int = 0


def _observe_probe(
    sink: ProbeObservationSink | None,
    **fields: object,
) -> None:
    if sink is None:
        return
    try:
        sink(dict(fields))
    except Exception:
        # Diagnostics must never change recovery routing or take down the probe
        # thread. The persistent runtime writer has its own logging fallback.
        metrics.increment("translation.fallback.probe_observation_error")


def active_engine(
    engines: Sequence[TranslationEngine],
    active_idx: int,
) -> TranslationEngine | None:
    if not engines:
        return None
    if active_idx < 0 or active_idx >= len(engines):
        return None
    return engines[active_idx]


def cache_key(
    text: str,
    incomplete: bool,
    prompt_ver: str,
    engine_name: str = "",
    model_name: str = "",
) -> CacheKey:
    return (
        text,
        incomplete,
        prompt_ver,
        str(engine_name or ""),
        str(model_name or ""),
    )


def cache_lookup(
    cache: MutableMapping[CacheKey, str],
    text: str,
    incomplete: bool,
    prompt_ver: str,
    engine_name: str = "",
    model_name: str = "",
) -> str | None:
    key = cache_key(text, incomplete, prompt_ver, engine_name, model_name)
    if key not in cache:
        return None
    if move_to_end := getattr(cache, "move_to_end", None):
        move_to_end(key)
    return cache[key]


def cache_store(
    cache: MutableMapping[CacheKey, str],
    text: str,
    incomplete: bool,
    value: str,
    prompt_ver: str,
    max_size: int,
    engine_name: str = "",
    model_name: str = "",
) -> None:
    key = cache_key(text, incomplete, prompt_ver, engine_name, model_name)
    if len(cache) >= max_size:
        cache.pop(next(iter(cache)))
    cache[key] = value


def call_with_fallback(
    engines: Sequence[TranslationEngine],
    state: FallbackState,
    text: str,
    system_prompt: str,
    incomplete: bool,
    history: list[tuple[str, str]] | None,
    probe_every: int,
    failure_threshold: int,
    looks_untranslated: UntranslatedCheck,
    log,
    *,
    circuit_breaker_enabled: bool = False,
    recovery_cooldown_seconds: float = 0.0,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str | None, int]:
    """Returns (result, engine_idx) where engine_idx is the engine that
    actually produced the result. On a soft fallback state.active_idx is NOT
    advanced, so callers must use the returned index — not the active engine —
    to attribute the result (engine label, diagnostics, DB cache rows)."""
    if not engines:
        metrics.increment("translation.fallback.no_engines")
        return None, -1

    # Try the current active engine. Primary recovery probes run on a background thread.
    primary_idx = state.active_idx
    metrics.increment("translation.fallback.attempt")
    primary = engines[primary_idx]
    reset_last_engine_diagnostics()
    reset_last_token_usage()
    try:
        result = primary.translate(text, system_prompt, incomplete, history)
    except Exception as exc:
        record_translation_attempt(primary, phase="fallback_chain", exception=exc)
        raise
    primary_bad = bool(result and looks_untranslated(result, text))
    primary_attempt = record_translation_attempt(
        primary,
        phase="fallback_chain",
        result=result,
        rejected_output=primary_bad,
    )

    if result and not primary_bad:
        state.consecutive_primary_failures = 0
        if primary_idx == 0:
            state.primary_cooldown_until = 0.0
            state.consecutive_probe_successes = 0
        select_translation_attempt(primary_attempt)
        return result, primary_idx

    if result:
        metrics.increment("translation.bad_output")

    state.consecutive_primary_failures += 1
    hard_switch = state.consecutive_primary_failures >= failure_threshold

    # ── Try fallback engines ──────────────────────────────────────────────────
    for index in range(primary_idx + 1, len(engines)):
        metrics.increment("translation.fallback.attempt")
        reset_last_engine_diagnostics()
        reset_last_token_usage()
        fallback = engines[index]
        try:
            fb_result = fallback.translate(text, system_prompt, incomplete, history)
        except Exception as exc:
            record_translation_attempt(fallback, phase="fallback_chain", exception=exc)
            raise
        fallback_bad = bool(fb_result and looks_untranslated(fb_result, text))
        fallback_attempt = record_translation_attempt(
            fallback,
            phase="fallback_chain",
            result=fb_result,
            rejected_output=fallback_bad,
        )
        if fb_result and not fallback_bad:
            if hard_switch:
                # N consecutive failures — commit to this engine until probe recovers primary
                metrics.increment("translation.fallback.success")
                log.warning(
                    "Engine %s failed %d consecutive times; switching to %s",
                    primary.engine_name,
                    state.consecutive_primary_failures,
                    fallback.engine_name,
                )
                state.active_idx = index
                state.consecutive_primary_failures = 0
                state.probe_counter = 0
                state.consecutive_probe_successes = 0
                state.primary_cooldown_until = (
                    clock() + max(0.0, recovery_cooldown_seconds)
                    if circuit_breaker_enabled
                    else 0.0
                )
            else:
                # Soft fallback: use this engine for this sentence only, retry primary next call
                log.info(
                    "Engine %s failed (failure %d/%d); using %s for this sentence",
                    primary.engine_name,
                    state.consecutive_primary_failures,
                    failure_threshold - 1,
                    fallback.engine_name,
                )
            select_translation_attempt(fallback_attempt)
            return fb_result, index
        if fb_result:
            metrics.increment("translation.bad_output")

    log.error("All engines failed for: %.40s", text)
    return None, primary_idx


def probe_primary_recovery(
    engines: Sequence[TranslationEngine],
    state: FallbackState,
    probe_text: str,
    system_prompt: str,
    looks_untranslated: UntranslatedCheck,
    log,
    *,
    circuit_breaker_enabled: bool = False,
    recovery_cooldown_seconds: float = 0.0,
    required_consecutive_successes: int = 1,
    history: list[tuple[str, str]] | None = None,
    observation_sink: ProbeObservationSink | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Probe the primary engine without putting a user sentence on the probe path."""
    if len(engines) < 2 or state.active_idx <= 0 or state.active_idx >= len(engines):
        return False

    now = clock()
    if circuit_breaker_enabled and now < state.primary_cooldown_until:
        metrics.increment("translation.fallback.probe_cooldown_skipped")
        _observe_probe(
            observation_sink,
            status="cooldown_skipped",
            recovered=False,
            success_streak=state.consecutive_probe_successes,
            cooldown_until=state.primary_cooldown_until,
            cooldown_remaining_seconds=max(0.0, state.primary_cooldown_until - now),
        )
        return False

    metrics.increment("translation.fallback.probe")
    reset_last_engine_diagnostics()
    reset_last_token_usage()
    try:
        probe = engines[0].translate(probe_text, system_prompt, False, history or [])
    except Exception as exc:
        if not circuit_breaker_enabled:
            raise
        metrics.increment("translation.fallback.probe_error")
        state.consecutive_probe_successes = 0
        state.primary_cooldown_until = now + max(0.0, recovery_cooldown_seconds)
        _observe_probe(
            observation_sink,
            status="exception",
            recovered=False,
            success_streak=0,
            cooldown_until=state.primary_cooldown_until,
            exception_type=type(exc).__name__,
        )
        log.debug("Primary probe raised; restarting recovery cooldown", exc_info=True)
        return False

    if probe and not looks_untranslated(probe, probe_text):
        metrics.increment("translation.fallback.probe_success")
        state.consecutive_probe_successes += 1
        success_streak = state.consecutive_probe_successes
        required_successes = (
            max(1, required_consecutive_successes)
            if circuit_breaker_enabled
            else 1
        )
        if state.consecutive_probe_successes < required_successes:
            _observe_probe(
                observation_sink,
                status="success",
                recovered=False,
                success_streak=success_streak,
                cooldown_until=state.primary_cooldown_until,
            )
            log.info(
                "Primary engine %s recovery probe succeeded (%d/%d); staying on %s",
                engines[0].engine_name,
                state.consecutive_probe_successes,
                required_successes,
                engines[state.active_idx].engine_name,
            )
            return False
        metrics.increment("translation.fallback.primary_recovered")
        log.info("Primary engine %s recovered; switching back", engines[0].engine_name)
        state.active_idx = 0
        state.probe_counter = 0
        state.consecutive_primary_failures = 0
        state.primary_cooldown_until = 0.0
        state.consecutive_probe_successes = 0
        _observe_probe(
            observation_sink,
            status="success",
            recovered=True,
            success_streak=success_streak,
            cooldown_until=0.0,
        )
        return True
    rejected_output = bool(probe)
    if rejected_output:
        metrics.increment("translation.bad_output")
    if circuit_breaker_enabled:
        state.consecutive_probe_successes = 0
        state.primary_cooldown_until = now + max(0.0, recovery_cooldown_seconds)
    _observe_probe(
        observation_sink,
        status="rejected_output" if rejected_output else "empty",
        recovered=False,
        success_streak=0,
        cooldown_until=state.primary_cooldown_until,
    )
    log.debug("Primary probe failed, staying on %s", engines[state.active_idx].engine_name)
    return False
